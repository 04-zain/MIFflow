package org.mif.registration;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import net.imglib2.realtransform.InvertibleRealTransform;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;

/** Full-transform sampling shared by alignment review and result acceptance. */
public final class AlignmentSampling {
    private AlignmentSampling() {}

    /** Grid states: outside the selected region, uncovered, covered, non-finite transform. */
    public static final byte OMITTED = 0, UNCOVERED = 1, COVERED = 2, INVALID = 3;

    /** The grid is transient display data; evidence retains the existing saved schema. */
    public record Result(Map<String, Object> evidence, double x, double y, double spacing,
                         int columns, int rows, byte[] states, double[] areaRatios) {}

    public static Result evaluate(Geometry region, double x, double y, double width,
            double height, double spacing, InvertibleRealTransform transform,
            int movingWidth, int movingHeight, double physicalAreaScale, boolean retainGrid) {
        if (!(spacing > 0) || !Double.isFinite(spacing))
            throw new IllegalArgumentException("Alignment sampling spacing must be positive.");
        int columns = Math.max(0, (int)Math.ceil(width / spacing - 0.5d));
        int rows = Math.max(0, (int)Math.ceil(height / spacing - 0.5d));
        int length = retainGrid ? Math.multiplyExact(columns, rows) : 0;
        byte[] states = new byte[length];
        double[] ratios = new double[length];
        Arrays.fill(ratios, Double.NaN);
        List<Double> jacobians = new ArrayList<>();
        var inverse = transform.inverse();
        int considered = 0, covered = 0, outside = 0;
        boolean finiteBounds = true;
        double[] target = new double[3], source = new double[3];
        for (int row = 0; row < rows; row++) {
            double py = y + spacing / 2d + row * spacing;
            for (int column = 0; column < columns; column++) {
                double px = x + spacing / 2d + column * spacing;
                int index = row * columns + column;
                if (region != null && !region.covers(region.getFactory().createPoint(new Coordinate(px, py))))
                    continue;
                considered++;
                target[0] = px; target[1] = py;
                inverse.apply(target, source);
                boolean finite = Arrays.stream(source).allMatch(Double::isFinite);
                finiteBounds &= finite;
                boolean inside = finite && source[0] >= 0 && source[1] >= 0
                        && source[0] < movingWidth && source[1] < movingHeight;
                if (!inside) {
                    outside++;
                    if (retainGrid) states[index] = finite ? UNCOVERED : INVALID;
                    continue;
                }
                covered++;
                double ratio = localJacobian(transform, source[0], source[1]) * physicalAreaScale;
                if (!Double.isFinite(ratio)) finiteBounds = false;
                else jacobians.add(ratio);
                if (retainGrid) {
                    states[index] = Double.isFinite(ratio) ? COVERED : INVALID;
                    ratios[index] = ratio;
                }
            }
        }
        Map<String, Object> evidence;
        if (considered == 0 || jacobians.isEmpty()) {
            String reason = considered == 0 ? "sampling_grid_has_no_points_in_region"
                    : "sampling_grid_has_no_valid_jacobian_points";
            evidence = values("available", false, "unavailable_reason", reason,
                    "landmarks", landmarks(), "deformation", values("available", false,
                    "sample_count", null, "jacobian_min", null, "jacobian_median", null,
                    "jacobian_max", null, "negative_jacobian_count", null, "unavailable_reason", reason),
                    "coverage_fraction", null, "out_of_bounds_fraction", null, "bounds_finite", null);
        } else {
            jacobians.sort(Double::compare);
            int middle = jacobians.size() / 2;
            double median = jacobians.size() % 2 == 1 ? jacobians.get(middle)
                    : (jacobians.get(middle - 1) + jacobians.get(middle)) / 2d;
            evidence = values("available", true, "unavailable_reason", null,
                    "landmarks", landmarks(), "deformation", values("available", true,
                    "sample_count", jacobians.size(), "jacobian_min", jacobians.get(0),
                    "jacobian_median", median, "jacobian_max", jacobians.get(jacobians.size() - 1),
                    "negative_jacobian_count", (int)jacobians.stream().filter(v -> v <= 0d).count(),
                    "unavailable_reason", null), "coverage_fraction", covered / (double)considered,
                    "out_of_bounds_fraction", outside / (double)considered, "bounds_finite", finiteBounds);
        }
        return new Result(evidence, x, y, spacing, columns, rows, states, ratios);
    }

    private static double localJacobian(InvertibleRealTransform transform, double x, double y) {
        double[] p = new double[3], px = new double[3], py = new double[3];
        transform.apply(new double[]{x, y, 0}, p);
        transform.apply(new double[]{x + 1, y, 0}, px);
        transform.apply(new double[]{x, y + 1, 0}, py);
        return (px[0] - p[0]) * (py[1] - p[1]) - (px[1] - p[1]) * (py[0] - p[0]);
    }

    private static Map<String, Object> landmarks() {
        return values("available", false, "count", null, "rms_residual_um", null,
                "max_residual_um", null, "unavailable_reason",
                "Warpy transform file exposes no registered landmark residuals");
    }

    private static Map<String, Object> values(Object... fields) {
        Map<String, Object> result = new LinkedHashMap<>();
        for (int i = 0; i < fields.length; i += 2) result.put((String)fields[i], fields[i + 1]);
        return result;
    }
}
