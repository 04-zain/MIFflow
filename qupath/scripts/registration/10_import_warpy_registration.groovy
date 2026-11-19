/** Evaluate one exact Warpy transform and emit registration-QC evidence. */

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import net.imglib2.realtransform.InvertibleRealTransform
import qupath.lib.common.GeneralTools
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

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
def Warpy = warpyLoader.loadClass("qupath.ext.warpy.Warpy")
def WarpyExtension = warpyLoader.loadClass("qupath.ext.warpy.WarpyExtension")

if (args.size() != 2)
    throw new IllegalArgumentException("Expected Warpy registration request and evidence paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1] as String).toAbsolutePath().normalize()
def gson = GsonTools.getInstance()
def prettyGson = gson.newBuilder().serializeNulls().setPrettyPrinting().create()
Map request = gson.fromJson(Files.readString(requestPath), Map.class) as Map
Set required = [
    "schema_version", "transform_reader", "registration_id", "project_file",
    "moving_project_entry_id", "fixed_project_entry_id",
    "moving_image_id", "fixed_image_id", "moving_image_path", "fixed_image_path",
    "moving_image_sha256", "fixed_image_sha256",
    "transform_file", "transform_file_sha256",
    "coordinate_pre_transform", "coordinate_post_transform",
    "accepted_tissue_sections", "sample_spacing_px"
] as Set
if (request.keySet() != required ||
        request.schema_version != "qupath-warpy-registration-request/v2" ||
        !(request.accepted_tissue_sections instanceof List) ||
        request.accepted_tissue_sections.isEmpty())
    throw new IllegalArgumentException("Invalid Warpy registration request")
if (request.coordinate_pre_transform != null || request.coordinate_post_transform != null)
    throw new IllegalArgumentException(
        "Full-image Warpy registration cannot invent coordinate pre/post transforms"
    )
double spacing = request.sample_spacing_px as double
if (!Double.isFinite(spacing) || spacing <= 0d)
    throw new IllegalArgumentException("sample_spacing_px must be positive and finite")

def project = getProject()
if (project == null)
    throw new IllegalStateException("No prepared QuPath project is open")
Path actualProject = project.getPath().toAbsolutePath().normalize()
if (actualProject != Paths.get(request.project_file as String).toAbsolutePath().normalize())
    throw new IllegalArgumentException("Registration request targets another project")
def movingMatches = project.getImageList().findAll {
    it.getID().toString() == request.moving_project_entry_id
}
def fixedMatches = project.getImageList().findAll {
    it.getID().toString() == request.fixed_project_entry_id
}
if (movingMatches.size() != 1 || fixedMatches.size() != 1)
    throw new IllegalStateException("Registration project-entry IDs must each resolve exactly once")
def movingEntry = movingMatches[0]
def fixedEntry = fixedMatches[0]
if (movingEntry == fixedEntry)
    throw new IllegalArgumentException("Moving and fixed project entries must differ")
validateEntryImage(movingEntry, request.moving_image_path as String,
    request.moving_image_sha256 as String)
validateEntryImage(fixedEntry, request.fixed_image_path as String,
    request.fixed_image_sha256 as String)

Path transformFile = Paths.get(request.transform_file as String).toAbsolutePath().normalize()
Path expectedTransform = fixedEntry.getEntryPath().resolve(
    "transform_${fixedEntry.getID()}_${movingEntry.getID()}.json"
).toAbsolutePath().normalize()
if (transformFile != expectedTransform || !Files.isRegularFile(transformFile))
    throw new IllegalArgumentException(
        "Warpy transform must be the exact fixed-entry transform_<fixed>_<moving>.json file"
    )
if (sha256(transformFile) != request.transform_file_sha256)
    throw new IllegalArgumentException("Warpy transform checksum does not match request")
// Load the same coefficient calculation used for the accepted image overlay.
// Its contents were selected and checked by the current Python installation.
Map reader=request.transform_reader
if(!(reader instanceof Map) || reader.keySet()!=["source","sha256"] as Set ||
   !(reader.source instanceof String) || !(reader.sha256==~/[0-9a-f]{64}/) ||
   reader.source.getBytes(StandardCharsets.UTF_8).length>512*1024 ||
   MessageDigest.getInstance("SHA-256").digest(reader.source.getBytes(StandardCharsets.UTF_8)).encodeHex().toString()!=reader.sha256)
    throw new IllegalArgumentException("The registration transform reader is invalid. Reopen the alignment step.")
Map transformApi=new GroovyShell(warpyLoader).evaluate(reader.source,"paired_geometry.groovy")
def transform = transformApi.load(transformFile)
if (transform == null)
    throw new IllegalStateException("Warpy could not load the exact requested transform")
if (!(transform instanceof InvertibleRealTransform))
    throw new IllegalStateException(
        "Registration QC requires an invertible Warpy transform for target-frame sampling"
    )
def inverse = (transform as InvertibleRealTransform).inverse()

def movingData = movingEntry.readImageData()
def fixedData = fixedEntry.readImageData()
def movingServer = movingData.getServer()
def fixedServer = fixedData.getServer()
Map movingCalibration = calibrationOf(movingServer)
Map fixedCalibration = calibrationOf(fixedServer)
double physicalAreaScale =
    (fixedCalibration.x_um_per_px * fixedCalibration.y_um_per_px) /
    (movingCalibration.x_um_per_px * movingCalibration.y_um_per_px)

def fixedHierarchy = fixedData.getHierarchy()
List sectionScopes = []
request.accepted_tissue_sections.each { row ->
    Set tissueFields = [
        "section_id", "tissue_roi_id", "tissue_path_object_id", "generation_id",
        "generation_status", "review_status", "pipeline_stage"
    ] as Set
    if (!(row instanceof Map) || row.keySet() != tissueFields)
        throw new IllegalArgumentException("Accepted tissue-section scope is invalid")
    def objects = fixedHierarchy.getFlattenedObjectList(null)
    def uuidMatches = objects.findAll {
        it.getID().toString() == row.tissue_path_object_id
    }
    def roiMatches = objects.findAll {
        metadataOf(it).get("roi_id") == row.tissue_roi_id
    }
    if (uuidMatches.size() != 1 || roiMatches.size() != 1 ||
            uuidMatches[0] != roiMatches[0] || !uuidMatches[0].isAnnotation() ||
            uuidMatches[0].getROI() == null)
        throw new IllegalStateException("Accepted TissueSection PathObject scope is stale")
    def tissueObject = uuidMatches[0]
    Map tissueMetadata = metadataOf(tissueObject)
    def pathClass = tissueObject.getPathClass()
    if (!transformApi.acceptedTissue(tissueObject) ||
            tissueMetadata.get("pipeline_stage") != row.pipeline_stage ||
            tissueMetadata.get("generation_id") != row.generation_id ||
            tissueMetadata.get("generation_status") != row.generation_status ||
            tissueMetadata.get("review_status") != row.review_status ||
            tissueMetadata.get("section_id") != row.section_id ||
            tissueMetadata.get("roi_id") != row.tissue_roi_id)
        throw new IllegalStateException("Scoped object is not an accepted TissueSection")
    sectionScopes << [
        section_id: row.section_id,
        tissue_roi_id: row.tissue_roi_id,
        tissue_path_object_id: row.tissue_path_object_id,
        pipeline_stage: tissueMetadata.get("pipeline_stage"),
        generation_status: tissueMetadata.get("generation_status"),
        review_status: tissueMetadata.get("review_status"),
        path_class: pathClass?.toString(),
        geometry: tissueObject.getROI().getGeometry(),
        bounds: tissueObject.getROI(),
    ]
}

Map globalEvidence = evaluateRegion(
    null, 0d, 0d, fixedServer.getWidth() as double,
    fixedServer.getHeight() as double, spacing,
    inverse, transform, movingServer, fixedServer, physicalAreaScale
)
List sectionEvidence = sectionScopes.collect { scope ->
    Map evidence = evaluateRegion(
        scope.geometry,
        scope.bounds.getBoundsX(), scope.bounds.getBoundsY(),
        scope.bounds.getBoundsWidth(), scope.bounds.getBoundsHeight(),
        spacing, inverse, transform, movingServer, fixedServer, physicalAreaScale
    )
    [
        section_id: scope.section_id,
        tissue_roi_id: scope.tissue_roi_id,
        tissue_path_object_id: scope.tissue_path_object_id,
        pipeline_stage: scope.pipeline_stage,
        generation_status: scope.generation_status,
        review_status: scope.review_status,
        path_class: scope.path_class,
        evidence: evidence,
    ]
}
validateEntryImage(movingEntry, request.moving_image_path as String,
    request.moving_image_sha256 as String)
validateEntryImage(fixedEntry, request.fixed_image_path as String,
    request.fixed_image_sha256 as String)

Map evidence = [
    schema_version: "qupath-warpy-registration-evidence/v1",
    registration_id: request.registration_id,
    moving_project_entry_id: request.moving_project_entry_id,
    fixed_project_entry_id: request.fixed_project_entry_id,
    moving_image_id: request.moving_image_id,
    fixed_image_id: request.fixed_image_id,
    moving_image_path: request.moving_image_path,
    fixed_image_path: request.fixed_image_path,
    moving_image_sha256: request.moving_image_sha256,
    fixed_image_sha256: request.fixed_image_sha256,
    transform_file: transformFile.toString(),
    transform_file_sha256: request.transform_file_sha256,
    provider: "warpy",
    provider_version: WarpyExtension.getWarpyVersion(),
    runtime_versions: [
        qupath: GeneralTools.getVersion().toString(),
        java: System.getProperty("java.version"),
        warpy: WarpyExtension.getWarpyVersion(),
    ],
    moving_physical_calibration: movingCalibration,
    fixed_physical_calibration: fixedCalibration,
    coordinate_pre_transform: null,
    coordinate_post_transform: null,
    global_evidence: globalEvidence,
    section_evidence: sectionEvidence,
]
atomicWrite(evidencePath, prettyGson.toJson(evidence) + "\n")
println "Evaluated Warpy transform ${request.registration_id}"

Map calibrationOf(server) {
    def calibration = server.getPixelCalibration()
    double x = calibration.getPixelWidthMicrons()
    double y = calibration.getPixelHeightMicrons()
    if (!Double.isFinite(x) || !Double.isFinite(y) || x <= 0d || y <= 0d)
        throw new IllegalStateException("Every Warpy project entry requires physical calibration")
    [x_um_per_px: x, y_um_per_px: y]
}

Map evaluateRegion(
        geometry, double x, double y, double width, double height, double spacing,
        inverse, transform, movingServer, fixedServer, double physicalAreaScale) {
    org.mif.registration.AlignmentSampling.evaluate(
        geometry, x, y, width, height, spacing, transform,
        movingServer.getWidth(), movingServer.getHeight(), physicalAreaScale, false
    ).evidence()
}

Map metadataOf(object) {
    Map metadata = [:]
    object.getMetadata().each { key, value -> metadata[key.toString()] = value }
    metadata
}

void validateEntryImage(entry, String expectedPath, String expectedSha256) {
    Path path = Paths.get(expectedPath).toAbsolutePath().normalize()
    def fileURIs = entry.getURIs().findAll { it.getScheme() == "file" }.collect {
        Paths.get(it).toAbsolutePath().normalize()
    }
    // Prepared-image identity is its completed root metadata, as in the image reader.
    Path member = Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) ? path.resolve("zarr.json") : path
    if (fileURIs != [path] || Files.isSymbolicLink(path) || Files.isSymbolicLink(member) ||
            !Files.isRegularFile(member, LinkOption.NOFOLLOW_LINKS) || sha256(member) != expectedSha256)
        throw new IllegalStateException("Warpy project-entry URI/content scope changed")
}

String sha256(Path path) {
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { stream ->
        byte[] buffer = new byte[1024 * 1024]
        int count
        while ((count = stream.read(buffer)) >= 0) {
            if (count > 0)
                digest.update(buffer, 0, count)
        }
    }
    digest.digest().collect { String.format("%02x", it) }.join()
}

void atomicWrite(Path path, String content) {
    Files.createDirectories(path.parent)
    Path temporary = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temporary, content, StandardCharsets.UTF_8)
    try {
        Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE,
            StandardCopyOption.REPLACE_EXISTING)
    } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
        Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING)
    }
}
