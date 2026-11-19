/**
 * Fits the binary tissue detector from the channel, image resolution and method selected in QuPath.
 *
 * We either apply a threshold to a smoothed intensity channel or fit RF/SVM from
 * Tissue and Background annotations. The trained methods use the selected channel
 * intensity and image filters at the visible physical scales. The initial recipe
 * retains intensity and Gaussian smoothing at one and three detector pixels.
 *
 * We prepare a copy of the current labelled annotations before fitting. Pixel
 * features are then read at the selected stored resolution, and the resulting
 * classifier retains that physical resolution, feature calculation and label map.
 * The editor uses the classifier for its overlay and saves it when we accept the
 * preview. Tissue polygons are generated afterward by the tissue-detection step.
 */

import qupath.lib.images.servers.ColorTransforms
import qupath.lib.images.servers.PixelCalibration
import qupath.lib.images.servers.ImageServerMetadata.ChannelType
import qupath.lib.objects.classes.PathClass
import qupath.opencv.ops.ImageOps
import qupath.opencv.ml.pixel.PixelClassifiers
import qupath.opencv.ml.OpenCVClassifiers
import qupath.lib.classifiers.pixel.PixelClassifierMetadata
import qupath.process.gui.commands.ml.PixelClassifierTraining
import org.mif.classification.PixelFeatureOps
import org.mif.classification.OpenCvTraining
import org.mif.classification.TissueTrainingRows
import org.bytedeco.opencv.opencv_ml.SVM
import org.bytedeco.opencv.opencv_ml.TrainData
import org.bytedeco.opencv.global.opencv_ml

/**
 * Collects the serialized annotation copy while limiting it to eight MiB.
 *
 * The editor copies reference geometry before starting background fitting. Both
 * write methods check the additional byte count before appending to this buffer,
 * so a very detailed annotation cannot create an unbounded snapshot in memory.
 */
class BoundedTissueSnapshot extends ByteArrayOutputStream {
    static final int LIMIT=8*1024*1024
    /**
     * Appends one serialized byte when the annotation copy still fits within eight MiB.
     *
     * Args:
     *     value: Next byte supplied by the annotation serializer.
     *
     * Raises:
     *     IllegalArgumentException: The next byte would exceed the supported snapshot size.
     */
    synchronized void write(int value) {
        if(count>=LIMIT)throw new IllegalArgumentException("Use smaller representative training regions; the annotation draft exceeds 8 MiB.")
        super.write(value)
    }
    /**
     * Appends a block of serialized annotation bytes after checking the remaining space.
     *
     * Args:
     *     value: Source byte array supplied by the annotation serializer.
     *     offset: First byte in that array to copy.
     *     length: Number of bytes to append.
     *
     * Raises:
     *     IllegalArgumentException: The block would exceed the supported snapshot size.
     */
    synchronized void write(byte[] value,int offset,int length) {
        if(length>LIMIT-count)throw new IllegalArgumentException("Use smaller representative training regions; the annotation draft exceeds 8 MiB.")
        super.write(value, offset, length)
    }
}

/**
 * Prepares the current reference annotations and fits the selected binary tissue model.
 *
 * Background is stored as class index 0 and Tissue as index 1. Background carries
 * QuPath's ignored-class marker, allowing its prediction colour to remain
 * transparent while the tissue prediction is shown over the source image.
 */
