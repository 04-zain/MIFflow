package org.mif.registration;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import net.imglib2.RealLocalizable;
import net.imglib2.RealPositionable;
import net.imglib2.realtransform.InverseRealTransform;
import net.imglib2.realtransform.InvertibleRealTransform;

/**
 * Evaluates the saved Elastix affine and cubic B-spline in physical coordinates.
 *
 * <p>Elastix's Compose convention samples moving pixels with E(x)=B(A(x)).
 * {@link #applyInverse(double[], double[])} evaluates that fixed-to-moving map
 * directly; {@link #apply(double[], double[])} solves its opposite direction
 * for annotation transfer. X coefficients precede Y coefficients, with X grid
 * positions changing fastest. The planar transform preserves Z.</p>
 *
 * <p>The immutable coefficient arrays belong to this transform. It borrows no
 * image server, native matrix or stage authority and publishes no files.</p>
 */
public final class MifElastixBSplineTransform implements InvertibleRealTransform {
    private final int nx, ny, ix, iy;
    private final double ox, oy, sx, sy, g00, g01, g10, g11;
    private final double fx, fy, mx, my, tolerance;
    private final int maxIterations;
    private final double[] affine, coefficients, direction;

    /** Decode the complete saved record once at its external transform boundary. */
    public MifElastixBSplineTransform(Map<?, ?> record) {
        Set<String> required = Set.of("type", "schema", "direction", "fixed_pixel_size_um",
                "moving_pixel_size_um", "initial_fixed_to_moving_physical", "grid",
                "coefficients", "bspline_order", "inverse");
        if (!record.keySet().equals(required)
                || !"MifElastixBSplineTransform".equals(record.get("type"))
                || !"mif-elastix-bspline/v1".equals(record.get("schema"))
                || !"moving-to-fixed".equals(record.get("direction"))
                || integer(record.get("bspline_order"), "B-spline order") != 3) {
            throw new IllegalArgumentException("The saved B-spline transform has an unsupported format.");
        }

        Map<?, ?> grid = checkedMap(record.get("grid"),
                Set.of("size", "index", "origin", "spacing", "direction"), "control grid");
        double[] sizes = vector(grid.get("size"), 2, "control-grid size");
        nx = integer(sizes[0], "grid width");
        ny = integer(sizes[1], "grid height");
        if (nx < 4 || ny < 4 || (long) nx * ny > 262144L) {
            throw new IllegalArgumentException("The B-spline control grid must contain 4 or more points per axis and at most 262,144 points.");
        }
        double[] indices = vector(grid.get("index"), 2, "control-grid index");
        ix = integer(indices[0], "grid X index");
        iy = integer(indices[1], "grid Y index");
        double[] origin = vector(grid.get("origin"), 2, "control-grid origin");
        ox = origin[0];
        oy = origin[1];
        double[] spacing = vector(grid.get("spacing"), 2, "control-grid spacing");
        sx = spacing[0];
        sy = spacing[1];
        direction = vector(grid.get("direction"), 4, "control-grid direction");

        // Elastix stores direction in column-major order. Its inverse, followed
        // by axis spacing, maps physical displacements into continuous grid units.
        double determinant = direction[0] * direction[3] - direction[2] * direction[1];
        if (sx <= 0d || sy <= 0d || Math.abs(determinant) < 1e-12d) {
            throw new IllegalArgumentException("B-spline grid spacing must be positive and its direction matrix invertible.");
        }
        g00 = direction[3] / determinant / sx;
        g01 = -direction[2] / determinant / sx;
        g10 = -direction[1] / determinant / sy;
        g11 = direction[0] / determinant / sy;

        double[] fixed = vector(record.get("fixed_pixel_size_um"), 2, "fixed pixel size");
        double[] moving = vector(record.get("moving_pixel_size_um"), 2, "moving pixel size");
        fx = fixed[0];
        fy = fixed[1];
        mx = moving[0];
        my = moving[1];
        if (Math.min(Math.min(fx, fy), Math.min(mx, my)) <= 0d) {
            throw new IllegalArgumentException("Both images require positive physical X/Y pixel sizes.");
        }
        affine = vector(record.get("initial_fixed_to_moving_physical"), 6, "initial physical affine");
        if (affine[0] * affine[3] - affine[1] * affine[2] <= 1e-12d) {
            throw new IllegalArgumentException("The initial affine must preserve orientation and have nonzero area.");
        }
        coefficients = vector(record.get("coefficients"), 2 * nx * ny, "B-spline coefficients");
        Map<?, ?> inverse = checkedMap(record.get("inverse"),
                Set.of("tolerance_pixels", "max_iterations"), "inverse settings");
        tolerance = number(inverse.get("tolerance_pixels"), "inverse tolerance");
        maxIterations = integer(inverse.get("max_iterations"), "inverse iterations");
        if (tolerance <= 0d || tolerance > 0.1d || maxIterations < 1 || maxIterations > 1000) {
            throw new IllegalArgumentException("Use an inverse tolerance above zero and at most 0.1 pixels, with 1–1000 iterations.");
        }
    }

