package org.mif.registration;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import net.imglib2.realtransform.AffineTransform3D;
import net.imglib2.realtransform.InvertibleRealTransform;
import net.imglib2.realtransform.InvertibleRealTransformSequence;
import net.imglib2.realtransform.ThinplateSplineTransform;
import net.imglib2.realtransform.inverse.WrappedIterativeInvertibleRealTransform;
import org.apache.commons.math3.linear.Array2DRowRealMatrix;
import org.apache.commons.math3.linear.QRDecomposition;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.GeometryFactory;

/**
 * Numerical landmark refinement and sampled checks for admitted paired images.
 * These methods borrow transforms and tissue geometry and own only their arrays
 * and returned ImgLib2 transforms. File authority, Warpy integration, review and
 * stage publication remain with the caller; no image read or fitting process is
 * launched here.
 */
public final class PairedGeometryOps {
    private PairedGeometryOps() {}

    /** Place the moving physical centre at the fixed centre plus the selected translation. */
    public static AffineTransform3D similarity(int fixedWidth, int fixedHeight,
            double fx, double fy, int movingWidth, int movingHeight, double mx, double my,
            double tx, double ty, double degrees, double scale) {
        if (!Double.isFinite(tx) || !Double.isFinite(ty) || !Double.isFinite(degrees)
                || !Double.isFinite(scale) || scale <= 0.01d || scale > 100d) {
            throw new IllegalArgumentException(
                    "Enter a finite translation/rotation and a positive uniform scale.");
        }
        if (!Double.isFinite(fx) || !Double.isFinite(fy) || !Double.isFinite(mx)
                || !Double.isFinite(my) || fx <= 0d || fy <= 0d || mx <= 0d || my <= 0d) {
            throw new IllegalArgumentException("Both images need physical calibration.");
        }
        double a = Math.cos(Math.toRadians(degrees)) * scale;
        double b = -Math.sin(Math.toRadians(degrees)) * scale;
        double c = -b, d = a;
        double xc = (movingWidth - 1) * mx / 2d, yc = (movingHeight - 1) * my / 2d;
        double ox = (fixedWidth - 1) * fx / 2d + tx - a * xc - b * yc;
        double oy = (fixedHeight - 1) * fy / 2d + ty - c * xc - d * yc;
        var result = new AffineTransform3D();
        result.set(a * mx / fx, b * my / fx, 0d, ox / fx,
                c * mx / fy, d * my / fy, 0d, oy / fy, 0d, 0d, 1d, 0d);
        return result;
    }

    /** Convert the pixel pose's inverse into Elastix's ordered physical affine parameters. */
    public static double[] initialPhysicalAffine(AffineTransform3D movingToFixed,
            double fx, double fy, double mx, double my) {
        var inverse = movingToFixed.inverse();
        return new double[]{inverse.get(0, 0) * mx / fx, inverse.get(0, 1) * mx / fy,
                inverse.get(1, 0) * my / fx, inverse.get(1, 1) * my / fy,
                inverse.get(0, 3) * mx, inverse.get(1, 3) * my};
    }

    /** The caller writes these centre and translation terms in its unchanged Euler header. */
    public record EulerStart(double centreX, double centreY, double tx, double ty) {}

    /** Align reduced-view centres in physical units without changing the angle convention. */
    public static EulerStart initialEuler(int fixedWidth, int fixedHeight, double fixedSpacingX,
            double fixedSpacingY, int movingWidth, int movingHeight, double movingSpacingX,
            double movingSpacingY) {
        double fx = (fixedWidth - 1) * fixedSpacingX / 2d;
        double fy = (fixedHeight - 1) * fixedSpacingY / 2d;
        double tx = (movingWidth - 1) * movingSpacingX / 2d - fx;
        double ty = (movingHeight - 1) * movingSpacingY / 2d - fy;
        return new EulerStart(fx, fy, tx, ty);
    }

    /** Origin and axis probes fit the affine; the last three positions test it independently. */
    public static List<List<Double>> affineProbes(int width, int height, double sx, double sy) {
        double ex = (width - 1) * sx, ey = (height - 1) * sy;
        return List.of(List.of(0d, 0d), List.of(ex, 0d), List.of(0d, ey),
                List.of(ex / 3d, ey / 4d), List.of(ex / 2d, ey / 2d), List.of(ex, ey));
    }

