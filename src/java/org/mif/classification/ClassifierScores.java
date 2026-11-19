package org.mif.classification;

import java.util.ArrayList;
import java.util.List;
import org.bytedeco.javacpp.indexer.Indexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_ml.LogisticRegression;
import org.bytedeco.opencv.opencv_ml.RTrees;
import org.bytedeco.opencv.opencv_ml.SVM;
import org.bytedeco.opencv.opencv_ml.StatModel;
import qupath.opencv.ml.OpenCVClassifiers;

/** Calculate the existing uncalibrated native scores without changing a cell.
 * Models and samples are borrowed. Temporary matrices and indexers are closed;
 * returned labels, score rows and SVM margins are ordinary caller-owned arrays.
 */
public final class ClassifierScores {
    private ClassifierScores() {}

    public record Prediction(int[] labels, double[][] scores, double[][] margins, int[][] pairs) {}

    /** Keep RF vote fractions, SVM pairwise agreement and LR normalized sigmoids. */
    public static Prediction predict(String method, StatModel model, Mat samples, int classes) {
        if ("logistic_regression".equals(method)) return logistic(model, samples, classes);
        if ("svm".equals(method)) return svm(model, samples, classes);
        if (!"rtrees".equals(method) || !(model instanceof RTrees))
            throw new IllegalStateException("Unsupported native cell head");
        try (Mat labels = new Mat(); Mat probabilities = new Mat()) {
            OpenCVClassifiers.wrapStatModel(model).predict(samples, labels, probabilities);
            if (probabilities.rows() != samples.rows() || probabilities.cols() != classes)
                throw new IllegalStateException("The forest did not return complete vote counts");
            boolean floatVotes = probabilities.depth() == opencv_core.CV_32F;
            int[] predicted = new int[samples.rows()];
            double[][] scores = new double[samples.rows()][classes];
            try (Indexer li = labels.createIndexer(); Indexer pi = probabilities.createIndexer()) {
                for (int row = 0; row < samples.rows(); row++) {
                    predicted[row] = (int) li.getDouble(row);
                    for (int col = 0; col < classes; col++)
                        scores[row][col] = explicitDouble(pi.getDouble(row, col), floatVotes);
                }
            }
            return new Prediction(predicted, scores, null, null);
        }
    }

    /** Evaluate saved LR coefficients in their original class order and log space. */
    private static Prediction logistic(StatModel model, Mat samples, int classes) {
        if (!(model instanceof LogisticRegression lr))
            throw new IllegalStateException("The LR resource contains a different native head");
        try (Mat theta = lr.get_learnt_thetas()) {
            if (theta.cols() != samples.cols() + 1 || theta.rows() != (classes == 2 ? 1 : classes))
                throw new IllegalStateException("The saved LR coefficient dimensions do not match its classes");
            boolean floatTheta = theta.depth() == opencv_core.CV_32F;
            boolean floatSamples = samples.depth() == opencv_core.CV_32F;
            int[] labels = new int[samples.rows()];
            double[][] scores = new double[samples.rows()][classes];
            try (Indexer ti = theta.createIndexer(); Indexer si = samples.createIndexer()) {
                // Convert each coefficient once, within a bounded block. Wide
                // heads also split columns; partial sums retain column order.
                int columns = samples.cols();
                int capacity = 32 * 1024 * 1024 / Double.BYTES;
                int blockRows = Math.min(theta.rows(),
                        Math.max(1, capacity / Math.max(1, columns)));
                int blockColumns = Math.min(columns,
                        Math.max(1, capacity / (blockRows + 1)));
                double[] input = new double[blockColumns];
                for (int first = 0; first < theta.rows(); first += blockRows) {
                    int size = Math.min(blockRows, theta.rows() - first);
                    double[][] coefficients = new double[size][blockColumns];
                    for (int row = 0; row < samples.rows(); row++) {
                        for (int k = 0; k < size; k++) {
                            scores[row][first + k] = ti.getDouble(first + k, 0);
                        }
                    }
                    for (int start = 0; start < columns; start += blockColumns) {
                        int count = Math.min(blockColumns, columns - start);
                        for (int k = 0; k < size; k++) {
                            for (int col = 0; col < count; col++) {
                                coefficients[k][col] = explicitDouble(
                                        ti.getDouble(first + k, start + col + 1), floatTheta);
                            }
                        }
                        for (int row = 0; row < samples.rows(); row++) {
                            for (int col = 0; col < count; col++) {
                                input[col] = explicitDouble(si.getDouble(row, start + col), floatSamples);
                            }
                            for (int k = 0; k < size; k++) {
                                double z = scores[row][first + k];
                                for (int col = 0; col < count; col++) {
                                    z += coefficients[k][col] * input[col];
                                }
                                scores[row][first + k] = z;
                            }
                        }
                    }
                    for (int row = 0; row < samples.rows(); row++) {
                        for (int k = 0; k < size; k++) {
                            double z = scores[row][first + k];
                            if (!Double.isFinite(z)) {
                                throw new IllegalStateException("Native LR produced a nonfinite linear response");
                            }
                            scores[row][first + k] = z >= 0 ?
                                    -Math.log1p(Math.exp(-z)) : z - Math.log1p(Math.exp(z));
                        }
                    }
                }
                for (int row = 0; row < samples.rows(); row++) {
                    if (classes == 2) {
                        double p = Math.exp(scores[row][0]);
                        scores[row][0] = 1 - p;
                        scores[row][1] = p;
                    } else {
                        double maximum = scores[row][0];
                        for (double value : scores[row]) maximum = Math.max(maximum, value);
                        double sum = 0;
                        for (int k = 0; k < classes; k++) {
                            scores[row][k] = Math.exp(scores[row][k] - maximum);
                            sum += scores[row][k];
                        }
                        for (int k = 0; k < classes; k++) scores[row][k] /= sum;
                    }
                    labels[row] = firstMaximum(scores[row]);
                }
            }
            return new Prediction(labels, scores, null, null);
        }
    }