    /** Read one nested record without permitting omitted or replacement fields. */
    private static Map<?, ?> checkedMap(Object value, Set<String> fields, String label) {
        if (!(value instanceof Map<?, ?> mapping) || !mapping.keySet().equals(fields)) {
            throw new IllegalArgumentException("Check the saved " + label + " fields.");
        }
        return mapping;
    }

    /** Saved scalar values must remain finite before interval checks or calculations. */
    private static double number(Object value, String label) {
        if (!(value instanceof Number numerical) || !Double.isFinite(numerical.doubleValue())) {
            throw new IllegalArgumentException("The " + label + " must be finite numerical data.");
        }
        return numerical.doubleValue();
    }

    /** Grid indices, sizes and iteration counts retain their exact integer domain. */
    private static int integer(Object value, String label) {
        double parsed = number(value, label);
        if (parsed != Math.rint(parsed) || parsed < Integer.MIN_VALUE || parsed > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("The " + label + " must be an integer.");
        }
        return (int) parsed;
    }

    /** Copy an ordered saved list; callers cannot subsequently alter our coefficients. */
    private static double[] vector(Object value, int count, String label) {
        if (!(value instanceof List<?> values) || values.size() != count) {
            throw new IllegalArgumentException("The " + label + " has the wrong number of values.");
        }
        double[] output = new double[count];
        for (int i = 0; i < count; i++) {
            output[i] = number(values.get(i), label);
        }
        return output;
    }

    /** Cubic weights correspond to floor(u)-1 through floor(u)+2 in each axis. */
    private static double weight(int index, double t) {
        return switch (index) {
            case 0 -> (1d - t) * (1d - t) * (1d - t) / 6d;
            case 1 -> (3d * t * t * t - 6d * t * t + 4d) / 6d;
            case 2 -> (-3d * t * t * t + 3d * t * t + 3d * t + 1d) / 6d;
            default -> t * t * t / 6d;
        };
    }

    /** Differentiate the same cubic basis for the inverse solver's analytic Jacobian. */
    private static double derivative(int index, double t) {
        return switch (index) {
            case 0 -> -0.5d * (1d - t) * (1d - t);
            case 1 -> 1.5d * t * t - 2d * t;
            case 2 -> -1.5d * t * t + t + 0.5d;
            default -> 0.5d * t * t;
        };
    }

    /**
     * Evaluate B(x,y)=position+displacement in physical units. A supported point
     * combines sixteen X-fast coefficients in the original accumulation order.
     * Outside complete 4×4 support, displacement is zero and derivative identity.
     * When requested, output[2..5] receives its row-major physical Jacobian.
     */
    private void spline(double x, double y, double[] output, boolean withDerivative) {
        double u = g00 * (x - ox) + g01 * (y - oy) - ix;
        double v = g10 * (x - ox) + g11 * (y - oy) - iy;
        output[0] = x;
        output[1] = y;
        if (withDerivative) {
            output[2] = 1d;
            output[3] = 0d;
            output[4] = 0d;
            output[5] = 1d;
        }
        if (u < 1d || v < 1d || u >= nx - 2d || v >= ny - 2d) {
            return;
        }
        int baseX = (int) Math.floor(u) - 1;
        int baseY = (int) Math.floor(v) - 1;
        double tx = u - Math.floor(u);
        double ty = v - Math.floor(v);
        double dx = 0d, dy = 0d, dxu = 0d, dxv = 0d, dyu = 0d, dyv = 0d;
        int count = nx * ny;
        for (int j = 0; j < 4; j++) {
            double wy = weight(j, ty);
            double dwy = withDerivative ? derivative(j, ty) : 0d;
            for (int i = 0; i < 4; i++) {
                double wx = weight(i, tx);
                double w = wx * wy;
                int position = (baseY + j) * nx + baseX + i;
                double cx = coefficients[position];
                double cy = coefficients[position + count];
                dx += w * cx;
                dy += w * cy;
                if (withDerivative) {
                    double dwx = derivative(i, tx);
                    dxu += dwx * wy * cx;
                    dxv += wx * dwy * cx;
                    dyu += dwx * wy * cy;
                    dyv += wx * dwy * cy;
                }
            }
        }
        output[0] += dx;
        output[1] += dy;
        if (withDerivative) {
            output[2] += dxu * g00 + dxv * g10;
            output[3] += dxu * g01 + dxv * g11;
            output[4] += dyu * g00 + dyv * g10;
            output[5] += dyu * g01 + dyv * g11;
        }
    }

    /** Convert fixed pixels to physical units, evaluate B(A(x)), then return moving pixels. */
    @Override
    public void applyInverse(double[] source, double[] target) {
        double x = target[0] * fx;
        double y = target[1] * fy;
        double z = target[2];
        spline(affine[0] * x + affine[1] * y + affine[4],
                affine[2] * x + affine[3] * y + affine[5], source, false);
        source[0] /= mx;
        source[1] /= my;
        source[2] = z;
    }

