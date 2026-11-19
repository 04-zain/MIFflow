/**
 * Fits local B-spline deformation after the global affine has aligned the image pair.
 *
 * We read finer reduced views of the original images and pass the physical
 * global affine to Elastix as the initial transform. Elastix optimizes a cubic
 * control grid from coarse to fine resolution. The saved result retains its
 * fitted coefficients and calibration so QuPath can sample original paired-image
 * pixels directly at any displayed resolution.
 */
import java.nio.file.Path
import java.nio.file.Files
import net.imglib2.realtransform.AffineTransform3D
import org.locationtech.jts.geom.Coordinate
import org.mif.registration.PairedGeometryOps

class AutomaticBSpline {
    /**
     * Creates the three-resolution B-spline fitting parameters from the selected settings.
     *
     * We minimize a weighted combination of Mattes mutual information and
     * bending energy. The latter penalizes rapid spatial changes in deformation.
     * Relative gradient weighting gives bending_weight its meaning relative to
     * the image metric rather than to the arbitrary physical unit of its derivative.
     *
     * The final control-point spacing is specified in the same physical units
     * as the image views: micrometres. Grid-spacing factors 4, 2 and 1 successively
     * introduce finer local displacement parameters. Moving intensity at each
     * proposed location is evaluated with cubic B-spline interpolation.
     *
     * Args:
     *     settings: Refinement iterations, physical grid spacing and relative
     *         bending weight selected for this image pair.
     *
     * Returns:
     *     Elastix parameter text for the deformable stage.
     */
    static String parameters(Map settings) {
        int iterations=(settings.iterations as Number).intValue()
        double grid=(settings.grid_spacing_um as Number).doubleValue()
        double bending=(settings.bending_weight as Number).doubleValue()
        if(iterations<32 || iterations>2048 || !Double.isFinite(grid) || grid<=0d ||
           !Double.isFinite(bending) || bending<0d || bending>1d)
            throw new IllegalArgumentException("Use 32–2048 refinement updates, a positive control-point spacing and a bending weight from 0 to 1.")
        return """(FixedImageDimension 2)
(MovingImageDimension 2)
(FixedInternalImagePixelType "float")
(MovingInternalImagePixelType "float")
(UseDirectionCosines "true")
(Registration "MultiMetricMultiResolutionRegistration")
(FixedImagePyramid "FixedSmoothingImagePyramid")
(MovingImagePyramid "MovingSmoothingImagePyramid")
(Interpolator "BSplineInterpolator")
(BSplineInterpolationOrder 3)
(ResampleInterpolator "FinalBSplineInterpolator")
(FinalBSplineInterpolationOrder 1)
(Resampler "DefaultResampler")
(Optimizer "AdaptiveStochasticGradientDescent")
(Transform "BSplineTransform")
(BSplineTransformSplineOrder 3)
(UseCyclicTransform "false")
(HowToCombineTransforms "Compose")
(Metric "AdvancedMattesMutualInformation" "TransformBendingEnergyPenalty")
(UseRelativeWeights "true")
(Metric0RelativeWeight 1.0)
(Metric1RelativeWeight ${bending})
(NumberOfResolutions 3)
(ImagePyramidSchedule 4 4 2 2 1 1)
(GridSpacingSchedule 4.0 2.0 1.0)
(FinalGridSpacingInPhysicalUnits ${grid} ${grid})
(MaximumNumberOfIterations ${iterations})
(NumberOfHistogramBins 32)
(NumberOfSpatialSamples 4096)
(NewSamplesEveryIteration "true")
(ImageSampler "RandomCoordinate")
(MaximumNumberOfSamplingAttempts 8)
(CheckNumberOfSamples "true")
(RandomSeed 271828)
(DefaultPixelValue 0)
(WriteResultImage "false")
"""
    }