class TissueAuthoring {
    static final tissue=PathClass.getInstance("Tissue")
    static final background=PathClass.getInstance("Background*")
    /**
     * Chooses an initial preview resolution close to the configured physical pixel size.
     *
     * For each stored downsample d, we calculate the pixel width pixelWidth*d and
     * height pixelHeight*d. Their ratios to targetPixelSize describe how much larger
     * or smaller those pixels are than the target. Taking the absolute logarithm
     * gives equal scores to reciprocal ratios, such as 2 and 0.5. We minimize the
     * larger X/Y score so both physical dimensions influence the initial choice:
     *
     *     score(d) = max(abs(log(pixelWidth*d/target)),
     *                    abs(log(pixelHeight*d/target)))
     *
     * Reduced levels are preferred when present. This supplies the editor's initial
     * selection; the visible level choice can then be changed before fitting.
     *
     * Args:
     *     downsample: Stored image downsample factors in native level order.
     *     pixelWidth: Full-resolution X pixel size in µm/pixel.
     *     pixelHeight: Full-resolution Y pixel size in µm/pixel.
     *     targetPixelSize: Requested initial pixel size from the tissue configuration.
     *
     * Returns:
     *     Index of the level with the smallest maximum X/Y score. Equal scores retain
     *     the first qualifying level in the supplied order.
     *
     * Raises:
     *     IllegalArgumentException: The image lacks valid positive calibration or
     *         valid stored downsample factors.
     */
    static int startingLevel(
        List downsample,
        double pixelWidth,
        double pixelHeight,
        double targetPixelSize
    ) {
        if(downsample.isEmpty() || ![pixelWidth,pixelHeight,targetPixelSize].every {Double.isFinite(it) && it>0d} ||
            !downsample.every {it instanceof Number && Double.isFinite(it.doubleValue()) && it>=1d})
            throw new IllegalArgumentException("Choose an image with recorded pixel calibration and image resolutions.")
        List indices=(0..<downsample.size()).findAll {downsample[it]>1d || downsample.size()==1}
        if(indices.isEmpty())indices=[0]
        return indices.min {i ->
            double d=downsample[i] as double
            Math.max(Math.abs(Math.log(pixelWidth*d/targetPixelSize)),Math.abs(Math.log(pixelHeight*d/targetPixelSize)))
        } as int
    }
    /**
     * Copies the current Tissue and Background geometry for one preview fit.
     *
     * The threshold method needs only the shared image reader. For RF/SVM, we select
     * annotations carrying the two expected labels and estimate their sampled area
     * as bounding-box area divided by downsample squared. Counts of annotations,
     * geometry vertices, sampled pixels and serialized bytes limit the amount of
     * reference data copied before fitting begins.
     *
     * We serialize the selected annotations to GeoJSON and read them into a separate
     * ImageData object using the same image server. Further annotation edits in the
     * viewer therefore belong to a later preview request.
     *
     * Args:
     *     data: Current QuPath image and its annotation hierarchy.
     *     options: Selected method, native downsample and maximum training-pixel count.
     *
     * Returns:
     *     ImageData using the current image server and a separate copy of the selected
     *     reference annotations. Threshold snapshots contain no copied annotations.
     *
     * Raises:
     *     IllegalArgumentException: The method, resolution or amount of reference
     *         geometry exceeds the supported preview settings.
     */
    static snapshot(data,Map options) {
        def copy=new qupath.lib.images.ImageData(data.server)
        if(options.method=="threshold")return copy
        if(!(options.method in ["rf","svm"]))throw new IllegalArgumentException("Choose a tissue detection method.")
        double scale=(options.downsample as Number).doubleValue()
        if(!Double.isFinite(scale) || scale<1d)throw new IllegalArgumentException("Choose a stored image resolution.")
        int maximum=(options.max_pixels as Number).intValue()
        if(maximum<1 || maximum>200000)throw new IllegalArgumentException("Use smaller representative training regions.")
        def selected=data.hierarchy.annotationObjects.findAll {it.pathClass in [tissue,background]}
        if(selected.size()>4096 || selected.sum {it.ROI.numPoints} > 250000 ||
            selected.sum {it.ROI.boundsWidth*it.ROI.boundsHeight/(scale*scale)} > maximum)
            throw new IllegalArgumentException("Use smaller representative training regions or a coarser image resolution before updating the preview.")
        def output=new BoundedTissueSnapshot()
        qupath.lib.io.PathIO.exportObjectsAsGeoJSON(output,selected,qupath.lib.io.PathIO.GeoJsonExportOptions.FEATURE_COLLECTION)
        copy.hierarchy.addObjects(qupath.lib.io.PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(output.toByteArray())))
        return copy
    }
    /**
     * Builds the selected tissue classifier at the chosen stored image resolution.
     *
     * For threshold detection, we read the selected channel, optionally smooth it
     * with the supplied Gaussian standard deviation, and apply QuPath's binary
     * threshold operation. The smoothing value is expressed in pixels at the
     * selected resolution; its physical X/Y size follows that level's calibration.
     *
     * For RF/SVM, each labelled pixel contributes the selected channel intensity
     * and filtered values. We use QuPath's feature extraction to collect those rows
     * and their Tissue/Background labels. The snapshot must contain both classes,
     * with at least two pixels from each, within the configured total sample count.
     *
     * SVM fitting balances the classes by selecting equally spaced rows from each
     * class, up to the configured per-class count. We calculate each feature's mean
     * and population standard deviation on these selected rows, then train on
     * (value-mean)/standard_deviation. A feature with negligible variation uses a
     * scale of one. The saved feature operation repeats the same subtraction and
     * scaling during prediction. RF uses the selected feature values with their
     * original scale.
     *
     * The RF route constructs OpenCV RTrees with the selected depth, split-feature
     * count, minimum sample count and tree limit. The SVM route constructs C_SVC with
     * an RBF kernel, selected C and gamma, and the iteration/termination settings.
     * Both retain the selected physical pixel size and the 0=Background, 1=Tissue
     * label map in the resulting QuPath pixel classifier.
     *
     * Args:
     *     data: ImageData snapshot prepared from the current image and labelled regions.
     *     options: Complete editor settings, including method, channel, downsample,
     *         threshold/smoothing values and the relevant RF or SVM parameters.
     *         The editor obtains these starting values from tissue_detection.json.
     *
     * Returns:
     *     Map containing classifier, method, sample count and physical resolution.
     *     Threshold results also contain the selected smoothing standard deviation.
     *
     * Raises:
     *     IllegalArgumentException: Channel, level, physical calibration, reference
     *         geometry, active model parameters or extracted feature values are invalid,
     *         or the native model fails to finish training.
     */
    static Map fit(data,Map options) {
        String method=options.method
        if(!(method in ["threshold","rf","svm"]))throw new IllegalArgumentException("Choose a tissue detection method.")
        int channel=(options.channel as Number).intValue()
        double scale=(options.downsample as Number).doubleValue()
        if(channel<0 || channel>=data.server.nChannels() || !Double.isFinite(scale) || scale<1d ||
           !data.server.preferredDownsamples.any {Math.abs(it-scale)<1e-9})
            throw new IllegalArgumentException("Select a channel and an image level from this slide.")
        def calibration=data.server.pixelCalibration
        if(!calibration.hasPixelSizeMicrons())throw new IllegalArgumentException("This image needs its recorded physical calibration.")

        // Convert the selected stored downsample into the classifier's physical pixel
        // width and height, which QuPath retains with its prediction operation.
        def resolution=new PixelCalibration.Builder().pixelSizeMicrons(
            calibration.pixelWidthMicrons*scale,calibration.pixelHeightMicrons*scale).build()
        Map labels=[(0):background,(1):tissue]
        if(method=="threshold") {
            double value=(options.threshold as Number).doubleValue()
            if(!Double.isFinite(value))throw new IllegalArgumentException("Enter a finite threshold.")
            Object requested=options.containsKey("smoothing_sigma_px") ? options.smoothing_sigma_px : 0d
            if(!(requested instanceof Number) || !Double.isFinite(requested.doubleValue()) || requested.doubleValue()<0d || requested.doubleValue()>20d)
                throw new IllegalArgumentException("Enter a Gaussian smoothing scale from 0 to 20 pixels at the selected resolution.")
            double sigma=requested.doubleValue()
            def thresholdOp=ImageOps.buildImageDataOp([ColorTransforms.createChannelExtractor(channel)])
            if(sigma>0d)thresholdOp=thresholdOp.appendOps(ImageOps.Filters.gaussianBlur(sigma))
            thresholdOp=thresholdOp.appendOps(ImageOps.Threshold.threshold(value))
            return [classifier:PixelClassifiers.createClassifier(thresholdOp,resolution,labels),
                method:method,samples:0,resolution:resolution,smoothing_sigma_px:sigma]
        }

        // Check only the parameters used by the selected model. An unused SVM choice
        // must not prevent an RF fit, and the reverse is also true.
        Map integerMinimums=[max_pixels:1]
        integerMinimums.putAll(method=="rf" ? [max_trees:1,max_depth:1,min_sample_count:1,active_var_count:0] : [max_pixels_per_class:2,max_iterations:1])
        integerMinimums.each {name,minimum ->
            def value=options[name]
            if(!(value instanceof Number) || !Double.isFinite(value.doubleValue()) ||
               value.doubleValue()!=Math.rint(value.doubleValue()) || value.doubleValue()<minimum || value.doubleValue()>Integer.MAX_VALUE)
                throw new IllegalArgumentException("Enter a whole number of at least "+minimum+" for "+name.replace('_',' ')+".")
        }
        if(!(options.random_seed instanceof Number) || !Double.isFinite(options.random_seed.doubleValue()) ||
           options.random_seed.doubleValue()!=Math.rint(options.random_seed.doubleValue()) ||
           options.random_seed.doubleValue()<Integer.MIN_VALUE || options.random_seed.doubleValue()>Integer.MAX_VALUE)
            throw new IllegalArgumentException("Enter a 32-bit integer random_seed in config/defaults/tissue_detection.json.")
        for(String name:(method=="svm" ? ["c","gamma","termination_epsilon"] : [])) {
            def value=options[name]
            if(!(value instanceof Number) || !Double.isFinite(value.doubleValue()) || value.doubleValue()<=0d)
                throw new IllegalArgumentException("Enter a positive finite value for "+name.replace('_',' ')+".")
        }
        int maximum=(options.max_pixels as Number).intValue()
        if(maximum>200000)throw new IllegalArgumentException("Training supports at most 200,000 sampled pixels.")
        def regions=data.hierarchy.annotationObjects.findAll {it.pathClass in [tissue,background]}
        if(!regions.any {it.pathClass==tissue} || !regions.any {it.pathClass==background})
            throw new IllegalArgumentException("Draw and label examples of Tissue and Background before training.")
        double estimated=regions.collect {it.ROI.boundsWidth*it.ROI.boundsHeight/(scale*scale)}.sum()
        if(estimated>maximum)throw new IllegalArgumentException("Training annotations cover too many pixels at this resolution. Choose a coarser image level or smaller representative regions.")
        if(regions.any {it.ROI.z!=0 || it.ROI.t!=0 || !it.ROI.isArea()})
            throw new IllegalArgumentException("Use area annotations on the current image plane.")

        // The saved operation retains this physical recipe for every prediction tile.
        Map recipe=options.image_features
        if(!(recipe instanceof Map) || !(recipe.include_intensity instanceof Boolean) ||
           recipe.keySet()!=(["include_intensity","scales"] as Set) || !(recipe.scales instanceof List))
            throw new IllegalArgumentException("Select the image features and their physical filter scales.")
        def prepared=PixelFeatureOps.create([channel] as int[],recipe.include_intensity as boolean,
            PixelFeatureOps.scales(recipe.scales),
            resolution.pixelWidthMicrons,resolution.pixelHeightMicrons)
        if(method=="rf" && (options.active_var_count as Number).intValue()>prepared.featureCount())
            throw new IllegalArgumentException("Features per split cannot exceed the "+prepared.featureCount()+" selected image features.")
        def featureOp=prepared.operation()
        def trainer=new PixelClassifierTraining(featureOp);
        trainer.setResolution(resolution)
        def built=trainer.createTrainingDataForLabelMap([data],[(background):0,(tissue):1])
        if(built==null)throw new IllegalArgumentException("No labelled pixels were available for training.")
        def supplied=built.trainData
        if(supplied==null)throw new IllegalArgumentException("No labelled pixels were available for training.")
        def rows=null,training=null,model=null
        try {
            rows=TissueTrainingRows.prepare(supplied.samples,supplied.responses,method=="svm",
                maximum,method=="svm" ? (options.max_pixels_per_class as Number).intValue() : 0)
            trainer.setFeatureOp(null) // Preparation owns only the rows needed for fitting.
            training=TrainData.create(rows.samples(),opencv_ml.ROW_SAMPLE,rows.targets())
            int seed=(options.random_seed as Number).intValue()
            if(method=="rf") {
                model=OpenCvTraining.fitForest(training,new OpenCvTraining.ForestSettings(
                    (options.max_depth as Number).intValue(),(options.min_sample_count as Number).intValue(),
                    false,(options.active_var_count as Number).intValue(),false,0,false,
                    (options.max_trees as Number).intValue(),0d),seed)
            } else {
                model=OpenCvTraining.fitSvm(training,new OpenCvTraining.SvmSettings(SVM.RBF,
                    (options.c as Number).doubleValue(),(options.gamma as Number).doubleValue(),
                    (options.max_iterations as Number).intValue(),(options.termination_epsilon as Number).doubleValue(),false),seed)
            }
            def wrapper=OpenCVClassifiers.wrapStatModel(model)

            // Store feature centring and scaling with the classifier so future tiles
            // receive the same transformation as its training pixels.
            def processed=featureOp.appendOps(ImageOps.Core.subtract(rows.centres()),ImageOps.Core.multiply(rows.scales()))
            def metadata=new PixelClassifierMetadata.Builder().inputResolution(resolution).classificationLabels(labels).setChannelType(ChannelType.CLASSIFICATION).build()
            return [classifier:PixelClassifiers.createClassifier(wrapper,processed,metadata,false),
                method:method,samples:rows.samples().rows(),resolution:resolution]
        } catch(Throwable error) {model?.close();
            throw error}
        finally {training?.close();
            rows?.close();
            if(rows==null)trainer.setFeatureOp(null);
            supplied.close()}
    }
}
return [snapshot:TissueAuthoring.&snapshot,fit:TissueAuthoring.&fit,startingLevel:TissueAuthoring.&startingLevel,tissue:TissueAuthoring.tissue,background:TissueAuthoring.background]
