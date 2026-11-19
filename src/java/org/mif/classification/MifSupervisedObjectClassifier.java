package org.mif.classification;

import java.awt.image.BufferedImage;
import java.nio.FloatBuffer;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.bytedeco.javacpp.indexer.Indexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_ml.StatModel;
import qupath.lib.classifiers.object.ObjectClassifier;
import qupath.lib.classifiers.object.ObjectClassifiers;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.classes.PathClass;
import qupath.opencv.ml.objects.features.FeatureExtractor;
import org.mif.inference.KronosEncoderInputs;

/** Persist a native cell head with its fitted preparation and uncalibrated scores.
 * The simple class name and fields are the existing QuPath serialized contract.
 * The stage owns its native model; prediction borrows it and closes temporary
 * matrices. Automatic cells are supplied in explicit batches, never collected
 * from the image hierarchy as if that were the complete canonical population.
 */
public final class MifSupervisedObjectClassifier implements ObjectClassifier<BufferedImage> {
    private String schema = "mif-supervised-object-classifier/v1";
    private String method;
    private String scoreKind;
    private StatModel nativeModel;
    private FeatureExtractor<BufferedImage> featureExtractor;
    private List<PathClass> pathClasses;
    private Map<String, Object> featureSource;

    static {
        ObjectClassifiers.ObjectClassifierTypeAdapterFactory.registerSubtype(MifSupervisedObjectClassifier.class);
    }

    public MifSupervisedObjectClassifier(String method, StatModel nativeModel,
            FeatureExtractor<BufferedImage> extractor, List<PathClass> classes, Map<String, Object> featureSource) {
        this.method = method;
        this.nativeModel = nativeModel;
        this.featureExtractor = extractor;
        this.pathClasses = new ArrayList<>(classes);
        this.featureSource = featureSource;
        this.scoreKind = switch (method) {
            case "rtrees" -> "native_tree_vote_fraction";
            case "svm" -> "native_svm_pairwise_vote_fraction";
            case "logistic_regression" -> "native_logistic_ovr_normalized_score";
            default -> throw new IllegalArgumentException("Unsupported native cell head");
        };
    }

    /** Trigger one class initialization before QuPath reads a retained classifier. */
    public static void ensureRegistered() {}

    public FeatureExtractor<BufferedImage> getFeatureExtractor() { return featureExtractor; }
    public StatModel getNativeModel() { return nativeModel; }
    /** Restore declared nullable and integer input fields after QuPath saves maps.
     * Required missing fields and present invalid values stay for validation.
     */
    public Map<String, Object> getFeatureSource() {
        if (featureSource == null || !"kronos-cell-feature-source/v1".equals(featureSource.get("schema")))
            return featureSource;
        Map<String, Object> result = new LinkedHashMap<>(featureSource);
        if (featureSource.get("encoder") instanceof Map<?, ?> saved) {
            Map<String, Object> encoder = new LinkedHashMap<>();
            saved.forEach((key, value) -> encoder.put((String) key, value));
            result.put("encoder", KronosEncoderInputs.saved(encoder));
        }
        return result;
    }
    public String getScoreKind() { return scoreKind; }
    @Override public Collection<PathClass> getPathClasses() { return Collections.unmodifiableList(pathClasses); }

    @Override public Collection<PathObject> getCompatibleObjects(ImageData<BufferedImage> imageData) {
        throw explicitBatchRequired();
    }

    @Override public int classifyObjects(ImageData<BufferedImage> imageData, boolean reset) {
        throw explicitBatchRequired();
    }

    /** Count missing measurements only in the caller's explicit canonical batch. */
    @Override public Map<String, Integer> getMissingFeatures(ImageData<BufferedImage> imageData,
            Collection<? extends PathObject> objects) {
        if (objects == null) throw new UnsupportedOperationException(
            "Check canonical cell features through the pipeline bounded phenotyping stage; an explicit cell batch is required");
        Map<String, Integer> missing = new LinkedHashMap<>();
        for (PathObject cell : objects) for (String name : featureExtractor.getMissingFeatures(imageData, cell))
            missing.merge(name, 1, Integer::sum);
        return missing;
    }

