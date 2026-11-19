package org.mif.inference;

import java.awt.image.BufferedImage;
import java.awt.image.Raster;
import java.io.IOException;
import java.io.InterruptedIOException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.function.BooleanSupplier;
import org.locationtech.jts.algorithm.locate.IndexedPointInAreaLocator;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Envelope;
import org.locationtech.jts.geom.prep.PreparedGeometryFactory;
import org.locationtech.jts.geom.Location;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.regions.ImagePlane;
import qupath.lib.regions.RegionRequest;

/** Native tissue-pixel sample moments for the current fit's authenticated scope.
 * The caller owns subject selection, image identity and the accepted geometry.
 * No crop padding, phenotype selection, normalization defaults or model loading.
 */
public final class KronosMarkerStatistics {
    public static final String METHOD = "kronos-native-tissue-sample-moments/v1";
    public static final long DEFAULT_BLOCK_BYTES = 16L * 1024 * 1024;
    private KronosMarkerStatistics() {}

    /** Immutable mergeable float64 sufficient statistics; zero-count is identity. */
    public static final class Moments {
        private final long count;
        private final double mean;
        private final double m2;
        public Moments(long count, double mean, double m2) {
            if (count < 0 || !Double.isFinite(mean) || !Double.isFinite(m2) || m2 < 0
                    || (count == 0 && (mean != 0 || m2 != 0)) || (count == 1 && m2 != 0))
                throw new IllegalArgumentException("Invalid native marker count/mean/M2 partial");
            this.count = count;
            this.mean = mean;
            this.m2 = m2;
        }
        public long getCount() { return count; }
        public double getMean() { return mean; }
        public double getM2() { return m2; }
        public Moments merge(Moments other) {
            if (count == 0) return other;
            if (other.count == 0) return this;
            long n = Math.addExact(count, other.count);
            double delta = other.mean - mean;
            double fraction = (double) other.count / n;
            return new Moments(n, mean + delta * fraction,
                    m2 + other.m2 + delta * delta * ((double) count * fraction));
        }
        /** CORAL's sample SD (ddof=1), checked at native float32 export precision. */
        public double sampleStandardDeviation() {
            if (count < 2)
                throw new IllegalArgumentException("Marker statistics require at least two accepted tissue pixels");
            double sd = Math.sqrt(m2 / (count - 1));
            if (!Double.isFinite(sd) || sd <= 0 || !Float.isFinite((float) sd)
                    || (float) sd <= 0 || !Float.isFinite((float) mean))
                throw new IllegalArgumentException("Marker mean/SD must remain finite with positive SD at float32 export precision");
            return sd;
        }
    }

    public static List<Moments> scan(ImageServer<BufferedImage> server, Geometry geometry,
            ImagePlane plane, int[] channels, double[] divisors) throws IOException {
        return scan(server, geometry, plane, channels, divisors, DEFAULT_BLOCK_BYTES, () -> false);
    }

