package org.mif.image;

import java.awt.image.Raster;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.List;
import org.locationtech.jts.geom.Coordinate;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.GeometryFactory;
import org.locationtech.jts.geom.prep.PreparedGeometryFactory;
import qupath.lib.roi.interfaces.ROI;

/** Numerical pixel and mask loops over already admitted cell/image rectangles. */
public final class CellImageSamples {
    private CellImageSamples() {}

    /** Raw pixels and the separate cell mask retain the encoder's existing inputs. */
    public record KronosPatch(float[] pixels, byte[] mask, int selectedCount) {}

    /** Consume an admitted channel-major crop after its unmasked cache identity
     * has been recorded. Pixels outside the cell become zero before the frozen
     * encoder applies normalization; nonzero mask entries retain their values.
     */
    public static void applyKronosMask(float[] pixels,byte[] mask,int channels) {
        for(int pixel=0;pixel<mask.length;pixel++)if(mask[pixel]==0)
            for(int channel=0;channel<channels;channel++)pixels[channel*mask.length+pixel]=0f;
    }

    /**
     * Sample the recorded physical pixel centres using nearest full-resolution
     * pixels. Borrow the exact rectangle raster and cell ROI. Outside-image
     * values remain zero; off-cell raw values remain for native encoder masking.
     * KRONOS keeps ROI.contains, which differs from Nimbus's covers rule.
     */
    public static KronosPatch sampleKronos(Raster pixels, int rectangleX, int rectangleY,
                                          int imageWidth, int imageHeight, ROI cellRoi,
                                          List<Integer> channels, double x0, double y0,
                                          double dx, double dy, int patchSize) {
        if (patchSize != 32 && patchSize != 64 && patchSize != 128 && patchSize != 256) {
            throw new IllegalArgumentException("KRONOS image size must be 32, 64, 128 or 256");
        }
        int patchPixels = patchSize * patchSize;
        float[] values = new float[channels.size() * patchPixels];
        byte[] mask = new byte[patchPixels];
        int selected = 0;
        double cellLeft = cellRoi.getBoundsX(), cellTop = cellRoi.getBoundsY();
        double cellRight = cellLeft + cellRoi.getBoundsWidth();
        double cellBottom = cellTop + cellRoi.getBoundsHeight();
        for (int y = 0; y < patchSize; y++) {
            double worldY = y0 + (y + 0.5) * dy;
            int py = (int) Math.floor(worldY);
            for (int x = 0; x < patchSize; x++) {
                double worldX = x0 + (x + 0.5) * dx;
                int px = (int) Math.floor(worldX);
                int offset = y * patchSize + x;
                boolean inside = px >= 0 && py >= 0 && px < imageWidth && py < imageHeight;
                if (!inside) {
                    continue;
                }
                // Keep the native predicate (including its boundary semantics),
                // but avoid walking an outline for samples outside its bounds.
                if (worldX >= cellLeft && worldX <= cellRight
                        && worldY >= cellTop && worldY <= cellBottom
                        && cellRoi.contains(worldX, worldY)) {
                    mask[offset] = 1;
                    selected++;
                }
                for (int channel = 0; channel < channels.size(); channel++) {
                    double value = pixels.getSampleDouble(px - rectangleX, py - rectangleY,
                            channels.get(channel));
                    if (!Double.isFinite(value) || Math.abs(value) > Float.MAX_VALUE) {
                        throw new IllegalArgumentException(
                                "The processed image contains nonfinite or unrepresentable KRONOS input");
                    }
                    values[channel * patchPixels + offset] = (float) value;
                }
            }
        }
        return new KronosPatch(values, mask, selected);
    }

