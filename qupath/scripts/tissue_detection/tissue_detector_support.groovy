/**
 * Checks the selected detector against the image before the pipeline imports or applies it.
 *
 * We inspect both the saved JSON description and the classifier reconstructed
 * by QuPath. The JSON identifies the channel and feature calculation; the native
 * classifier supplies its output classes and physical prediction resolution.
 * These descriptions must agree with the selected image and one of its permitted
 * stored resolutions.
 *
 * A saved RF/SVM detector retains its own feature operations, centring and scaling
 * values. We validate the saved transformation and native model dimensions. New configuration
 * defaults affect the next model we fit, while an existing model keeps the feature
 * calculation used for its training.
 */

import qupath.lib.classifiers.pixel.PixelClassifier
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServerMetadata.ChannelType
import qupath.lib.objects.classes.PathClassTools

/**
 * Checks the three supported detector forms: a direct threshold, a smoothed
 * threshold, or a native RF/SVM using the saved channel features.
 *
 * All forms must produce scalar class indices for background and tissue and
 * must have a physical input resolution available in the selected image.
 */
final class MifTissueDetectorSupportV1 {
    /**
     * Stops detector inspection with the relevant explanation when a requirement fails.
     *
     * Args:
     *     condition: Result of the specific saved-model or image comparison.
     *     message: Explanation of the failed requirement for the detector-selection step.
     *
     * Raises:
     *     IllegalArgumentException: condition is false.
     */
    private static void require(boolean condition, String message) {
        if (!condition) throw new IllegalArgumentException(message)
    }

    /**
     * Checks the nonnegative index representation supplied by the JSON decoder.
     *
     * The detector reader decodes stored integer fields without using a Float or
     * Double wrapper. We also require the value to fit the native signed-integer
     * range before it is compared with a selected channel or class index.
     *
     * Args:
     *     value: Decoded channel, class or other nonnegative index.
     *
     * Returns:
     *     True for an admitted Number representation between zero and Integer.MAX_VALUE.
     */
    private static boolean integer(Object value) {
        value instanceof Number && !(value instanceof Float) && !(value instanceof Double) &&
            value.longValue() >= 0L && value.longValue() <= Integer.MAX_VALUE
    }

    /**
     * Checks that a decoded detector parameter is numerical and finite.
     *
     * Args:
     *     value: Threshold, Gaussian scale or feature-normalization value from the saved model.
     *
     * Returns:
     *     True when conversion to double yields neither NaN nor infinity.
     */
    private static boolean finite(Object value) {
        value instanceof Number && Double.isFinite(value.doubleValue())
    }

    /**
     * Collects the saved feature operations in their execution order.
     *
     * QuPath can store a sequential operation inside another sequential operation.
     * We visit each child in order and collect the nonsequential operations into one
     * list. At most three nested levels and four items per sequence are supported,
     * which bounds the structure inspected before checking the actual features.
     *
     * Args:
     *     operation: Decoded operation map from the saved classifier.
     *     depth: Current nesting depth, initially zero.
     *
     * Returns:
     *     Ordered list of feature, centring, scaling and prediction operations.
     *
     * Raises:
     *     IllegalArgumentException: The sequence is malformed, empty or too deeply nested.
     */
    private static List flattenSequence(Object operation,int depth=0) {
        require(depth<=3 && operation instanceof Map, "Unsupported learned tissue feature operation")
        if(operation.type=="op.core.sequential") {
            require(operation.keySet()==["type","ops"] as Set && operation.ops instanceof List &&
                !operation.ops.isEmpty() && operation.ops.size()<=4, "Unsupported learned tissue feature sequence")
            return operation.ops.collectMany {flattenSequence(it,depth+1)}
        }
        return [operation]
    }

    /**
     * Checks a saved Gaussian smoothing operation followed by one intensity threshold.
     *
     * The Gaussian must use the same finite standard deviation on X and Y, between
     * zero and 20 pixels at the classifier's selected resolution. The following
     * operation must supply exactly one finite threshold for that smoothed channel.
     *
     * Args:
     *     operation: Decoded two-step sequential operation.
     *
     * Raises:
     *     IllegalArgumentException: The saved operation order, Gaussian parameters
     *         or threshold description differs from the supported calculation.
     */
    private static void validateGaussianThreshold(Object operation) {
        require(operation instanceof Map && operation.keySet()==["type","ops"] as Set &&
            operation.type=="op.core.sequential" && operation.ops instanceof List && operation.ops.size()==2,
            "Use one Gaussian smoothing step followed by the tissue threshold")
        Object gaussian=operation.ops[0],threshold=operation.ops[1]
        require(gaussian instanceof Map && gaussian.keySet()==["type","sigmaX","sigmaY"] as Set &&
            gaussian.type=="op.filters.gaussian" && finite(gaussian.sigmaX) && finite(gaussian.sigmaY) &&
            gaussian.sigmaX>0d && gaussian.sigmaX<=20d && gaussian.sigmaX==gaussian.sigmaY,
            "Gaussian smoothing must use one finite scale from above 0 to 20 pixels at the selected resolution")
        require(threshold instanceof Map && threshold.keySet()==["type","thresholds"] as Set &&
            threshold.type=="op.threshold.constant" && threshold.thresholds instanceof List &&
            threshold.thresholds.size()==1 && finite(threshold.thresholds[0]),
            "Choose one finite threshold after Gaussian smoothing")
    }