    /** Read bounded native blocks, counting every pixel center in the union once.
     * The budget accounts for every source band at eight bytes per sample, not
     * merely the requested novel channels. It bounds requested raster payload;
     * an ImageServer's decoder/cache overhead remains the server's responsibility.
     */
    public static List<Moments> scan(ImageServer<BufferedImage> server, Geometry geometry,
            ImagePlane plane, int[] channels, double[] divisors,
            long maxBlockBytes, BooleanSupplier cancelled) throws IOException {
        if (server == null || geometry == null || plane == null || cancelled == null
                || channels == null || divisors == null || channels.length != divisors.length)
            throw new IllegalArgumentException("Native statistics require image, authenticated area/plane and matching channels/divisors");
        if (geometry.isEmpty() || geometry.getDimension() != 2 || !geometry.isValid())
            throw new IllegalArgumentException("Native statistics require a nonempty valid accepted tissue area");
        if (plane.getZ() < 0 || plane.getZ() >= server.nZSlices()
                || plane.getT() < 0 || plane.getT() >= server.nTimepoints())
            throw new IllegalArgumentException("Accepted statistics plane is outside the image");
        var distinct = new HashSet<Integer>();
        for (int i = 0; i < channels.length; i++) {
            if (channels[i] < 0 || channels[i] >= server.nChannels() || !distinct.add(channels[i])
                    || !Double.isFinite(divisors[i]) || divisors[i] <= 0)
                throw new IllegalArgumentException("Native statistics require distinct valid channels and finite positive saved divisors");
        }
        if (channels.length == 0) return List.of();
        // RGB raster storage can have an alpha band beyond the exposed channels.
        int bands = Math.max(4, server.nChannels());
        long bytesPerPixel = Math.multiplyExact(8L, bands);
        if (maxBlockBytes < bytesPerPixel || maxBlockBytes > 256L * 1024 * 1024)
            throw new IllegalArgumentException("Native statistics block budget must fit source bands and not exceed 256 MiB");
        int side = (int) Math.min(512, Math.floor(Math.sqrt(maxBlockBytes / bytesPerPixel)));
        var envelope = geometry.getEnvelopeInternal();
        if (!Double.isFinite(envelope.getMinX()) || !Double.isFinite(envelope.getMinY())
                || !Double.isFinite(envelope.getMaxX()) || !Double.isFinite(envelope.getMaxY()))
            throw new IllegalArgumentException("Accepted statistics geometry must have finite image coordinates");
        int minX = (int) Math.max(0, Math.min(server.getWidth(), Math.floor(envelope.getMinX())));
        int minY = (int) Math.max(0, Math.min(server.getHeight(), Math.floor(envelope.getMinY())));
        int maxX = (int) Math.max(0, Math.min(server.getWidth(), Math.ceil(envelope.getMaxX())));
        int maxY = (int) Math.max(0, Math.min(server.getHeight(), Math.ceil(envelope.getMaxY())));
        var locator = new IndexedPointInAreaLocator(geometry);
        var preparedArea = PreparedGeometryFactory.prepare(geometry);
        var point = new Coordinate();
        var totals = new ArrayList<Moments>(channels.length);
        for (int ignored : channels) totals.add(new Moments(0, 0, 0));
        for (int y = minY; y < maxY;) {
            int height = Math.min(side, maxY - y);
            for (int x = minX; x < maxX;) {
                checkCancelled(cancelled);
                int width = Math.min(side, maxX - x);
                if (!preparedArea.intersects(geometry.getFactory().toGeometry(
                        new Envelope(x, (double) x + width, y, (double) y + height)))) {
                    x += width;
                    continue;
                }
                var request = RegionRequest.createInstance(server.getPath(), 1.0, x, y, width, height, plane);
                BufferedImage block = server.readRegion(request);
                if (block == null || block.getWidth() != width || block.getHeight() != height)
                    throw new IOException("Native marker statistics reader returned an incomplete block at " + x + "," + y);
                Raster raster = block.getRaster();
                if (raster.getNumBands() > bands || raster.getNumBands() < server.nChannels())
                    throw new IOException("Native marker statistics raster bands differ from the declared source budget");
                long n = 0;
                double[] means = new double[channels.length], m2 = new double[channels.length];
                for (int dy = 0; dy < height; dy++) {
                    checkCancelled(cancelled);
                    point.y = y + dy + 0.5;
                    for (int dx = 0; dx < width; dx++) {
                        point.x = x + dx + 0.5;
                        if (locator.locate(point) == Location.EXTERIOR) continue;
                        n++;
                        for (int c = 0; c < channels.length; c++) {
                            double value = raster.getSampleDouble(dx + raster.getMinX(), dy + raster.getMinY(), channels[c]) / divisors[c];
                            if (!Double.isFinite(value))
                                throw new IOException("Nonfinite marker intensity in channel " + channels[c]
                                        + " at native pixel " + (x + dx) + "," + (y + dy));
                            double delta = value - means[c];
                            means[c] += delta / n;
                            m2[c] += delta * (value - means[c]);
                        }
                    }
                }
                for (int c = 0; c < channels.length; c++)
                    totals.set(c, totals.get(c).merge(new Moments(n, means[c], m2[c])));
                x += width;
            }
            y += height;
        }
        checkCancelled(cancelled);
        return List.copyOf(totals);
    }

    private static void checkCancelled(BooleanSupplier cancelled) throws InterruptedIOException {
        if (Thread.currentThread().isInterrupted() || cancelled.getAsBoolean())
            throw new InterruptedIOException("Native marker statistics cancelled");
    }
}
