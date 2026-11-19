/**
 * Exchanges a manual starting pose, a returned global affine and optional local pairs.
 *
 * We first export the original image entries, calibration and accepted tissue
 * annotations from the cluster project. Local preparation changes the image
 * reader paths to their mounted locations and creates a separate QuPath viewing
 * project. The original cluster image pixels remain in their existing files.
 *
 * The first return fits an image-based Elastix affine from the selected pose
 * and channels. That affine remains inside the review session while the local
 * editor inspects it and places optional local pairs. The second return checks
 * that exact affine, fits the physical-coordinate TPS when selected, and saves
 * the final proposal with displacement, signed-area and inverse checks.
 */

import java.nio.file.Path
import java.nio.file.Files
import java.nio.file.StandardOpenOption
import java.awt.image.BufferedImage
import java.security.MessageDigest
import qupath.lib.projects.ProjectIO
import qupath.lib.projects.Projects
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServerBuilder
import qupath.lib.io.GsonTools
import qupath.lib.io.PathIO
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.StreamReadFeature
import com.fasterxml.jackson.databind.ObjectMapper
import org.locationtech.jts.operation.union.UnaryUnionOp

if(args.size()!=2)throw new IllegalArgumentException("Expected landmark request and result paths")
Path input=Path.of(args[0]),output=Path.of(args[1])
if(Files.size(input)>32L*1024*1024 || Files.exists(output))throw new IllegalArgumentException("Invalid landmark exchange files")
def mapper=new ObjectMapper(JsonFactory.builder().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
Map request=mapper.readValue(Files.readString(input),Map.class)
if(request.schema!="landmark-exchange/v2")throw new IllegalArgumentException("Unsupported landmark request")
/**
 * Checks a project or script file and calculates its content checksum.
 *
 * Args:
 *     p: Regular file no larger than 256 MiB. Symbolic links are rejected
 *         for these recorded project/script locations.
 *
 * Returns:
 *     Lowercase SHA-256 of the file read for this exchange operation.
 *
 * Raises:
 *     IllegalArgumentException: The selected file cannot be inspected as required.
 */
def sha={Path p ->
    if(Files.isSymbolicLink(p) || !Files.isRegularFile(p) || Files.size(p)>256L*1024*1024)
        throw new IllegalArgumentException("The saved project file is unavailable or too large")
    MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)).encodeHex().toString()
}
def gson=GsonTools.getInstance()
/**
 * Collects the image fields that must agree after a source path is mapped to a local mount.
 *
 * Args:
 *     server: Open image reader for the original or locally mounted image.
 *
 * Returns:
 *     Width, height, channel count, scalar pixel type and physical X/Y pixel sizes.
 */
def signature={server -> [width:server.width,height:server.height,channels:server.nChannels(),
    pixel_type:server.pixelType.toString(),x_um:server.pixelCalibration.pixelWidthMicrons,y_um:server.pixelCalibration.pixelHeightMicrons]}
/**
 * Checks that the mounted reader describes the selected cluster image.
 *
 * Args:
 *     server: Open local or cluster image reader.
 *     expected: Image dimensions, channels, type and calibration retained in
 *         the original pair description.
 *
 * Raises:
 *     IllegalArgumentException: A compared image field differs.
 */
def checkSignature={server,Map expected ->
    if(signature(server)!=expected)throw new IllegalArgumentException("The mounted image dimensions, channels or pixel size differ from this pair")
}
/**
 * Converts a local file URI to a normalized absolute URI for path comparison.
 *
 * Args:
 *     uri: Source or mounted-image file URI returned by the image reader.
 *
 * Returns:
 *     Absolute normalized URI text used when checking the selected image path.
 */
def canonicalUri={URI uri -> Path.of(uri).toAbsolutePath().normalize().toUri().toString()}
/**
 * Retains the opened source's identity member without reading its image pixels.
 *
 * An admitted completed processed-image reader owns the digest of its Zarr
 * root metadata. Vendor readers retain their original regular-file stat facts.
 * The returned member is used only for freshness; the server URI remains the
 * image-root URI used to relocate mounted readers.
 */
