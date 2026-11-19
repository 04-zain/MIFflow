package org.mif.classification;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.awt.image.BufferedImage;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.bytedeco.javacpp.PointerScope;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.prep.PreparedGeometryFactory;
import org.slf4j.LoggerFactory;
import qupath.lib.classifiers.pixel.PixelClassifier;
import qupath.lib.images.ImageData;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.io.GsonTools;
import qupath.lib.regions.RegionRequest;
import qupath.opencv.ml.pixel.PixelClassifierTools;

/** Exact score ties from an unchanged native RF and its original pixel grid.
 * The temporary output operation requests existing RF votes and preserves their
 * floating-point values. Saved model/features and class decisions are unchanged.
 */
public final class ClassifierPrecisionRecall {
    private ClassifierPrecisionRecall() {}

    private static Map<String, Object> unavailable(String reason) {
        LoggerFactory.getLogger(ClassifierPrecisionRecall.class).info("Precision-recall unavailable: {}", reason);
        return null;
    }

    private static JsonObject scoreTree(PixelClassifier classifier) {
        JsonObject tree = GsonTools.getInstance().toJsonTree(classifier, PixelClassifier.class).getAsJsonObject();
        if (!tree.has("op") || !tree.get("op").isJsonObject()) return null;
        JsonObject data = tree.getAsJsonObject("op");
        if (!data.has("op") || !data.get("op").isJsonObject()) return null;
        JsonObject operation = data.getAsJsonObject("op");
        if (!operation.has("ops") || !operation.get("ops").isJsonArray()) return null;
        JsonArray ops = operation.getAsJsonArray("ops");
        if (ops.size() < 2) return null;
        JsonObject model = ops.get(ops.size() - 2).getAsJsonObject();
        JsonObject convert = ops.get(ops.size() - 1).getAsJsonObject();
        if (!"op.ml.opencv-statmodel".equals(model.get("type").getAsString())
                || !model.has("model") || !"RTrees".equals(model.getAsJsonObject("model").get("class").getAsString())) return null;
        if (!"op.core.convert".equals(convert.get("type").getAsString())
                || !"UINT8".equals(convert.get("pixelType").getAsString())
                || model.get("requestProbabilities").getAsBoolean())
            return null;
        model.addProperty("requestProbabilities", true);
        convert.addProperty("pixelType", "FLOAT32");
        JsonObject metadata = tree.getAsJsonObject("metadata");
        metadata.addProperty("outputType", "MULTICLASS_PROBABILITY");
        JsonArray channels = new JsonArray();
        int n = metadata.getAsJsonObject("classificationLabels").size();
        for (int i = 0; i < n; i++) {
            JsonObject channel = new JsonObject();
            channel.addProperty("name", "Native RF vote " + i);
            channel.addProperty("color", 0xffffff);
            channels.add(channel);
        }
        metadata.add("outputChannels", channels);
        return tree;
    }

