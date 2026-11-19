// Automatic affine estimation from bounded image views. Quantitative mIF is never resampled.
import java.nio.file.Path
import java.nio.file.Files
import java.util.concurrent.TimeUnit
import org.mif.image.RegistrationImageViews
import org.mif.registration.PairedGeometryOps
import net.imglib2.realtransform.AffineTransform3D
import qupath.lib.regions.RegionRequest

class AutomaticElastix {
    /**
     * Builds the initial moving-to-fixed position from physical translation, rotation and uniform scale.
     *
     * We convert the moving-image centre to micrometres, rotate and scale around
     * that centre, then place it at the fixed-image centre plus the selected
     * translation. Dividing by fixed X/Y pixel sizes expresses the final mapping
     * in full-resolution fixed-image pixels.
     *
     * Args:
     *     fixed: Fixed mIF image server with physical pixel calibration.
     *     moving: Paired-image server with physical pixel calibration.
     *     tx, ty: Horizontal and vertical translations in micrometres.
     *     degrees: Rotation angle in degrees.
     *     scale: Positive uniform physical scale factor.
     *
     * Returns:
     *     Moving-to-fixed affine transform in full-resolution pixel coordinates.
     */
    static AffineTransform3D similarity(fixed,moving,double tx,double ty,double degrees,double scale) {
        double fx=fixed.pixelCalibration.pixelWidthMicrons,fy=fixed.pixelCalibration.pixelHeightMicrons
        double mx=moving.pixelCalibration.pixelWidthMicrons,my=moving.pixelCalibration.pixelHeightMicrons
        return PairedGeometryOps.similarity(fixed.width as int,fixed.height as int,fx,fy,
            moving.width as int,moving.height as int,mx,my,tx,ty,degrees,scale)
    }
    /**
     * Converts the selected initial pixel transform into Elastix's physical-coordinate convention.
     *
     * The selected pose maps moving pixels to fixed pixels. Elastix needs the
     * inverse mapping to locate a moving sample for each fixed position. We invert
     * the pose and apply the source/destination pixel spacings to obtain physical
     * matrix terms and translations before writing the six affine parameters.
     *
     * Args:
     *     movingToFixed: Selected moving-to-fixed pixel-coordinate pose.
     *     fixed, moving: Prepared image-view descriptions including native X/Y pixel sizes.
     *
     * Returns:
     *     Elastix initial-transform text for the corresponding fixed-to-moving map.
     */
    static String initialFromPose(AffineTransform3D movingToFixed,Map fixed,Map moving) {
        List parameters=PairedGeometryOps.initialPhysicalAffine(movingToFixed,
            fixed.nativeX as double,fixed.nativeY as double,
            moving.nativeX as double,moving.nativeY as double).toList()
        return "(Transform \"AffineTransform\")\n(NumberOfParameters 6)\n(TransformParameters "+parameters.join(" ")+")\n"+
            "(CenterOfRotationPoint 0 0)\n(InitialTransformParameterFileName \"NoInitialTransform\")\n"+
            "(HowToCombineTransforms \"Compose\")\n(FixedImageDimension 2)\n(MovingImageDimension 2)\n"
    }
    /**
     * Reads a reduced image view and prepares one intensity channel for registration.
     *
     * We choose downsample = max(1, max(image_width,image_height)/limit), then read
     * the full image extent at that scale. A selected channel supplies its values;
     * combined RGB contrast uses -log(max(1,mean_RGB)/255), and combined non-RGB
     * contrast uses the channel mean.
     *
     * We sort the resulting values and use positions 0.5% and 99.5% through the
     * ordered array as low/high contrast limits. Each intensity becomes
     * max(0,min(1,(value-low)/(high-low))). This gives the optimizer a comparable
     * contrast range while retaining the physical spacing of the reduced view.
     *
     * Args:
     *     server: Image server to read with measured X/Y pixel sizes.
     *     channel: Selected zero-based channel, or -1 for combined contrast.
     *     limit: Maximum reduced-view dimension, from 128 through 4096 pixels.
     *
     * Returns:
     *     Float intensity array, view dimensions, downsample and reduced/native
     *     physical pixel sizes. Insufficient contrast or nonfinite values stop the fit.
     */
    static Map frame(server,int channel,int limit) {
        def calibration=server.pixelCalibration
        double xum=calibration.pixelWidthMicrons,yum=calibration.pixelHeightMicrons
        if(!Double.isFinite(xum)||!Double.isFinite(yum)||xum<=0||yum<=0)
            throw new IllegalArgumentException("Both images need their measured pixel size before alignment.")
        if(limit<128||limit>4096)throw new IllegalArgumentException("Alignment views must be between 128 and 4096 pixels wide.")
        double ds=Math.max(1d,Math.max(server.width,server.height)/(double)limit)
        def pixels=server.readRegion(RegionRequest.createInstance(server.path,ds,0,0,server.width,server.height))
        int w=pixels.width,h=pixels.height,n=pixels.raster.numBands
        if((long)w*h>(long)(limit+1)*(limit+1))throw new IllegalArgumentException("The image reader exceeded the alignment view size.")
        if(channel < -1 || channel>=n)throw new IllegalArgumentException("The selected alignment channel is not present in this image.")
        float[] values=RegistrationImageViews.intensities(pixels,server.isRGB(),channel)
        return [values:values,width:w,height:h,spacingX:ds*xum,spacingY:ds*yum,downsample:ds,nativeX:xum,nativeY:yum]
    }
    /**
     * Writes the prepared view in the image format read by Elastix.
     *
     * The header records two-dimensional shape, physical pixel spacing and a
     * zero physical origin. We store intensity views as little-endian float32
     * values and tissue masks as bytes. Intensity rows are written using one
     * reused row buffer, avoiding another full image-sized byte array.
     *
     * Args:
     *     path: Temporary .mha filename.
     *     frame: Prepared view values, dimensions and physical spacing.
     *     mask: Optional per-pixel binary mask; null writes the intensity values.
     */
    static void writeMha(Path path,Map frame,byte[] mask=null) {
        String type=mask==null?"MET_FLOAT":"MET_UCHAR"
        String header="ObjectType = Image\nNDims = 2\nBinaryData = True\nBinaryDataByteOrderMSB = False\n"+
            "TransformMatrix = 1 0 0 1\nOffset = 0 0\nElementSpacing = "+frame.spacingX+" "+frame.spacingY+"\n"+
            "DimSize = "+frame.width+" "+frame.height+"\nElementType = "+type+"\nElementDataFile = LOCAL\n"
        path.toFile().withOutputStream {stream ->
            stream.write(header.getBytes("US-ASCII"))
            if(mask!=null)stream.write(mask)
            else {
                RegistrationImageViews.writeFloatRows(stream,frame.values as float[],
                    frame.width as int,frame.height as int)
            }
        }
    }
    /**
     * Converts selected fixed-image tissue geometry into a mask at the registration-view resolution.
     *
     * We scale full-resolution tissue coordinates by the reciprocal downsample,
     * fill the shape and convert covered pixels to one. At least 256 pixels must
     * remain covered at this resolution; otherwise the selected tissue is too
     * small to supply the intended registration mask.
     *
     * Args:
     *     fixed: Prepared fixed-image view and downsample.
     *     geometry: Selected full-resolution tissue geometry, or null for no mask.
     *     directory: Working folder for this registration attempt.
     *
     * Returns:
     *     Mask .mha path, or null when no tissue restriction was selected.
     */
    static Path maskFile(Map fixed,geometry,Path directory) {
        if(geometry==null)return null
        byte[] mask=RegistrationImageViews.tissueMask(geometry,fixed.width as int,
            fixed.height as int,fixed.downsample as double)
        Path path=directory.resolve("fixed-mask.mha");
        writeMha(path,fixed,mask);
        return path
    }
    /**
     * Creates the numerical settings for the three-resolution affine optimization.
     *
     * We compare image intensities with Mattes mutual information using 32 bins
     * and 4096 random-coordinate samples, renewed at each update. Elastix applies
     * adaptive stochastic gradient descent from coarse to fine resolutions with
     * schedules 4, 2 and 1. Automatic parameter scaling accounts for different
     * numerical effects of affine matrix terms and physical translations.
     *
     * The selected transformation is affine. BSplineInterpolator specifies how
     * moving-image intensity is sampled during that fit; it does not select a
     * deformable B-spline transformation grid. We retain transform parameters and
     * logs instead of requesting an additional resampled output image.
     *
     * Args:
     *     iterations: Maximum optimizer updates at each resolution, from 32 through 2048.
     *
     * Returns:
     *     Complete Elastix parameter-file text for this affine fitting procedure.
     */
    static String parameters(int iterations, String model='affine') {
        if(iterations<32||iterations>2048)throw new IllegalArgumentException("Choose 32 to 2048 alignment updates per resolution.")
        if(!(model in ['affine','rigid']))throw new IllegalArgumentException('Select affine or rigid alignment.')
        return """(FixedImageDimension 2)
(MovingImageDimension 2)
(FixedInternalImagePixelType "float")
(MovingInternalImagePixelType "float")
(UseDirectionCosines "true")
(Registration "MultiResolutionRegistration")
(FixedImagePyramid "FixedSmoothingImagePyramid")
(MovingImagePyramid "MovingSmoothingImagePyramid")
(Interpolator "BSplineInterpolator")
(ResampleInterpolator "FinalBSplineInterpolator")
(Resampler "DefaultResampler")
(Optimizer "AdaptiveStochasticGradientDescent")
(Transform "${model=='rigid'?'EulerTransform':'AffineTransform'}")
(Metric "AdvancedMattesMutualInformation")
(AutomaticScalesEstimation "true")
(AutomaticTransformInitialization "false")
(HowToCombineTransforms "Compose")
(NumberOfResolutions 3)
(ImagePyramidSchedule 4 4 2 2 1 1)
(MaximumNumberOfIterations ${iterations})
(NumberOfHistogramBins 32)
(NumberOfSpatialSamples 4096)
(NewSamplesEveryIteration "true")
(ImageSampler "RandomCoordinate")
(MaximumNumberOfSamplingAttempts 8)
(CheckNumberOfSamples "true")
(RandomSeed 271828)
(FinalBSplineInterpolationOrder 1)
(DefaultPixelValue 0)
(WriteResultImage "false")
"""
    }
    /**
     * Runs one registration program and retains its combined output in a log file.
     *
     * We wait for the process within the specified time limit and require a zero
     * exit status. On timeout or failure, the remaining child processes are stopped
     * before returning the error, so the attempt does not retain active fitting work.
     *
     * Args:
     *     command: Program and ordered arguments, passed directly to ProcessBuilder.
     *     log: File receiving standard output and error text.
     *     seconds: Maximum process duration before cancellation is requested.
     */
    static void process(List<String> command,Path log,int seconds) {
        Process child=new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(log.toFile()).start()
        try {
            if(!child.waitFor(seconds,TimeUnit.SECONDS))throw new IOException("Automatic alignment exceeded its time limit. Use fewer image pixels or landmark alignment.")
            if(child.exitValue()!=0)throw new IOException("Elastix could not align these images. Review the selected channels and tissue coverage. Log: "+log)
        } finally {
            if(child.isAlive()){
                child.descendants().forEach {it.destroy()};
                child.destroy()
                if(!child.waitFor(5,TimeUnit.SECONDS)){child.descendants().forEach {it.destroyForcibly()};
                    child.destroyForcibly();
                    child.waitFor()}
            }
        }
    }
    /**
     * Creates a physical-coordinate starting rotation with the two image centres aligned.
     *
     * We calculate each reduced view's centre from its pixel dimensions and
     * physical spacing. The difference between moving and fixed centres gives
     * the initial translation; the selected angle is applied around the fixed centre.
     *
     * Args:
     *     fixed, moving: Prepared image-view dimensions and physical pixel sizes.
     *     angle: Starting rotation in radians.
     *
     * Returns:
     *     Elastix Euler-transform text used to initialize the subsequent affine fit.
     */
    static String initialTransform(Map fixed,Map moving,double angle) {
        def start=PairedGeometryOps.initialEuler(fixed.width as int,fixed.height as int,
            fixed.spacingX as double,fixed.spacingY as double,moving.width as int,moving.height as int,
            moving.spacingX as double,moving.spacingY as double)
        double fx=start.centreX(),fy=start.centreY(),tx=start.tx(),ty=start.ty()
        return "(Transform \"EulerTransform\")\n(NumberOfParameters 3)\n(TransformParameters "+angle+" "+tx+" "+ty+")\n"+
            "(CenterOfRotationPoint "+fx+" "+fy+")\n(InitialTransformParameterFileName \"NoInitialTransform\")\n"+
            "(HowToCombineTransforms \"Compose\")\n(FixedImageDimension 2)\n(MovingImageDimension 2)\n"
    }
    /**
     * Fits the paired image to the fixed image and returns a checked moving-to-fixed pixel transform.
     *
     * We perform the calculation in four steps:
     *
     * i) Prepare reduced intensity views and the optional fixed-tissue mask, then
     * write their physical spacing and the selected affine parameters for Elastix.
     *
     * ii) Fit from the supplied pose, or compare starts at 0, 90, 180 and 270 degrees
     * when orientation search is selected. Each trial performs the full three-level
     * optimization. We retain the successful trial with the lowest recorded metric.
     *
     * iii) Use Transformix to map six fixed physical points. The origin and two
     * axis points determine the affine matrix and translation. Three additional
     * points check that this reconstructed affine reproduces the fitted map within
     * 0.01 moving-image pixels.
     *
     * iv) Convert the fixed-to-moving physical transform to pixel units using
     * both images' X/Y calibration, check its positive noncollapsed determinant,
     * and invert it for moving-to-fixed QuPath coordinates.
     *
     * Args:
     *     fixedServer: Fixed mIF image server.
     *     movingServer: Paired-image server to align.
     *     options: Selected channels, view size, iteration limit, threads and starting rotations.
     *     tools: Folder containing the Elastix and Transformix programs.
     *     directory: Separate working folder for this fitting attempt.
     *     geometry: Optional fixed-image tissue mask in full-resolution coordinates.
     *     initialPose: Optional manually selected moving-to-fixed affine pose.
     *
     * Returns:
     *     Transform, selected metric, initial angle, point-check error and view
     *     sizes/downsamples. The metric describes fitted image similarity; landmark
     *     correspondence and tissue appearance are inspected during the later review.
     */
    static Map align(fixedServer,movingServer,Map options,Path tools,Path directory,geometry,AffineTransform3D initialPose=null) {
        int threads=(options.threads as Number).intValue()
        if(threads<1||threads>16)throw new IllegalArgumentException("Choose between one and sixteen alignment threads.")
        String suffix=System.getProperty("os.name").toLowerCase().contains("win")?".exe":""
        Path elastix=tools.resolve("elastix"+suffix),transformix=tools.resolve("transformix"+suffix)
        if(!Files.isExecutable(elastix)||!Files.isExecutable(transformix))throw new IllegalArgumentException("Select the folder containing the Elastix and transformix programs.")
        Files.createDirectories(directory)
        Map fixed=frame(fixedServer,(options.fixed_channel as Number).intValue(),(options.max_dimension as Number).intValue())
        Map moving=frame(movingServer,(options.moving_channel as Number).intValue(),(options.max_dimension as Number).intValue())
        Path fixedFile=directory.resolve("fixed.mha"),movingFile=directory.resolve("moving.mha")
        writeMha(fixedFile,fixed);
        writeMha(movingFile,moving)
        Path mask=maskFile(fixed,geometry,directory)
        Path settings=directory.resolve("parameters.txt");
        String model=options.transform_model ?: 'affine'
        Files.writeString(settings,parameters((options.iterations as Number).intValue(),model))
        List angles=(initialPose==null && options.orientation_search) ? [0d,Math.PI/2d,Math.PI,3d*Math.PI/2d] : [0d]
        List trials=[]
        for(int i=0;i<angles.size();i++){
            Path trial=directory.resolve("orientation-"+i);
            Files.createDirectory(trial)
            Path initial=trial.resolve("initial.txt");
            Files.writeString(initial,initialPose==null ? initialTransform(fixed,moving,angles[i] as double) : initialFromPose(initialPose,fixed,moving))
            List<String> command=[elastix.toString(),"-f",fixedFile.toString(),"-m",movingFile.toString(),"-out",trial.toString(),
                "-p",settings.toString(),"-t0",initial.toString(),"-threads",threads.toString()]
            if(mask!=null)command.addAll(["-fMask",mask.toString()])
            try {
                process(command,trial.resolve("process.log"),600)
                String log=Files.readString(trial.resolve("elastix.log"))
                def matcher=log=~/Final metric value\s*=\s*([-+0-9.eE]+)/
                if(!matcher.find())throw new IOException("The registration did not report a final image-similarity measure.")
                double metric=Double.parseDouble(matcher.group(1))
                if(!Double.isFinite(metric))throw new IOException("The registration similarity is not finite.")
                trials.add([angle:angles[i],metric:metric,parameters:trial.resolve("TransformParameters.0.txt")])
            } catch(IOException error) {if(angles.size()==1)throw error}
        }
        if(trials.isEmpty())throw new IOException("None of the starting orientations aligned successfully. Use another alignment channel or landmarks.")
        Map best=trials.min {it.metric}
        List probes=PairedGeometryOps.affineProbes(fixed.width as int,fixed.height as int,
            fixed.spacingX as double,fixed.spacingY as double)
        Path points=directory.resolve("points.txt");
        Files.writeString(points,"point\n"+probes.size()+"\n"+probes.collect {it.join(" ")}.join("\n")+"\n")
        Path transformed=directory.resolve("points-result");
        Files.createDirectory(transformed)
        process([transformix.toString(),"-def",points.toString(),"-tp",best.parameters.toString(),"-out",transformed.toString(),"-threads",threads.toString()],transformed.resolve("process.log"),120)
        List mapped=[]
        Files.readAllLines(transformed.resolve("outputpoints.txt")).each {line ->
            def matcher=line=~/OutputPoint\s*=\s*\[([^]]+)\]/
            if(matcher.find())mapped.add(matcher.group(1).trim().split(/\s+/).collect {Double.parseDouble(it)})
        }
        if(mapped.size()!=probes.size()||mapped.any {it.size()!=2||it.any {!Double.isFinite(it)}})
            throw new IOException("Transformix returned incomplete coordinate checks.")
        def checked=PairedGeometryOps.affineFromProbes(probes,mapped,fixed.nativeX as double,
            fixed.nativeY as double,moving.nativeX as double,moving.nativeY as double)
        return [transform:checked.transform(),metric:best.metric,startingAngleRadians:best.angle,
            independentPointErrorPixels:checked.maximumErrorPixels(),maximumThumbnailPixels:Math.max(fixed.width*fixed.height,moving.width*moving.height),
            fixedDownsample:fixed.downsample,movingDownsample:moving.downsample,method:"elastix-multiresolution-"+model]
    }
}
return [align:AutomaticElastix.&align,frame:AutomaticElastix.&frame,parameters:AutomaticElastix.&parameters,similarity:AutomaticElastix.&similarity,initialFromPose:AutomaticElastix.&initialFromPose,writeMha:AutomaticElastix.&writeMha,maskFile:AutomaticElastix.&maskFile,process:AutomaticElastix.&process]
