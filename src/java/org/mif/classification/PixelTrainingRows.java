package org.mif.classification;

import java.awt.image.BufferedImage;
import java.awt.Color;
import java.awt.image.DataBufferByte;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.bytedeco.javacpp.PointerScope;
import org.bytedeco.javacpp.indexer.IntIndexer;
import org.bytedeco.javacpp.indexer.FloatIndexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.global.opencv_ml;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_core.MatVector;
import org.bytedeco.opencv.opencv_ml.TrainData;
import qupath.lib.images.ImageData;
import qupath.lib.objects.classes.PathClass;
import qupath.lib.objects.PathObject;
import qupath.lib.regions.RegionRequest;
import qupath.opencv.ops.ImageDataOp;
import qupath.opencv.ops.ImageOps;
import qupath.process.gui.commands.ml.PixelClassifierTraining;

/** Keep pixel entry order, OpenCV subset selection and inverse-frequency weights. */
public final class PixelTrainingRows {
    public record MatrixRows(Mat samples, Mat responses,
            Map<String, List<Integer>> ranges) implements AutoCloseable {
        @Override public void close() {
            try {
                responses.close();
            } finally {
                samples.close();
            }
        }
    }

    public record Fit(TrainData trainData, ImageDataOp serializedOp,
            Map<String, Integer> entryCounts, Map<String, Integer> classCounts,
            int sampleCount, int featureCount, PointerScope allocations) implements AutoCloseable {
        @Override public void close() {
            allocations.deallocate();
        }
    }

    private PixelTrainingRows() {}

    /** Bound retained rows for area labels without reading image features.
     * QuPath fills these shapes into a byte mask with this same transform.
     * The caller supplies raster dimensions at least as large as the feature
     * tile and uses this only when no boundary class can add painted pixels.
     * Unknown/non-area geometry retains the full-tile conservative bound.
     */
    public static long boundAreaRows(Collection<? extends PathObject> objects,
            RegionRequest request, int width, int height) {
        require(width > 0 && height > 0 && Double.isFinite(request.getDownsample()) &&
                request.getDownsample() > 0, "pixel mask dimensions are invalid");
        long full = Math.multiplyExact((long) width, height);
        for (var object : objects) {
            if (object == null || object.getROI() == null || !object.getROI().isArea()) {
                return full;
            }
        }
        var mask = new BufferedImage(width, height, BufferedImage.TYPE_BYTE_GRAY);
        var graphics = mask.createGraphics();
        try {
            graphics.scale(1.0 / request.getDownsample(), 1.0 / request.getDownsample());
            graphics.translate(-request.getX(), -request.getY());
            graphics.setColor(Color.WHITE);
            for (var object : objects) {
                graphics.fill(object.getROI().getShape());
            }
        } finally {
            graphics.dispose();
        }
        long count = 0;
        for (byte value : ((DataBufferByte) mask.getRaster().getDataBuffer()).getData()) {
            if (value != 0) {
                count++;
            }
        }
        return count;
    }

    private static void require(boolean valid, String message) {
        if (!valid) {
            throw new IllegalStateException(message);
        }
    }

    /** Borrow QuPath's configured extractor, cloning each entry before reuse.
     * Its mutable training matrices are replaced by the next entry. Concatenate
     * those retained rows in entry order, then release the temporary parts.
     */
    public static MatrixRows extract(PixelClassifierTraining helper,
            List<ImageData<BufferedImage>> views, List<String> entryIds,
            Map<PathClass, Integer> labels) throws IOException {
        List<Mat> samples = new ArrayList<>(), responses = new ArrayList<>();
        Map<String, List<Integer>> ranges = new LinkedHashMap<>();
        int offset = 0;
        Mat allSamples = new Mat(), allResponses = new Mat();
        boolean returned = false;
        Throwable primary = null;
        try {
            for (int i = 0; i < views.size(); i++) {
                var generated = helper.createTrainingDataForLabelMap(
                        java.util.Collections.singletonList(views.get(i)), labels);
                try (TrainData data = generated.getTrainData();
                        Mat sampleView = data.getTrainSamples();
                        Mat responseView = data.getTrainResponses()) {
                    Mat part = sampleView.clone();
                    samples.add(part);
                    Mat target = responseView.clone();
                    responses.add(target);
                    require(part.rows() > 0 && target.rows() == part.rows(),
                            "pixel entry generated no aligned training samples");
                    ranges.put(entryIds.get(i), List.of(offset, offset + part.rows()));
                    offset += part.rows();
                }
            }
            try (MatVector sx = new MatVector(samples.toArray(Mat[]::new));
                    MatVector ty = new MatVector(responses.toArray(Mat[]::new))) {
                opencv_core.vconcat(sx, allSamples);
                opencv_core.vconcat(ty, allResponses);
            }
            returned = true;
            return new MatrixRows(allSamples, allResponses, ranges);
        } catch (IOException | RuntimeException | Error error) {
            primary = error;
            throw error;
        } finally {
            RuntimeException failure = null;
            for (int i = responses.size() - 1; i >= 0; i--) {
                failure = close(responses.get(i), failure);
            }
            for (int i = samples.size() - 1; i >= 0; i--) {
                failure = close(samples.get(i), failure);
            }
            if (!returned || failure != null) {
                failure = close(allResponses, failure);
                failure = close(allSamples, failure);
            }
            if (failure != null) {
                if (primary != null) {
                    primary.addSuppressed(failure);
                } else {
                    throw failure;
                }
            }
        }
    }

