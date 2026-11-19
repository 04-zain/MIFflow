package org.mif.classification;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Envelope;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.prep.PreparedGeometryFactory;
import org.locationtech.jts.geom.prep.PreparedGeometry;
import org.locationtech.jts.index.strtree.STRtree;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.regions.RegionRequest;
import qupath.lib.roi.interfaces.ROI;

/** Count predictions with each existing evaluation route's coverage convention. */
public final class ClassifierEvaluation {
    public record Votes(Map<String, Long> counts, long ignored) {}

    private ClassifierEvaluation() {}

    private static void require(boolean valid, String message) {
        if (!valid) {
            throw new IllegalStateException(message);
        }
    }

    /** Saved resource JSON may represent an integer patch size as a floating-point Number. */
    public static int requireKronosPatchSize(Object value) {
        require(value instanceof Number, "KRONOS patch size must be 32, 64, 128 or 256");
        double size = ((Number) value).doubleValue();
        require(size == 32 || size == 64 || size == 128 || size == 256,
                "KRONOS patch size must be 32, 64, 128 or 256");
        return (int) size;
    }

    /** Declared class order resolves a tied majority exactly as before. */
    public static String majorityLabel(Map<String, ? extends Number> counts, List<String> classes) {
        require(!classes.isEmpty() && counts.keySet().equals(new HashSet<>(classes)) &&
                counts.values().stream().allMatch(value -> value.longValue() >= 0),
                "pixel label counts are invalid");
        long maximum = counts.values().stream().mapToLong(Number::longValue).max().orElse(0);
        require(maximum > 0, "pixel target has zero declared-class samples");
        for (String id : classes) {
            if (counts.get(id).longValue() == maximum) {
                return id;
            }
        }
        throw new IllegalStateException("pixel label counts are invalid");
    }

    /** Held-out native tiles use ROI.contains at each classification-pixel centre. */
    public static Votes nativePixels(ImageServer<BufferedImage> server, ROI roi,
            RegionRequest region, Map<Integer, Map<String, Object>> labels,
            List<String> classes) throws IOException {
        double downsample = server.getDownsampleForResolution(0);
        require(Double.isFinite(downsample) && downsample > 0,
                "pixel classifier resolution is invalid");
        Map<String, Long> counts = new TreeMap<>();
        classes.forEach(id -> counts.put(id, 0L));
        long ignored = 0;
        var seen = new HashSet<String>();
        var tiles = server.getTileRequestManager().getTileRequests(region);
        require(tiles != null && !tiles.isEmpty(),
                "held-out target has no native classification tiles");
        for (var tile : tiles) {
            require(tile != null && tile.getLevel() == 0 &&
                    tile.getZ() == roi.getZ() && tile.getT() == roi.getT() &&
                    tile.getRegionRequest().getPath().equals(server.getPath()) &&
                    Double.isFinite(tile.getDownsample()) && tile.getDownsample() == downsample,
                    "native classification tile identity differs");
            int x = tile.getTileX(), y = tile.getTileY(),
            width = tile.getTileWidth(), height = tile.getTileHeight();
            require(x >= 0 && y >= 0 && width > 0 && height > 0 &&
                    (long) width * height <= 1048576L,
                    "native classification tile exceeds the bounded pixel limit");
            String key = tile.getLevel() + ":" + x + ":" + y + ":" + width + ":" + height +
                    ":" + tile.getZ() + ":" + tile.getT();
            require(seen.add(key), "native classification tile is repeated");
            var image = server.readRegion(tile.getRegionRequest());
            require(image != null && image.getWidth() == width && image.getHeight() == height &&
                    image.getRaster().getNumBands() >= 1, "native classification tile raster differs");
            for (int v = 0; v < height; v++) {
                for (int u = 0; u < width; u++) {
                    if (!roi.contains((x + u + 0.5) * downsample, (y + v + 0.5) * downsample)) {
                        continue;
                    }
                    var label = labels.get(image.getRaster().getSample(u, v, 0));
                    require(label != null, "pixel classifier produced a foreign native label");
                    if (Boolean.TRUE.equals(label.get("ignored"))) {
                        ignored++;
                    } else {
                        String id = (String) label.get("canonical_class_id");
                        counts.put(id, counts.get(id) + 1);
                    }
                }
            }
        }
        return new Votes(counts, ignored);
    }

