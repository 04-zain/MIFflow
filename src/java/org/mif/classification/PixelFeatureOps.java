package org.mif.classification;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import qupath.lib.images.servers.ColorTransforms;
import qupath.lib.regions.Padding;
import qupath.opencv.ops.ImageDataOp;
import qupath.opencv.ops.ImageOp;
import qupath.opencv.ops.ImageOps;
import qupath.opencv.tools.MultiscaleFeatures.MultiscaleFeature;

/** Assemble raw channels and scale-specific filters using QuPath image operations.
 *
 * The saved classifier contains the resulting QuPath operation, including its
 * filters, scales, padding and channel order. Fitting and application therefore
 * calculate the same features without storing intermediate images.
 */
public final class PixelFeatureOps {
    private PixelFeatureOps() {}

    /** Filters calculated together at one physical Gaussian sigma in micrometres. */
    public record Scale(double sigmaUm, List<String> filters) {
        public Scale {
            if (!Double.isFinite(sigmaUm) || sigmaUm <= 0)
                throw new IllegalArgumentException("Enter a positive finite Gaussian sigma for each scale.");
            filters = List.copyOf(filters);
            if (filters.isEmpty() || new HashSet<>(filters).size() != filters.size())
                throw new IllegalArgumentException("Select different image filters within each scale.");
            for (String name : filters) feature(name);
        }
    }

    /** Decode ordered scale groups from the current native request's recipe. */
    public static List<Scale> scales(List<?> rows) {
        List<Scale> result = new ArrayList<>();
        for (Object value : rows) {
            if (!(value instanceof Map<?, ?> row) || !row.keySet().equals(Set.of("sigma_um", "filters"))
                    || !(row.get("sigma_um") instanceof Number sigma)
                    || !(row.get("filters") instanceof List<?> filters)
                    || filters.stream().anyMatch(item -> !(item instanceof String)))
                throw new IllegalArgumentException("Each image-feature scale requires sigma_um and a list of filters.");
            result.add(new Scale(sigma.doubleValue(), filters.stream().map(String.class::cast).toList()));
        }
        return List.copyOf(result);
    }

    /** The operation and the dimensions needed to account for its tile workspace. */
    public record Prepared(ImageDataOp operation, Padding padding, int featureCount) {}

    /** Build features at the classifier's square-pixel sampling resolution. */
    public static Prepared create(int[] channels, boolean includeIntensity,
            List<Scale> scales, double resolutionUm) {
        return create(channels, includeIntensity, scales, resolutionUm, resolutionUm);
    }

    /** Convert each scale to X/Y pixels, then concatenate its selected filters.
     *
     * Raw channels precede scale groups in their supplied order. QuPath defines
     * the output-channel order within each multiscale operation. The returned
     * operation contains only QuPath's serializable classes, not this recipe type.
     */
    public static Prepared create(int[] channels, boolean includeIntensity,
            List<Scale> scales, double resolutionXUm, double resolutionYUm) {
        if (channels.length == 0 || !Double.isFinite(resolutionXUm) || resolutionXUm <= 0
                || !Double.isFinite(resolutionYUm) || resolutionYUm <= 0)
            throw new IllegalArgumentException("Select image channels and a positive classification resolution.");
        if (!includeIntensity && scales.isEmpty())
            throw new IllegalArgumentException("Include channel intensity or select at least one image filter.");
        if (scales.stream().map(Scale::sigmaUm).distinct().count() != scales.size())
            throw new IllegalArgumentException("Use a different Gaussian sigma for each scale.");
        var selected = new ArrayList<ColorTransforms.ColorTransform>(channels.length);
        for (int channel : channels) selected.add(ColorTransforms.createChannelExtractor(channel));
        ImageDataOp base = ImageOps.buildImageDataOp(selected);
        if (scales.isEmpty()) return new Prepared(base, Padding.empty(), channels.length);

        // i) Share derivative calculations only among the filters chosen at a scale.
        List<ImageOp> operations = new ArrayList<>();
        if (includeIntensity) operations.add(ImageOps.Core.identity());
        int perChannel = includeIntensity ? 1 : 0;
        for (Scale scale : scales) {
            double sigmaX = scale.sigmaUm() / resolutionXUm, sigmaY = scale.sigmaUm() / resolutionYUm;
            double maximumSigma = (Integer.MAX_VALUE - 1.0) / 16;
            if (!Double.isFinite(sigmaX) || !Double.isFinite(sigmaY) || sigmaX <= 0 || sigmaY <= 0
                    || sigmaX > maximumSigma || sigmaY > maximumSigma)
                throw new IllegalArgumentException("Choose positive smoothing scales that fit at the classification resolution.");
            var features = scale.filters().stream().map(PixelFeatureOps::feature).toList();
            operations.add(ImageOps.Filters.features(features, sigmaX, sigmaY));
            perChannel = Math.addExact(perChannel, features.size());
        }
        // ii) Preserve group order and let QuPath retain the operation in its model.
        ImageOp combined = ImageOps.Core.splitMerge(operations);
        return new Prepared(base.appendOps(combined), combined.getPadding(),
            Math.multiplyExact(channels.length, perChannel));
    }

    private static MultiscaleFeature feature(String name) {
        MultiscaleFeature value = MultiscaleFeature.valueOf(name.toUpperCase(Locale.ROOT));
        if (!value.supports2D())
            throw new IllegalArgumentException("The selected image filter does not support two-dimensional images.");
        return value;
    }
}