    /**
     * Checks the feature sequence, saved scaling and native model of a tissue RF/SVM classifier.
     *
     * The sequence ends with centring, scaling and categorical RF/SVM prediction.
     * Any preceding feature operations are retained as deserialized by QuPath;
     * intensity-only models need no preceding operation. The centring vector
     * defines the feature count, which must match the positive scaling vector
     * and native model input count. This preserves the training transformation
     * without restricting saved filters to today's initial feature recipe.
     *
     * The native model must use class indices 0 and 1. QuPath's label map must
     * identify them as Background and Tissue, with categorical output consumed
     * by tissue-region generation.
     *
     * Args:
     *     operation: Decoded saved feature-to-class prediction operation.
     *     classifier: QuPath classifier reconstructed from that saved document.
     *
     * Raises:
     *     IllegalArgumentException: Feature structure, scaling, native model inputs,
     *         output mode or class labels are incompatible.
     */
    private static void validateLearned(Object operation,PixelClassifier classifier) {
        List ops=flattenSequence(operation)
        require(ops.size()>=3,"Unsupported learned tissue detector operations: select a saved detector with its scaling and model steps")
        Map centering=ops[-3],scaling=ops[-2],prediction=ops[-1]
        require(centering.keySet()==["type","values"] as Set && centering.type=="op.core.subtract" &&
            centering.values instanceof List && !centering.values.isEmpty() && centering.values.every {finite(it)},
            "Learned tissue centering values are invalid")
        int featureCount=centering.values.size()
        require(scaling.keySet()==["type","values"] as Set && scaling.type=="op.core.multiply" &&
            scaling.values instanceof List && scaling.values.size()==featureCount && scaling.values.every {finite(it) && it>0d},
            "Learned tissue scaling values are invalid")
        require(prediction.keySet()==["type","model","requestProbabilities"] as Set &&
            prediction.type=="op.ml.opencv-statmodel" && prediction.requestProbabilities==false &&
            prediction.model instanceof Map && prediction.model.keySet()==["class","statmodel"] as Set &&
            prediction.model["class"] in ["RTrees","SVM"] && prediction.model.statmodel instanceof Map,
            "Choose a native tissue random forest or SVM classifier")
        def stored=prediction.model.statmodel
        String key=prediction.model["class"]=="RTrees"?"opencv_ml_rtrees":"opencv_ml_svm"
        require(stored.keySet()==[key] as Set && stored[key] instanceof Map && integer(stored[key].var_count) && stored[key].var_count==featureCount &&
            (stored[key].class_labels==[0,1] || (key=="opencv_ml_svm" && stored[key].class_labels==[type_id:"opencv-matrix",rows:2,cols:1,dt:"i",data:[0,1]])),
            "Learned tissue model input or class identity differs")
        def labels=classifier.metadata.classificationLabels
        require(labels[0]?.toString()=="Background*" && labels[1]?.toString()=="Tissue",
            "Learned tissue classes must be Background and Tissue")
    }

