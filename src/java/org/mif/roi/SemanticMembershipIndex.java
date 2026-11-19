package org.mif.roi;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Point;
import org.locationtech.jts.index.strtree.STRtree;

/** Index already admitted semantic regions without retaining cells or changing boundary rules. */
public final class SemanticMembershipIndex {
    private record Region(int code, Geometry geometry) {}
    private final STRtree index = new STRtree();

    /** Borrow geometries in the stage's dictionary order; positions become Int32 codes. */
    public SemanticMembershipIndex(List<Geometry> orderedRegions) {
        for (int code = 0; code < orderedRegions.size(); code++) {
            Geometry geometry = orderedRegions.get(code);
            if (!geometry.isEmpty()) {
                index.insert(geometry.getEnvelopeInternal(), new Region(code, geometry));
            }
        }
        index.build();
    }

    /** Check prospective primary parents before the stage changes accepted annotations. */
    public void requireUniquePrimary(Point centroid) {
        boolean found = false;
        for (Object item : index.query(centroid.getEnvelopeInternal())) {
            Region region = (Region) item;
            if (centroid.coveredBy(region.geometry())) {
                if (found) {
                    throw new IllegalStateException("Cell has conflicting prospective primary parents");
                }
                found = true;
            }
        }
    }

    /** Calculate membership again for each cell written after the accepted annotation changes. */
    public List<Integer> secondaryCodes(Point centroid) {
        List<Integer> codes = new ArrayList<>();
        for (Object item : index.query(centroid.getEnvelopeInternal())) {
            Region region = (Region) item;
            if (centroid.coveredBy(region.geometry())) codes.add(region.code());
        }
        Collections.sort(codes);
        return codes;
    }
}