    /** The fitted pixel transform and its original independent-coordinate maximum error. */
    public record AffineFit(AffineTransform3D transform, double maximumErrorPixels) {}

    /**
     * Reconstruct the physical affine from admitted Transformix responses, check
     * the remaining probes in moving pixels, then convert and invert its pixel
     * map. Response count/shape/finite admission stays with the file reader.
     */
    public static AffineFit affineFromProbes(List<? extends List<? extends Number>> probes,
            List<? extends List<? extends Number>> mapped, double fx, double fy,
            double mx, double my) throws IOException {
        double ex = probes.get(1).get(0).doubleValue(), ey = probes.get(2).get(1).doubleValue();
        double x0 = mapped.get(0).get(0).doubleValue(), y0 = mapped.get(0).get(1).doubleValue();
        double ax = (mapped.get(1).get(0).doubleValue() - x0) / ex;
        double bx = (mapped.get(2).get(0).doubleValue() - x0) / ey;
        double ay = (mapped.get(1).get(1).doubleValue() - y0) / ex;
        double by = (mapped.get(2).get(1).doubleValue() - y0) / ey;
        double maximum = 0d;
        for (int i = 3; i < probes.size(); i++) {
            double dx = (x0 + ax * probes.get(i).get(0).doubleValue()
                    + bx * probes.get(i).get(1).doubleValue() - mapped.get(i).get(0).doubleValue()) / mx;
            double dy = (y0 + ay * probes.get(i).get(0).doubleValue()
                    + by * probes.get(i).get(1).doubleValue() - mapped.get(i).get(1).doubleValue()) / my;
            maximum = Math.max(maximum, Math.hypot(dx, dy));
        }
        if (maximum > 0.01d) {
            throw new IOException("The affine conversion failed the independent coordinate check.");
        }
        var fixedToMoving = new AffineTransform3D();
        fixedToMoving.set(ax * fx / mx, bx * fy / mx, 0d, x0 / mx,
                ay * fx / my, by * fy / my, 0d, y0 / my, 0d, 0d, 1d, 0d);
        double determinant = fixedToMoving.get(0, 0) * fixedToMoving.get(1, 1)
                - fixedToMoving.get(0, 1) * fixedToMoving.get(1, 0);
        if (!Double.isFinite(determinant) || determinant <= 1e-8d) {
            throw new IOException("The alignment collapses or reverses the tissue orientation.");
        }
        return new AffineFit(fixedToMoving.inverse(), maximum);
    }

    /** Retain the 7×7 row-major physical grid used for the independent B-spline check. */
    public static List<List<Double>> bsplineProbes(int width, int height, double sx, double sy) {
        List<List<Double>> result = new ArrayList<>(49);
        for (int row = 0; row < 7; row++) {
            for (int column = 0; column < 7; column++) {
                result.add(List.of((width - 1) * sx * column / 6d,
                        (height - 1) * sy * row / 6d));
            }
        }
        return result;
    }

    /** Compare admitted physical Transformix responses with the coefficient map in moving pixels. */
    public static double checkBsplineProbes(InvertibleRealTransform transform,
            List<? extends List<? extends Number>> points,
            List<? extends List<? extends Number>> expected, double fx, double fy,
            double mx, double my) {
        double maximum = 0d;
        for (int i = 0; i < points.size(); i++) {
            double[] output = new double[3];
            transform.applyInverse(output, new double[]{points.get(i).get(0).doubleValue() / fx,
                    points.get(i).get(1).doubleValue() / fy, 0d});
            maximum = Math.max(maximum, Math.hypot(output[0] - expected.get(i).get(0).doubleValue() / mx,
                    output[1] - expected.get(i).get(1).doubleValue() / my));
        }
        if (!Double.isFinite(maximum) || maximum > 0.01d) {
            throw new IllegalArgumentException("The B-spline coefficient calculation differs "
                    + "from Transformix. The candidate was not accepted.");
        }
        return maximum;
    }

    /** The physical 2D fit is wrapped by the caller's existing optional Warpy adapter. */
    public record LandmarkFit(InvertibleRealTransform transform, int landmarkCount,
                              double rmsMicrometres) {}

