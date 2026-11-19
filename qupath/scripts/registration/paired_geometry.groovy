/**
 * Fits, evaluates and saves the coordinate transforms used to align a paired image with mIF.
 *
 * We expose moving-to-fixed coordinates for annotation transfer and the inverse
 * fixed-to-moving map for sampling the original paired image. Global affine,
 * manual local TPS and automatic local B-spline results use the same image
 * calibration and review helpers before a transform is accepted.
 *
 * The automatic B-spline reader retains Elastix's coefficient grid and physical
 * affine. A viewport request evaluates only the coefficients needed at its
 * requested locations. The original images remain the source of pixel values.
 */
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.charset.StandardCharsets
import net.imglib2.realtransform.AffineTransform3D
import org.mif.registration.MifElastixBSplineTransform
import org.mif.registration.PairedGeometryOps
import net.imglib2.realtransform.RealTransform
import net.imglib2.realtransform.InvertibleRealTransform
import qupath.ext.imagecombinerwarpy.realtransform.InvertibleWrapped2DTransformAs3D
import qupath.ext.warpy.Warpy
import qupath.ext.imagecombinerwarpy.realtransform.RealTransformSerializer
import qupath.ext.imagecombinerwarpy.gui.RealTransformInterpolation
import qupath.ext.imagecombinerwarpy.gui.RealTransformImageServer
import qupath.lib.images.servers.ImageServerMetadata

/**
 * Provides the shared fitting, sampling, saving and review checks for the selected image pair.
 *
 * We use the same moving-to-fixed convention for landmark and automatic
 * transforms. Their inverse is passed to the paired-image reader, while the
 * forward map transfers coordinates into the fixed mIF analysis.
 */
class PairedGeometry {
    /** Identify uncovered display pixels at RealTransformImageServer's exact sample positions. */
    @groovy.transform.CompileStatic
    static boolean[] uncoveredPixels(InvertibleRealTransform transform,int x,int y,double downsample,
            int columns,int rows,int movingWidth,int movingHeight) {
        boolean[] outside=new boolean[Math.multiplyExact(columns,rows)]
        InvertibleRealTransform inverse=transform.inverse()
        double[] fixed=new double[3],moving=new double[3]
        for(int row=0;row<rows;row++) {
            if(Thread.currentThread().isInterrupted())throw new java.util.concurrent.CancellationException()
            fixed[1]=y+row*downsample
            for(int column=0;column<columns;column++) {
                fixed[0]=x+column*downsample
                inverse.apply(fixed,moving)
                outside[row*columns+column]=!Double.isFinite(moving[0]) || !Double.isFinite(moving[1]) || !Double.isFinite(moving[2]) ||
                    moving[0]<0d || moving[1]<0d || moving[0]>=movingWidth || moving[1]>=movingHeight
            }
        }
        return outside
    }

    /** Select accepted tissue by its saved identity and review metadata, independent of display class. */
    static boolean acceptedTissue(object) {
        if (!object.isAnnotation() || object.getROI() == null) return false
        Map metadata = object.getMetadata()
        metadata.get("pipeline_stage") == "tissue_detection" &&
            metadata.get("generation_status") == "accepted" &&
            metadata.get("review_status") == "accepted" &&
            ["generation_id", "section_id", "roi_id"].every { metadata.get(it) }
    }

    /** Sample the composed transform at the same positions used when accepting it. */
    static Map reviewSampling(image,movingServer,InvertibleRealTransform transform) {
        def fixed=image.server
        double scale=fixed.pixelCalibration.pixelWidthMicrons*fixed.pixelCalibration.pixelHeightMicrons /
            (movingServer.pixelCalibration.pixelWidthMicrons*movingServer.pixelCalibration.pixelHeightMicrons)
        Closure sample={geometry,double x,double y,double w,double h ->
            org.mif.registration.AlignmentSampling.evaluate(geometry,x,y,w,h,128d,transform,
                movingServer.width,movingServer.height,scale,true)
        }
        List scopes=[[name:"Whole image",geometry:null,result:sample(null,0d,0d,fixed.width as double,fixed.height as double)]]
        image.hierarchy.annotationObjects.findAll {acceptedTissue(it)}.eachWithIndex {object,index ->
            def roi=object.ROI
            scopes.add([name:object.name?:"Tissue region "+(index+1),geometry:roi.geometry,
                result:sample(roi.geometry,roi.boundsX,roi.boundsY,roi.boundsWidth,roi.boundsHeight)])
        }
        return [scopes:scopes,fixedWidth:fixed.width,fixedHeight:fixed.height,
            movingWidth:movingServer.width,movingHeight:movingServer.height,transform:transform]
    }

