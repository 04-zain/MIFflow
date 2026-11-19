package org.mif.image;

import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;
import org.locationtech.jts.awt.ShapeWriter;
import org.locationtech.jts.geom.Geometry;

/** Pixel calculations over an already admitted, reduced registration image. */
public final class RegistrationImageViews {
    private RegistrationImageViews() {}

    /**
     * Extract and normalize the selected contrast without changing its Float32
     * rounding. RGB uses optical-density contrast; other combined views average
     * channels in their source order. A cloned Float32 sort supplies the original
     * truncated 0.5% and 99.5% percentile positions.
     */
    public static float[] intensities(BufferedImage pixels, boolean rgb, int channel) {
        int width = pixels.getWidth();
        int height = pixels.getHeight();
        int channels = pixels.getRaster().getNumBands();
        float[] values = new float[width * height];
        if (rgb && channel == -1) {
            for (int y = 0; y < height; y++) {
                for (int x = 0; x < width; x++) {
                    int packed = pixels.getRGB(x, y);
                    double light = (((packed >> 16) & 255)
                            + ((packed >> 8) & 255) + (packed & 255)) / 3d;
                    values[y * width + x] = (float) -Math.log(Math.max(1d, light) / 255d);
                }
            }
        } else if (channel >= 0) {
            pixels.getRaster().getSamples(0, 0, width, height, channel, values);
        } else {
            float[] one = new float[values.length];
            for (int c = 0; c < channels; c++) {
                pixels.getRaster().getSamples(0, 0, width, height, c, one);
                for (int i = 0; i < values.length; i++) {
                    // Groovy divides and adds in double, then rounds on each
                    // assignment to the Float32 array, rather than once at the end.
                    values[i] = (float) (values[i] + one[i] / (double) channels);
                }
            }
        }

        for (float value : values) {
            if (!Float.isFinite(value)) {
                throw new IllegalArgumentException(
                        "The alignment image contains nonfinite intensities.");
            }
        }
        float[] ordered = values.clone();
        Arrays.sort(ordered);
        double low = ordered[(int) (0.005d * (ordered.length - 1))];
        double high = ordered[(int) (0.995d * (ordered.length - 1))];
        if (!(high > low)) {
            throw new IllegalArgumentException("The selected image channel has insufficient contrast "
                    + "for automatic alignment. Choose another channel or landmark alignment.");
        }
        for (int i = 0; i < values.length; i++) {
            values[i] = (float) Math.max(0d, Math.min(1d, (values[i] - low) / (high - low)));
        }
        return values;
    }

    /**
     * Fill the selected tissue with the existing Java2D shape policy. The scaled
     * byte-gray samples become binary mask bytes; this raster policy is separate
     * from cell-centre containment used by the other image calculations.
     */
    public static byte[] tissueMask(Geometry geometry, int width, int height, double downsample) {
        BufferedImage image = new BufferedImage(width, height, BufferedImage.TYPE_BYTE_GRAY);
        Graphics2D graphics = image.createGraphics();
        try {
            graphics.setColor(Color.WHITE);
            graphics.scale(1d / downsample, 1d / downsample);
            graphics.fill(new ShapeWriter().toShape(geometry));
        } finally {
            graphics.dispose();
        }

        byte[] mask = new byte[width * height];
        int supported = 0;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                int value = image.getRaster().getSample(x, y, 0) > 0 ? 1 : 0;
                mask[y * width + x] = (byte) value;
                supported += value;
            }
        }
        if (supported < 256) {
            throw new IllegalArgumentException(
                    "The accepted tissue regions are too small at this alignment resolution.");
        }
        return mask;
    }

    /** Write Float32 rows using one little-endian row buffer; the caller closes the stream. */
    public static void writeFloatRows(OutputStream stream, float[] values, int width, int height)
            throws IOException {
        ByteBuffer row = ByteBuffer.allocate(Float.BYTES * width).order(ByteOrder.LITTLE_ENDIAN);
        for (int y = 0; y < height; y++) {
            row.clear();
            for (int x = 0; x < width; x++) {
                row.putFloat(values[y * width + x]);
            }
            stream.write(row.array());
        }
    }
}