def sourceFacts={server ->
    Path source=Path.of(server.URIs.first()),member=source
    String generation=null
    if(Files.isDirectory(source)) {
        if(!server.metaClass.respondsTo(server,"getCompletedGenerationSha256"))
            throw new IllegalArgumentException("This image directory requires its completed processed-image reader")
        generation=server.getCompletedGenerationSha256()
        member=source.resolve("zarr.json")
        if(!(generation==~/[0-9a-f]{64}/) || sha(member)!=generation)
            throw new IllegalArgumentException("The completed image generation changed while opening")
    } else if(!Files.isRegularFile(source) || Files.isSymbolicLink(source)) {
        throw new IllegalArgumentException("The selected registration image is unavailable")
    }
    [source:member,size:Files.size(member),modified:Files.getLastModifiedTime(member),generation:generation]
}
Map result=[:];
List close=[]
try {
    // i) Create the local project with the original reader settings and mounted
    // image paths. The fixed image receives only the exported tissue outlines.
    if(request.mode=="local") {
        Path folder=Path.of(request.local_folder)
        if(Files.exists(folder.resolve("project.qpproj")))throw new IllegalArgumentException("This landmark draft already exists")
        Files.createDirectories(folder)
        def project=Projects.createProject(folder.toFile(),BufferedImage.class)
        Map localEntries=[:]
        ["fixed","moving"].each {role ->
            Map remote=request.pair[role]
            Path local=Path.of(request.local_images[role])
            def builder=gson.fromJson(remote.server_builder,ImageServerBuilder.ServerBuilder.class)
            def before=builder.URIs as List
            if(before.size()!=1 || canonicalUri(before[0])!=canonicalUri(new URI(remote.source_uri)))
                throw new IllegalArgumentException("The paired image reader does not match its source")

            // Replace the source reader's single original image path with its mounted
            // counterpart. Reopening that reader must reproduce the recorded image fields
            // before it is added to the local viewing project.
            def relocated=builder.updateURIs([(before[0]):local.toUri()])
            if((relocated.URIs.collect {canonicalUri(it)})!=[canonicalUri(local.toUri())])
                throw new IllegalArgumentException("The mounted image reader could not be selected")
            def server=relocated.build();
            close.add(server);
            checkSignature(server,remote.signature)
            sourceFacts(server)
            def entry=project.addImage(relocated);
            entry.setImageName(remote.image_name)
            entry.metadata.put("mif_pipeline.image_role",role=="fixed"?"primary_mif":"moving_reference")
            entry.metadata.put("mif_pipeline.landmark_ticket",request.ticket_sha256)
            def data=new ImageData(server)
            data.setImageType(ImageData.ImageType.valueOf(remote.image_type))
            if(role=="fixed") {
                byte[] objects=mapper.writeValueAsBytes(request.tissue_objects)
                def annotations=PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(objects))

                // Include accepted fixed-tissue annotations for orientation and geometry
                // checks. Editable pose inputs and local points remain in a separate draft.
                annotations.each {it.setLocked(true)};
                data.hierarchy.addObjects(annotations)
            }
            entry.saveImageData(data);
            localEntries[role]=entry.ID.toString()
        }
        project.syncChanges()
        result=[project_file:folder.resolve("project.qpproj").toString(),entries:localEntries]
    } else {
        Path projectFile=Path.of(request.project_file)
        if(sha(projectFile)!=request.project_sha256)throw new IllegalArgumentException("The image project changed")
        def project=ProjectIO.loadProject(projectFile.toFile(),BufferedImage.class)
        Map selected=[:],original=[:]
        ["fixed","moving"].each {role ->
            Map expected=request.entries[role]
            def matches=project.imageList.findAll {it.ID.toString()==expected.project_entry_id}
            if(matches.size()!=1)throw new IllegalArgumentException("An image in this pair is missing")
            def entry=matches[0]
            Path qpdata=entry.entryPath.resolve("data.qpdata")
            String before=sha(qpdata)
            if(expected.qpdata_sha256 && before!=expected.qpdata_sha256)throw new IllegalArgumentException("Saved image information changed")
            def data=entry.readImageData(),server=data.server;
            close.add(server)
            if(server.URIs.size()!=1 || canonicalUri(server.URIs.first())!=canonicalUri(new URI(expected.source_uri)))
                throw new IllegalArgumentException("The image source differs from this pair")
            if(request.mode in ["verify-local","candidate"]) {
                if(entry.metadata.get("mif_pipeline.landmark_ticket")!=request.ticket_sha256)
                    throw new IllegalArgumentException("The local project belongs to another landmark review")
                checkSignature(server,expected.signature)
                def expectedBuilder=gson.fromJson(expected.server_builder,ImageServerBuilder.ServerBuilder.class)
                def uris=expectedBuilder.URIs as List
                expectedBuilder=expectedBuilder.updateURIs([(uris[0]):new URI(expected.source_uri)])
                if(gson.toJson(entry.serverBuilder,ImageServerBuilder.ServerBuilder.class)!=gson.toJson(expectedBuilder,ImageServerBuilder.ServerBuilder.class))
                    throw new IllegalArgumentException("The image selected within the file changed")
            } else {
                if(server.width!=expected.width || server.height!=expected.height ||
                    Math.abs(server.pixelCalibration.pixelWidthMicrons-expected.x_um)>1e-10 ||
                    Math.abs(server.pixelCalibration.pixelHeightMicrons-expected.y_um)>1e-10)
                    throw new IllegalArgumentException("The image dimensions or pixel size changed")
            }
            selected[role]=[entry:entry,data:data,server:server]
            original[role]=[file:qpdata,sha:before]+sourceFacts(server)
        }
        // ii) Export the original source-reader definitions and selected tissue
        // outlines. These identify the images later reopened on the local mount.
        if(request.mode=="export") {
            Map pair=[:]
            selected.each {role,item -> pair[role]=[project_entry_id:item.entry.ID.toString(),
                image_name:item.entry.imageName,image_type:item.data.imageType.name(),source_uri:canonicalUri(item.server.URIs.first()),
                server_builder:gson.toJson(item.server.builder,ImageServerBuilder.ServerBuilder.class),signature:signature(item.server)]}
            def tissue=selected.fixed.data.hierarchy.annotationObjects.findAll {it.ID.toString() in request.tissue_object_ids}
            if(tissue.size()!=request.tissue_object_ids.size() || tissue.any {
                    def metadata=it.getMetadata()
                    it.ROI==null || metadata.get("pipeline_stage")!="tissue_detection" ||
                        metadata.get("generation_status")!="accepted" || metadata.get("review_status")!="accepted" ||
                        !["generation_id","section_id","roi_id"].every {key -> metadata.get(key)}
                })
                throw new IllegalArgumentException("The accepted tissue regions changed")
            def stream=new ByteArrayOutputStream()
            PathIO.exportObjectsAsGeoJSON(stream,tissue,PathIO.GeoJsonExportOptions.FEATURE_COLLECTION)
            result=[pair:pair,tissue_objects:mapper.readValue(stream.toByteArray(),Map.class)]
        } else if(request.mode in ["fit-global","fit-final"]) {
            // iii) The global request fits image intensities from an optional
            // starting pose. The final request uses the exact returned affine
            // and only the optional local point set.
            def loader=new GroovyClassLoader(qupath.lib.gui.QuPathGUI.getExtensionCatalogManager().extensionClassLoader)
            try {loader.loadClass("qupath.ext.warpy.Warpy")}
            catch(ClassNotFoundException missing) {
                // Packaged Windows and Linux installations use different application
                // folders. Load only the verified Warpy JAR accompanying that launcher.
                String launcher=System.getProperty("jpackage.app-path")
                boolean windows=System.getProperty("os.name").toLowerCase().contains("win")
                Path extension=launcher==null?null:(windows?
                    Path.of(launcher).parent.resolve("app/qupath-extension-warpy-0.5.0.jar"):
                    Path.of(launcher).parent.parent.resolve("lib/app/qupath-extension-warpy-0.5.0.jar"))
                if(extension==null || !Files.isRegularFile(extension) || Files.isSymbolicLink(extension) ||
                    Files.size(extension)!=113594L || sha(extension)!="e4bc1c636adbcfd6e6dafde32d615afa032d3c0a21458660b2de380a884f5cb8")
                    throw new IllegalArgumentException("Install the selected Warpy extension in Software setup before fitting landmarks.")
                loader.addClasspath(extension.toString())
            }
            Map apis=[:]
            request.helpers.each {name,row ->
                Path file=Path.of(row.path)
                if(sha(file)!=row.sha256)throw new IllegalArgumentException("The registration software changed")
                apis[name]=new GroovyShell(loader).evaluate(file.toFile())
            }
            Map alignment=apis.geometry.validateManualAlignment(request.alignment,selected.fixed.server,selected.moving.server)
            def tissue=selected.fixed.data.hierarchy.annotationObjects.findAll {it.ID.toString() in request.tissue_object_ids}
            if(tissue.size()!=request.tissue_object_ids.size() || tissue.any {!apis.geometry.acceptedTissue(it)})
                throw new IllegalArgumentException("The accepted tissue regions changed")
            def mask=tissue?UnaryUnionOp.union(tissue.collect {it.ROI.geometry}):null
            Map pair=[fixed_entry_id:request.entries.fixed.project_entry_id,moving_entry_id:request.entries.moving.project_entry_id,
                direction:"moving-to-fixed-level0",ticket_sha256:request.ticket_sha256]
            if(request.mode=="fit-global") {
                if(alignment.stage!="global" || !(request.global_fit_id==~/[0-9a-f]{64}/) ||
                   !(request.tools_directory instanceof String) || !Path.of(request.tools_directory).isAbsolute())
                    throw new IllegalArgumentException("Use the current saved pose and resolved cluster fitting tools")
                def fixed=selected.fixed.server,moving=selected.moving.server
                Map pose=alignment.starting_pose
                def initial=alignment.use_starting_pose ? apis.algorithm.similarity(fixed,moving,
                    pose.x_um as double,pose.y_um as double,pose.rotation_degrees as double,pose.scale as double) : null
                Path work=output.parent.resolve("global")
                Files.createDirectories(work)
                Map fitted=apis.algorithm.align(fixed,moving,alignment.global_settings,Path.of(request.tools_directory),work,mask,initial)
                Path global=output.parent.resolve("global.json")
                Map facts=pair+[schema:"manual-global-result/v1",global_fit_id:request.global_fit_id,
                    alignment:alignment,global_metrics:fitted.findAll {key,value -> key!="transform"}]
                apis.geometry.save(global,fitted.transform,facts)
                result=facts+[global_transform_path:global.toString(),global_transform_sha256:sha(global)]
            } else {
                if(alignment.stage!="final" || !(request.global_transform_path instanceof String))
                    throw new IllegalArgumentException("Select the returned global affine before final fitting")
                Path globalFile=Path.of(request.global_transform_path)
                if(!globalFile.isAbsolute() || sha(globalFile)!=alignment.global_transform_sha256)
                    throw new IllegalArgumentException("The returned global affine differs from this final request")
                def global=apis.geometry.load(globalFile)
                if(!(global instanceof net.imglib2.realtransform.AffineTransform3D))
                    throw new IllegalArgumentException("Local refinement requires the returned global affine")
                Map fitted=[transform:global,method:"global-affine",rmsMicrometres:null]
                if(alignment.local_refinement)fitted=apis.geometry.refineLandmarks(global,
                    alignment.local_moving_points,alignment.local_fixed_points,
                    selected.fixed.server.pixelCalibration.pixelWidthMicrons,selected.fixed.server.pixelCalibration.pixelHeightMicrons)
                Map qc=apis.geometry.checkDeformation(fitted.transform,global,selected.fixed.server,mask,alignment.maximum_displacement_um as double)
                Path candidate=output.parent.resolve("candidate.json")
                Map facts=pair+[schema:"manual-final-result/v1",alignment:alignment,qc:qc,
                    method:fitted.method,local_rms_um:fitted.rmsMicrometres]
                apis.geometry.save(candidate,fitted.transform,facts)
                if(sha(globalFile)!=alignment.global_transform_sha256)
                    throw new IllegalArgumentException("The global affine changed during local fitting")
                result=facts+[candidate_path:candidate.toString(),candidate_sha256:sha(candidate)]
            }
        } else if(request.mode in ["verify-local","candidate"]) {
            if(request.mode=="candidate") {
                // iv) Copy the cluster's proposed coordinate transform into the
                // verified local viewing project. Its pixels remain in their
                // original image files, read through the mounted paths.
                Path source=Path.of(request.candidate_source)
                if(Files.size(source)>16L*1024*1024 || sha(source)!=request.candidate_sha256)
                    throw new IllegalArgumentException("The mounted candidate differs from the current review")
                byte[] encoded=Files.readAllBytes(source)
                if(MessageDigest.getInstance("SHA-256").digest(encoded).encodeHex().toString()!=request.candidate_sha256)
                    throw new IllegalArgumentException("The candidate changed while reading")
                Path local=selected.fixed.entry.entryPath.resolve("transform_"+selected.fixed.entry.ID+"_"+selected.moving.entry.ID+".json")
                if(Files.exists(local)) {
                    if(sha(local)!=request.candidate_sha256)throw new IllegalArgumentException("A different alignment is already present in this viewing project")
                }else Files.write(local,encoded,StandardOpenOption.CREATE_NEW)
            }
            result=[verified:true,ticket_sha256:request.ticket_sha256]
        }
        else throw new IllegalArgumentException("Unsupported landmark operation")
        // Check the original project information and source-file values after
        // the operation, before returning a result associated with this review.
        original.values().each {item ->
            if(sha(item.file)!=item.sha || Files.size(item.source)!=item.size || Files.getLastModifiedTime(item.source)!=item.modified ||
               (item.generation!=null && sha(item.source)!=item.generation))
                throw new IllegalArgumentException("An image changed during landmark preparation")
        }
        if(sha(projectFile)!=request.project_sha256)throw new IllegalArgumentException("The image project changed during landmark preparation")
    }
    byte[] encoded=mapper.writeValueAsBytes(result)
    if(encoded.length>32L*1024*1024)throw new IllegalArgumentException("This landmark review exceeds its size limit")
    Files.write(output,encoded,StandardOpenOption.CREATE_NEW)
} finally {close.each {it.close()}}