    /**
     * Convert selected pixel pairs through the global affine and fixed-image
     * calibration, then fit the same local physical TPS. Pair order is retained
     * throughout the source sums, QR design, TPS inputs and fitting-pair RMS.
     */
    public static LandmarkFit fitLandmarks(AffineTransform3D global, List<?> moving,
                                          List<?> fixed, double xum, double yum) {
        if (!Double.isFinite(xum) || !Double.isFinite(yum) || xum <= 0d || yum <= 0d) {
            throw new IllegalArgumentException("The fixed image needs positive X/Y calibration.");
        }
        if (moving.size() != fixed.size() || moving.size() < 6 || moving.size() > 256) {
            throw new IllegalArgumentException("Add six to 256 distributed local landmark pairs.");
        }
        int n = moving.size();
        double[][] aligned = new double[n][2], target = new double[n][2];
        for (int i = 0; i < n; i++) {
            double[] p = coordinate(moving.get(i));
            double[] q = new double[3];
            global.apply(new double[]{p[0], p[1], 0d}, q);
            aligned[i][0] = q[0] * xum;
            aligned[i][1] = q[1] * yum;
            double[] destination = coordinate(fixed.get(i));
            target[i][0] = destination[0] * xum;
            target[i][1] = destination[1] * yum;
        }
        return fitPhysicalLandmarks(aligned, target);
    }

    /** Read the current finite two-coordinate input, without decoding stage records again. */
    private static double[] coordinate(Object value) {
        if (!(value instanceof List<?> pair) || pair.size() != 2
                || !(pair.get(0) instanceof Number x) || !(pair.get(1) instanceof Number y)
                || !Double.isFinite(x.doubleValue()) || !Double.isFinite(y.doubleValue())) {
            throw new IllegalArgumentException("Landmarks must contain finite image coordinates.");
        }
        return new double[]{x.doubleValue(), y.doubleValue()};
    }

    /**
     * Check the centred affine design before constructing the existing TPS.
     * QR tolerance, determinant cutoff and inverse optimizer settings retain
     * the original physical-coordinate conventions and operation order.
     */
    private static LandmarkFit fitPhysicalLandmarks(double[][] moving, double[][] fixed) {
        int n = moving.length;
        // The global transform and physical conversion can produce a nonfinite
        // position even when the selected input coordinates themselves are finite.
        for (int i = 0; i < n; i++) {
            if (!Double.isFinite(moving[i][0]) || !Double.isFinite(moving[i][1])
                    || !Double.isFinite(fixed[i][0]) || !Double.isFinite(fixed[i][1])) {
                throw new IllegalArgumentException("Landmarks must contain finite image coordinates.");
            }
        }
        double sumX = 0d, sumY = 0d;
        for (double[] pair : moving) {
            sumX += pair[0];
            sumY += pair[1];
        }
        double mx = sumX / n, my = sumY / n;
        double squaredRadius = 0d;
        for (double[] pair : moving) {
            squaredRadius += Math.pow(pair[0] - mx, 2) + Math.pow(pair[1] - my, 2);
        }
        double scale = Math.max(1d, Math.sqrt(squaredRadius / n));
        double[][] design = new double[n][3];
        for (int i = 0; i < n; i++) {
            design[i][0] = (moving[i][0] - mx) / scale;
            design[i][1] = (moving[i][1] - my) / scale;
            design[i][2] = 1d;
        }
        var qr = new QRDecomposition(new Array2DRowRealMatrix(design), 1e-10d).getSolver();
        if (!qr.isNonSingular()) {
            throw new IllegalArgumentException("Place landmarks across the tissue rather than along one line.");
        }
        var weights = qr.solve(new Array2DRowRealMatrix(fixed));
        double determinant = (weights.getEntry(0, 0) * weights.getEntry(1, 1)
                - weights.getEntry(1, 0) * weights.getEntry(0, 1)) / (scale * scale);
        if (!Double.isFinite(determinant) || Math.abs(determinant) < 1e-10d) {
            throw new IllegalArgumentException("The landmarks produce a collapsed transformation.");
        }

        double[][] a = new double[2][n], b = new double[2][n];
        for (int i = 0; i < n; i++) {
            a[0][i] = moving[i][0];
            a[1][i] = moving[i][1];
            b[0][i] = fixed[i][0];
            b[1][i] = fixed[i][1];
        }
        var transform = new WrappedIterativeInvertibleRealTransform<>(new ThinplateSplineTransform(a, b));
        transform.getOptimzer().setTolerance(1e-6d);
        transform.getOptimzer().setMaxIters(1000);
        double squared = 0d;
        for (int i = 0; i < n; i++) {
            double[] output = new double[2];
            transform.apply(moving[i], output);
            squared += Math.pow(output[0] - fixed[i][0], 2)
                    + Math.pow(output[1] - fixed[i][1], 2);
        }
        return new LandmarkFit(transform, n, Math.sqrt(squared / n));
    }

