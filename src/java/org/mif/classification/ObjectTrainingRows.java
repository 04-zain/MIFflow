package org.mif.classification;

import java.awt.image.BufferedImage;
import java.nio.FloatBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.bytedeco.javacpp.PointerScope;
import org.bytedeco.javacpp.indexer.IntIndexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.global.opencv_ml;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_core.MatVector;
import org.bytedeco.opencv.opencv_ml.TrainData;
import org.locationtech.jts.geom.CoordinateSequence;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.MultiPolygon;
import org.locationtech.jts.geom.Polygon;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;
import qupath.opencv.ml.objects.features.FeatureExtractor;
import qupath.opencv.ml.objects.features.FeatureExtractors;

/** Assemble only the stage's authenticated selected cells in their given order. */
public final class ObjectTrainingRows {
    public record Entry(String id, ImageData<BufferedImage> image,
            List<PathObject> cells, int[] responses) {}
    public record Fit(TrainData trainData, FeatureExtractor<BufferedImage> extractor,
            Map<String, Integer> entryCounts, Map<String, Integer> classCounts,
            int sampleCount, int featureCount, PointerScope allocations) implements AutoCloseable {
        @Override public void close() {
            allocations.deallocate();
        }
    }

    private ObjectTrainingRows() {}

    /** Preserve the selected cell's exact native polygon and ring order.
     * Snapshot production and live fit admission use this same representation;
     * no GeoJSON rounding, ring normalization or centroid substitute is applied.
     * An absent nucleus is represented separately by the caller's empty map.
     */
    public static Map<String, Object> geometry(Geometry geometry) {
        if (geometry == null || geometry.isEmpty() || !geometry.isValid()) {
            throw new IllegalStateException("live cell geometry is invalid");
        }
        for (var coordinate : geometry.getCoordinates()) {
            if (!Double.isFinite(coordinate.x) || !Double.isFinite(coordinate.y)) {
                throw new IllegalStateException("live cell geometry is invalid");
            }
        }
        if (geometry instanceof Polygon polygon) {
            return Map.of("type", "Polygon", "coordinates", polygonCoordinates(polygon));
        }
        if (geometry instanceof MultiPolygon polygons) {
            List<List<List<List<Double>>>> coordinates = new ArrayList<>();
            for (int i = 0; i < polygons.getNumGeometries(); i++) {
                coordinates.add(polygonCoordinates((Polygon) polygons.getGeometryN(i)));
            }
            return Map.of("type", "MultiPolygon", "coordinates", coordinates);
        }
        throw new IllegalStateException("unsupported live cell geometry " + geometry.getGeometryType());
    }

    private static List<List<List<Double>>> polygonCoordinates(Polygon polygon) {
        List<List<List<Double>>> rings = new ArrayList<>();
        rings.add(ring(polygon.getExteriorRing().getCoordinateSequence()));
        for (int i = 0; i < polygon.getNumInteriorRing(); i++) {
            rings.add(ring(polygon.getInteriorRingN(i).getCoordinateSequence()));
        }
        return rings;
    }

    private static List<List<Double>> ring(CoordinateSequence sequence) {
        List<List<Double>> result = new ArrayList<>();
        for (var coordinate : sequence.toCoordinateArray()) {
            result.add(List.of(coordinate.x, coordinate.y));
        }
        return result;
    }

    /** Extract the stage's selected measurements in entry/cell order.
     * Each invocation fits normalization/PCA on its own training rows, including
     * separate subject folds. The fit owns native matrices and fitted PCA;
     * selected image and cell objects remain borrowed from the stage.
     */
    public static Fit prepare(List<Entry> entries, List<String> measurements,
            List<String> classes, FeaturePreparation.Settings settings) {
        try (PointerScope allocations = new PointerScope()) {
            FeatureExtractor<BufferedImage> extractor =
                    FeatureExtractors.createMeasurementListFeatureExtractor(measurements);
            List<Mat> parts = new ArrayList<>();
            Map<String, Integer> entryCounts = new TreeMap<>();
            int count = 0;
            for (Entry entry : entries) {
                if (entry.responses().length != entry.cells().size()) {
                    throw new IllegalStateException("object training responses are not aligned");
                }
                entryCounts.merge(entry.id(), entry.cells().size(), Integer::sum);
                // Unlabelled sections retain their evidence without allocating
                // an empty native matrix or invoking the feature extractor.
                if (entry.cells().isEmpty()) {
                    continue;
                }
                Mat part = new Mat(entry.cells().size(), measurements.size(), opencv_core.CV_32FC1);
                parts.add(part);
                extractor.extractFeatures(entry.image(), entry.cells(), (FloatBuffer) part.createBuffer());
                count += entry.cells().size();
            }
            if (count == 0) {
                throw new IllegalStateException(
                        "Add saved training points for the declared cell types before fitting");
            }
            Mat samples = new Mat();
            try (MatVector vector = new MatVector(parts.toArray(Mat[]::new))) {
                opencv_core.vconcat(vector, samples);
            } finally {
                for (Mat part : parts) {
                    part.close();
                }
            }

            // Concatenated response order exactly follows feature-row order.
            Mat responses = new Mat(count, 1, opencv_core.CV_32SC1);
            Map<String, Integer> classCounts = new TreeMap<>();
            classes.forEach(id -> classCounts.put(id, 0));
            try (IntIndexer writer = responses.createIndexer()) {
                int offset = 0;
                for (Entry entry : entries) {
                    for (int label : entry.responses()) {
                        writer.put(offset++, label);
                        String id = classes.get(label);
                        classCounts.put(id, classCounts.get(id) + 1);
                    }
                }
            }

            // Keep the fitted extractor with the model for later application.
            var prepared = FeaturePreparation.object(samples, extractor, settings);
            samples = prepared.samples();
            int sampleCount = samples.rows(), featureCount = samples.cols();
            TrainData data = TrainData.create(samples, opencv_ml.ROW_SAMPLE, responses);
            samples.close();
            responses.close();
            allocations.extend();
            return new Fit(data, prepared.extractor(), entryCounts, classCounts,
                    sampleCount, featureCount, allocations);
        }
    }
}
