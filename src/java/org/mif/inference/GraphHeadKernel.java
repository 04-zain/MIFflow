package org.mif.inference;

import java.io.IOException;
import java.util.LinkedHashMap;
import org.ejml.data.DMatrixRMaj;
import org.ejml.dense.row.CommonOps_DDRM;

/** Immutable learned mean-self matrices with bounded native matrix batches. */
public final class GraphHeadKernel {
    public static final int MAX_BATCH_ROWS = 64;
    private final DMatrixRMaj selfWeight, neighbourWeight, headWeight;
    private final float[] bias, headBias;
    private final int inputWidth, hiddenWidth, classCount;

    public GraphHeadKernel(float[][] own, float[][] neighbour, float[] spatialBias,
                           float[][] head, float[] classBias, int classes) {
        if (own == null || own.length < 1 || own.length > 4096
                || spatialBias == null || spatialBias.length < 1 || spatialBias.length > 768
                || classes < 2 || classes > 254 || classBias == null || classBias.length != classes)
            throw new IllegalArgumentException("Graph/head dimensions disagree");
        inputWidth = own.length;
        hiddenWidth = spatialBias.length;
        classCount = classes;
        selfWeight = matrix(own, inputWidth, hiddenWidth);
        neighbourWeight = matrix(neighbour, inputWidth, hiddenWidth);
        headWeight = matrix(head, hiddenWidth, classes);
        bias = finiteCopy(spatialBias);
        headBias = finiteCopy(classBias);
    }

    private static float[] finiteCopy(float[] values) {
        float[] copy = values.clone();
        for (float value : copy)
            if (!Float.isFinite(value)) throw new IllegalArgumentException("Graph parameters must be finite");
        return copy;
    }

    private static DMatrixRMaj matrix(float[][] values, int rows, int columns) {
        if (values == null || values.length != rows)
            throw new IllegalArgumentException("Graph matrix dimensions disagree");
        DMatrixRMaj matrix = new DMatrixRMaj(rows, columns);
        for (int row = 0; row < rows; row++) {
            if (values[row] == null || values[row].length != columns)
                throw new IllegalArgumentException("Graph matrix dimensions disagree");
            for (int col = 0; col < columns; col++) {
                float value = values[row][col];
                if (!Float.isFinite(value)) throw new IllegalArgumentException("Graph parameters must be finite");
                matrix.set(row, col, value);
            }
        }
        return matrix;
    }

    public int getInputWidth() { return inputWidth; }
    public int getHiddenWidth() { return hiddenWidth; }
    public int getClassCount() { return classCount; }

    /** Preserve the ordered float32 neighbour mean and float32 feature output.
     * Matrix products use double accumulation, matching the retired Groovy
     * scalar accumulator; parameters and encoder vectors retain their precision.
     */
    public float[][] features(float[][] own, float[][][] neighbours) throws IOException {
        int rows = own.length;
        if (rows < 1 || rows > MAX_BATCH_ROWS || neighbours.length != rows)
            throw new IllegalArgumentException("Graph work must contain one to 64 rows");
        DMatrixRMaj self = new DMatrixRMaj(rows, inputWidth);
        DMatrixRMaj means = new DMatrixRMaj(rows, inputWidth);
        for (int row = 0; row < rows; row++) {
            if (own[row].length != inputWidth || neighbours[row].length > 4)
                throw new IllegalArgumentException("Graph context dimensions disagree");
            float[] mean = new float[inputWidth];
            for (float[] adjacent : neighbours[row]) {
                if (adjacent.length != inputWidth)
                    throw new IllegalArgumentException("Graph neighbour width disagrees");
                for (int col = 0; col < inputWidth; col++) mean[col] += adjacent[col];
            }
            for (int col = 0; col < inputWidth; col++) {
                self.set(row, col, own[row][col]);
                means.set(row, col, neighbours[row].length == 0 ? 0f
                    : mean[col] / neighbours[row].length);
            }
        }
        DMatrixRMaj result = new DMatrixRMaj(rows, hiddenWidth);
        CommonOps_DDRM.mult(self, selfWeight, result);
        CommonOps_DDRM.multAdd(means, neighbourWeight, result);
        float[][] output = new float[rows][hiddenWidth];
        for (int row = 0; row < rows; row++) {
            for (int col = 0; col < hiddenWidth; col++) {
                double value = result.get(row, col) + bias[col];
                if (!Double.isFinite(value) || Math.abs(value) > Float.MAX_VALUE)
                    throw new IOException("Spatial feature calculation became nonfinite");
                output[row][col] = (float)Math.max(0.0, value);
            }
        }
        return output;
    }

