package org.mif.classification;

import java.awt.image.BufferedImage;
import org.bytedeco.opencv.opencv_core.Mat;
import qupath.lib.classifiers.Normalization;
import qupath.opencv.ml.FeaturePreprocessor;
import qupath.opencv.ml.objects.features.FeatureExtractor;
import qupath.opencv.ml.objects.features.FeatureExtractors;
import qupath.opencv.ml.objects.features.Preprocessing;

/** Fit QuPath preparation on the supplied training rows, never held-out rows. */
public final class FeaturePreparation {
    public record Settings(Normalization normalization,
            double retainedVariance, boolean normalizePca) {}
    public record ObjectFeatures(Mat samples, FeatureExtractor<BufferedImage> extractor) {}

    private FeaturePreparation() {}

    public static Normalization normalization(String value) {
        return switch (value) {
            case "none" -> Normalization.NONE;
            case "mean_variance" -> Normalization.MEAN_VARIANCE;
            case "min_max" -> Normalization.MIN_MAX;
            default -> throw new IllegalStateException("unsupported normalization");
        };
    }

    /** Pixel/KRONOS callers retain their distinct missing-value conventions. */
    public static FeaturePreprocessor pixel(Mat samples, Settings settings, double missingValue) {
        if (settings.retainedVariance() > 0) {
            return PcaFitting.pixel(samples, settings, missingValue);
        }
        return FeaturePreprocessor.builder().normalize(settings.normalization())
                .missingValue(missingValue)
                .pca(settings.retainedVariance(), settings.normalizePca()).build(samples, true);
    }

    /** Apply the fitted transformations to training rows and to the saved cell feature extractor. */
    public static ObjectFeatures object(Mat samples, FeatureExtractor<BufferedImage> extractor,
            Settings settings) {
        var normalizer = settings.retainedVariance() > 0
                ? PcaFitting.normalizer(samples, settings.normalization(), Double.NaN)
                : Preprocessing.createNormalizer(settings.normalization(), samples, Double.NaN);
        Preprocessing.normalize(samples, normalizer);
        extractor = FeatureExtractors.createNormalizingFeatureExtractor(extractor, normalizer);
        if (settings.retainedVariance() > 0) {
            var projector = PcaFitting.projector(samples, settings.retainedVariance(), settings.normalizePca());
            Mat projected = new Mat();
            projector.project(samples, projected);
            samples.close();
            samples = projected;
            extractor = FeatureExtractors.createPCAProjectFeatureExtractor(extractor, projector);
        }
        return new ObjectFeatures(samples, extractor);
    }
}