    /** Exact source rectangle: legacy sample centres, or only the coarsened
     * cell's native bounds. The caller still owns whole-cell field validation.
     */
    public static int[] kronosReadBounds(ROI cellRoi, int imageWidth, int imageHeight,
                                         double x0, double y0, double dx, double dy,
                                         int patchSize, boolean areaPolicy) {
        if (areaPolicy && (dx > 1.0 || dy > 1.0)) {
            return new int[]{Math.max(0, (int)Math.floor(cellRoi.getBoundsX())),
                    Math.max(0, (int)Math.floor(cellRoi.getBoundsY())),
                    Math.min(imageWidth, (int)Math.ceil(cellRoi.getBoundsX()+cellRoi.getBoundsWidth())),
                    Math.min(imageHeight, (int)Math.ceil(cellRoi.getBoundsY()+cellRoi.getBoundsHeight()))};
        }
        return new int[]{Math.max(0, (int)Math.floor(x0+0.5*dx)),
                Math.max(0, (int)Math.floor(y0+0.5*dy)),
                Math.min(imageWidth, (int)Math.floor(x0+(patchSize-0.5)*dx)+1),
                Math.min(imageHeight, (int)Math.floor(y0+(patchSize-0.5)*dy)+1)};
    }

    /** V2: average source-cell intensities over each coarser physical footprint.
     * Native pixels are masked at their centres before integration. The original
     * target-centre predicate is retained, including its image-edge behavior.
     * Zero padding participates in the full footprint denominator; intensities
     * are never renormalized by the surviving cell area. When both axes retain
     * their native pitch, the v1 samples and validation remain bit-exact.
     */
    public static KronosPatch sampleKronosArea(Raster pixels, int rectangleX, int rectangleY,
            int imageWidth, int imageHeight, ROI cellRoi, List<Integer> channels,
            double x0, double y0, double dx, double dy, int patchSize) {
        if ((patchSize != 32 && patchSize != 64 && patchSize != 128 && patchSize != 256)
                || channels == null || channels.isEmpty() || channels.size() > 32
                || !Double.isFinite(x0) || !Double.isFinite(y0)
                || !Double.isFinite(dx) || !Double.isFinite(dy) || dx < 1 || dy < 1
                || !Double.isFinite(dx*dy)) {
            throw new IllegalArgumentException("Area sampling requires a bounded panel and finite native-or-coarser pixel sizes");
        }
        if (dx == 1.0 && dy == 1.0) {
            return sampleKronos(pixels, rectangleX, rectangleY, imageWidth, imageHeight,
                    cellRoi, channels, x0, y0, dx, dy, patchSize);
        }
        int patchPixels = patchSize*patchSize;
        long elements = (long)channels.size()*patchPixels;
        // One temporary double accumulator, one returned float tensor and mask.
        // The temporary is released before another cell is sampled in a batch.
        if (elements*(Double.BYTES+Float.BYTES)+patchPixels > 64L*1024*1024) {
            throw new IllegalArgumentException("The area-sampling accumulator exceeds its 64 MiB allowance");
        }
        int[] bounds = kronosReadBounds(cellRoi,imageWidth,imageHeight,x0,y0,dx,dy,patchSize,true);
        if (bounds[0] < rectangleX || bounds[1] < rectangleY
                || bounds[2] > (long)rectangleX+pixels.getWidth()
                || bounds[3] > (long)rectangleY+pixels.getHeight()) {
            throw new IllegalArgumentException("The native source rectangle does not cover the cell bounds");
        }
        float[] values = new float[(int)elements];
        double[] accumulated = new double[(int)elements];
        byte[] mask = new byte[patchPixels];
        double left=cellRoi.getBoundsX(), top=cellRoi.getBoundsY();
        double right=left+cellRoi.getBoundsWidth(), bottom=top+cellRoi.getBoundsHeight();
        int selected=0;
        for (int y=0;y<patchSize;y++) {
            double cy=y0+(y+0.5)*dy;
            if (cy < 0 || cy >= imageHeight || cy < top || cy > bottom) continue;
            for (int x=0;x<patchSize;x++) {
                double cx=x0+(x+0.5)*dx;
                if (cx >= 0 && cx < imageWidth && cx >= left && cx <= right && cellRoi.contains(cx,cy)) {
                    mask[y*patchSize+x]=1; selected++;
                }
            }
        }
        int[] target = new int[4];
        double[] weights = new double[4];
        for (int py=bounds[1];py<bounds[3];py++) {
            for (int px=bounds[0];px<bounds[2];px++) {
                if (!cellRoi.contains(px+0.5,py+0.5)) continue;
                int firstX=Math.max(0,(int)Math.floor((px-x0)/dx));
                int lastX=Math.min(patchSize-1,(int)Math.floor((px+1.0-x0)/dx));
                int firstY=Math.max(0,(int)Math.floor((py-y0)/dy));
                int lastY=Math.min(patchSize-1,(int)Math.floor((py+1.0-y0)/dy));
                int count=0;
                // dx,dy >= 1: a native pixel intersects at most four targets.
                for (int y=firstY;y<=lastY;y++) for (int x=firstX;x<=lastX;x++) {
                    int at=y*patchSize+x;
                    if (mask[at]==0) continue;
                    double overlapX=Math.min(px+1.0,x0+(x+1)*dx)-Math.max(px,x0+x*dx);
                    double overlapY=Math.min(py+1.0,y0+(y+1)*dy)-Math.max(py,y0+y*dy);
                    if (overlapX > 0 && overlapY > 0) {
                        target[count]=at; weights[count]=overlapX*overlapY; count++;
                    }
                }
                for (int channel=0;channel<channels.size();channel++) {
                    double value=pixels.getSampleDouble(px-rectangleX,py-rectangleY,channels.get(channel));
                    if (!Double.isFinite(value) || Math.abs(value) > Float.MAX_VALUE) {
                        throw new IllegalArgumentException("The cell contains nonfinite or unrepresentable KRONOS input");
                    }
                    for (int index=0;index<count;index++) {
                        accumulated[channel*patchPixels+target[index]] += value*weights[index];
                    }
                }
            }
        }
        double footprint=dx*dy;
        for (int index=0;index<values.length;index++) {
            values[index]=(float)(accumulated[index]/footprint);
            if (!Float.isFinite(values[index])) throw new IllegalArgumentException("Area-averaged KRONOS input is not representable");
        }
        return new KronosPatch(values,mask,selected);
    }

