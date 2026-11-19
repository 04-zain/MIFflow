/**
 * Fits the selected image pair by global affine alignment and optional local B-spline refinement.
 *
 * We reopen the requested mIF and paired-image entries and check their source
 * locations, full-resolution dimensions and physical calibration. Selected
 * tissue annotations form the fixed-image mask used by the intensity comparison.
 * The global affine is fitted first; the optional B-spline stage uses that
 * completed affine and its separately selected image/control-grid settings.
 *
 * The result contains the complete transform, image pair, selected settings,
 * fitting metrics and geometry checks. We save the proposed transformation for
 * review while leaving the original image pixels and project hierarchy unchanged.
 * Image readers close when the operation ends; the Python caller removes the
 * temporary .mha fitting views after collecting the result.
 */

import java.nio.file.Path
import java.nio.file.Files
import java.security.MessageDigest
import java.awt.image.BufferedImage
import com.google.gson.Gson
import qupath.lib.projects.ProjectIO
import org.locationtech.jts.operation.union.UnaryUnionOp

/**
 * Calculates a complete file checksum through sequential one-megabyte reads.
 *
 * We use the same calculation before and after registration to confirm that
 * the selected project and saved transform correspond to the operation being
 * returned. The read buffer stays fixed in size while the digest includes all bytes.
 *
 * Args:
 *     path: Existing project, helper or candidate file to identify.
 *
 * Returns:
 *     Lowercase SHA-256 text for that file's complete contents.
 */
def sha={Path path ->
    def digest=MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream {stream -> byte[] bytes=new byte[1024*1024];
        int n;
        while((n=stream.read(bytes))>0)digest.update(bytes,0,n)}
    digest.digest().encodeHex().toString()
}
if(args.size()!=2)throw new IllegalArgumentException("Expected an alignment request and result path")
Path input=Path.of(args[0]),output=Path.of(args[1])
if(Files.size(input)>512*1024||Files.exists(output))throw new IllegalArgumentException("The alignment request or output is invalid")
Map request=new Gson().fromJson(Files.readString(input),Map)
if(request.schema!="automatic-alignment/v2")throw new IllegalArgumentException("Unsupported automatic alignment request")
Path projectFile=Path.of(request.project_file),directory=Path.of(request.work_directory)
if(directory!=output.parent||sha(projectFile)!=request.project_sha256)throw new IllegalArgumentException("The project changed before alignment")
def warpyLoader = new GroovyClassLoader(qupath.lib.gui.QuPathGUI.getExtensionCatalogManager().extensionClassLoader)
try { warpyLoader.loadClass("qupath.ext.warpy.Warpy") }
catch (ClassNotFoundException missing) {
    String launcher = System.getProperty("jpackage.app-path")
    // Packaged QuPath stores its application JARs beside the Windows launcher
    // in app/, and above the Linux bin/ launcher in lib/app/.
    boolean windows = System.getProperty("os.name").toLowerCase().contains("win")
    Path extension = launcher == null ? null : (windows ?
        Path.of(launcher).parent.resolve("app/qupath-extension-warpy-0.5.0.jar") :
        Path.of(launcher).parent.parent.resolve("lib/app/qupath-extension-warpy-0.5.0.jar"))
    if(extension == null || !Files.isRegularFile(extension) || Files.isSymbolicLink(extension) || Files.size(extension)!=113594L ||
       MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(extension)).encodeHex().toString()!="e4bc1c636adbcfd6e6dafde32d615afa032d3c0a21458660b2de380a884f5cb8")
        throw new IllegalArgumentException("Install the verified Warpy extension in Software setup before alignment.")
    warpyLoader.addClasspath(extension.toString())
}