    /**
     * Solve B(u)=v with the saved damped Newton iteration, then invert A.
     * Residuals use moving-axis pixel sizes; each update tries up to sixteen
     * halvings and must decrease the residual. The final check includes the
     * last permitted update without changing its tolerance or acceptance rule.
     */
    @Override
    public void apply(double[] source, double[] target) {
        double vx = source[0] * mx;
        double vy = source[1] * my;
        double z = source[2];
        if (!Double.isFinite(vx) || !Double.isFinite(vy)) {
            throw new IllegalArgumentException("Transform coordinates must be finite.");
        }
        double x = vx, y = vy;
        double[] value = new double[6], trial = new double[2];
        boolean complete = false;
        for (int iteration = 0; iteration < maxIterations; iteration++) {
            spline(x, y, value, true);
            double rx = value[0] - vx, ry = value[1] - vy;
            double error = Math.hypot(rx / mx, ry / my);
            if (error <= tolerance) {
                complete = true;
                break;
            }
            double det = value[2] * value[5] - value[3] * value[4];
            if (!Double.isFinite(det) || det <= 1e-10d) {
                throw new IllegalArgumentException("The B-spline inverse encountered a collapsed or reversed local region.");
            }
            double dx = (value[5] * rx - value[3] * ry) / det;
            double dy = (-value[4] * rx + value[2] * ry) / det;
            double step = 1d;
            boolean decreased = false;
            for (int attempt = 0; attempt < 16; attempt++) {
                spline(x - step * dx, y - step * dy, trial, false);
                if (Math.hypot((trial[0] - vx) / mx, (trial[1] - vy) / my) < error) {
                    x -= step * dx;
                    y -= step * dy;
                    decreased = true;
                    break;
                }
                step *= 0.5d;
            }
            if (!decreased) {
                break;
            }
        }
        if (!complete) {
            spline(x, y, trial, false);
            complete = Math.hypot((trial[0] - vx) / mx, (trial[1] - vy) / my) <= tolerance;
        }
        if (!complete) {
            throw new IllegalArgumentException("The B-spline inverse did not reach the requested pixel tolerance.");
        }
        double det = affine[0] * affine[3] - affine[1] * affine[2];
        double ax = x - affine[4], ay = y - affine[5];
        target[0] = (affine[3] * ax - affine[1] * ay) / det / fx;
        target[1] = (-affine[2] * ax + affine[0] * ay) / det / fy;
        target[2] = z;
    }

    /** ImgLib2 position overloads use the same planar array calculation. */
    @Override
    public void apply(RealLocalizable source, RealPositionable target) {
        double[] a = new double[3], b = new double[3];
        source.localize(a);
        apply(a, b);
        target.setPosition(b);
    }

    @Override
    public void applyInverse(RealPositionable source, RealLocalizable target) {
        double[] a = new double[3], b = new double[3];
        target.localize(b);
        applyInverse(a, b);
        source.setPosition(a);
    }

    @Override
    public int numSourceDimensions() {
        return 3;
    }

    @Override
    public int numTargetDimensions() {
        return 3;
    }

    /** The inverse view delegates directly to this transform's opposite direction. */
    @Override
    public InvertibleRealTransform inverse() {
        return new InverseRealTransform(this);
    }

    /** A copy owns independent saved arrays and retains the exact evaluation settings. */
    @Override
    public InvertibleRealTransform copy() {
        return new MifElastixBSplineTransform(toMap());
    }

    /** Encode the unchanged saved field order with copied numerical lists. */
    public Map<String, Object> toMap() {
        Map<String, Object> grid = new LinkedHashMap<>();
        grid.put("size", new ArrayList<>(List.of(nx, ny)));
        grid.put("index", new ArrayList<>(List.of(ix, iy)));
        grid.put("origin", new ArrayList<>(List.of(ox, oy)));
        grid.put("spacing", new ArrayList<>(List.of(sx, sy)));
        grid.put("direction", list(direction));
        Map<String, Object> inverse = new LinkedHashMap<>();
        inverse.put("tolerance_pixels", tolerance);
        inverse.put("max_iterations", maxIterations);
        Map<String, Object> record = new LinkedHashMap<>();
        record.put("type", "MifElastixBSplineTransform");
        record.put("schema", "mif-elastix-bspline/v1");
        record.put("direction", "moving-to-fixed");
        record.put("fixed_pixel_size_um", new ArrayList<>(List.of(fx, fy)));
        record.put("moving_pixel_size_um", new ArrayList<>(List.of(mx, my)));
        record.put("initial_fixed_to_moving_physical", list(affine));
        record.put("grid", grid);
        record.put("coefficients", list(coefficients));
        record.put("bspline_order", 3);
        record.put("inverse", inverse);
        return record;
    }

    private static List<Double> list(double[] values) {
        List<Double> output = new ArrayList<>(values.length);
        for (double value : values) {
            output.add(value);
        }
        return output;
    }
}