    /** Held-out KRONOS observes complete covered, in-image patches at the saved size. */
    public static Votes kronosPatches(ImageServer<BufferedImage> server, ROI roi,
            double downsample, Map<Integer, Map<String, Object>> labels,
            List<String> classes, int patchSize) throws IOException {
        requireKronosPatchSize(patchSize);
        require(roi.isArea() && roi.getZ() == 0 && roi.getT() == 0 &&
                Double.isFinite(downsample) && downsample >= 1,
                "KRONOS evaluation requires a supported two-dimensional area");
        double side = patchSize * downsample;
        require(Math.abs(side - Math.rint(side)) <= 1e-9,
                "KRONOS patch footprints must have exact level-0 boundaries");
        int firstRow = Math.max(0, (int) Math.floor(roi.getBoundsY() / side)),
        lastRow = (int) Math.ceil((roi.getBoundsY() + roi.getBoundsHeight()) / side);
        int firstCol = Math.max(0, (int) Math.floor(roi.getBoundsX() / side)),
        lastCol = (int) Math.ceil((roi.getBoundsX() + roi.getBoundsWidth()) / side);
        require((long) (lastRow - firstRow) * (lastCol - firstCol) <= 100000,
                "The held-out annotation exceeds the bounded patch inventory");
        Map<String, Long> counts = new LinkedHashMap<>();
        classes.forEach(id -> counts.put(id, 0L));
        long ignored = 0;
        var geometry = roi.getGeometry();
        var factory = new org.locationtech.jts.geom.GeometryFactory();
        for (int row = firstRow; row < lastRow; row++) {
            for (int col = firstCol; col < lastCol; col++) {
                double x = col * side, y = row * side;
                if (x + side > server.getWidth() || y + side > server.getHeight()) {
                    continue;
                }
                if (!geometry.covers(factory.toGeometry(new Envelope(x, x + side, y, y + side)))) {
                    continue;
                }
                var region = RegionRequest.createInstance(server.getPath(), downsample,
                        (int) Math.round(x), (int) Math.round(y),
                        (int) Math.round(side), (int) Math.round(side), 0, 0);
                var prediction = server.readRegion(region);
                require(prediction != null && prediction.getWidth() == patchSize &&
                        prediction.getHeight() == patchSize,
                        "The classifier did not return one complete recorded patch");
                int code = prediction.getRaster().getSample(patchSize / 2, patchSize / 2, 0);
                if (code == 255) {
                    ignored++;
                    continue;
                }
                var label = labels.get(code);
                require(label != null, "The held-out patch emitted an undeclared class");
                if (Boolean.TRUE.equals(label.get("ignored"))) {
                    ignored++;
                } else {
                    String id = (String) label.get("canonical_class_id");
                    require(counts.containsKey(id), "The held-out patch class is foreign");
                    counts.put(id, counts.get(id) + 1);
                }
            }
        }
        return new Votes(counts, ignored);
    }

    /** Tuning retains Geometry.covers and rejects every undeclared observation.
     * A zero patch size selects conventional pixels; KRONOS supplies its saved size.
     */
    public static List<List<Long>> pixelConfusion(ImageServer<BufferedImage> server,
            Map<String, Geometry> regions, Map<Integer, String> labels,
            List<String> classes, int patchSize) throws IOException {
        return pixelConfusion(server, regions, labels, classes, patchSize, null);
    }

    public record ReferenceConfusion(List<List<Long>> matrix,
            Map<String, Map<String, Long>> referenceCounts) {}

    /** Observe the original fold samples without changing class-union scoring.
     * Centres may belong to overlapping same-class references: their counts are
     * non-additive. A KRONOS centre association does not mean that its individual
     * reference covers the entire patch; the existing union coverage still does.
     */
    public static ReferenceConfusion pixelConfusionWithReferences(ImageServer<BufferedImage> server,
            Map<String, Geometry> regions, Map<Integer, String> labels,
            List<String> classes, int patchSize, Map<String, ROI> references,
            Map<String, String> referenceClasses) throws IOException {
        var memberships = new ReferenceMemberships(references, referenceClasses, classes);
        var matrix = pixelConfusion(server, regions, labels, classes, patchSize, memberships);
        return new ReferenceConfusion(matrix, memberships.counts);
    }

    private record ReferenceMember(String id, String truth, ROI roi, PreparedGeometry geometry) {}

    private static final class ReferenceMemberships {
        private final STRtree index = new STRtree();
        private final Map<String, Map<String, Long>> counts = new TreeMap<>();
        private final List<String> classes;

        ReferenceMemberships(Map<String, ROI> references, Map<String, String> truths, List<String> classes) {
            this.classes = classes;
            references.forEach((id, roi) -> {
                var votes = new LinkedHashMap<String, Long>();
                classes.forEach(label -> votes.put(label, 0L));
                counts.put(id, votes);
                var geometry = roi.getGeometry();
                index.insert(geometry.getEnvelopeInternal(),
                        new ReferenceMember(id, truths.get(id), roi, PreparedGeometryFactory.prepare(geometry)));
            });
            index.build();
        }

        void add(String truth, int predicted, org.locationtech.jts.geom.Point point, int z, int t) {
            for (Object raw : index.query(point.getEnvelopeInternal())) {
                var member = (ReferenceMember) raw;
                if (truth.equals(member.truth()) && member.roi().getZ() == z && member.roi().getT() == t
                        && member.geometry().covers(point)) {
                    var votes = counts.get(member.id());
                    votes.put(classes.get(predicted), votes.get(classes.get(predicted)) + 1);
                }
            }
        }
    }