    private static RuntimeException close(Mat value, RuntimeException failure) {
        try {
            value.close();
        } catch (RuntimeException error) {
            if (failure == null) {
                failure = error;
            } else {
                failure.addSuppressed(error);
            }
        }
        return failure;
    }

    /** Consume assembled rows and preserve OpenCV's seeded subset selection.
     * Source indices supply exact entry counts before the full table closes.
     * Weights and fitted normalization/PCA use only the selected rows. The fit
     * owns that native preparation and TrainData until saving/evaluation ends;
     * the source image operation is borrowed and extended for serialization.
     */
    public static Fit prepare(MatrixRows rows, List<String> entryIds,
            List<? extends Number> candidateRows, int features, int maximum, int seed,
            boolean reweight, Map<Integer, String> classByLabel,
            FeaturePreparation.Settings settings, ImageDataOp baseOp) {
        try (PointerScope allocations = new PointerScope()) {
            Map<String, Integer> entries = new TreeMap<>();
            Mat samples, responses;

            // Check bounded extraction against the stage's existing preflight.
            try (MatrixRows owned = rows) {
                require(rows.samples().cols() == features,
                        "pixel extracted feature count exceeds preflight metadata");
                for (int i = 0; i < entryIds.size(); i++) {
                    String id = entryIds.get(i);
                    entries.put(id, 0);
                    var range = rows.ranges().get(id);
                    require(range != null && range.get(1) - range.get(0) <=
                            candidateRows.get(i).longValue(),
                            "pixel extracted entry rows exceed preflight estimate");
                }

                // Copy the selected native rows and count their original owners.
                try (TrainData split = TrainData.create(rows.samples(),
                        opencv_ml.ROW_SAMPLE, rows.responses())) {
                    opencv_core.setRNGSeed(seed);
                    if (maximum > 0 && split.getNTrainSamples() > maximum) {
                        split.setTrainTestSplit(maximum, true);
                    } else {
                        split.shuffleTrainTest();
                    }
                    try (Mat view = split.getTrainSamples()) {
                        samples = view.clone();
                    }
                    try (Mat view = split.getTrainResponses()) {
                        responses = view.clone();
                    }
                    int selected = samples.rows();
                    require(responses.rows() == selected && split.getNTrainSamples() == selected,
                            "pixel selected training rows are inconsistent");
                    try (Mat indices = split.getTrainSampleIdx()) {
                        boolean implicit = indices.empty();
                        int nr = indices.rows(), nc = indices.cols();
                        if (implicit) {
                            require(selected == rows.samples().rows(),
                                    "empty pixel training index cannot select a subset");
                        } else {
                            require(nr > 0 && nc > 0 && (nr == 1 || nc == 1) &&
                                    (long) nr * nc == selected,
                                    "pixel training indices are not one exact vector");
                            require(indices.type() == opencv_core.CV_32SC1,
                                    "pixel training indices are not INT32");
                        }
                        IntIndexer index = implicit ? null : indices.createIndexer();
                        try {
                            for (int i = 0; i < selected; i++) {
                                int source = implicit ? i : nr == 1 ? index.get(0, i) : index.get(i, 0);
                                require(source >= 0 && source < rows.samples().rows(),
                                        "pixel training index is outside source rows");
                                String owner = null;
                                for (var range : rows.ranges().entrySet()) {
                                    if (source >= range.getValue().get(0) &&
                                            source < range.getValue().get(1)) {
                                        owner = range.getKey();
                                        break;
                                    }
                                }
                                require(owner != null, "pixel sample lost its entry owner");
                                entries.put(owner, entries.get(owner) + 1);
                            }
                        } finally {
                            if (index != null) {
                                index.release();
                            }
                        }
                    }
                }
            }

            // The original assembled table is already released at this point.
            // Preserve inverse-frequency Float32 weights and native label counts.
            Mat weights = new Mat();
            Map<Integer, Integer> counts = new LinkedHashMap<>();
            try (IntIndexer index = responses.createIndexer()) {
                for (int i = 0; i < responses.rows(); i++) {
                    counts.merge(index.get(i), 1, Integer::sum);
                }
                if (reweight) {
                    weights.create(responses.rows(), 1, opencv_core.CV_32FC1);
                    try (FloatIndexer writer = weights.createIndexer()) {
                        for (int i = 0; i < responses.rows(); i++) {
                            writer.put(i, (float) responses.rows() / (float) counts.get(index.get(i)));
                        }
                    }
                }
            }

            // Fit QuPath preparation on these rows and retain it in the saved op.
            var preparation = FeaturePreparation.pixel(samples, settings, Double.NaN);
            var operation = preparation.doesSomething() ?
                    baseOp.appendOps(ImageOps.ML.preprocessor(preparation)) : baseOp;
            TrainData data;
            try (Mat vars = new Mat(); Mat indices = new Mat(); Mat types = new Mat()) {
                data = TrainData.create(samples, opencv_ml.ROW_SAMPLE, responses,
                        vars, indices, weights, types);
            }
            Map<String, Integer> classes = new TreeMap<>();
            classByLabel.values().forEach(id -> classes.put(id, 0));
            for (var count : counts.entrySet()) {
                String id = classByLabel.get(count.getKey());
                require(id != null, "pixel training response is undeclared");
                classes.put(id, classes.get(id) + count.getValue());
            }
            int sampleCount = samples.rows(), featureCount = samples.cols();

            // TrainData retains the native storage; these wrappers add no table.
            samples.close();
            responses.close();
            weights.close();
            allocations.extend();
            return new Fit(data, operation, entries, classes, sampleCount, featureCount, allocations);
        }
    }
}