    /** Serialize the selected Nimbus channels in the existing channel-major Float32 order. */
    public static ByteBuffer nimbusPixels(Raster pixels, int width, int height,
                                          List<Integer> channels) {
        int bytes = Math.multiplyExact(Math.multiplyExact(width, height),
                Math.multiplyExact(Float.BYTES, channels.size()));
        ByteBuffer result = ByteBuffer.allocate(bytes).order(ByteOrder.LITTLE_ENDIAN);
        for (int channel : channels) {
            for (int y = 0; y < height; y++) {
                for (int x = 0; x < width; x++) {
                    result.putFloat(pixels.getSampleFloat(x, y, channel));
                }
            }
        }
        return result;
    }

    /**
     * Paint one already accepted Nimbus identity into the caller's bounded mask.
     * Keep prepared-geometry covers at native pixel centres, including holes and
     * boundaries. Repeated coverage by the same identity is allowed; another
     * identity at that pixel retains the existing overlap error.
     */
    public static void paintNimbusCell(int[] labels, int x, int y, int width, int height,
                                       Geometry cell, int label, GeometryFactory points) {
        var geometry = PreparedGeometryFactory.prepare(cell);
        var envelope = cell.getEnvelopeInternal();
        int x0 = Math.max(x, (int) Math.floor(envelope.getMinX()));
        int x1 = Math.min(x + width, (int) Math.ceil(envelope.getMaxX()));
        int y0 = Math.max(y, (int) Math.floor(envelope.getMinY()));
        int y1 = Math.min(y + height, (int) Math.ceil(envelope.getMaxY()));
        for (int yy = y0; yy < y1; yy++) {
            for (int xx = x0; xx < x1; xx++) {
                if (geometry.covers(points.createPoint(new Coordinate(xx + 0.5, yy + 0.5)))) {
                    int at = (yy - y) * width + xx - x;
                    if (labels[at] != 0 && labels[at] != label) {
                        throw new IllegalStateException("Finalized cell masks overlap at a native pixel");
                    }
                    labels[at] = label;
                }
            }
        }
    }

    /** Serialize the caller-owned mask without allocating a second label plane. */
    public static ByteBuffer nimbusLabels(int[] labels) {
        ByteBuffer result = ByteBuffer.allocate(labels.length * Integer.BYTES)
                .order(ByteOrder.LITTLE_ENDIAN);
        for (int label : labels) {
            result.putInt(label);
        }
        return result;
    }
}