    /** Extract, predict and replace classes within the existing native workspaces. */
    @Override public int classifyObjects(ImageData<BufferedImage> imageData,
            Collection<? extends PathObject> objects, boolean reset) {
        if (!reset) throw new IllegalArgumentException("Supervised cell heads require explicit class replacement");
        if (!"mif-supervised-object-classifier/v1".equals(schema) || nativeModel == null || !nativeModel.isTrained()
                || pathClasses == null || pathClasses.size() < 2 || featureExtractor == null)
            throw new IllegalStateException("The saved supervised classifier is incomplete");
        List<PathObject> cells = new ArrayList<>(objects);
        if (!getMissingFeatures(imageData, cells).isEmpty())
            throw new IllegalArgumentException("A required classifier measurement is missing");
        int width = featureExtractor.nFeatures();
        long pairCount = "svm".equals(method) ? pathClasses.size() * (pathClasses.size() - 1L) / 2L : 0;
        long scoreRowBytes = 32L * (pairCount + pathClasses.size()) + 128L;
        if (scoreRowBytes > 32L * 1024 * 1024)
            throw new IllegalArgumentException("The class inventory exceeds the bounded native score workspace");
        int batchSize = Math.max(1, Math.min(4096, Math.min(
            8 * 1024 * 1024 / Math.max(1, width), (int) (32L * 1024 * 1024 / scoreRowBytes))));
        for (int start = 0; start < cells.size(); start += batchSize) {
            List<PathObject> batch = cells.subList(start, Math.min(start + batchSize, cells.size()));
            try (Mat samples = new Mat(batch.size(), width, opencv_core.CV_32F)) {
                featureExtractor.extractFeatures(imageData, batch, (FloatBuffer) samples.createBuffer());
                try (Indexer si = samples.createIndexer()) {
                    for (int row = 0; row < samples.rows(); row++) for (int col = 0; col < width; col++)
                        if (!Double.isFinite(si.getDouble(row, col)))
                            throw new IllegalArgumentException("Classifier features must be finite");
                }
                ClassifierScores.Prediction prediction = predict(samples);
                for (int row = 0; row < batch.size(); row++) {
                    PathObject cell = batch.get(row);
                    int label = prediction.labels()[row];
                    if (label < 0 || label >= pathClasses.size())
                        throw new IllegalStateException("Native cell prediction has an invalid class or score");
                    double score = prediction.scores()[row][label];
                    if (!Double.isFinite(score) || score < 0 || score > 1)
                        throw new IllegalStateException("Native cell prediction has an invalid class or score");
                    clearScores(cell);
                    cell.setPathClass(pathClasses.get(label), score);
                    cell.getMetadata().put("mif_classifier_score_kind", scoreKind);
                    for (int col = 0; col < pathClasses.size(); col++)
                        cell.getMeasurementList().put("Classifier: score: " + pathClasses.get(col), prediction.scores()[row][col]);
                    if (prediction.margins() != null) for (int p = 0; p < prediction.pairs().length; p++) {
                        int[] pair = prediction.pairs()[p];
                        cell.getMeasurementList().put("Classifier: SVM margin: " + pathClasses.get(pair[0])
                            + " vs " + pathClasses.get(pair[1]), prediction.margins()[row][p]);
                    }
                }
            }
        }
        return cells.size();
    }

    public static boolean isScoreName(String name) {
        return name.startsWith("Classifier: score: ") || name.startsWith("Classifier: SVM margin: ");
    }

    public static void clearScores(PathObject cell) {
        String[] names = cell.getMeasurementList().getNames().stream().filter(MifSupervisedObjectClassifier::isScoreName)
            .toArray(String[]::new);
        if (names.length > 0) cell.getMeasurementList().removeAll(names);
    }

    /** Return labels and scores without mutating hierarchy or fitted preparation. */
    public ClassifierScores.Prediction predict(Mat samples) {
        return ClassifierScores.predict(method, nativeModel, samples, pathClasses.size());
    }

    private static UnsupportedOperationException explicitBatchRequired() {
        return new UnsupportedOperationException(
            "Classify canonical cells through the pipeline bounded phenotyping stage; an explicit cell batch is required");
    }
}
