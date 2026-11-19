package org.mif.roi;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import org.locationtech.jts.geom.Geometry;
import qupath.lib.objects.PathObject;

/** Full-geometry containment selection among the caller's admitted biological parents. */
public final class BiologicalParentSelector {
    private static final Comparator<String> IDS = Comparator.nullsFirst(Comparator.naturalOrder());

    /** The caller supplies its existing domain UUID or biological ROI ID tie convention. */
    public record Parent(PathObject object, Geometry geometry, String stableId) {}

    private record Candidate(Parent parent, double area) {}

    private final List<Candidate> parents;

    /**
     * Capture areas once while the admitted parent geometry stays unchanged for
     * this calculation. This is an operation-local selection, not an index or
     * persistent cache; the caller still owns objects, source checks and mutation.
     */
    public BiologicalParentSelector(List<Parent> admitted) {
        parents = new ArrayList<>(admitted.size());
        for (Parent parent : admitted) {
            parents.add(new Candidate(parent, parent.geometry().getArea()));
        }
    }

    /**
     * Select the smallest complete containing geometry, then lexical stable ID.
     * Equal area/ID retains the first admitted parent. Boundary containment uses
     * the original JTS coveredBy operation, separate from cell-centroid rules.
     */
    public PathObject smallestContaining(Geometry child) {
        Candidate best = null;
        for (Candidate current : parents) {
            if (!child.coveredBy(current.parent().geometry())) {
                continue;
            }
            if (best == null) {
                best = current;
                continue;
            }
            int areaOrder = Double.compare(current.area(), best.area());
            if (areaOrder < 0 || (areaOrder == 0
                    && IDS.compare(current.parent().stableId(), best.parent().stableId()) < 0)) {
                best = current;
            }
        }
        return best == null ? null : best.parent().object();
    }
}