    /** Compose the global pixel affine and caller-wrapped local physical map in saved order. */
    public static InvertibleRealTransformSequence composeLandmarks(AffineTransform3D global,
            InvertibleRealTransform localPhysical3D, double xum, double yum) {
        var physical = new AffineTransform3D();
        physical.set(xum, 0d, 0d, 0d, 0d, yum, 0d, 0d, 0d, 0d, 1d, 0d);
        var result = new InvertibleRealTransformSequence();
        result.add(global.copy());
        result.add(physical);
        result.add(localPhysical3D);
        result.add(physical.inverse());
        return result;
    }

    /** Evaluate a planar forward or inverse coordinate with Z initialized to zero. */
    public static double[] point(InvertibleRealTransform transform, double x, double y,
                                 boolean inverse) {
        double[] result = new double[3], source = {x, y, 0d};
        if (inverse) {
            transform.applyInverse(result, source);
        } else {
            transform.apply(source, result);
        }
        return result;
    }

    /**
     * Sample the same row-major 17×17 fixed-image grid, restricted by tissue
     * covers when present. Relative Jacobians use one-pixel finite differences;
     * added shift uses fixed physical axes and inverse error uses moving pixels.
     * The returned five fields retain the stage's current thresholds and format.
     */
    public static Map<String, Object> checkDeformation(InvertibleRealTransform transform,
            InvertibleRealTransform global, int width, int height, double xum, double yum,
            Geometry tissue, double maximumDisplacementUm) {
        double minimumJacobian = Double.POSITIVE_INFINITY, maximumJacobian = 0d;
        double inverseError = 0d, maximumDisplacement = 0d;
        int count = 0;
        var factory = new GeometryFactory();
        for (int row = 0; row < 17; row++) {
            for (int column = 0; column < 17; column++) {
                double x = (width - 1) * column / 16d, y = (height - 1) * row / 16d;
                if (tissue != null && !tissue.covers(factory.createPoint(new Coordinate(x, y)))) {
                    continue;
                }
                double[] m = point(global, x, y, true);
                double[] q = point(transform, m[0], m[1], false);
                double[] back = point(transform, q[0], q[1], true);
                inverseError = Math.max(inverseError, Math.hypot(back[0] - m[0], back[1] - m[1]));
                maximumDisplacement = Math.max(maximumDisplacement,
                        Math.hypot((q[0] - x) * xum, (q[1] - y) * yum));
                double[] mx = point(global, x + 1d, y, true);
                double[] my = point(global, x, y + 1d, true);
                double[] qx = point(transform, mx[0], mx[1], false);
                double[] qy = point(transform, my[0], my[1], false);
                double jacobian = (qx[0] - q[0]) * (qy[1] - q[1])
                        - (qx[1] - q[1]) * (qy[0] - q[0]);
                minimumJacobian = Math.min(minimumJacobian, jacobian);
                maximumJacobian = Math.max(maximumJacobian, jacobian);
                count++;
            }
        }
        if (count < 6 || !Double.isFinite(minimumJacobian) || !Double.isFinite(maximumJacobian)
                || !Double.isFinite(inverseError) || !Double.isFinite(maximumDisplacement)
                || minimumJacobian <= 0.05d || maximumJacobian > 20d || inverseError > 0.5d
                || maximumDisplacement > maximumDisplacementUm) {
            throw new IllegalArgumentException("The local transformation exceeds its displacement or area-change limits, or cannot be inverted accurately. Review the local settings or landmark pairs.");
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("sampleCount", count);
        result.put("minimumJacobian", minimumJacobian);
        result.put("maximumJacobian", maximumJacobian);
        result.put("maximumInverseErrorPixels", inverseError);
        result.put("maximumDisplacementUm", maximumDisplacement);
        return result;
    }
}
