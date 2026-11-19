package org.mif.geometry;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.operation.union.UnaryUnionOp;

/** Compare tissue outlines while keeping identity assignment with the review stage. */
public final class TissueSectionGeometry {
    /** A negative previous index means the stage must assign a new section identity. */
    public record Match(int previousIndex, List<String> derivedFrom) {}

    private TissueSectionGeometry() {}

    /**
     * Compare each new outline with the accepted outlines using the original JTS
     * intersection and union calculations. We cache area and centroid per outline,
     * then require a unique eligible match in both directions. Ambiguous splits,
     * merges and close alternatives retain their intersecting source identifiers
     * without inheriting an accepted identity.
     */
    public static List<Match> match(List<Geometry> candidates, List<Geometry> previous,
            List<String> previousIds, double pixelWidth, double pixelHeight,
            double minimumIou, double maximumDisplacement, double maximumAreaChange,
            double uniquenessMargin) {
        int n = candidates.size(), m = previous.size();
        if (m == 0) {
            return java.util.Collections.nCopies(n, new Match(-1, List.of()));
        }
        double[][] iou = new double[n][m];
        boolean[][] eligible = new boolean[n][m];
        int[] reverseCounts = new int[m];
        double[] oldAreas = new double[m];
        Coordinate[] oldCentres = new Coordinate[m];
        for (int j = 0; j < m; j++) {
            oldAreas[j] = previous.get(j).getArea();
            oldCentres[j] = previous.get(j).getCentroid().getCoordinate();
        }
        for (int i = 0; i < n; i++) {
            Geometry current = candidates.get(i);
            double currentArea = current.getArea();
            Coordinate centre = current.getCentroid().getCoordinate();
            for (int j = 0; j < m; j++) {
                Geometry old = previous.get(j);
                double intersection = old.intersection(current).getArea();
                double union = old.union(current).getArea();
                iou[i][j] = union == 0d ? 0d : intersection / union;
                double dx = (oldCentres[j].x - centre.x) * pixelWidth;
                double dy = (oldCentres[j].y - centre.y) * pixelHeight;
                double displacement = Math.sqrt(dx * dx + dy * dy);
                double areaChange = oldAreas[j] == 0d ? Double.POSITIVE_INFINITY
                        : Math.abs(currentArea - oldAreas[j]) / oldAreas[j];
                eligible[i][j] = iou[i][j] >= minimumIou && displacement <= maximumDisplacement
                        && areaChange <= maximumAreaChange;
                if (eligible[i][j]) reverseCounts[j]++;
            }
        }
        List<Match> result = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            int selected = -1, count = 0;
            List<String> derived = new ArrayList<>();
            for (int j = 0; j < m; j++) {
                if (iou[i][j] > 0d) derived.add(previousIds.get(j));
                if (eligible[i][j]) {
                    selected = j;
                    count++;
                }
            }
            if (count == 1 && reverseCounts[selected] == 1) {
                double runnerUp = 0d, reverseRunnerUp = 0d;
                for (int j = 0; j < m; j++) {
                    if (j != selected) runnerUp = Math.max(runnerUp, iou[i][j]);
                }
                for (int k = 0; k < n; k++) {
                    if (k != i) reverseRunnerUp = Math.max(reverseRunnerUp, iou[k][selected]);
                }
                if (!(iou[i][selected] - runnerUp >= uniquenessMargin
                        && iou[i][selected] - reverseRunnerUp >= uniquenessMargin)) selected = -1;
            } else {
                selected = -1;
            }
            derived.sort(Comparator.naturalOrder());
            result.add(new Match(selected, List.copyOf(derived)));
        }
        return result;
    }

    /** Measure the combined candidate coverage and the existing acceptance criteria. */
    public static Map<String, Object> quality(List<Geometry> geometries,
            int imageWidth, int imageHeight, double pixelWidth, double pixelHeight,
            double minimumArea) {
        boolean valid = geometries.stream().allMatch(g -> g != null && !g.isEmpty() && g.isValid());
        double area = valid ? UnaryUnionOp.union(geometries).getArea() : 0d;
        double imageArea = imageWidth * (double) imageHeight;
        boolean contained = true, areaCompliant = true;
        for (Geometry geometry : geometries) {
            var bounds = geometry.getEnvelopeInternal();
            contained &= bounds.getMinX() >= 0d && bounds.getMinY() >= 0d
                    && bounds.getMaxX() <= imageWidth && bounds.getMaxY() <= imageHeight;
            areaCompliant &= geometry.getArea() * pixelWidth * pixelHeight + 1e-9d >= minimumArea;
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("geometry_valid", valid);
        result.put("component_count", geometries.size());
        result.put("tissue_area_fraction", imageArea <= 0d ? 0d : area / imageArea);
        result.put("image_bounds_contained", contained);
        result.put("minimum_area_compliant", areaCompliant);
        return result;
    }
}
