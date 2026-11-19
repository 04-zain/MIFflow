package org.mif.cells;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Point;
import org.locationtech.jts.geom.prep.PreparedGeometry;
import org.locationtech.jts.geom.prep.PreparedGeometryFactory;
import org.locationtech.jts.index.strtree.STRtree;
import qupath.lib.roi.interfaces.ROI;

/** Centroid population membership for already admitted tissue and classifier regions. */
public final class PopulationMembership {
    /** A primary entry retains the exact half-open boundary selector's ROI and identity. */
    public record Region(String domainId, String classId,
                         PrimaryCompartmentMembership.Region primary,
                         PreparedGeometry secondary) {
        public static Region primary(String domainId, String roiId, String classId, ROI roi) {
            return new Region(domainId, classId,
                    new PrimaryCompartmentMembership.Region(roiId, classId, roi), null);
        }

        public static Region secondary(String domainId, String classId, Geometry geometry) {
            return new Region(domainId, classId, null, PreparedGeometryFactory.prepare(geometry));
        }

        private Geometry geometry() {
            return primary == null ? secondary.getGeometry() : primary.geometry();
        }
    }

    /** Class labels retain sorted unique order before the caller's dictionary-code conversion. */
    public record Membership(boolean detectedTissue, List<String> primaryStructure,
                             List<String> secondaryStructure) {}

    private final PreparedGeometry tissue;
    private final STRtree regions = new STRtree();

    /** Borrow admitted geometries and prepare one spatial index; do not retain cell objects. */
    public PopulationMembership(Geometry tissueGeometry, List<Region> admittedRegions) {
        tissue = PreparedGeometryFactory.prepare(tissueGeometry);
        for (var region : admittedRegions) {
            regions.insert(region.geometry().getEnvelopeInternal(), region);
        }
        regions.build();
    }

    /**
     * Select only candidates in this cell's admitted analysis domain. Tissue and
     * secondary regions keep their covers predicate; primary ties keep the
     * existing class-neutral half-open rule, and Ignore is not a population.
     */
    public Membership select(Point centroid, String domainId) {
        var primary = new ArrayList<PrimaryCompartmentMembership.Region>();
        var secondary = new TreeSet<String>();
        for (Object raw : regions.query(centroid.getEnvelopeInternal())) {
            var region = (Region) raw;
            if (!java.util.Objects.equals(region.domainId(), domainId)) {
                continue;
            }
            if (region.primary() != null) {
                primary.add(region.primary());
            } else if (region.secondary().covers(centroid)) {
                secondary.add(region.classId());
            }
        }
        String selected = PrimaryCompartmentMembership.select(centroid, primary);
        List<String> primaryClasses = selected.isEmpty() || "Ignore".equals(selected)
                ? List.of() : List.of(selected);
        return new Membership(tissue.covers(centroid), primaryClasses, List.copyOf(secondary));
    }
}