    /**
     * Reopens a saved transform for original-image sampling or annotation transfer.
     *
     * We parse the JSON with duplicate-field detection. A saved pipeline B-spline
     * is reconstructed from its physical coefficient record; other current
     * Warpy-compatible transforms are read through Warpy's transform reader.
     * The resulting map must provide an inverse for fixed-to-moving pixel queries.
     *
     * Args:
     *     file: Regular local transform JSON file, at most 16 MiB and not a symbolic link.
     *
     * Returns:
     *     InvertibleRealTransform used by the viewer or coordinate-transfer operation.
     *
     * Raises:
     *     IllegalArgumentException: The file is unsuitable or its transform
     *         cannot supply the required inverse. File/JSON errors propagate
     *         from the corresponding read operation.
     */
    static def load(Path file) {
        if(Files.isSymbolicLink(file) || !Files.isRegularFile(file) || Files.size(file)>16*1024*1024)
            throw new IllegalArgumentException("Select a local Warpy transformation file.")
        def mapper = new com.fasterxml.jackson.databind.ObjectMapper(
            com.fasterxml.jackson.core.JsonFactory.builder()
                .enable(com.fasterxml.jackson.core.StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
        Map stored = mapper.readValue(Files.readAllBytes(file), Map.class)
        def value = stored.type == "MifElastixBSplineTransform" ?
            new MifElastixBSplineTransform(stored) : Warpy.getRealTransform(file.toFile())
        if(!(value instanceof InvertibleRealTransform))throw new IllegalArgumentException("This alignment is not invertible.")
        return value
    }
    /**
     * Saves the current coordinate transform and preserves an earlier fitting result.
     *
     * We encode the pipeline B-spline directly from its physical coefficient
     * record, or use Warpy's serializer for other supported transforms. The JSON
     * is staged and reopened through load() before changing the project files.
     * An earlier transform and fitting description are copied into a fresh
     * registration-history directory. The description is published first and
     * the transform is replaced atomically last. A failed transform publication
     * restores the previous description. The application permits an accepted
     * result to be edited only after its ordinary stage revision.
     *
     * Args:
     *     file: Current transform filename for the selected project image pair.
     *     transform: Completed invertible moving-to-fixed map.
     *     evidence: Landmark and fitting settings associated with that calculation.
     *
     * Raises:
     *     IllegalArgumentException: The reopened transform has incompatible
     *         dimensions. Staging or preservation errors leave the current
     *         transform untouched. Filesystem errors propagate.
     */
    static void save(Path file,InvertibleRealTransform transform,Map evidence) {
        Files.createDirectories(file.parent)
        def gson=RealTransformSerializer.getRealTransformAdapter()
        String encoded = transform instanceof MifElastixBSplineTransform ?
            new com.google.gson.GsonBuilder().setPrettyPrinting().create().toJson(transform.toMap()) :
            gson.toJson(transform, RealTransform.class)
        byte[] bytes=(encoded+"\n").getBytes(StandardCharsets.UTF_8)
        Path staged=Files.createTempFile(file.parent,".alignment-", ".json")
        Path description=file.resolveSibling(file.fileName.toString()+".landmarks.json")
        Path stagedDescription=null,backup=null
        boolean descriptionPublished=false,transformPublished=false
        try {
            Files.write(staged,bytes)
            def reopened=load(staged)
            if(reopened.numSourceDimensions()!=3)throw new IllegalArgumentException("Alignment has the wrong dimensions.")
            stagedDescription=Files.createTempFile(file.parent,".alignment-description-", ".json")
            Files.writeString(stagedDescription,new com.google.gson.Gson().toJson(evidence)+"\n")
            if(Files.exists(file) || Files.exists(description)) {
                Path history=Files.createDirectories(file.parent.resolve("registration-history"))
                backup=Files.createTempDirectory(history,"revision-")
                if(Files.exists(file))Files.copy(file,backup.resolve(file.fileName))
                if(Files.exists(description))Files.copy(description,backup.resolve(description.fileName))
            }
            Files.move(stagedDescription,description,StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE)
            descriptionPublished=true
            Files.move(staged,file,StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE)
            transformPublished=true
        } finally {
            if(descriptionPublished && !transformPublished) {
                if(backup!=null && Files.isRegularFile(backup.resolve(description.fileName))) {
                    Files.copy(backup.resolve(description.fileName),stagedDescription,StandardCopyOption.REPLACE_EXISTING)
                    Files.move(stagedDescription,description,StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE)
                } else Files.deleteIfExists(description)
            }
            Files.deleteIfExists(staged)
            if(stagedDescription!=null)Files.deleteIfExists(stagedDescription)
        }
    }


    /**
     * Checks a manual affine request or a final choice bound to its returned affine.
     *
     * Global input specifies a physical starting pose and image-fitting settings.
     * Final input identifies the reviewed global result and retains original-image
     * coordinates for optional local TPS. The editor can retain fewer than six
     * local pairs in an unfinished draft; execution always requires six.
     *
     * Args:
     *     values: Current manual-alignment/v1 global or final request.
     *     fixed, moving: Original image readers used for coordinate bounds.
     *     unfinished: Permit zero to 256 local pairs in an unsaved draft.
     *
     * Returns:
     *     Independent normalized request with the exact selected stage fields.
     *
     * Raises:
     *     IllegalArgumentException: Stage fields, pose, settings, fit identity,
     *         pair coordinates or displacement limits are invalid.
     */
    static Map validateManualAlignment(Map values,fixed,moving,boolean unfinished=false) {
        if(values==null || values.schema!="manual-alignment/v1")
            throw new IllegalArgumentException("Save the current manual alignment request from the registration editor.")
        if(values.stage=="global") {
            Set required=["schema","stage","starting_pose","use_starting_pose","global_settings","tools_directory"] as Set
            Map pose=values.starting_pose instanceof Map ? values.starting_pose : null
            Map settings=values.global_settings instanceof Map ? values.global_settings : null
            Set poseFields=["x_um","y_um","rotation_degrees","scale"] as Set
            Set settingFields=["fixed_channel","moving_channel","max_dimension","iterations","threads","orientation_search"] as Set
            if(values.keySet()!=required || pose==null || pose.keySet()!=poseFields ||
               !pose.values().every {it instanceof Number && Double.isFinite(it.doubleValue())} ||
               pose.scale<=0.01d || pose.scale>100d || !(values.use_starting_pose instanceof Boolean) ||
               settings==null || settings.keySet()!=settingFields || !(settings.orientation_search instanceof Boolean))
                throw new IllegalArgumentException("Save the complete finite starting pose and image-fitting settings.")
            Map ranges=[fixed_channel:[-1,511],moving_channel:[-1,511],max_dimension:[128,4096],iterations:[32,2048],threads:[1,16]]
            ranges.each {key,range ->
                def value=settings[key]
                if(!(value instanceof Byte || value instanceof Short || value instanceof Integer || value instanceof Long || value instanceof BigInteger) ||
                   value<range[0] || value>range[1])throw new IllegalArgumentException("Check the global affine "+key+" value.")
            }
            def tools=values.tools_directory
            if(tools!=null && (!(tools instanceof String) || !tools.startsWith("/") || tools!=tools.trim() ||
               tools.split("/").contains("..") || tools.toCharArray().any {((int)it)<32}))
                throw new IllegalArgumentException("Use an absolute cluster Elastix folder, or leave it empty.")
            return [schema:"manual-alignment/v1",stage:"global",
                starting_pose:poseFields.collectEntries {[(it):pose[it].doubleValue()]},
                use_starting_pose:values.use_starting_pose,
                global_settings:settingFields.collectEntries {[(it):it=="orientation_search"?settings[it]:settings[it].intValue()]},
                tools_directory:tools]
        }
        Set required=["schema","stage","global_fit_id","global_transform_sha256","local_refinement",
            "local_moving_points","local_fixed_points","maximum_displacement_um"] as Set
        if(values.stage!="final" || values.keySet()!=required ||
           ![values.global_fit_id,values.global_transform_sha256].every {it instanceof String && it==~/[0-9a-f]{64}/} ||
           !(values.local_refinement instanceof Boolean) || !(values.maximum_displacement_um instanceof Number) ||
           !Double.isFinite(values.maximum_displacement_um.doubleValue()) ||
           values.maximum_displacement_um<=0d || values.maximum_displacement_um>1e6d)
            throw new IllegalArgumentException("Review the returned global affine and save the current local-refinement choice.")
        def a=values.local_moving_points,b=values.local_fixed_points
        int minimum=unfinished?0:6
        if(!(a instanceof List) || !(b instanceof List) || a.size()!=b.size() ||
           (values.local_refinement && (a.size()<minimum || a.size()>256)) ||
           (!values.local_refinement && (!a.isEmpty() || !b.isEmpty())))
            throw new IllegalArgumentException("Provide six to 256 local pairs, or retain the global affine with no local pairs.")
        [moving:a,fixed:b].each {role,rows ->
            def server=role=="fixed"?fixed:moving
            if(rows.any {!(it instanceof List) || it.size()!=2 || !it.every {v -> v instanceof Number && Double.isFinite(v.doubleValue())} ||
                it[0]<0d || it[1]<0d || it[0]>server.width || it[1]>server.height} ||
                rows.collect {p -> p.collect {v -> v.doubleValue()}}.toSet().size()!=rows.size())
                throw new IllegalArgumentException("Place unique local landmarks within each original image.")
        }
        return [schema:"manual-alignment/v1",stage:"final",global_fit_id:values.global_fit_id,
            global_transform_sha256:values.global_transform_sha256,local_refinement:values.local_refinement,
            local_moving_points:a.collect {it.collect {v -> v.doubleValue()}},
            local_fixed_points:b.collect {it.collect {v -> v.doubleValue()}},
            maximum_displacement_um:values.maximum_displacement_um.doubleValue()]
    }

    /**
     * Fits the remaining local landmark displacement after applying the global affine.
     *
     * We first transform each original moving landmark into the fixed image using
     * the global affine. Both transformed moving points and original fixed points
     * are multiplied by the fixed X/Y pixel sizes, then a TPS is fitted between
     * these physical-coordinate pairs.
     *
     * The complete forward map is applied in four stages: global affine, conversion
     * from fixed pixels to micrometres, local TPS and conversion back to fixed
     * pixels. The initial global correspondence is therefore retained explicitly.
     *
     * Args:
     *     global: Completed moving-to-fixed AffineTransform3D.
     *     moving: At least six original moving-image landmark coordinates.
     *     fixed: Matching original fixed-image coordinates in the same pair order.
     *     xum: Fixed-image pixel width in µm/pixel.
     *     yum: Fixed-image pixel height in µm/pixel.
     *
     * Returns:
     *     Composed transform, local pair count and fitting-pair RMS in micrometres.
     *
     * Raises:
     *     IllegalArgumentException: Calibration is invalid or the local fitting
     *         pairs cannot define the required transformation.
     */
    static Map refineLandmarks(AffineTransform3D global,List moving,List fixed,double xum,double yum) {
        def local = PairedGeometryOps.fitLandmarks(global, moving, fixed, xum, yum)
        // Warpy's optional wrapper retains its existing saved subtype. All
        // landmark fitting and pixel/physical composition run in compiled Java.
        def wrapped = new InvertibleWrapped2DTransformAs3D(local.transform())
        def result = PairedGeometryOps.composeLandmarks(global, wrapped, xum, yum)
        return [transform:result, method:"global-affine-local-tps",
            landmarkCount:local.landmarkCount(), rmsMicrometres:local.rmsMicrometres()]
    }

    /**
     * Samples local displacement, relative area change and numerical inverse error against the global fit.
     *
     * We place a 17-by-17 grid across the fixed-image extent and retain points
     * covered by the optional tissue geometry. For fixed point x, let m=G^-1(x)
     * and q=T(m), where G is the global affine and T the complete proposed map.
     * The calibrated distance from q to x measures added local displacement.
     * Applying T^-1 to q measures the return error in moving-image pixels.
     *
     * We also evaluate fixed positions one pixel to the right and one pixel
     * below x through T composed with G^-1. Their differences approximate its
     * 2-by-2 derivative; the determinant measures signed area change relative
     * to the global fit. We retain extrema over these sampled locations.
     *
     * Args:
     *     transform: Proposed complete moving-to-fixed transformation.
     *     global: Completed global affine used as the comparison.
     *     fixed: Fixed-image reader supplying dimensions and physical pixel sizes.
     *     tissue: Optional geometry selecting the grid points to inspect.
     *     maximumDisplacementUm: Allowed maximum added local shift in micrometres.
     *
     * Returns:
     *     Sample count, minimum/maximum relative determinant and largest observed
     *     displacement/inverse error. These are sampled checks of the candidate.
     *
     * Raises:
     *     IllegalArgumentException: Fewer than six points are checked, a value is
     *         nonfinite, relative area is outside (0.05,20], inverse error exceeds
     *         0.5 moving pixels, or local shift exceeds its selected limit.
     */
    static Map checkDeformation(transform,global,fixed,tissue,double maximumDisplacementUm) {
        return PairedGeometryOps.checkDeformation(transform, global,
            fixed.width as int, fixed.height as int,
            fixed.pixelCalibration.pixelWidthMicrons as double,
            fixed.pixelCalibration.pixelHeightMicrons as double,
            tissue, maximumDisplacementUm)
    }

    /**
     * Finds the accepted record for the selected image pair and verifies its current transform file.
     *
     * We require exactly one accepted record naming both selected image entries.
     * Its path must equal the supplied file, and the complete-file SHA-256 must
     * match the accepted transform checksum before it is used for annotation overlay.
     *
     * Args:
     *     file: Local transform JSON selected for accepted-image review.
     *     fixedId: Current mIF image-entry ID.
     *     movingId: Current paired-image entry ID.
     *     records: Accepted registration records supplied by the analysis.
     *
     * Returns:
     *     Matching accepted record after checking its transform file.
     *
     * Raises:
     *     IllegalArgumentException: The pair is unreviewed, multiple records
     *         match, the file differs, or its contents changed after acceptance.
     */
    static Map approved(Path file,String fixedId,String movingId,List records) {
        def matches=records.findAll {it.fixed_entry_id==fixedId && it.moving_entry_id==movingId}
        if(matches.size()!=1)throw new IllegalArgumentException("Complete the registration review in the pipeline before opening the annotation overlay. Preview candidates in the main application's registration editor.")
        Map record=matches.first()
        if(Path.of(record.transform_path as String)!=file || !(record.transform_sha256==~/[0-9a-f]{64}/) ||
           !(record.registration_sha256==~/[0-9a-f]{64}/) || Files.isSymbolicLink(file) || !Files.isRegularFile(file) || Files.size(file)>16*1024*1024)
            throw new IllegalArgumentException("The accepted registration does not match this paired image.")
        String digest=java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file)).encodeHex().toString()
        if(digest!=record.transform_sha256)throw new IllegalArgumentException("The alignment changed after review. Return to the registration step to review the new transform.")
        return record
    }
    /**
     * Creates the paired-image reader that supplies pixels in the fixed mIF coordinate system.
     *
     * For a requested fixed-image pixel, the inverse transform gives the original
     * moving-image sample position. RealTransformImageServer performs those
     * coordinate queries and reads the corresponding source pixels. We assign
     * the fixed image extent and calibration to its output metadata so overlays
     * and annotations continue to use the mIF image coordinates.
     *
     * Args:
     *     server: Original paired-image reader.
     *     transform: Accepted or currently reviewed moving-to-fixed transform.
     *     width: Fixed full-resolution image width in pixels.
     *     height: Fixed full-resolution image height in pixels.
     *     xum: Fixed-image pixel width in µm/pixel.
     *     yum: Fixed-image pixel height in µm/pixel.
     *
     * Returns:
     *     RealTransformImageServer reading paired-image pixels at the requested
     *     fixed-image positions using the selected transform's inverse.
     */
    static def warped(server,InvertibleRealTransform transform,int width,int height,double xum,double yum) {
        // Warpy's default server ID omits the transform; a new fit must not reuse
        // tiles rendered with an earlier pose of the same source image.
        def result=new RealTransformImageServer(server,new RealTransformInterpolation(transform.inverse(),0,false,1)) {
            @Override
            protected String createID() { super.createID()+":"+java.util.UUID.randomUUID() }
        }
        result.setMetadata(new ImageServerMetadata.Builder(result.metadata)
            .width(width).height(height)
            .levelsFromDownsamples(result.preferredDownsamples)
            .pixelSizeMicrons(xum,yum).build())
        return result
    }
}
return [uncoveredPixels:PairedGeometry.&uncoveredPixels,reviewSampling:PairedGeometry.&reviewSampling,acceptedTissue:PairedGeometry.&acceptedTissue,approved:PairedGeometry.&approved,load:PairedGeometry.&load,save:PairedGeometry.&save,warped:PairedGeometry.&warped,bspline:{Map record -> new MifElastixBSplineTransform(record)},refineLandmarks:PairedGeometry.&refineLandmarks,validateManualAlignment:PairedGeometry.&validateManualAlignment,checkDeformation:PairedGeometry.&checkDeformation]
