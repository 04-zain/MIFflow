package org.mif.cells;

import java.awt.Shape;
import java.util.ArrayList;
import java.util.Collection;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Point;
import qupath.lib.roi.interfaces.ROI;

/** One primary class at a cell centroid, with class-neutral shared-edge ownership. */
public final class PrimaryCompartmentMembership {
    private PrimaryCompartmentMembership() {}

    /** Cache the admitted ROI's geometry and shape once, alongside its audit identity. */
    public record Region(String roiId, String classId, Geometry geometry, Shape shape) {
        public Region(String roiId, String classId, ROI roi) {
            this(roiId, classId, roi.getGeometry(), roi.getShape());
        }
    }

    /**
     * Unique covering classes retain their existing association. Only ties where
     * every candidate meets the centroid on its boundary use Java Shape's
     * half-open boundary convention. No biological class receives priority.
     * The caller supplies candidates from the cell's authenticated analysis domain.
     */
    public static String select(Point centroid, Collection<Region> candidates) {
        var covering = new ArrayList<Region>();
        String selectedClass = null;
        boolean conflictingClasses = false;
        for (var region : candidates) {
            if (region.geometry().covers(centroid)) {
                covering.add(region);
                if (selectedClass == null) selectedClass = region.classId();
                else if (!selectedClass.equals(region.classId())) conflictingClasses = true;
            }
        }
        if (selectedClass == null) return "";
        if (!conflictingClasses) return selectedClass;
        for (var region : covering) {
            if (region.geometry().contains(centroid))
                throw ambiguity(centroid, covering, "overlapping primary classes");
        }
        String boundaryClass = null;
        boolean conflictingBoundaryClasses = false;
        for (var region : covering) {
            if (region.shape().contains(centroid.getX(), centroid.getY())) {
                if (boundaryClass == null) boundaryClass = region.classId();
                else if (!boundaryClass.equals(region.classId())) conflictingBoundaryClasses = true;
            }
        }
        if (boundaryClass != null && !conflictingBoundaryClasses) return boundaryClass;
        throw ambiguity(centroid, covering, "an unresolved primary boundary");
    }

    private static IllegalStateException ambiguity(Point centroid, Collection<Region> regions, String reason) {
        String examples = regions.stream().map(r -> r.roiId() + " (" + r.classId() + ")")
                .sorted().limit(5).collect(java.util.stream.Collectors.joining(", "));
        return new IllegalStateException("Cell centroid (" + centroid.getX() + ", " + centroid.getY()
                + ") has " + reason + " among " + regions.size() + " matched regions: " + examples
                + (regions.size() > 5 ? "; further matches omitted" : "")
                + ". Review the primary classification for this analysis region before retrying segmentation or measurements.");
    }
}