// Load the image-fitting and coordinate functions recorded in the request.
// Their current source files must match the supplied checksums before execution.
Map apis=[:]
request.helpers.each {name,descriptor ->
    Path helper=Path.of(descriptor.path)
    if(!Files.isRegularFile(helper)||Files.isSymbolicLink(helper)||sha(helper)!=descriptor.sha256)
        throw new IllegalArgumentException("The registered alignment tools changed")
    apis[name]=new GroovyShell(warpyLoader).evaluate(helper.toFile())
}
def project=ProjectIO.loadProject(projectFile.toFile(),BufferedImage.class)
List images=[];
Map observations=[:]
try {

    // Open the two original image entries and retain their readers until fitting
    // completes. The requested source URI, dimensions and X/Y scale determine
    // the coordinates and image intensities used by both fitting stages.
    request.entries.each {role,expected ->
        def matches=project.imageList.findAll {it.ID.toString()==expected.project_entry_id}
        if(matches.size()!=1)throw new IllegalArgumentException("The selected image entry is missing")
        def entry=matches[0],data=entry.readImageData(),server=data.server
        images.add(data)
        def uris=server.getURIs().collect {Path.of(it).toAbsolutePath().normalize().toUri().toString()}.sort()
        if(uris!=[Path.of(new URI(expected.source_uri)).toAbsolutePath().normalize().toUri().toString()]||server.width!=expected.width||server.height!=expected.height)
            throw new IllegalArgumentException("The selected image is not the reviewed registration image")
        def calibration=server.pixelCalibration
        if(Math.abs(calibration.pixelWidthMicrons-expected.x_um)>1e-10||Math.abs(calibration.pixelHeightMicrons-expected.y_um)>1e-10)
            throw new IllegalArgumentException("Image pixel size changed before alignment")
        Path source=Path.of(new URI(expected.source_uri))
        observations[role]=[data:data,path:source,size:Files.size(source),modified:Files.getLastModifiedTime(source)]
    }
    def fixed=observations.fixed.data,moving=observations.moving.data
    List wanted=request.tissue_object_ids
    List tissue=fixed.hierarchy.annotationObjects.findAll {it.ID.toString() in wanted}
    if(tissue.size()!=wanted.size()||tissue.any {!apis.geometry.acceptedTissue(it)})
        throw new IllegalArgumentException("Accepted mIF tissue regions have changed")

    // Union the selected fixed-tissue polygons once. The affine and B-spline
    // fitting functions rasterize this same geometry at their own view resolutions.
    def mask=tissue?UnaryUnionOp.union(tissue.collect {it.ROI.geometry}):null
    // Global affine is always completed before the optional local deformation.
    Map global=apis.algorithm.align(fixed.server,moving.server,request.settings,
        Path.of(request.tools_directory),directory.resolve("global"),mask)
    Map fit=global
    if(request.settings.local_refinement==true) {
        Map selected=new LinkedHashMap(request.settings.bspline)
        selected.fixed_channel=request.settings.fixed_channel
        selected.moving_channel=request.settings.moving_channel
        selected.threads=request.settings.threads
        fit=apis.bspline.align(fixed.server,moving.server,global.transform,selected,
            Path.of(request.tools_directory),directory.resolve("local"),mask,apis.algorithm,apis.geometry)
    }

    // Save the global fit's numerical description separately from the final map.
    // This retains its settings and metric whether we stop at the affine or add
    // local displacement coefficients.
    Map globalDetails=global.findAll {key,value -> key!="transform"}
    fit=new LinkedHashMap(fit)
    fit.global=globalDetails
    Path candidate=directory.resolve("candidate.json")
    Map facts=fit.findAll {key,value -> key!="transform"}
    facts.schema="automatic-alignment-result/v2"
    facts.fixed_entry_id=request.entries.fixed.project_entry_id;
    facts.moving_entry_id=request.entries.moving.project_entry_id
    facts.settings=request.settings

    // The shared writer reopens the proposed transform before installing it.
    // B-spline output contains the actual fitted coefficients and physical affine,
    // so later image sampling does not depend on temporary Elastix parameter paths.
    apis.geometry.save(candidate,fit.transform,facts)
    observations.each {role,item ->
        if(Files.size(item.path)!=item.size||Files.getLastModifiedTime(item.path)!=item.modified)
            throw new IllegalArgumentException("A registration source changed during alignment")
    }
    if(sha(projectFile)!=request.project_sha256)throw new IllegalArgumentException("The project changed during alignment")
    facts.candidate_path=candidate.toString();
    facts.candidate_sha256=sha(candidate)
    Files.writeString(output,new Gson().toJson(facts),java.nio.file.StandardOpenOption.CREATE_NEW)
} finally {images.each {it.server.close()}}