    /**
     * Reads named values from an Elastix transform-parameter file.
     *
     * Each noncomment line contains one parenthesized field. Quoted paths and
     * unquoted numerical values are retained as individual strings until their
     * specific grid or transform checks are performed. Repeated fields are
     * rejected so a later entry cannot replace an earlier grid definition.
     *
     * Args:
     *     file: TransformParameters file written by the current fitting attempt.
     *
     * Returns:
     *     Map from field names to their ordered value lists.
     */
    static Map readParameters(Path file) {
        if(!Files.isRegularFile(file) || Files.size(file)>16*1024*1024)
            throw new IllegalArgumentException("The fitted transform parameter file is missing or too large.")
        Map result=[:]
        Files.readAllLines(file).each {line ->
            String text=line.trim()
            if(!text || text.startsWith("//"))return
            def match=text=~/^\(\s*([A-Za-z0-9_]+)\s+(.*?)\s*\)\s*(?:\/\/.*)?$/
            if(!match.matches())throw new IllegalArgumentException("Cannot read the fitted Elastix parameter line: "+text.take(100))
            String name=match.group(1)
            if(result.containsKey(name))throw new IllegalArgumentException("The fitted transform repeats "+name)
            List values=[]
            def tokens=match.group(2)=~/"([^"]*)"|([^\s]+)/
            while(tokens.find())values.add(tokens.group(1)!=null ? tokens.group(1) : tokens.group(2))
            result[name]=values
        }
        return result
    }

    /**
     * Converts the fitted grid into the portable coefficient record used for display and annotation transfer.
     *
     * The selected global affine is written as one physical transform before
     * this fit. We require the returned B-spline file to refer to that same
     * initial file and use Compose, cubic order and noncyclic coefficients.
     * Saving the complete affine and grid values removes temporary file-path
     * dependencies from the accepted transform.
     *
     * Args:
     *     file: Fitted B-spline parameter file.
     *     initial: Initial affine parameter file created for this fit.
     *     fixed, moving: Prepared views containing native X/Y pixel calibration.
     *     settings: Maximum control-point count and numerical inverse settings.
     *
     * Returns:
     *     JSON-compatible coefficient record for paired_geometry.bspline().
     */
    static Map coefficientRecord(Path file,Path initial,Map fixed,Map moving,Map settings) {
        Map p=readParameters(file),a=readParameters(initial)
        // Elastix writes the host platform's path separators. Compare the
        // absolute paths so a Windows backslash does not invalidate the same file.
        if(p.Transform!=["BSplineTransform"] || p.HowToCombineTransforms!=["Compose"] ||
           p.BSplineTransformSplineOrder!=["3"] || p.UseCyclicTransform==["true"] ||
           p.FixedImageDimension!=["2"] || p.MovingImageDimension!=["2"] ||
           !(p.InitialTransformParameterFileName instanceof List) ||
           p.InitialTransformParameterFileName.size()!=1 ||
           !Path.of(p.InitialTransformParameterFileName[0]).isAbsolute() ||
           Path.of(p.InitialTransformParameterFileName[0]).toAbsolutePath().normalize()!=initial.toAbsolutePath().normalize())
            throw new IllegalArgumentException("The fitted parameters do not describe the requested affine-plus-cubic-B-spline transform.")
        if(a.Transform!=["AffineTransform"] || a.CenterOfRotationPoint!=["0","0"] ||
           a.InitialTransformParameterFileName!=["NoInitialTransform"])
            throw new IllegalArgumentException("The initial physical affine has changed.")
        Closure numbers={String name,int count ->
            List raw=p[name]
            if(raw==null || (count>=0 && raw.size()!=count))throw new IllegalArgumentException("The fitted transform is missing "+name)
            List values=raw.collect {Double.parseDouble(it)}
            if(!values.every {Double.isFinite(it)})throw new IllegalArgumentException("The fitted "+name+" contains nonfinite values.")
            values
        }
        List size=numbers("GridSize",2)
        if(size.any {it!=Math.rint(it) || it<4d} || size[0]*size[1]>(settings.maximum_control_points as int))
            throw new IllegalArgumentException("The fitted control grid exceeds the selected point limit.")
        int count=(size[0]*size[1]) as int
        List coefficients=numbers("TransformParameters",2*count)
        if(p.NumberOfParameters!=[(2*count).toString()])throw new IllegalArgumentException("The fitted coefficient count differs from its control grid.")
        return [type:"MifElastixBSplineTransform",schema:"mif-elastix-bspline/v1",direction:"moving-to-fixed",
            fixed_pixel_size_um:[fixed.nativeX,fixed.nativeY],moving_pixel_size_um:[moving.nativeX,moving.nativeY],
            initial_fixed_to_moving_physical:a.TransformParameters.collect {Double.parseDouble(it)},
            grid:[size:size,index:numbers("GridIndex",2),origin:numbers("GridOrigin",2),
                  spacing:numbers("GridSpacing",2),direction:numbers("GridDirection",4)],
            coefficients:coefficients,bspline_order:3,
            inverse:[tolerance_pixels:settings.inverse_tolerance_pixels,max_iterations:settings.inverse_max_iterations]]
    }

    /**
     * Checks the fitted map against Transformix at independent fixed-image positions.
     *
     * We submit physical points to Transformix, then compare its moving locations
     * with direct evaluation of the saved coefficient record. The resulting
     * error is expressed in moving-image pixels and must stay below 0.01 pixels.
     * This verifies coefficient order, transform composition and physical-unit
     * conversion independently of the image-similarity objective.
     */
    static double checkCoordinates(transform,Map fixed,Map moving,Path parameters,Path tools,Path directory,int threads,Map algorithm) {
        List points=PairedGeometryOps.bsplineProbes(fixed.width as int,fixed.height as int,
            fixed.spacingX as double,fixed.spacingY as double)
        Path input=directory.resolve("coordinate-check.txt"),out=directory.resolve("coordinate-check")
        Files.createDirectories(out)
        Files.writeString(input,"point\n"+points.size()+"\n"+points.collect {it.join(" ")}.join("\n")+"\n")
        String suffix=System.getProperty("os.name").toLowerCase().contains("win")?".exe":""
        algorithm.process([tools.resolve("transformix"+suffix).toString(),"-def",input.toString(),"-tp",parameters.toString(),"-out",out.toString(),"-threads",threads.toString()],out.resolve("process.log"),120)
        List expected=[]
        Files.readAllLines(out.resolve("outputpoints.txt")).each {line ->
            def match=line=~/OutputPoint\s*=\s*\[([^]]+)\]/
            if(match.find())expected.add(match.group(1).trim().split(/\s+/).collect {Double.parseDouble(it)})
        }
        if(expected.size()!=points.size() || expected.any {it.size()!=2 || !it.every {Double.isFinite(it)}})
            throw new IllegalArgumentException("Transformix returned incomplete coordinate checks.")
        return PairedGeometryOps.checkBsplineProbes(transform,points,expected,fixed.nativeX as double,
            fixed.nativeY as double,moving.nativeX as double,moving.nativeY as double)
    }

    /**
     * Fits and checks the local cubic B-spline after a successful global affine.
     *
     * We read finer views directly from the original images and write their
     * physical spacing with the selected tissue mask. Elastix uses the global
     * affine as its initial transform and refines the B-spline grid through
     * three resolutions. The result is checked against Transformix before
     * sampled local-area, displacement and inverse tests are applied.
     *
     * Args:
     *     fixedServer, movingServer: Original calibrated image servers.
     *     global: Previously fitted moving-to-fixed affine.
     *     options: Selected refinement view, iterations, grid spacing, bending
     *         weight, displacement limit, inverse settings and control-point limit.
     *     tools: Folder containing Elastix and Transformix.
     *     directory: New working folder for this refinement attempt.
     *     tissue: Optional accepted fixed-tissue geometry restricting metric samples.
     *     algorithm: Shared image-view, physical-affine and process functions.
     *     geometry: Shared exact coefficient constructor and deformation checks.
     *
     * Returns:
     *     Proposed transform, method, fitted metric, coefficient/grid description
     *     and checked coordinate/deformation results for review.
     */
    static Map align(fixedServer,movingServer,AffineTransform3D global,Map options,Path tools,Path directory,tissue,Map algorithm,Map geometry) {
        int threads=(options.threads as Number).intValue(),limit=(options.max_dimension as Number).intValue()
        int maximum=(options.maximum_control_points as Number).intValue()
        double spacing=(options.grid_spacing_um as Number).doubleValue()
        double maxShift=(options.maximum_displacement_um as Number).doubleValue()
        if(threads<1 || threads>16 || maximum<16 || maximum>262144 || !Double.isFinite(maxShift) || maxShift<=0d)
            throw new IllegalArgumentException("Check the B-spline thread, control-point and displacement settings.")
        String parameterText=parameters(options)
        String suffix=System.getProperty("os.name").toLowerCase().contains("win")?".exe":""
        if(!Files.isExecutable(tools.resolve("elastix"+suffix)) || !Files.isExecutable(tools.resolve("transformix"+suffix)))
            throw new IllegalArgumentException("Select the folder containing Elastix and Transformix.")
        Map fixed=algorithm.frame(fixedServer,(options.fixed_channel as int),limit)
        Map moving=algorithm.frame(movingServer,(options.moving_channel as int),limit)
        double estimated=(Math.ceil((fixed.width-1)*fixed.spacingX/spacing)+5d)*(Math.ceil((fixed.height-1)*fixed.spacingY/spacing)+5d)
        if(estimated>maximum)throw new IllegalArgumentException("The selected B-spline grid has too many control points. Increase its spacing in µm.")
        Files.createDirectories(directory)
        Path f=directory.resolve("fixed.mha"),m=directory.resolve("moving.mha")
        algorithm.writeMha(f,fixed);
        algorithm.writeMha(m,moving)
        Path mask=algorithm.maskFile(fixed,tissue,directory)
        Path initial=directory.resolve("initial-affine.txt"),settings=directory.resolve("bspline-parameters.txt")
        Files.writeString(initial,algorithm.initialFromPose(global,fixed,moving))
        Files.writeString(settings,parameterText)
        Path output=directory.resolve("fitted");
        Files.createDirectories(output)
        List<String> command=[tools.resolve("elastix"+suffix).toString(),"-f",f.toString(),"-m",m.toString(),
            "-out",output.toString(),"-p",settings.toString(),"-t0",initial.toString(),"-threads",threads.toString()]
        if(mask!=null)command.addAll(["-fMask",mask.toString()])
        algorithm.process(command,output.resolve("process.log"),900)
        Path fitted=output.resolve("TransformParameters.0.txt")
        Map record=coefficientRecord(fitted,initial,fixed,moving,options)
        def transform=geometry.bspline(record)
        double error=checkCoordinates(transform,fixed,moving,fitted,tools,directory,threads,algorithm)
        Map quality=geometry.checkDeformation(transform,global,fixedServer,tissue,maxShift)
        def metricMatcher=Files.readString(output.resolve("elastix.log"))=~/Final metric value\s*=\s*([-+0-9.eE]+)/
        if(!metricMatcher.find())throw new IllegalArgumentException("The B-spline fit did not report its final metric.")
        double metric=Double.parseDouble(metricMatcher.group(1))
        if(!Double.isFinite(metric))throw new IllegalArgumentException("The B-spline fit returned a nonfinite metric.")
        return [transform:transform,method:"elastix-affine-bspline",metric:metric,
            independentPointErrorPixels:error,controlPoints:record.grid.size[0]*record.grid.size[1],
            gridSpacingUm:record.grid.spacing,fixedDownsample:fixed.downsample,movingDownsample:moving.downsample,
            maximumViewPixels:Math.max(fixed.width*fixed.height,moving.width*moving.height),qc:quality]
    }
}
return [align:AutomaticBSpline.&align,parameters:AutomaticBSpline.&parameters,readParameters:AutomaticBSpline.&readParameters]