    /**
     * Checks that the selected detector can classify the intended channel at a permitted native resolution.
     *
     * The direct-threshold form names its input band and finite threshold. The OpenCV
     * form names one extracted channel and its threshold or learned feature sequence.
     * A named-channel extractor must match the selected channel uniquely, so duplicate
     * channel names cannot resolve to an arbitrary input band.
     *
     * We then read the classifier's physical X/Y pixel sizes and compare them with
     * the permitted stored levels. Both dimensions must agree within 1e-9 µm/pixel.
     * Finally, require its complete binary label map and at least one visible
     * foreground class. Ignored or null background classes are recorded explicitly.
     *
     * Args:
     *     classifier: Native PixelClassifier loaded from the selected detector file.
     *     document: Decoded JSON from the same file.
     *     imageData: Selected QuPath image and its current channel metadata.
     *     channelIndex: Zero-based channel selected by the tissue setup step.
     *     permittedNativeRows: Stored resolution descriptions, each providing its
     *         native index and physical X/Y pixel sizes in µm/pixel.
     *
     * Returns:
     *     Map containing the classifier type, physical resolution, binary class
     *     descriptions and the matching native resolution indices.
     *
     * Raises:
     *     IllegalArgumentException: Detector structure, channel, physical resolution,
     *         output mode or foreground/background labels are incompatible.
     */
    static Map validate(
        PixelClassifier classifier,
        Map document,
        ImageData imageData,
        int channelIndex,
        List<Map> permittedNativeRows
    ) {
        require(classifier != null && imageData != null && channelIndex >= 0 &&
            channelIndex < imageData.server.nChannels(), "Threshold band is not present in the current image")
        String type = document.pixel_classifier_type
        if (type == "ThresholdPixelClassifier") {
            require(document.keySet() == ["pixel_classifier_type", "metadata", "fun"] as Set &&
                document.fun instanceof Map &&
                document.fun.keySet() == ["classifier_function", "band", "threshold"] as Set &&
                document.fun.classifier_function == "ClassifierGreaterEquals" &&
                finite(document.fun.threshold), "Unsupported direct native threshold function")
            require(integer(document.fun.band) && document.fun.band == channelIndex,
                "Threshold classifier band differs from the canonical channel")
            // QuPath 0.7 leaves inputNumChannels=3 in this family's metadata.
            // Its native annotation path does not call supportsImage; the exact
            // single-band function above consumes that band of the live raster.
        } else if (type == "OpenCVPixelClassifier") {
            require(document.keySet() == ["pixel_classifier_type", "metadata", "op"] as Set &&
                document.op instanceof Map &&
                document.op.keySet() == ["type", "colorTransforms", "op"] as Set &&
                document.op.type == "data.op.channels" &&
                document.op.colorTransforms instanceof List && document.op.colorTransforms.size() == 1,
                "Unsupported native threshold operation or multiple channel extractors")
            Object transform = document.op.colorTransforms[0]
            require(transform instanceof Map, "Unsupported threshold channel transform")
            if (transform.keySet() == ["channel"] as Set) {
                require(integer(transform.channel) && transform.channel == channelIndex,
                    "Threshold classifier band differs from the canonical channel")
            } else if (transform.keySet() == ["channelName"] as Set) {
                // Callers have already matched their canonical reference name
                // at channelIndex. Native GUI extractors use names, so require
                // exactly one live match rather than picking the first duplicate.
                List names = imageData.server.metadata.channels.collect { it.name }
                require(transform.channelName instanceof String &&
                    transform.channelName == names[channelIndex],
                    "Threshold classifier name differs from the canonical channel")
                require(names.count(transform.channelName) == 1,
                    "Threshold classifier channel name is ambiguous in the current image")
            } else {
                throw new IllegalArgumentException("Unsupported threshold channel transform")
            }
            Object operation = document.op.op
            if(operation instanceof Map && operation.type=="op.core.sequential") {
                if(operation.ops instanceof List && operation.ops.size()==2 &&
                   operation.ops[0] instanceof Map && operation.ops[0].type=="op.filters.gaussian")
                    validateGaussianThreshold(operation)
                else validateLearned(operation,classifier)
            } else require(operation instanceof Map && operation.keySet() == ["type", "thresholds"] as Set &&
                operation.type == "op.threshold.constant" && operation.thresholds instanceof List &&
                operation.thresholds.size() == 1 && finite(operation.thresholds[0]),
                "Choose a saved threshold, Gaussian-smoothed threshold, or supported tissue classifier")
            require(classifier.supportsImage(imageData), "Native threshold operation does not support the current image")
        } else {
            throw new IllegalArgumentException("Unsupported classifier; select a native simple thresholder")
        }
        def metadata = classifier.getMetadata()
        require(metadata != null && metadata.getOutputType() == ChannelType.CLASSIFICATION,
            "Classifier classification labels require classification output")
        def resolution = metadata.getInputResolution()
        require(resolution != null && resolution.hasPixelSizeMicrons(), "Classifier lacks physical input calibration")
        double x = resolution.pixelWidthMicrons
        double y = resolution.pixelHeightMicrons
        require(Double.isFinite(x) && Double.isFinite(y) && x > 0 && y > 0,
            "Classifier input calibration is invalid")

        // A selected level is compatible only when both physical pixel dimensions
        // match the model. Native level numbers alone do not establish that match.
        List<Integer> compatible = permittedNativeRows.findAll { Map row ->
            Math.abs(x - row.x_um_per_px.doubleValue()) <= 1e-9 &&
                Math.abs(y - row.y_um_per_px.doubleValue()) <= 1e-9
        }.collect { it.native_resolution_index.intValue() }.unique().sort()
        require(!compatible.isEmpty(), "Classifier calibration has no permitted native pyramid level")
        def nativeLabels = metadata.getClassificationLabels()
        require(nativeLabels instanceof Map && nativeLabels.keySet() == [0, 1] as Set,
            "Threshold classification labels must describe both binary outputs")
        List<Map> labels = []
        nativeLabels.each { value, pathClass ->
            require(integer(value), "Classifier label value is invalid")
            boolean isNull = PathClassTools.isNullClass(pathClass)
            boolean ignored = isNull || PathClassTools.isIgnoredClass(pathClass)
            String name = isNull ? null : pathClass.toString()
            require(isNull || (name != null && !name.isBlank()), "Classifier label name is invalid")
            labels << [value: value.intValue(), name: name, ignored: ignored]
        }
        labels.sort { it.value }
        require(labels.any { !it.ignored }, "Classifier labels contain no foreground class")
        return [classifier_type: type, input_resolution_um: [x: x, y: y],
            classification_labels: labels, compatible_native_resolution_indices: compatible]
    }
}

return { PixelClassifier classifier, Map document, ImageData imageData,
         int channelIndex, List<Map> permittedNativeRows ->
    MifTissueDetectorSupportV1.validate(classifier, document, imageData, channelIndex, permittedNativeRows)
}
