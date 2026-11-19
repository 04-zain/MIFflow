package org.mif.classification;

import java.util.Objects;
import org.bytedeco.javacpp.indexer.FloatIndexer;
import org.bytedeco.javacpp.indexer.IntIndexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.global.opencv_ml;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_core.TermCriteria;
import org.bytedeco.opencv.opencv_ml.LogisticRegression;
import org.bytedeco.opencv.opencv_ml.RTrees;
import org.bytedeco.opencv.opencv_ml.SVM;
import org.bytedeco.opencv.opencv_ml.TrainData;

/** Fit the native RF/SVM/LR heads used by tissue, cell and KRONOS classifiers.
 *
 * Stage owners decode and validate settings before calling these operations.
 * Training data is borrowed; the caller owns a returned trained model. A failed
 * fit closes its new model, and each operation closes its temporary allocations.
 */
public final class OpenCvTraining {
    private OpenCvTraining() {}

    public record ForestSettings(int maxDepth, int minSampleCount, boolean use1seRule,
                                 int activeVarCount, boolean calculateVariableImportance,
                                 int cvFolds, boolean useSurrogates, int maxTrees,
                                 double terminationEpsilon) {}

    public record SvmSettings(int kernel, double c, double gamma, int maxIterations,
                              double terminationEpsilon, boolean balanceClasses) {}

    public record LogisticSettings(double learningRate, int iterations, int regularization,
                                   double terminationEpsilon) {}

    /** Fit a forest with the caller's controls and the existing RNG seed. */
    public static RTrees fitForest(TrainData data, ForestSettings settings, int seed) {
        Objects.requireNonNull(data, "training data");
        Objects.requireNonNull(settings, "forest settings");
        RTrees model = RTrees.create();
        try {
            model.setMaxDepth(settings.maxDepth());
            model.setMinSampleCount(settings.minSampleCount());
            model.setUse1SERule(settings.use1seRule());
            model.setActiveVarCount(settings.activeVarCount());
            model.setCalculateVarImportance(settings.calculateVariableImportance());
            model.setCVFolds(settings.cvFolds());
            model.setUseSurrogates(settings.useSurrogates());
            int flags = (settings.maxTrees() > 0 ? TermCriteria.MAX_ITER : 0)
                | (settings.terminationEpsilon() > 0 ? TermCriteria.EPS : 0);
            try (TermCriteria criteria = new TermCriteria(
                    flags, settings.maxTrees(), settings.terminationEpsilon())) {
                model.setTermCriteria(criteria);
            }
            opencv_core.setRNGSeed(seed);
            if (!model.train(data) || !model.isTrained())
                throw new IllegalStateException("The native forest did not train successfully");
            return model;
        } catch (RuntimeException | Error failure) {
            model.close();
            throw failure;
        }
    }

    /** Fit C-SVC, optionally preserving tissue SVM inverse-frequency penalties.
     * OpenCV uses class penalties for SVM; TrainData sample weights alone do not
     * provide that balancing. Cell and KRONOS callers leave this option disabled.
     */
    public static SVM fitSvm(TrainData data, SvmSettings settings, int seed) {
        Objects.requireNonNull(data, "training data");
        Objects.requireNonNull(settings, "SVM settings");
        SVM model = SVM.create();
        try (Mat weights = new Mat()) {
            model.setType(SVM.C_SVC);
            model.setKernel(settings.kernel());
            model.setC(settings.c());
            if (settings.kernel() == SVM.RBF) model.setGamma(settings.gamma());
            try (TermCriteria criteria = new TermCriteria(
                    TermCriteria.MAX_ITER | TermCriteria.EPS,
                    settings.maxIterations(), settings.terminationEpsilon())) {
                model.setTermCriteria(criteria);
            }
            if (settings.balanceClasses()) {
                try (Mat responses = data.getTrainNormCatResponses();
                     Mat labels = data.getClassLabels();
                     IntIndexer responseIndex = responses.createIndexer()) {
                    int[] counts = new int[(int) labels.total()];
                    for (int row = 0; row < responses.rows(); row++)
                        counts[responseIndex.get(row)]++;
                    weights.create(counts.length, 1, opencv_core.CV_32FC1);
                    try (FloatIndexer weightIndex = weights.createIndexer()) {
                        for (int label = 0; label < counts.length; label++) {
                            if (counts[label] == 0)
                                throw new IllegalArgumentException("SVM class has no training samples");
                            weightIndex.put(label, (float) (responses.rows() / (double) counts[label]));
                        }
                    }
                    model.setClassWeights(weights);
                }
            }
            opencv_core.setRNGSeed(seed);
            if (!model.train(data) || !model.isTrained())
                throw new IllegalStateException("The native SVM did not train successfully");
            return model;
        } catch (RuntimeException | Error failure) {
            model.close();
            throw failure;
        }
    }
    /** Fit the existing batch LR head using float responses and borrowed data. */
    public static LogisticRegression fitLogistic(TrainData data, LogisticSettings settings, int seed) {
        Objects.requireNonNull(data, "training data");
        Objects.requireNonNull(settings, "LR settings");
        LogisticRegression model = LogisticRegression.create();
        try (Mat floatResponses = new Mat(); Mat responses = data.getResponses(); Mat samples = data.getSamples()) {
            model.setLearningRate(settings.learningRate());
            model.setIterations(settings.iterations());
            model.setRegularization(settings.regularization());
            model.setTrainMethod(LogisticRegression.BATCH);
            try (TermCriteria criteria = new TermCriteria(TermCriteria.MAX_ITER | TermCriteria.EPS,
                    settings.iterations(), settings.terminationEpsilon())) {
                model.setTermCriteria(criteria);
            }
            responses.convertTo(floatResponses, opencv_core.CV_32F);
            opencv_core.setRNGSeed(seed);
            if (!model.train(samples, opencv_ml.ROW_SAMPLE, floatResponses) || !model.isTrained())
                throw new IllegalStateException("The native supervised classifier did not train");
            return model;
        } catch (RuntimeException | Error failure) {
            model.close();
            throw failure;
        }
    }

}
