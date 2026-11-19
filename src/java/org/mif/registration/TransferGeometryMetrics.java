package org.mif.registration;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;

/** Physical and spatial measurements for one already transformed ROI. */
public final class TransferGeometryMetrics {
    private TransferGeometryMetrics() {}

    /** Section order is the caller's admitted tissue order, retained in candidate IDs. */
    public record Section(String id, Geometry geometry) {}

    /** Describe the target before the preview's existing unusable-area branch. */
    public record ShapeFacts(boolean valid, boolean nonempty, boolean finite,
                             boolean selfIntersection, double area, int fragments) {}

    /** Geometry predicates and physical area retain the current target coordinate frame. */
    public static ShapeFacts describe(Geometry geometry, double areaScale) {
        boolean valid = geometry.isValid();
        boolean nonempty = !geometry.isEmpty();
        boolean finite = true;
        for (Coordinate coordinate : geometry.getCoordinates()) {
            if (!Double.isFinite(coordinate.x) || !Double.isFinite(coordinate.y)) {
                finite = false;
                break;
            }
        }
        boolean selfIntersection = !geometry.isSimple();
        double area = geometry.getArea() * areaScale;
        int fragments = geometry.getNumGeometries();
        return new ShapeFacts(valid, nonempty, finite, selfIntersection, area, fragments);
    }

    /**
     * Preview and commit share these calculations, but each receives its own
     * freshly transformed geometry and current sections. The preview supplies
     * facts only after its existing positive-area admission has succeeded.
     */
    public static Map<String, Object> measure(double originalArea, int originalFragments,
            Geometry geometry, ShapeFacts target, List<Section> sections, Geometry imageBounds) {
        double bestIntersection = 0d;
        List<String> candidates = new ArrayList<>();
        for (Section section : sections) {
            double fraction = geometry.intersection(section.geometry()).getArea() / geometry.getArea();
            if (fraction > 0d) {
                candidates.add(section.id());
            }
            bestIntersection = Math.max(bestIntersection, fraction);
        }
        double inBoundsArea = geometry.intersection(imageBounds).getArea();
        double outOfBounds = Math.max(0d, Math.min(1d, 1d - inBoundsArea / geometry.getArea()));

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("geometry_valid", target.valid());
        result.put("geometry_nonempty", target.nonempty());
        result.put("coordinates_finite", target.finite());
        result.put("target_tissue_intersection_fraction", bestIntersection);
        result.put("original_area_um2", originalArea);
        result.put("transformed_area_um2", target.area());
        result.put("area_ratio", target.area() / originalArea);
        result.put("self_intersection", target.selfIntersection());
        result.put("original_fragment_count", originalFragments);
        result.put("transformed_fragment_count", target.fragments());
        result.put("candidate_target_section_ids", candidates);
        result.put("out_of_bounds_fraction", outOfBounds);
        return result;
    }

    /** Commit recomputes both physical areas and all target facts from current geometry. */
    public static Map<String, Object> measure(Geometry source, Geometry target, List<Section> sections,
            Geometry imageBounds, double movingAreaScale, double fixedAreaScale) {
        double originalArea = source.getArea() * movingAreaScale;
        return measure(originalArea, source.getNumGeometries(), target,
                describe(target, fixedAreaScale), sections, imageBounds);
    }
}