    /** Apply the learned inspection head once per batch; first class wins ties. */
    public int[] labels(float[][] features) throws IOException {
        int rows = features.length;
        if (rows < 1 || rows > MAX_BATCH_ROWS)
            throw new IllegalArgumentException("Head work must contain one to 64 rows");
        DMatrixRMaj input = matrix(features, rows, hiddenWidth);
        DMatrixRMaj scores = new DMatrixRMaj(rows, classCount);
        CommonOps_DDRM.mult(input, headWeight, scores);
        int[] labels = new int[rows];
        for (int row = 0; row < rows; row++) {
            double best = Double.NEGATIVE_INFINITY;
            for (int col = 0; col < classCount; col++) {
                double score = scores.get(row, col) + headBias[col];
                if (!Double.isFinite(score)) throw new IOException("Spatial preview score is nonfinite");
                if (score > best) { best = score; labels[row] = col; }
            }
        }
        return labels;
    }

    private static void checkRaster(byte[] mask, int width, int height, int patchSize) {
        if (patchSize != 32 && patchSize != 64 && patchSize != 128 && patchSize != 256)
            throw new IllegalArgumentException("KRONOS image size must be 32, 64, 128 or 256");
        if (width < 1 || height < 1 || (long)width * height > 1048576
                || mask.length != (long)width * height)
            throw new IllegalArgumentException("Prediction tile exceeds the bounded raster limit");
    }

    private static long position(int row, int col) {
        return ((long)row << 32) | (col & 0xffffffffL);
    }

    /** Return only patches actually touched by the selected pixel mask. */
    public static int[][] requiredPatches(byte[] mask, int width, int height, int left, int top, int patchSize) {
        checkRaster(mask, width, height, patchSize);
        LinkedHashMap<Long, int[]> positions = new LinkedHashMap<>();
        long previous = Long.MIN_VALUE;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                if ((mask[y * width + x] & 0xff) == 0) continue;
                int row = Math.floorDiv(top + y, patchSize), col = Math.floorDiv(left + x, patchSize);
                long key = position(row, col);
                if (key != previous) {
                    if (!positions.containsKey(key)) positions.put(key, new int[] {row, col});
                    previous = key;
                }
            }
        }
        return positions.values().toArray(int[][]::new);
    }

    /** Fill the exact selected class raster; excluded pixels retain code 255. */
    public static byte[] rasterize(byte[] mask, int width, int height, int left, int top,
                                  int[][] positions, int[] labels, int patchSize) {
        checkRaster(mask, width, height, patchSize);
        if (positions.length != labels.length) throw new IllegalArgumentException("Patch classes disagree");
        LinkedHashMap<Long, Integer> byPosition = new LinkedHashMap<>();
        for (int i = 0; i < positions.length; i++) {
            if (positions[i].length != 2 || labels[i] < 0 || labels[i] > 255)
                throw new IllegalArgumentException("Patch classes disagree");
            byPosition.put(position(positions[i][0], positions[i][1]), labels[i]);
        }
        byte[] output = new byte[mask.length];
        for (int y = 0; y < height; y++) {
            int row = Math.floorDiv(top + y, patchSize);
            int previousCol = Integer.MIN_VALUE, label = 255;
            for (int x = 0; x < width; x++) {
                int index = y * width + x;
                int col = Math.floorDiv(left + x, patchSize);
                if (col != previousCol) {
                    label = byPosition.getOrDefault(position(row, col), 255);
                    previousCol = col;
                }
                output[index] = (byte)((mask[index] & 0xff) == 0 ? 255 :
                    label);
            }
        }
        return output;
    }
}
