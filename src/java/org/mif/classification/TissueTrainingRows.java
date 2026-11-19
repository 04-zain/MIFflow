package org.mif.classification;

import java.util.Arrays;
import org.bytedeco.javacpp.indexer.FloatIndexer;
import org.bytedeco.javacpp.indexer.Indexer;
import org.bytedeco.javacpp.indexer.IntIndexer;
import org.bytedeco.opencv.opencv_core.Mat;
import static org.bytedeco.opencv.global.opencv_core.CV_32F;
import static org.bytedeco.opencv.global.opencv_core.CV_32S;

/** Prepare labelled tissue pixels without retaining an extra full SVM matrix. */
public final class TissueTrainingRows {
    /** Own the copied rows; retain their feature transformation with the classifier. */
    public record Prepared(Mat samples, Mat targets, double[] centres, double[] scales)
            implements AutoCloseable {
        @Override public void close() {
            try {
                targets.close();
            } finally {
                samples.close();
            }
        }
    }

    private TissueTrainingRows() {}

    /**
     * Borrow QuPath's extracted rows until copying finishes. RF uses every row;
     * SVM uses equal class counts selected at fixed positions within each class.
     * We calculate SVM means and population deviations from that subset, then
     * transform its float32 values in place. The caller can release QuPath's
     * extraction matrices as soon as this method returns.
     */
    public static Prepared prepare(Mat source, Mat labels, boolean svm,
            int maximum, int maximumPerClass) {
        if (source.rows() < 4 || source.rows() > maximum) {
            throw new IllegalArgumentException("Review the number of labelled training pixels.");
        }
        int[] counts = new int[2];
        byte[] categories = svm ? new byte[labels.rows()] : null;
        try (Indexer values = labels.createIndexer()) {
            long[] position = new long[2];
            for (int row = 0; row < labels.rows(); row++) {
                position[0] = row;
                int category = (int) values.getDouble(position);
                if (category != 0 && category != 1) {
                    throw new IllegalArgumentException("Training regions must use Tissue or Background labels.");
                }
                if (svm) categories[row] = (byte) category;
                counts[category]++;
            }
        }
        if (counts[0] < 2 || counts[1] < 2) {
            throw new IllegalArgumentException("Each class needs at least two labelled pixels.");
        }

        Mat samples = null, targets = null;
        try {
            if (svm) {
                int perClass = Math.min(maximumPerClass, Math.min(counts[0], counts[1]));
                int[][] grouped = {new int[counts[0]], new int[counts[1]]};
                int[] positions = new int[2];
                for (int row = 0; row < categories.length; row++) {
                    int category = categories[row];
                    grouped[category][positions[category]++] = row;
                }
                samples = new Mat(perClass * 2, source.cols(), CV_32F);
                targets = new Mat(perClass * 2, 1, CV_32S);
                try (FloatIndexer from = source.createIndexer();
                        FloatIndexer to = samples.createIndexer();
                        IntIndexer toTargets = targets.createIndexer()) {
                    for (int category = 0; category < 2; category++) {
                        for (int i = 0; i < perClass; i++) {
                            int row = grouped[category][(int) Math.floor(i * counts[category] / (double) perClass)];
                            int selected = category * perClass + i;
                            for (int column = 0; column < source.cols(); column++) {
                                to.put(selected, column, from.get(row, column));
                            }
                            toTargets.put(selected, 0, category);
                        }
                    }
                }
            } else {
                samples = source.clone();
                targets = labels.clone();
            }
            double[] centres = new double[samples.cols()];
            double[] scales = new double[samples.cols()];
            Arrays.fill(scales, 1d);
            if (svm) {
                try (FloatIndexer values = samples.createIndexer()) {
                    for (int column = 0; column < samples.cols(); column++) {
                        double mean = 0d, m2 = 0d;
                        for (int row = 0; row < samples.rows(); row++) {
                            double value = values.get(row, column);
                            if (!Double.isFinite(value)) {
                                throw new IllegalArgumentException("Training features contain a nonfinite value.");
                            }
                            double delta = value - mean;
                            mean += delta / (row + 1d);
                            m2 += delta * (value - mean);
                        }
                        double deviation = Math.sqrt(Math.max(0d, m2 / samples.rows()));
                        centres[column] = mean;
                        scales[column] = deviation > 1e-12d ? 1d / deviation : 1d;
                        for (int row = 0; row < samples.rows(); row++) {
                            values.put(row, column, (float) ((values.get(row, column) - mean) * scales[column]));
                        }
                    }
                }
            }
            return new Prepared(samples, targets, centres, scales);
        } catch (RuntimeException | Error failure) {
            if (targets != null) targets.close();
            if (samples != null) samples.close();
            throw failure;
        }
    }
}