    private record DecisionFunction(double rho, double[] alpha, int[] indices) {}

    /** Use native support vectors and decision functions, retaining OpenCV ties. */
    private static Prediction svm(StatModel model, Mat samples, int classes) {
        if (!(model instanceof SVM svm))
            throw new IllegalStateException("The SVM resource contains a different native head");
        int kernelType = svm.getKernelType();
        if (kernelType != SVM.LINEAR && kernelType != SVM.RBF)
            throw new IllegalStateException("Unsupported saved SVM kernel");
        try (Mat support = svm.getSupportVectors(); Mat nativeLabels = new Mat();
             Indexer si = samples.createIndexer(); Indexer vi = support.createIndexer()) {
            List<DecisionFunction> functions = new ArrayList<>();
            List<int[]> pairList = new ArrayList<>();
            for (int a = 0; a < classes; a++) for (int b = a + 1; b < classes; b++) {
                try (Mat alpha = new Mat(); Mat indices = new Mat()) {
                    double rho = svm.getDecisionFunction(pairList.size(), alpha, indices);
                    double[] coefficients = new double[(int) alpha.total()];
                    int[] vectors = new int[(int) indices.total()];
                    try (Indexer ai = alpha.createIndexer(); Indexer ii = indices.createIndexer()) {
                        for (int i = 0; i < coefficients.length; i++) coefficients[i] = ai.getDouble(0, i);
                        for (int i = 0; i < vectors.length; i++) vectors[i] = (int) ii.getDouble(0, i);
                    }
                    functions.add(new DecisionFunction(rho, coefficients, vectors));
                    pairList.add(new int[]{a, b});
                }
            }
            int[][] pairs = pairList.toArray(int[][]::new);
            svm.predict(samples, nativeLabels, 0);
            int[] labels = new int[samples.rows()];
            double[][] scores = new double[samples.rows()][classes];
            double[][] margins = new double[samples.rows()][pairs.length];
            try (Indexer li = nativeLabels.createIndexer()) {
                for (int row = 0; row < samples.rows(); row++) {
                    double[] votes = new double[classes];
                    for (int p = 0; p < pairs.length; p++) {
                        DecisionFunction function = functions.get(p);
                        double margin = -function.rho();
                        for (int i = 0; i < function.alpha().length; i++) {
                            int sv = function.indices()[i];
                            double dot = 0, distance = 0;
                            for (int col = 0; col < samples.cols(); col++) {
                                double x = si.getDouble(row, col), v = vi.getDouble(sv, col);
                                dot += x * v;
                                distance += (x - v) * (x - v);
                            }
                            double kernel = kernelType == SVM.LINEAR ? dot : Math.exp(-svm.getGamma() * distance);
                            margin += function.alpha()[i] * kernel;
                        }
                        if (!Double.isFinite(margin))
                            throw new IllegalStateException("Native SVM returned a nonfinite margin");
                        margins[row][p] = margin;
                        votes[pairs[p][margin > 0 ? 0 : 1]]++;
                    }
                    int predicted = firstMaximum(votes);
                    if (predicted != (int) li.getDouble(row))
                        throw new IllegalStateException("SVM decision functions disagree with native label prediction");
                    labels[row] = predicted;
                    for (int k = 0; k < classes; k++) scores[row][k] = votes[k] / (classes - 1.0);
                }
            }
            return new Prediction(labels, scores, margins, pairs);
        }
    }

    /** Preserve Groovy's explicit Float-as-double decimal conversion. The old
     * score code used this for RF votes and LR products, while LR intercepts and
     * SVM kernel inputs used ordinary binary widening. Keep that distinction.
     */
    private static double explicitDouble(double value, boolean nativeFloat) {
        return nativeFloat ? Double.parseDouble(Float.toString((float) value)) : value;
    }

    private static int firstMaximum(double[] values) {
        int index = 0;
        for (int k = 1; k < values.length; k++) if (values[k] > values[index]) index = k;
        return index;
    }
}