    private static List<List<Long>> pixelConfusion(ImageServer<BufferedImage> server,
            Map<String, Geometry> regions, Map<Integer, String> labels,
            List<String> classes, int patchSize, ReferenceMemberships references) throws IOException {
        require(patchSize == 0 || patchSize == 32 || patchSize == 64 || patchSize == 128 || patchSize == 256,
                "Select conventional pixels or a supported KRONOS patch size");
        long[][] matrix = new long[classes.size()][classes.size()];
        double ds = server.getDownsampleForResolution(0);
        require(Double.isFinite(ds) && ds > 0, "Fold classifier sampling resolution is invalid");
        for (var row : regions.entrySet()) {
            Geometry geometry = row.getValue();
            var bounds = geometry.getEnvelopeInternal();
            var prepared = PreparedGeometryFactory.prepare(geometry);
            int truth = classes.indexOf(row.getKey());
            require(truth >= 0, "Reference class is undeclared");
            if (patchSize > 0) {
                double side = patchSize * ds;
                long firstRow = (long) Math.floor(bounds.getMinY() / side),
                lastRow = (long) Math.ceil(bounds.getMaxY() / side);
                long firstCol = (long) Math.floor(bounds.getMinX() / side),
                lastCol = (long) Math.ceil(bounds.getMaxX() / side);
                require((lastRow - firstRow) * (lastCol - firstCol) <= 100000,
                        "Fold reference patch inventory exceeds its bound");
                for (long y = firstRow; y < lastRow; y++) {
                    for (long x = firstCol; x < lastCol; x++) {
                        var footprint = geometry.getFactory().toGeometry(
                                new Envelope(x * side, (x + 1) * side, y * side, (y + 1) * side));
                        if (!prepared.covers(footprint)) {
                            continue;
                        }
                        var request = RegionRequest.createInstance(server.getPath(), ds,
                                (int) Math.round(x * side), (int) Math.round(y * side),
                                (int) Math.round(side), (int) Math.round(side));
                        int column = classes.indexOf(labels.get(
                                server.readRegion(request).getRaster().getSample(patchSize / 2, patchSize / 2, 0)));
                        require(column >= 0,
                                "A held-out patch has no declared prediction; no observation is silently dropped");
                        matrix[truth][column]++;
                        if (references != null) {
                            references.add(row.getKey(), column, geometry.getFactory().createPoint(
                                    new Coordinate((x + 0.5) * side, (y + 0.5) * side)), request.getZ(), request.getT());
                        }
                    }
                }
            } else {
                int x = Math.max(0, (int) Math.floor(bounds.getMinX())),
                y = Math.max(0, (int) Math.floor(bounds.getMinY()));
                int maxX = Math.min(server.getWidth(), (int) Math.ceil(bounds.getMaxX())),
                maxY = Math.min(server.getHeight(), (int) Math.ceil(bounds.getMaxY()));
                require((double) (maxX - x) * (maxY - y) / ds / ds <= 50000000,
                        "Fold reference pixel inventory exceeds its bound");
                var region = RegionRequest.createInstance(server.getPath(), ds,
                        x, y, maxX - x, maxY - y);
                for (var tile : server.getTileRequestManager().getTileRequests(region)) {
                    var request = tile.getRegionRequest();
                    var raster = server.readRegion(request).getRaster();
                    for (int yy = 0; yy < raster.getHeight(); yy++) {
                        for (int xx = 0; xx < raster.getWidth(); xx++) {
                            double px = request.getX() + (xx + 0.5) * request.getDownsample(),
                            py = request.getY() + (yy + 0.5) * request.getDownsample();
                            var point = geometry.getFactory().createPoint(new Coordinate(px, py));
                            if (!prepared.covers(point)) {
                                continue;
                            }
                            int column = classes.indexOf(labels.get(raster.getSample(xx, yy, 0)));
                            require(column >= 0,
                                    "A held-out pixel has no declared prediction; review the boundary/ignore policy");
                            matrix[truth][column]++;
                            if (references != null) {
                                references.add(row.getKey(), column, point, request.getZ(), request.getT());
                            }
                        }
                    }
                }
            }
        }
        return matrixRows(matrix);
    }

    /** Accumulate already-admitted cell labels in declared class order. */
    public static List<List<Long>> objectConfusion(List<? extends Number> truth,
            List<String> predictions, List<String> classes) {
        long[][] matrix = new long[classes.size()][classes.size()];
        for (int i = 0; i < predictions.size(); i++) {
            int predicted = classes.indexOf(predictions.get(i));
            require(predicted >= 0, "Object fold classifier returned an undeclared label");
            matrix[truth.get(i).intValue()][predicted]++;
        }
        return matrixRows(matrix);
    }

    private static List<List<Long>> matrixRows(long[][] matrix) {
        var result = new java.util.ArrayList<List<Long>>(matrix.length);
        for (long[] row : matrix) {
            var values = new java.util.ArrayList<Long>(row.length);
            for (long value : row) {
                values.add(value);
            }
            result.add(values);
        }
        return result;
    }
}
