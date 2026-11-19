package org.mif.classification;

import com.google.gson.JsonObject;
import org.bytedeco.javacpp.PointerScope;
import org.bytedeco.javacpp.indexer.Indexer;
import org.bytedeco.opencv.opencv_core.Mat;
import qupath.lib.classifiers.Normalization;
import qupath.lib.io.GsonTools;
import qupath.opencv.io.OpenCVTypeAdapters;
import qupath.opencv.ml.FeaturePreprocessor;
import qupath.opencv.ml.objects.features.Normalizer;
import qupath.opencv.ml.objects.features.Preprocessing;
import qupath.opencv.ml.objects.features.Preprocessing.PCAProjector;

import static org.bytedeco.opencv.global.opencv_core.*;

/** Select a PCA basis when fitting. Prediction uses QuPath's ordinary saved objects. */
final class PcaFitting {
    private PcaFitting() {}

    static Normalizer normalizer(Mat samples, Normalization mode, double missingValue) {
        var fitted = Preprocessing.createNormalizer(mode, samples, missingValue);
        int columns = fitted.nFeatures();
        double[] offsets = new double[columns], scales = new double[columns];
        boolean corrected = false;
        for (int col = 0; col < columns; col++) {
            offsets[col] = fitted.getOffset(col);
            scales[col] = fitted.getScale(col);
            if (!Double.isFinite(scales[col])) {
                // Give a constant feature a unit divisor and centre it exactly at zero.
                try (Mat column = samples.col(col)) {
                    double[] min = new double[1], max = new double[1];
                    minMaxLoc(column, min, max, null, null, null);
                    if (!Double.isFinite(min[0]) || min[0] != max[0])
                        throw new IllegalArgumentException("PCA could not normalize training feature " + (col + 1));
                    offsets[col] = -min[0];
                    scales[col] = 1;
                    corrected = true;
                }
            }
        }
        return corrected ? Normalizer.createNormalizer(offsets, scales, missingValue) : fitted;
    }

    static PCAProjector projector(Mat normalized, double target, boolean whiten) {
        return fit(normalized, target, whiten).projector();
    }

    static FeaturePreprocessor pixel(Mat samples, FeaturePreparation.Settings settings, double missingValue) {
        int inputLength = samples.cols();
        var normalizer = normalizer(samples, settings.normalization(), missingValue);
        Preprocessing.normalize(samples, normalizer);
        var basis = fit(samples, settings.retainedVariance(), settings.normalizePca());

        // The training matrix is already normalized. Apply only PCA here.
        try (var temporary = new PointerScope()) {
            basis.projector().project(samples, samples);
        }
        // QuPath 0.7 has no public constructor for a pre-fitted pixel pipeline.
        // Reopen its standard serialization so ImageOps applies both transforms to new pixels.
        var gson = GsonTools.getInstance();
        var fields = new JsonObject();
        fields.add("normalizer", gson.toJsonTree(normalizer));
        fields.add("pca", basis.fields());
        fields.addProperty("inputLength", inputLength);
        fields.addProperty("outputLength", basis.count());
        return gson.fromJson(fields, FeaturePreprocessor.class);
    }

    private record Basis(JsonObject fields, int count) {
        PCAProjector projector() {
            return GsonTools.getInstance().fromJson(fields, PCAProjector.class);
        }
    }

    private static Basis fit(Mat normalized, double target, boolean whiten) {
        if (!(target > 0 && target <= 1))
            throw new IllegalArgumentException("PCA variance target must be greater than zero and at most one");
        if (whiten && normalized.depth() != CV_32F)
            throw new IllegalArgumentException("QuPath PCA whitening requires 32-bit floating-point training features");
        if (!checkRange(normalized))
            throw new IllegalArgumentException("PCA requires finite training features after normalization");
        try (Mat preciseMean = new Mat(); Mat mean = new Mat();
             Mat vectors = new Mat(); Mat values = new Mat()) {
            // Accumulating the mean in float can give repeated identical values spurious variance.
            reduce(normalized, preciseMean, 0, REDUCE_AVG, CV_64F);
            preciseMean.convertTo(mean, normalized.depth());
            if (normalized.depth() == CV_64F) {
                // Even double accumulation can round a repeated double. Use its exact value
                // as the mean wherever a column's extrema agree.
                try (Mat min = new Mat(); Mat max = new Mat(); Mat constant = new Mat()) {
                    reduce(normalized, min, 0, REDUCE_MIN);
                    reduce(normalized, max, 0, REDUCE_MAX);
                    compare(min, max, constant, CMP_EQ);
                    min.copyTo(mean, constant);
                }
            }
            PCACompute2(normalized, mean, vectors, values, 0); // Integer overload returns the full basis.
            int count = componentCount(values, target);
            try (Mat selectedVectors = vectors.rowRange(0, count);
                 Mat selectedValues = values.rowRange(0, count)) {
                var adapter = OpenCVTypeAdapters.getTypeAdaptor(Mat.class);
                var fields = new JsonObject();
                fields.add("mean", adapter.toJsonTree(mean));
                fields.add("eigenvectors", adapter.toJsonTree(selectedVectors));
                fields.add("eigenvalues", adapter.toJsonTree(selectedValues));
                fields.addProperty("normalize", whiten);
                return new Basis(fields, count);
            }
        }
    }

    private static int componentCount(Mat values, double target) {
        try (Indexer eigenvalues = values.createIndexer()) {
            long[] index = {0, 0};
            double total = 0;
            int positive = 0;
            // OpenCV returns the eigenvalues in decreasing order.
            for (int i = 0; i < values.rows(); i++) {
                index[0] = i;
                double value = eigenvalues.getDouble(index);
                if (!Double.isFinite(value))
                    throw new IllegalArgumentException("PCA produced a nonfinite training variance");
                if (value > 0) {
                    total += value;
                    positive++;
                }
            }
            if (positive == 0)
                throw new IllegalArgumentException("PCA requires training features with positive variance");
            if (target == 1)
                return positive;
            double cumulative = 0;
            int minimum = Math.min(2, positive);
            for (int i = 0; i < positive; i++) {
                index[0] = i;
                cumulative += eigenvalues.getDouble(index);
                if (i + 1 >= minimum && cumulative >= target * total)
                    return i + 1;
            }
            return positive;
        }
    }
}