    /** Null explicitly means this classifier has no supported native RF scores. */
    public static Map<String, Object> evaluate(ImageData<BufferedImage> image,
            PixelClassifier classifier, ImageServer<BufferedImage> hardServer,
            Map<String, Geometry> regions, Map<Integer, String> labels,
            List<String> classes) throws Exception {
        JsonObject tree = scoreTree(classifier);
        if (tree == null) return unavailable("this classifier has no supported native RF score operation");
        int width = classes.size(), outputs = labels.size();
        // Older pixel classifiers may use sparse response codes. Their native
        // hard prediction remains supported, but this score view cannot assert
        // a corresponding probability-column identity.
        for (int i = 0; i < outputs; i++) if (!labels.containsKey(i))
            return unavailable("native response codes are noncontiguous");
        List<Integer> bands = new ArrayList<>();
        for (String id : classes) {
            List<Integer> found = labels.entrySet().stream().filter(e -> id.equals(e.getValue())).map(Map.Entry::getKey).toList();
            if (found.size() != 1) throw new IllegalArgumentException("The RF score class mapping is ambiguous");
            bands.add(found.get(0));
        }
        List<String> truths = new ArrayList<>(regions.keySet());
        var sharedBoundaries = new ArrayList<org.locationtech.jts.geom.prep.PreparedGeometry>();
        for (int i = 0; i < truths.size(); i++) {
            if (!classes.contains(truths.get(i))) throw new IllegalArgumentException("A score reference class is undeclared");
            for (int j = i + 1; j < truths.size(); j++) {
                var overlap = regions.get(truths.get(i)).intersection(regions.get(truths.get(j)));
                if (overlap.getArea() > 1e-9)
                    return unavailable("reference-class areas overlap, so pixel truth is ambiguous");
                if (!overlap.isEmpty()) sharedBoundaries.add(PreparedGeometryFactory.prepare(overlap));
            }
        }
        List<TreeMap<Double, long[]>> bins = new ArrayList<>();
        for (int i = 0; i < width; i++) bins.add(new TreeMap<>());
        long[] supports = new long[width];
        // Native allocations of the deserialized score view belong to this call.
        try (PointerScope allocations = new PointerScope()) {
            PixelClassifier scored = GsonTools.getInstance().fromJson(tree, PixelClassifier.class);
            try (ImageServer<BufferedImage> server = PixelClassifierTools.createPixelClassificationServer(image, scored)) {
                double ds = hardServer.getDownsampleForResolution(0);
                if (!(Double.isFinite(ds) && ds > 0 && ds == server.getDownsampleForResolution(0)))
                    return unavailable("the score view cannot preserve the native sampling resolution");
                for (var entry : regions.entrySet()) {
                    Geometry geometry = entry.getValue();
                    var bounds = geometry.getEnvelopeInternal();
                    var prepared = PreparedGeometryFactory.prepare(geometry);
                    int truth = classes.indexOf(entry.getKey());
                    int x = Math.max(0, (int) Math.floor(bounds.getMinX()));
                    int y = Math.max(0, (int) Math.floor(bounds.getMinY()));
                    int maxX = Math.min(hardServer.getWidth(), (int) Math.ceil(bounds.getMaxX()));
                    int maxY = Math.min(hardServer.getHeight(), (int) Math.ceil(bounds.getMaxY()));
                    if (maxX <= x || maxY <= y || (double)(maxX - x) * (maxY - y) / ds / ds > 50000000)
                        return unavailable("the optional score bounding-grid inventory is empty or exceeds 50 million pixels");
                    var region = RegionRequest.createInstance(hardServer.getPath(), ds, x, y, maxX - x, maxY - y);
                    for (var tile : hardServer.getTileRequestManager().getTileRequests(region)) {
                        var request = tile.getRegionRequest();
                        var hard = hardServer.readRegion(request).getRaster();
                        var scores = server.readRegion(RegionRequest.createInstance(server.getPath(), request)).getRaster();
                        if (scores.getWidth() != hard.getWidth() || scores.getHeight() != hard.getHeight()
                                || scores.getNumBands() != outputs)
                            return unavailable("the native score raster cannot represent the original pixels and class columns");
                        for (int yy = 0; yy < hard.getHeight(); yy++) for (int xx = 0; xx < hard.getWidth(); xx++) {
                            var point = geometry.getFactory().createPoint(new Coordinate(
                                request.getX() + (xx + .5) * ds, request.getY() + (yy + .5) * ds));
                            if (!prepared.covers(point)) continue;
                            if (sharedBoundaries.stream().anyMatch(boundary -> boundary.covers(point)))
                                return unavailable("a sampled pixel centre has contradictory reference classes");
                            double[] row = scores.getPixel(xx, yy, (double[]) null);
                            int best = 0; double total = 0;
                            for (int c = 0; c < outputs; c++) {
                                if (!Double.isFinite(row[c]) || row[c] < 0 || row[c] > 1)
                                    throw new IllegalArgumentException("The forest emitted a nonfinite or invalid native vote fraction");
                                total += row[c];
                                if (row[c] > row[best]) best = c;
                            }
                            if (Math.abs(total - 1) > 1e-5 || best != hard.getSample(xx, yy, 0))
                                throw new IllegalArgumentException("The retained RF scores disagree with the original native decision");
                            supports[truth]++;
                            for (int c = 0; c < width; c++) {
                                long[] counts = bins.get(c).computeIfAbsent(row[bands.get(c)], ignored -> new long[2]);
                                counts[c == truth ? 0 : 1]++;
                                if (bins.get(c).size() > 100001)
                                    return unavailable("the optional exact score histogram exceeds 100,001 bins");
                            }
                        }
                    }
                }
            }
        }
        List<Long> supportRows = new ArrayList<>();
        for (long count : supports) supportRows.add(count);
        List<List<List<Number>>> histograms = new ArrayList<>();
        for (var histogram : bins) {
            List<List<Number>> rows = new ArrayList<>();
            histogram.forEach((score, counts) -> rows.add(List.of(score, counts[0], counts[1])));
            histograms.add(rows);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("reference_class_counts", supportRows);
        result.put("histograms", histograms);
        return result;
    }

    public static Map<String, Object> evidence(String split, String fit, List<String> classes,
            List<Map<String, Object>> entries) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("schema", "mif-held-out-pixel-scores/v1");
        result.put("split", split); result.put("fit_execution_sha256", fit);
        result.put("score_kind", "native_tree_vote_fraction"); result.put("sample_unit", "pixels");
        result.put("sampling", "reference_class_union_pixel_centres");
        result.put("class_ids", classes); result.put("entries", entries);
        return result;
    }
}
