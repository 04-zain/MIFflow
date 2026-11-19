// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/**
 * Create/review native QuPath TissueSection PathObjects.
 *
 * Arguments are request.json, stage-result.v1.json and
 * generation-manifest.v1.json. All geometry is created and retained by
 * QuPath/JTS in global level-0 coordinates; this script never writes OME.
 */

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.geom.MultiPolygon
import org.locationtech.jts.geom.Polygon
import org.mif.geometry.TissueSectionGeometry
import qupath.lib.objects.PathObject
import qupath.lib.io.GsonTools
import qupath.lib.regions.RegionRequest

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "tissue_detection/01_detect_and_review_tissue_sections.groovy"
final String STAGE = "tissue_detection"
final String STAGE_RESULT_SCHEMA = "roi-stage-result/v1"
final String MANIFEST_SCHEMA = "roi-generation-manifest/v1"
final String CHANNEL_INTERACTION_CONTEXT_SCHEMA = "tissue-detection-channel-interaction/v1"
final String THRESHOLDER_INTERACTION_CONTEXT_SCHEMA = "tissue-thresholder-interaction/v1"
final String REVIEW_INTERACTION_CONTEXT_SCHEMA = "tissue-section-review-interaction/v1"

if (args.size() != 3)
    throw new IllegalArgumentException("Expected request, stage-result and generation-manifest paths")
Path requestPath = Paths.get(args[0] as String)
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
// We preserve integer JSON tokens while reopening the saved selection and read sizes.
// Decimal coordinates retain their floating representation in the same native maps.
def gson = GsonTools.getInstance().newBuilder()
    .setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.LONG_OR_DOUBLE).create()
def prettyGson = GsonTools.getInstance().newBuilder()
    .serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(requestPath), Map.class) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != STAGE)
    throw new IllegalArgumentException("Invalid tissue-detection request envelope")
Map request = envelope.request as Map
def boundProject = getProject()
def boundImageData = getCurrentImageData()
def boundEntry = getProjectEntry()
if (boundProject == null || boundImageData == null || boundEntry == null)
    throw new IllegalStateException("Stage execution requires an open saved project image")
Path boundProjectPath = boundProject.getPath().toAbsolutePath().normalize()
String requestedProjectFile = request.project_file as String
String requestedEntryId = request.project_entry_id as String
String requestedCanonicalSha256 = request.canonical_image_sha256 as String
if (request.exchange_version != "roi-stage-request/v2" ||
        !requestedProjectFile || Paths.get(requestedProjectFile).toAbsolutePath().normalize() != boundProjectPath ||
        !requestedEntryId || boundEntry.getID().toString() != requestedEntryId ||
        !(requestedCanonicalSha256 ==~ /[0-9a-f]{64}/) ||
        !((request.script_bundle_sha256 as String) ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException("Exact project entry identity and script bundle are required")
String requestedScriptBundleSha256 = request.script_bundle_sha256 as String
Path projectRoot = boundProjectPath.parent
Path scriptsRoot = projectRoot.resolve("scripts").normalize()
Path requestedBundleRoot = scriptsRoot.resolve("bundle-${requestedScriptBundleSha256}").normalize()
Path bundleManifestPath = requestedBundleRoot.resolve("qupath-script-bundle.v1.json").normalize()
Path dataRoot = projectRoot.resolve("data").normalize()
Path scriptBundleEvidencePath = dataRoot
    .resolve("qupath-script-bundle.evidence.json").normalize()
if (requestedBundleRoot.parent != scriptsRoot ||
        bundleManifestPath.parent != requestedBundleRoot ||
        dataRoot.parent != projectRoot || scriptBundleEvidencePath.parent != dataRoot ||
        projectRoot.toRealPath() != projectRoot ||
        scriptsRoot.toRealPath() != scriptsRoot || requestedBundleRoot.toRealPath() != requestedBundleRoot ||
        bundleManifestPath.toRealPath() != bundleManifestPath ||
        dataRoot.toRealPath() != dataRoot ||
        scriptBundleEvidencePath.toRealPath() != scriptBundleEvidencePath ||
        Files.isSymbolicLink(scriptsRoot) || !Files.isDirectory(scriptsRoot) ||
        Files.isSymbolicLink(requestedBundleRoot) || !Files.isDirectory(requestedBundleRoot) ||
        Files.isSymbolicLink(bundleManifestPath) || !Files.isRegularFile(bundleManifestPath) ||
        Files.isSymbolicLink(dataRoot) || !Files.isDirectory(dataRoot) ||
        Files.isSymbolicLink(scriptBundleEvidencePath) || !Files.isRegularFile(scriptBundleEvidencePath))
    throw new IllegalStateException("Immutable QuPath script bundle path ancestry is invalid")
byte[] authenticatedManifestBytes = Files.readAllBytes(bundleManifestPath)
String authenticatedManifestSha256 = MessageDigest.getInstance("SHA-256")
    .digest(authenticatedManifestBytes).encodeHex().toString()
if (authenticatedManifestSha256 != requestedScriptBundleSha256)
    throw new IllegalStateException("Immutable QuPath script bundle manifest does not match the request")
Map authenticatedManifest = gson.fromJson(
    new String(authenticatedManifestBytes, StandardCharsets.UTF_8), Map.class
) as Map
if (authenticatedManifest == null ||
        authenticatedManifest.keySet() != ["schema_version", "assets"] as Set ||
        authenticatedManifest.schema_version != "qupath-script-bundle/v1" ||
        !(authenticatedManifest.assets instanceof List) ||
        (authenticatedManifest.assets as List).isEmpty())
    throw new IllegalStateException("Authenticated QuPath script bundle manifest is invalid")
final String verifierAssetPath = "general/00_verify_stage_script_bundle.groovy"
List verifierEvidence = (authenticatedManifest.assets as List).findAll { raw ->
    raw instanceof Map && raw.keySet() == ["path", "sha256"] as Set && raw.path == verifierAssetPath
}
Path verifierPath = requestedBundleRoot.resolve(verifierAssetPath).normalize()
if (verifierEvidence.size() != 1 ||
        !((verifierEvidence.first().sha256 as String) ==~ /[0-9a-f]{64}/) ||
        verifierPath.toRealPath() != verifierPath ||
        Files.isSymbolicLink(verifierPath) || !Files.isRegularFile(verifierPath) ||
        MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(verifierPath))
            .encodeHex().toString() != verifierEvidence.first().sha256)
    throw new IllegalStateException("Immutable QuPath script bundle verifier is invalid")
Closure verifyCurrentScriptBundle = evaluate(verifierPath.toFile()) as Closure
verifyCurrentScriptBundle(request, boundProjectPath, SCRIPT_ASSET_PATH, gson)
Closure tissueDetectorSupport = evaluate(requestedBundleRoot.resolve(
    "tissue_detection/tissue_detector_support.groovy").toFile()) as Closure
String requestedCanonicalUri = request.canonical_image_uri as String
URI canonicalUri
try {
    canonicalUri = new URI(requestedCanonicalUri).normalize()
} catch (Exception error) {
    throw new IllegalStateException("Canonical image URI is invalid", error)
}
List<String> liveCanonicalUris = boundEntry.getURIs()
    .collect { URI uri -> MifProjectStageBootstrapV1.mifCanonicalUri(uri) }
    .toSet().toList().sort()
Object liveRole = boundEntry.getMetadataMap().get("mif_pipeline.image_role")
Object liveDigest = boundEntry.getMetadataMap().get(
    "mif_pipeline.source_image_sha256"
)
if (canonicalUri.scheme != "file" || canonicalUri.query != null ||
        canonicalUri.fragment != null ||
        canonicalUri.toASCIIString() != requestedCanonicalUri ||
        liveRole != "primary_mif" || liveDigest != requestedCanonicalSha256 ||
        liveCanonicalUris != [requestedCanonicalUri])
    throw new IllegalStateException(
        "Request image authority does not match the open canonical mIF entry"
    )
Map config = (request.config ?: request) as Map
Map detector = config.tissue_detection as Map
List<Map> canonicalChannels = (config.channels ?: request.channels ?: []) as List<Map>

def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temporary = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temporary, prettyGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try {
        Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE)
    } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
        Files.move(temporary, path)
    }
}
def persistCurrentEntryState = {
    if (!mifAuthenticatedHeadlessProjectStage ||
            !boundImageData.isChanged()) return
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
def sha256File = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    def stream = Files.newInputStream(path)
    try {
        byte[] buffer = new byte[1024 * 1024]
        int count
        while ((count = stream.read(buffer)) >= 0) {
            if (count > 0) digest.update(buffer, 0, count)
        }
    } finally {
        stream.close()
    }
    digest.digest().encodeHex().toString()
}
def canonicalValue
canonicalValue = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap<String, Object>()
        (value as Map).each { key, nested ->
            sorted.put(key as String, canonicalValue(nested))
        }
        return sorted
    }
    if (value instanceof List)
        return (value as List).collect { canonicalValue(it) }
    value
}
def canonicalSha256 = { Object value ->
    byte[] encoded = gson.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8)
    MessageDigest.getInstance("SHA-256").digest(encoded).encodeHex().toString()
}
def resultEnvelope = { Map result, Map manifestReference ->
    [schema_version: STAGE_RESULT_SCHEMA,
     invocation_id: envelope.invocation_id, run_id: envelope.run_id,
     stage: STAGE, slide_id: envelope.slide_id,
     result: result, generation_manifest: manifestReference]
}
def interaction = { String reason, String message, List actions, Map resume, String generationId = null ->
    String checkpoint = resume.checkpoint_id as String
    Map result = [status: "needs_interaction", stage: STAGE,
                  slide_id: envelope.slide_id, generation_id: generationId,
                  interaction_reason: reason, affected_object_ids: [],
                  message: message, allowed_actions: actions,
                  resume_context: resume, checkpoint_id: checkpoint,
                  outputs: null, error: null]
    mifPublishProjectStageResult(resultPath, resultEnvelope(result, null))
}

List<String> availableChannelIds = canonicalChannels.collect { it.channel_id as String }
String referenceChannelId = detector.reference_channel_id as String
def imageData = boundImageData
if (imageData == null)
    throw new IllegalStateException("No canonical mIF image is open")
def server = imageData.server
double[] preferredDownsamples = server.getPreferredDownsamples()
double baseXMicrons = server.pixelCalibration.pixelWidthMicrons
double baseYMicrons = server.pixelCalibration.pixelHeightMicrons
if (preferredDownsamples == null || preferredDownsamples.length == 0 ||
        !Double.isFinite(baseXMicrons) || !Double.isFinite(baseYMicrons) ||
        baseXMicrons <= 0d || baseYMicrons <= 0d)
    throw new IllegalStateException("Canonical image exposes no valid native pyramid inventory")
List<Map> liveNativePyramidInventory = []
double previousDownsample = 0d
preferredDownsamples.eachWithIndex { rawDownsample, index ->
    double downsample = rawDownsample as double
    if (!Double.isFinite(downsample) || downsample <= previousDownsample ||
            (index == 0 && Math.abs(downsample - 1d) > 1e-9))
        throw new IllegalStateException("Canonical image native pyramid inventory is invalid")
    liveNativePyramidInventory << [
        native_resolution_index: index,
        x_downsample: downsample,
        y_downsample: downsample,
        x_um_per_px: baseXMicrons * downsample,
        y_um_per_px: baseYMicrons * downsample,
    ]
    previousDownsample = downsample
}
Object rawRequestedInventory = request.native_pyramid_inventory
if (!(rawRequestedInventory instanceof List))
    throw new IllegalArgumentException("Frozen native pyramid inventory changed before tissue read")
List<Map> requestedNativePyramidInventory = rawRequestedInventory as List<Map>
Set<String> nativeLevelFields = [
    "native_resolution_index", "x_downsample", "y_downsample",
    "x_um_per_px", "y_um_per_px"
] as Set<String>
if (requestedNativePyramidInventory.size() != liveNativePyramidInventory.size() ||
        (0..<requestedNativePyramidInventory.size()).any { index ->
            Object requestedLevel = requestedNativePyramidInventory[index]
            !(requestedLevel instanceof Map) ||
                (requestedLevel.keySet() as Set) != nativeLevelFields ||
                nativeLevelFields.any { field -> !(requestedLevel[field] instanceof Number) } ||
                (requestedLevel.native_resolution_index as Number).doubleValue() != (index as double) ||
                nativeLevelFields.findAll { it != "native_resolution_index" }.any { field ->
                    (requestedLevel[field] as Number).doubleValue() !=
                        (liveNativePyramidInventory[index][field] as Number).doubleValue()
                }
        })
    throw new IllegalArgumentException("Frozen native pyramid inventory changed before tissue read")
String liveInventorySha256 = canonicalSha256(liveNativePyramidInventory)

Map tissueResolutionSelection = request.tissue_resolution_selection as Map
Set<String> selectionFields = [
    "schema_version", "selection_source", "native_pyramid_inventory_sha256",
    "native_resolution_index", "x_downsample", "y_downsample",
    "x_um_per_px", "y_um_per_px", "resolution_source", "coordinate_system",
    "level0_opt_in", "selection_record_sha256"
] as Set<String>
if (tissueResolutionSelection == null ||
        (tissueResolutionSelection.keySet() as Set) != selectionFields)
    throw new IllegalArgumentException("Tissue resolution selection evidence is invalid")
if (tissueResolutionSelection.schema_version != "coarse-tissue-resolution-selection/v1" ||
        tissueResolutionSelection.resolution_source != "native_pyramid" ||
        tissueResolutionSelection.coordinate_system != "global_level0_pixels" ||
        tissueResolutionSelection.native_pyramid_inventory_sha256 != liveInventorySha256 ||
        !(tissueResolutionSelection.level0_opt_in instanceof Boolean) ||
        !(tissueResolutionSelection.native_resolution_index instanceof Number))
    throw new IllegalArgumentException("Tissue resolution selection semantics are invalid")
Number rawNativeResolutionIndex = tissueResolutionSelection.native_resolution_index as Number
int nativeResolutionIndex = rawNativeResolutionIndex.intValue()
if (rawNativeResolutionIndex.doubleValue() != (nativeResolutionIndex as double) ||
        nativeResolutionIndex < 0 || nativeResolutionIndex >= preferredDownsamples.length)
    throw new IllegalArgumentException("Selected native tissue resolution index is unavailable")
Map liveSelectedLevel = liveNativePyramidInventory[nativeResolutionIndex]
Set<String> selectedFloatFields = [
    "x_downsample", "y_downsample", "x_um_per_px", "y_um_per_px"
] as Set<String>
if (selectedFloatFields.any { field ->
        !(tissueResolutionSelection[field] instanceof Number) ||
            (tissueResolutionSelection[field] as Number).doubleValue() !=
                (liveSelectedLevel[field] as Number).doubleValue()
    })
    throw new IllegalArgumentException("Selected native tissue resolution changed before read")
Map selectionDigestPayload = [
    schema_version: tissueResolutionSelection.schema_version,
    selection_source: tissueResolutionSelection.selection_source,
    native_pyramid_inventory_sha256: tissueResolutionSelection.native_pyramid_inventory_sha256,
    native_resolution_index: nativeResolutionIndex,
    x_downsample: (liveSelectedLevel.x_downsample as Number).doubleValue(),
    y_downsample: (liveSelectedLevel.y_downsample as Number).doubleValue(),
    x_um_per_px: (liveSelectedLevel.x_um_per_px as Number).doubleValue(),
    y_um_per_px: (liveSelectedLevel.y_um_per_px as Number).doubleValue(),
    resolution_source: tissueResolutionSelection.resolution_source,
    coordinate_system: tissueResolutionSelection.coordinate_system,
    level0_opt_in: tissueResolutionSelection.level0_opt_in,
]
if (tissueResolutionSelection.selection_record_sha256 != canonicalSha256(selectionDigestPayload))
    throw new IllegalArgumentException("Tissue resolution selection record digest is invalid")
tissueResolutionSelection = selectionDigestPayload + [
    selection_record_sha256: tissueResolutionSelection.selection_record_sha256
]
String action = (request.action ?: "detect") as String
Map reviewResponse = request.action_context instanceof Map && request.action_context.response instanceof Map
    ? request.action_context.response as Map : [:]
List<String> excludedRoiIds = []
if (reviewResponse.containsKey("excluded_roi_ids")) {
    Object selected = reviewResponse.excluded_roi_ids
    if (action != "accept" || !(selected instanceof List) || selected.any {
            !(it instanceof String) || !(it as String).trim()
        } || selected.toSet().size() != selected.size())
        throw new IllegalArgumentException("Excluded tissue sections require distinct ROI IDs on acceptance")
    excludedRoiIds = selected as List<String>
}
if (!(request.review_required instanceof Boolean) || !(request.new_native_read instanceof Boolean) ||
        (request.new_native_read && !request.review_required) ||
        (request.new_native_read && action != "repeat"))
    throw new IllegalArgumentException("Tissue review policy differs from its current decision")
if (!request.containsKey("native_level_read_attestation"))
    throw new IllegalArgumentException("Tissue request lacks explicit native read attestation state")
Object priorReadEvidence = request.native_level_read_attestation
if (priorReadEvidence == null) {
    if (action in ["accept", "edit", "reject", "repeat"] && !request.new_native_read)
        throw new IllegalArgumentException("Tissue continuation requires the prior native read attestation")
} else {
    Set<String> readAttestationFields = [
        "native_resolution_index", "requested_downsample", "returned_width_px",
        "returned_height_px", "resolution_source", "coordinate_system",
        "selection_record_sha256"
    ] as Set<String>
    if (!(priorReadEvidence instanceof Map) ||
            (priorReadEvidence.keySet() as Set) != readAttestationFields ||
            !["native_resolution_index", "requested_downsample", "returned_width_px",
              "returned_height_px"].every { priorReadEvidence[it] instanceof Number })
        throw new IllegalArgumentException("Prior native read attestation is invalid")
    int priorIndex = (priorReadEvidence.native_resolution_index as Number).intValue()
    int priorWidth = (priorReadEvidence.returned_width_px as Number).intValue()
    int priorHeight = (priorReadEvidence.returned_height_px as Number).intValue()
    if ((priorReadEvidence.native_resolution_index as Number).doubleValue() != (priorIndex as double) ||
            (priorReadEvidence.returned_width_px as Number).doubleValue() != (priorWidth as double) ||
            (priorReadEvidence.returned_height_px as Number).doubleValue() != (priorHeight as double) ||
            priorWidth <= 0 || priorHeight <= 0 || priorIndex != nativeResolutionIndex ||
            (priorReadEvidence.requested_downsample as Number).doubleValue() !=
                (liveSelectedLevel.x_downsample as Number).doubleValue() ||
            priorReadEvidence.resolution_source != "native_pyramid" ||
            priorReadEvidence.coordinate_system != "global_level0_pixels" ||
            priorReadEvidence.selection_record_sha256 !=
                tissueResolutionSelection.selection_record_sha256)
        throw new IllegalArgumentException("Prior native read attestation does not bind the selected request")
}
String selectionMode = detector.native_level_selection_mode as String
String expectedSelectionSource = [
    prompt: "interactive", configured_index: "configured_index",
    automatic_target: "automatic_target"
][selectionMode]
if (expectedSelectionSource == null ||
        tissueResolutionSelection.selection_source != expectedSelectionSource)
    throw new IllegalArgumentException("Tissue resolution selection source conflicts with configuration")
boolean level0OptIn = tissueResolutionSelection.level0_opt_in as Boolean
if ((nativeResolutionIndex == 0 && detector.allow_level0_selection != true) ||
        (selectionMode == "prompt" && nativeResolutionIndex == 0 && !level0OptIn) ||
        (nativeResolutionIndex > 0 && level0OptIn))
    throw new IllegalArgumentException("Level-0 tissue selection was not explicitly authorized")
if (selectionMode == "configured_index" &&
        (detector.native_resolution_index as Number).intValue() != nativeResolutionIndex)
    throw new IllegalArgumentException("Selected native tissue level conflicts with configured index")
double configuredResolution = (detector.resolution_um_per_px as Number).doubleValue()
double configuredTolerance = configuredResolution *
    (detector.native_level_match_tolerance_fraction as Number).doubleValue()
if (selectionMode == "automatic_target") {
    List<Map> matchingLevels = liveNativePyramidInventory.findAll { level ->
        Math.abs((level.x_um_per_px as Number).doubleValue() - configuredResolution) <= configuredTolerance &&
            Math.abs((level.y_um_per_px as Number).doubleValue() - configuredResolution) <= configuredTolerance
    }
    if (matchingLevels.size() != 1 ||
            (matchingLevels.first().native_resolution_index as Number).intValue() != nativeResolutionIndex)
        throw new IllegalArgumentException("Automatic native tissue selection is not the unique target match")
}
double nativeDownsample = (liveSelectedLevel.x_downsample as Number).doubleValue()
double nativeXMicrons = (liveSelectedLevel.x_um_per_px as Number).doubleValue()
double nativeYMicrons = (liveSelectedLevel.y_um_per_px as Number).doubleValue()
if (referenceChannelId == null || !availableChannelIds.contains(referenceChannelId)) {
    interaction("tissue_detection_channel_required",
        "Select a channel to use only as the QuPath tissue reference",
        ["select_channel"],
        [schema_version: CHANNEL_INTERACTION_CONTEXT_SCHEMA,
         checkpoint_id: "cp-tissue-channel",
         available_channel_ids: availableChannelIds])
    return
}
int channelIndex = canonicalChannels.findIndexOf { it.channel_id == referenceChannelId }
def serverChannels = server.metadata.channels
if (channelIndex < 0 || channelIndex >= serverChannels.size()) {
    interaction("tissue_detection_channel_required",
        "Select a channel to use only as the QuPath tissue reference",
        ["select_channel"],
        [schema_version: CHANNEL_INTERACTION_CONTEXT_SCHEMA,
         checkpoint_id: "cp-tissue-channel",
         available_channel_ids: availableChannelIds])
    return
}
String canonicalChannelName = canonicalChannels[channelIndex].name as String
if (canonicalChannelName == null || serverChannels[channelIndex].name != canonicalChannelName) {
    interaction("tissue_detection_channel_required",
        "Select a channel to use only as the QuPath tissue reference",
        ["select_channel"],
        [schema_version: CHANNEL_INTERACTION_CONTEXT_SCHEMA,
         checkpoint_id: "cp-tissue-channel",
         available_channel_ids: availableChannelIds])
    return
}
int probeWidthLevel0 = Math.min(server.width, Math.max(1, Math.round(64d * nativeDownsample) as int))
int probeHeightLevel0 = Math.min(server.height, Math.max(1, Math.round(64d * nativeDownsample) as int))
def nativeProbe = server.readRegion(RegionRequest.createInstance(
    server.path, nativeDownsample, 0, 0, probeWidthLevel0, probeHeightLevel0, 0, 0
))
if (nativeProbe == null || nativeProbe.width <= 0 || nativeProbe.height <= 0)
    throw new IllegalStateException("Native coarse tissue resolution probe read failed")
Map nativeReadAttestation = [
    native_resolution_index: nativeResolutionIndex,
    requested_downsample: nativeDownsample,
    returned_width_px: nativeProbe.width,
    returned_height_px: nativeProbe.height,
    resolution_source: "native_pyramid",
    coordinate_system: "global_level0_pixels",
    selection_record_sha256: tissueResolutionSelection.selection_record_sha256,
]
Map resolutionOutputs = [
    tissue_resolution_selection: tissueResolutionSelection,
    native_level_read_attestation: nativeReadAttestation,
    coordinate_system: "global_level0_pixels",
]

def project = getProject()
Path classifierProjectRoot = project.path.parent
Path classifierPath = classifierProjectRoot.resolve("classifiers").resolve("pixel_classifiers")
    .resolve(detector.classifier_name as String)
String classifierResourceName = (detector.classifier_name as String).replaceFirst(/\.json$/, "")
def classifier = null
if (Files.isRegularFile(classifierPath) && sha256File(classifierPath) == detector.classifier_sha256) {
    classifier = project.getPixelClassifiers().get(classifierResourceName)
}
boolean classifierBound = classifier != null
if (classifierBound) {
    Map classifierDocument = MifProjectStageBootstrapV1.parseMap(
        Files.readAllBytes(classifierPath), "tissue classifier"
    )
    try {
        tissueDetectorSupport.call(classifier, classifierDocument, imageData,
            channelIndex, [liveSelectedLevel])
    } catch (IllegalArgumentException unsupported) {
        classifierBound = false
    }
}
if (!classifierBound) {
    interaction("other_explicit_reason",
        "Create the configured saved native QuPath tissue thresholder with the exact channel and resolution",
        ["create_thresholder"],
        [schema_version: THRESHOLDER_INTERACTION_CONTEXT_SCHEMA,
         checkpoint_id: "cp-tissue-thresholder",
         project_path: project.path.toString(), classifier_path: classifierPath.toString(),
         reference_channel_id: referenceChannelId])
    return
}

def metadata = { PathObject object -> object.getMetadata() }
def tagged = { String generationId ->
    if (generationId == null) return []
    getAnnotationObjects().findAll { object ->
        metadata(object).get("pipeline_stage") == STAGE &&
            metadata(object).get("generation_id") == generationId
    }
}
// Retained generation records keep the history. The live hierarchy carries only
// the accepted tissue outlines; removing an outline must retain researcher work.
def retirePreviousTissueMaterial = { String currentGenerationId, boolean replacesAccepted ->
    List<PathObject> annotations = getAnnotationObjects()
    if (replacesAccepted) {
        annotations.findAll { object ->
            Map m = metadata(object)
            m.get("pipeline_stage") == "analysis_domain_selection" &&
                m.get("analysis_domain_status") == "accepted"
        }.each { object ->
            Map m = metadata(object)
            m.put("analysis_domain_status", "retired")
            m.put("analysis_domain_active", "false")
            m.put("analysis_domain_audit_only", "true")
            m.put("analysis_domain_retired_reason", "tissue_generation_replaced")
            m.remove("analysis_domain_retired_by_set_sha256")
            object.setLocked(true)
        }
    }
    List<PathObject> obsolete = annotations.findAll { object ->
        Map m = metadata(object)
        m.get("pipeline_stage") == STAGE &&
            m.get("generation_id") != currentGenerationId
    }
    if (!obsolete.isEmpty()) removeObjects(obsolete, true)
}

def contextFromRequest = {
    [canonical_image_id: request.canonical_image_id,
     canonical_image_sha256: request.canonical_image_sha256,
     detector_id: detector.detector_id,
     detector_version: detector.detector_version,
     settings_hash: request.detector_settings_hash,
     reference_channel_id: referenceChannelId,
     reference_channel_selection_source: detector.reference_channel_selection_source,
     stage_version: detector.stage_version,
     tissue_resolution_selection: tissueResolutionSelection,
     native_level_read_attestation: nativeReadAttestation]
}
def contextFromObject = { PathObject object ->
    Map m = metadata(object)
    Object rawSelection = m.get("tissue_resolution_selection")
    Object storedSelection = rawSelection instanceof String ?
        gson.fromJson(rawSelection as String, Object.class) : null
    Object rawReadAttestation = m.get("native_level_read_attestation")
    Object storedReadAttestation = rawReadAttestation instanceof String ?
        gson.fromJson(rawReadAttestation as String, Object.class) : null
    [canonical_image_id: m.get("canonical_image_id"),
     canonical_image_sha256: m.get("canonical_image_sha256"),
     detector_id: m.get("detector_id"), detector_version: m.get("detector_version"),
     settings_hash: m.get("detector_settings_hash"),
     reference_channel_id: m.get("reference_channel_id"),
     reference_channel_selection_source: m.get("reference_channel_selection_source"),
     stage_version: m.get("pipeline_stage_version"),
     tissue_resolution_selection: storedSelection,
     native_level_read_attestation: storedReadAttestation]
}
def ring = { sequence ->
    sequence.toCoordinateArray().collect { coordinate -> [coordinate.x, coordinate.y] }
}
def polygonCoordinates
polygonCoordinates = { Polygon polygon ->
    List rings = [ring(polygon.exteriorRing.coordinateSequence)]
    (0..<polygon.numInteriorRing).each { index ->
        rings << ring(polygon.getInteriorRingN(index).coordinateSequence)
    }
    rings
}
def geometryJson = { Geometry geometry ->
    if (geometry instanceof Polygon)
        return [type: "Polygon", coordinates: polygonCoordinates(geometry)]
    if (geometry instanceof MultiPolygon)
        return [type: "MultiPolygon", coordinates: (0..<geometry.numGeometries).collect {
            polygonCoordinates(geometry.getGeometryN(it) as Polygon)
        }]
    throw new IllegalStateException("Tissue geometry must be Polygon/MultiPolygon, got ${geometry.geometryType}")
}
def sectionMapping = { PathObject object ->
    Map m = metadata(object)
    [slide_id: m.get("slide_id"), section_id: m.get("section_id"), roi_id: m.get("roi_id"),
     path_object_id: object.getID().toString(),
     display_number: Integer.parseInt(m.get("display_number") as String),
     generation_id: m.get("generation_id"), revision: Integer.parseInt(m.get("revision") as String),
     review_status: m.get("review_status"),
     detector_context: contextFromObject(object),
     provenance_pointer: m.get("provenance_record_id"), geometry: geometryJson(object.getROI().getGeometry()),
     derived_from: gson.fromJson((m.get("derived_from") ?: "[]") as String, List.class),
     supersedes: gson.fromJson((m.get("supersedes") ?: "[]") as String, List.class)]
}
def generationMapping = { String generationId, String status, String parentId ->
    List<PathObject> generationObjects = tagged(generationId).sort { object ->
         Integer.parseInt(metadata(object).get("display_number") as String)
     }
    if (generationObjects.isEmpty())
        throw new IllegalStateException("No TissueSection objects for ${generationId}")
    Map context = contextFromObject(generationObjects.first())
    [slide_id: envelope.slide_id, generation_id: generationId, status: status,
     canonical_image_id: context.canonical_image_id,
     canonical_image_sha256: context.canonical_image_sha256,
     detector_id: context.detector_id, detector_version: context.detector_version,
     settings_hash: context.settings_hash,
     reference_channel_id: context.reference_channel_id,
     reference_channel_selection_source: context.reference_channel_selection_source,
     stage_version: context.stage_version,
     tissue_resolution_selection: context.tissue_resolution_selection,
     native_level_read_attestation: context.native_level_read_attestation,
     sections: generationObjects.collect(sectionMapping), derived_from_generation_id: parentId]
}
def writeOutputs = { Map current, List<Map> history, Map stageResult, List<Map> revisionEvents = [] ->
    List<Map> generations = history.findAll { it.generation_id != current.generation_id } + [current]
    Map manifest = [schema_version: MANIFEST_SCHEMA,
        invocation_id: envelope.invocation_id, run_id: envelope.run_id,
        stage: STAGE, slide_id: envelope.slide_id,
        generation_ids: generations.collect { it.generation_id }, generations: generations,
        revision_events: revisionEvents]
    persistCurrentEntryState()
    atomicJson(manifestPath, manifest)
    mifPublishProjectStageResult(resultPath, resultEnvelope(stageResult,
        [path: manifestPath.fileName.toString(), sha256: sha256File(manifestPath)]))
}

String generationId = (request.generation_id ?: "generation-${UUID.randomUUID()}") as String
List<Map> history = (request.generation_history ?: []) as List<Map>
String acceptedId = request.accepted_generation_id as String
List<String> actualAcceptedIds = getAnnotationObjects().findAll { object ->
    metadata(object).get("pipeline_stage") == STAGE &&
        metadata(object).get("generation_status") == "accepted"
}.collect { metadata(it).get("generation_id") as String }.unique()
if (actualAcceptedIds.size() > 1)
    throw new IllegalStateException("More than one authoritative accepted tissue generation exists")
String actualAcceptedId = actualAcceptedIds.isEmpty() ? null : actualAcceptedIds.first()
if (actualAcceptedId == generationId) {
    if (acceptedId != null && acceptedId != generationId)
        throw new IllegalStateException(
            "Requested authoritative generation does not match the accepted QuPath PathObjects"
        )
    acceptedId = null
} else if (acceptedId != null && acceptedId != actualAcceptedId) {
    throw new IllegalStateException(
        "Requested authoritative generation does not match the accepted QuPath PathObjects"
    )
} else {
    acceptedId = actualAcceptedId
}

if (action in ["accept", "edit", "reject"]) {
    List<PathObject> objects = tagged(generationId)
    if (objects.isEmpty()) throw new IllegalStateException("No candidate PathObjects for ${generationId}")
    Map expectedContext = contextFromRequest()
    if (objects.any { contextFromObject(it) != expectedContext })
        throw new IllegalStateException("Candidate material context does not match the requested acceptance context")
    Set<String> generationStatuses = objects.collect {
        (metadata(it).get("generation_status") ?: "candidate") as String
    } as Set<String>
    if (generationStatuses.size() != 1)
        throw new IllegalStateException("Generation ${generationId} has inconsistent object status")
    String existingStatus = generationStatuses.first()
    if (!excludedRoiIds.isEmpty()) {
        Map priorGeneration = history.find { it.generation_id == generationId }
        List<Map> priorSections = priorGeneration?.sections as List<Map>
        Set<String> priorRois = priorSections?.collect { it.roi_id as String } as Set<String>
        if (priorSections == null || !priorRois.containsAll(excludedRoiIds))
            throw new IllegalStateException("Only sections in the reviewed candidate can be excluded")
        if (priorRois.size() == excludedRoiIds.size())
            throw new IllegalStateException("Keep at least one tissue section before accepting")
        // Reject unsaved edits before changing the hierarchy. The pending review
        // supplies the exact outlines; excluding a section never alters another.
        if (existingStatus == "candidate" && objects.collect(sectionMapping).sort {
                it.display_number
            } != priorSections.sort { it.display_number })
            throw new IllegalStateException("Tissue outlines changed; load the saved edits before accepting")
    }
    if (action == "edit") {
        if (existingStatus != "candidate")
            throw new IllegalStateException("Only a candidate generation can be edited")
        Map priorGeneration = history.find { it.generation_id == generationId }
        if (priorGeneration == null)
            throw new IllegalStateException("Editing requires the stored candidate generation manifest")
        Map<String, Map> priorSections = (priorGeneration?.sections ?: []).collectEntries {
            [(it.roi_id as String): it as Map]
        }
        List<PathObject> changed = []
        objects.each { object ->
            Map m = metadata(object)
            Map prior = priorSections[m.get("roi_id") as String]
            if (prior == null)
                throw new IllegalStateException("Stored candidate section is missing ${m.get("roi_id")}")
            if (geometryJson(object.getROI().getGeometry()) == prior.geometry)
                return
            m.put("revision", (Integer.parseInt(m.get("revision") as String) + 1).toString())
            m.put("review_status", "needs_review")
            m.put("provenance_record_id", "tissue/${generationId}/${m.get("roi_id")}/revision-${m.get("revision")}")
            changed << object
        }
        if (!changed.isEmpty()) fireHierarchyUpdate()
        Map current = generationMapping(generationId, "candidate", acceptedId)
        // Recalculate outline quality from the saved edits; this reads geometry,
        // not image pixels, and also supports returning without changing a region.
        Map editedQc = TissueSectionGeometry.quality(
            objects.collect { it.getROI().getGeometry() },
            imageData.server.width, imageData.server.height,
            imageData.server.pixelCalibration.pixelWidthMicrons,
            imageData.server.pixelCalibration.pixelHeightMicrons,
            (detector.minimum_object_area_um2 as Number).doubleValue())
        Set changedRois = changed.collect { metadata(it).get("roi_id") } as Set
        List<Map> revisionEvents = current.sections.findAll { section ->
            changedRois.contains(section.roi_id)
        }.collect { section ->
            Map prior = priorSections[section.roi_id as String]
            [event_type: "manual_edit", provenance_record_id: section.provenance_pointer,
             slide_id: section.slide_id, section_id: section.section_id, roi_id: section.roi_id,
             generation_id: generationId, revision: section.revision,
             prior_geometry: prior?.geometry, current_geometry: section.geometry]
        }
        writeOutputs(current, history, [status: "needs_interaction", stage: STAGE,
            slide_id: envelope.slide_id, generation_id: generationId,
            interaction_reason: "tissue_section_review_required",
            affected_object_ids: current.sections.collect { it.roi_id },
            message: changed.isEmpty()
                ? "The saved tissue outlines are unchanged. Review them, then accept or run detection again."
                : "Review ${changed.size()} corrected tissue sections".toString(),
            allowed_actions: ["accept", "edit", "repeat"],
            resume_context: [schema_version: REVIEW_INTERACTION_CONTEXT_SCHEMA,
                checkpoint_id: "cp-${generationId}".toString(),
                generation_id: generationId,
                tissue_detection_qc: editedQc,
                changed_object_ids: changed.collect { metadata(it).get("roi_id") }] + resolutionOutputs,
            checkpoint_id: "cp-${generationId}".toString(), outputs: null, error: null], revisionEvents)
        return
    }
    String status = action == "accept" ? "accepted" : "rejected"
    if (existingStatus == status) {
        if (status == "accepted") retirePreviousTissueMaterial(generationId, false)
        Map current = generationMapping(generationId, status, acceptedId)
        Map outputs = [generation_status: status]
        if (status == "rejected" && acceptedId != null)
            outputs.authoritative_generation_id = acceptedId
        if (status == "rejected" && acceptedId == null) {
            writeOutputs(current, history, [status: "needs_interaction", stage: STAGE,
                slide_id: envelope.slide_id, generation_id: generationId,
                interaction_reason: "tissue_section_review_required", affected_object_ids: [],
                message: "The candidate is rejected and no accepted tissue generation exists",
                allowed_actions: ["repeat"],
                resume_context: [schema_version: REVIEW_INTERACTION_CONTEXT_SCHEMA,
                    checkpoint_id: "cp-${generationId}-rejected".toString(),
                    generation_id: generationId, authoritative_generation_id: null] + resolutionOutputs,
                checkpoint_id: "cp-${generationId}-rejected".toString(), outputs: null, error: null])
        } else {
            writeOutputs(current, history, [status: "completed", stage: STAGE,
                slide_id: envelope.slide_id, generation_id: generationId,
                interaction_reason: null, affected_object_ids: [], message: null,
                allowed_actions: [], resume_context: null, checkpoint_id: null,
                outputs: outputs + resolutionOutputs, error: null])
        }
        return
    }
    if (existingStatus != "candidate")
        throw new IllegalStateException("Cannot ${action} ${existingStatus} generation ${generationId}")
    if (acceptedId != null && acceptedId != generationId) {
        List<PathObject> authoritative = tagged(acceptedId)
        if (authoritative.isEmpty() || authoritative.any {
                metadata(it).get("generation_status") != "accepted"
            })
            throw new IllegalStateException(
                "Authoritative accepted generation ${acceptedId} is not present in QuPath"
            )
    }
    if (!excludedRoiIds.isEmpty()) {
        List<PathObject> excludedObjects = objects.findAll {
            excludedRoiIds.contains(metadata(it).get("roi_id") as String)
        }
        removeObjects(excludedObjects, true)
        objects = objects.findAll { !excludedObjects.contains(it) }
    }
    objects.each { object ->
        Map m = metadata(object)
        m.put("review_status", status)
        m.put("generation_status", status)
        if (action == "accept") m.put("supersedes", m.get("derived_from") ?: "[]")
    }
    if (action == "accept" && acceptedId != null) {
        tagged(acceptedId).each { metadata(it).put("generation_status", "superseded") }
        history = history.collect { value ->
            value.generation_id == acceptedId ? value + [status: "superseded"] : value
        }
    }
    if (action == "accept") retirePreviousTissueMaterial(generationId, acceptedId != null)
    fireHierarchyUpdate()
    Map current = generationMapping(generationId, status, acceptedId)
    if (status == "rejected" && acceptedId == null) {
        writeOutputs(current, history, [status: "needs_interaction", stage: STAGE,
            slide_id: envelope.slide_id, generation_id: generationId,
            interaction_reason: "tissue_section_review_required", affected_object_ids: [],
            message: "The candidate is rejected and no accepted tissue generation exists",
            allowed_actions: ["repeat"],
            resume_context: [schema_version: REVIEW_INTERACTION_CONTEXT_SCHEMA,
                checkpoint_id: "cp-${generationId}-rejected".toString(),
                generation_id: generationId, authoritative_generation_id: null] + resolutionOutputs,
            checkpoint_id: "cp-${generationId}-rejected".toString(), outputs: null, error: null])
    } else {
        Map outputs = [generation_status: status]
        if (status == "rejected") outputs.authoritative_generation_id = acceptedId
        writeOutputs(current, history, [status: "completed", stage: STAGE,
            slide_id: envelope.slide_id, generation_id: generationId,
            interaction_reason: null, affected_object_ids: [],
            message: null, allowed_actions: [], resume_context: null, checkpoint_id: null,
            outputs: outputs + resolutionOutputs, error: null])
    }
    return
}

// The classifier input resolution exactly matches the selected preferred
// server level validated and probed above. QuPath owns backend tile selection;
// SPLIT creates disconnected annotations, minimumObjectArea is QuPath's own
// post-filter, and minHoleArea=0 preserves real internal holes. Resulting
// PathObject ROIs remain in the server's global level-0 coordinate frame.
if (!(action in ["detect", "repeat"]))
    throw new IllegalArgumentException("Unknown tissue-detection action ${action}")
List<PathObject> created = tagged(generationId)
boolean existingGeneration = !created.isEmpty()
String detectedExistingStatus = null
if (existingGeneration) {
    if (created.any { contextFromObject(it) != contextFromRequest() })
        throw new IllegalStateException("Duplicate generation ID has a different material context")
    Set<String> statuses = created.collect {
        (metadata(it).get("generation_status") ?: "candidate") as String
    } as Set<String>
    if (statuses.size() != 1)
        throw new IllegalStateException("Generation ${generationId} has inconsistent object status")
    detectedExistingStatus = statuses.first()
} else {
    Set<PathObject> before = new HashSet<>(getAnnotationObjects())
    resetSelection()
    createAnnotationsFromPixelClassifier(classifier,
        (detector.minimum_object_area_um2 as Number).doubleValue(), 0.0, "SPLIT")
    created = getAnnotationObjects().findAll { !before.contains(it) }
    if (created.isEmpty())
        throw new IllegalStateException("Native QuPath tissue detection created no annotations")
}

int nextDisplay = ((request.max_display_number ?: 0) as Number).intValue()
history.each { generation ->
    (generation.sections ?: []).each { section ->
        nextDisplay = Math.max(nextDisplay, (section.display_number as Number).intValue())
    }
}
Map<String, Map> oldSections = [:]
List<PathObject> acceptedObjects = tagged(acceptedId)
boolean compatibleAcceptedContext = acceptedObjects.isEmpty() ||
    acceptedObjects.every { contextFromObject(it) == contextFromRequest() }
// Existing-generation replay already has its recorded identities. Draft
// previews, unaccepted repeats and different detector contexts have no accepted
// identity comparison to perform. Accepted identity is not permission to reuse
// downstream calculations after geometry or detector changes.
boolean matchAccepted = !existingGeneration && compatibleAcceptedContext && !acceptedObjects.isEmpty()
if (matchAccepted)
    acceptedObjects.each { oldSections[metadata(it).get("roi_id") as String] = sectionMapping(it) }

// Compare accepted outlines once; identifiers and revisions remain stage decisions.
List<PathObject> comparisonObjects = matchAccepted ? acceptedObjects : []
List<String> comparisonIds = comparisonObjects.collect { metadata(it).get("roi_id") as String }
def matches = matchAccepted ? TissueSectionGeometry.match(
    created.collect { it.getROI().getGeometry() },
    comparisonObjects.collect { it.getROI().getGeometry() }, comparisonIds,
    imageData.server.pixelCalibration.pixelWidthMicrons,
    imageData.server.pixelCalibration.pixelHeightMicrons,
    (detector.matching.min_iou as Number).doubleValue(),
    (detector.matching.max_centroid_displacement_um as Number).doubleValue(),
    (detector.matching.max_relative_area_change as Number).doubleValue(),
    (detector.matching.min_uniqueness_margin as Number).doubleValue()) :
    Collections.nCopies(created.size(), new TissueSectionGeometry.Match(-1, []))
Map<PathObject, TissueSectionGeometry.Match> matchByObject = [:]
created.eachWithIndex { candidate, index -> matchByObject[candidate] = matches[index] }
created.sort { a, b ->
    a.getROI().getBoundsX() <=> b.getROI().getBoundsX() ?:
        a.getROI().getBoundsY() <=> b.getROI().getBoundsY()
}
if (!existingGeneration) created.each { candidate ->
    def match = matchByObject[candidate]
    int previousIndex = match.previousIndex()
    Map inherited = previousIndex < 0 ? null : oldSections[comparisonIds[previousIndex]]
    PathObject inheritedObject = previousIndex < 0 ? null : comparisonObjects[previousIndex]
    Map m = metadata(candidate)
    List<String> derived = match.derivedFrom()
    if (inherited == null) {
        nextDisplay++
        m.put("section_id", "section-${UUID.randomUUID()}")
        m.put("roi_id", "roi-${UUID.randomUUID()}")
        m.put("display_number", nextDisplay.toString())
        m.put("revision", "0")
        m.put("derived_from", gson.toJson(derived))
    } else {
        m.put("section_id", inherited.section_id as String)
        m.put("roi_id", inherited.roi_id as String)
        m.put("display_number", inherited.display_number.toString())
        int inheritedRevision = (inherited.revision as Number).intValue()
        if (!candidate.getROI().getGeometry().equalsExact(
                inheritedObject.getROI().getGeometry())) inheritedRevision++
        m.put("revision", inheritedRevision.toString())
        m.put("derived_from", "[]")
    }
    m.put("pipeline_stage", STAGE)
    m.put("pipeline_stage_version", detector.stage_version as String)
    m.put("run_id", envelope.run_id as String)
    m.put("source", "classifier")
    m.put("slide_id", envelope.slide_id as String)
    m.put("canonical_image_id", request.canonical_image_id as String)
    m.put("canonical_image_sha256", request.canonical_image_sha256 as String)
    m.put("detector_id", detector.detector_id as String)
    m.put("detector_version", detector.detector_version as String)
    m.put("detector_settings_hash", request.detector_settings_hash as String)
    m.put("reference_channel_id", referenceChannelId)
    m.put("reference_channel_selection_source", detector.reference_channel_selection_source as String)
    m.put("tissue_resolution_selection", gson.toJson(tissueResolutionSelection))
    m.put("native_level_read_attestation", gson.toJson(nativeReadAttestation))
    m.put("generation_id", generationId)
    m.put("generation_status", "candidate")
    m.put("review_status", "needs_review")
    m.put("provenance_record_id", "tissue/${generationId}/${m.get("roi_id")}/revision-${m.get("revision")}")
    m.put("supersedes", "[]")
    candidate.setName(String.format("TissueSection_%03d", Integer.parseInt(m.get("display_number") as String)))
}
fireHierarchyUpdate()

Map current = generationMapping(
    generationId, existingGeneration ? detectedExistingStatus : "candidate", acceptedId
)
double pixelWidth = imageData.server.pixelCalibration.pixelWidthMicrons
double pixelHeight = imageData.server.pixelCalibration.pixelHeightMicrons
double minimumArea = (detector.minimum_object_area_um2 as Number).doubleValue()
List<Geometry> geometries = created.collect { it.getROI().getGeometry() }
Map qc = TissueSectionGeometry.quality(geometries, imageData.server.width, imageData.server.height,
    pixelWidth, pixelHeight, minimumArea)
def nonblankString = { Object value ->
    value instanceof String && !(value as String).trim().isEmpty()
}
def normalizeDetectorValidation = { Object raw ->
    if (!(raw instanceof Map)) return null
    Map value = raw as Map
    Set<String> requiredFields = [
        "detector_id", "detector_version", "settings_hash", "reference_channel_id",
        "reference_channel_selection_source", "stage_version", "validation_domain_id",
        "validation_image_ids", "validator_identity", "validation_date",
        "qc_thresholds", "status"
    ] as Set<String>
    if ((value.keySet() as Set) != requiredFields) return null
    if (!nonblankString(value.detector_id) || !nonblankString(value.detector_version) ||
            !nonblankString(value.stage_version) || !nonblankString(value.validator_identity) ||
            !nonblankString(value.validation_date)) return null
    if (!(value.settings_hash instanceof String) ||
            !((value.settings_hash as String) ==~ /[0-9a-f]{64}/)) return null
    if (value.reference_channel_id == null) {
        if (value.reference_channel_selection_source != null) return null
    } else if (!nonblankString(value.reference_channel_id) ||
            !(value.reference_channel_selection_source in ["canonical_metadata", "user_override"])) {
        return null
    }
    if (value.validation_domain_id != null && !nonblankString(value.validation_domain_id)) return null
    if (!(value.validation_image_ids instanceof List) || value.validation_image_ids.isEmpty() ||
            !(value.validation_image_ids as List).every { nonblankString(it) }) return null
    List<String> validationImageIds = (value.validation_image_ids as List).collect { it as String }
    if (validationImageIds.toSet().size() != validationImageIds.size()) return null
    if (!(value.status in ["provisional", "validated"])) return null
    if (!(value.qc_thresholds instanceof Map)) return null
    Map thresholds = value.qc_thresholds as Map
    Set<String> thresholdFields = [
        "min_tissue_area_fraction", "max_tissue_area_fraction"
    ] as Set<String>
    if ((thresholds.keySet() as Set) != thresholdFields) return null
    Object rawMinimum = thresholds.min_tissue_area_fraction
    Object rawMaximum = thresholds.max_tissue_area_fraction
    if (!(rawMinimum instanceof Number) || rawMinimum instanceof Boolean ||
            !(rawMaximum instanceof Number) || rawMaximum instanceof Boolean) return null
    double minimum = (rawMinimum as Number).doubleValue()
    double maximum = (rawMaximum as Number).doubleValue()
    if (!Double.isFinite(minimum) || !Double.isFinite(maximum) ||
            minimum < 0.0 || minimum > maximum || maximum > 1.0) return null
    [detector_id: value.detector_id as String,
     detector_version: value.detector_version as String,
     settings_hash: value.settings_hash as String,
     reference_channel_id: value.reference_channel_id as String,
     reference_channel_selection_source: value.reference_channel_selection_source as String,
     stage_version: value.stage_version as String,
     validation_domain_id: value.validation_domain_id as String,
     validation_image_ids: validationImageIds,
     validator_identity: value.validator_identity as String,
     validation_date: value.validation_date as String,
     qc_thresholds: [min_tissue_area_fraction: minimum,
                     max_tissue_area_fraction: maximum],
     status: value.status as String]
}
Map validation = normalizeDetectorValidation(request.detector_validation) as Map
boolean exactValidated = validation != null && validation.status == "validated" &&
    validation.detector_id == detector.detector_id && validation.detector_version == detector.detector_version &&
    validation.settings_hash == request.detector_settings_hash &&
    validation.reference_channel_id == referenceChannelId &&
    validation.reference_channel_selection_source == detector.reference_channel_selection_source &&
    validation.stage_version == detector.stage_version &&
    validation.validation_domain_id == detector.validation_domain_id
boolean qcPass = qc != null && qc.geometry_valid == true && (qc.component_count as int) > 0 &&
    qc.image_bounds_contained == true && qc.minimum_area_compliant == true
boolean validatedQcPass = qcPass && validation != null
if (validatedQcPass) {
    double fraction = (qc.tissue_area_fraction as Number).doubleValue()
    validatedQcPass = fraction >= (validation.qc_thresholds.min_tissue_area_fraction as Number).doubleValue() &&
        fraction <= (validation.qc_thresholds.max_tissue_area_fraction as Number).doubleValue()
}
if (existingGeneration && detectedExistingStatus == "accepted") {
    retirePreviousTissueMaterial(generationId, false)
    writeOutputs(current, history, [status: "completed", stage: STAGE,
        slide_id: envelope.slide_id, generation_id: generationId,
        interaction_reason: null, affected_object_ids: [], message: null,
        allowed_actions: [], resume_context: null, checkpoint_id: null,
        outputs: [generation_status: "accepted", tissue_detection_qc: qc] + resolutionOutputs,
        error: null])
    return
}
if (existingGeneration && detectedExistingStatus == "rejected") {
    if (acceptedId == null) {
        writeOutputs(current, history, [status: "needs_interaction", stage: STAGE,
            slide_id: envelope.slide_id, generation_id: generationId,
            interaction_reason: "tissue_section_review_required", affected_object_ids: [],
            message: "The candidate is rejected and no accepted tissue generation exists",
            allowed_actions: ["repeat"],
            resume_context: [schema_version: REVIEW_INTERACTION_CONTEXT_SCHEMA,
                checkpoint_id: "cp-${generationId}-rejected".toString(),
                generation_id: generationId, authoritative_generation_id: null] + resolutionOutputs,
            checkpoint_id: "cp-${generationId}-rejected".toString(), outputs: null, error: null])
    } else {
        writeOutputs(current, history, [status: "completed", stage: STAGE,
            slide_id: envelope.slide_id, generation_id: generationId,
            interaction_reason: null, affected_object_ids: [], message: null,
            allowed_actions: [], resume_context: null, checkpoint_id: null,
            outputs: [generation_status: "rejected",
                authoritative_generation_id: acceptedId] + resolutionOutputs,
            error: null])
    }
    return
}
boolean autoAccept = false
switch (detector.review_policy as String) {
    case "if_unvalidated": autoAccept = exactValidated && validatedQcPass;
    break
    case "if_qc_failed": autoAccept = qcPass;
    break
    case "unattended": autoAccept = exactValidated && validatedQcPass;
    break
    case "always": autoAccept = false;
    break
    default: throw new IllegalArgumentException("Unknown tissue review policy ${detector.review_policy}")
}
if (request.review_required) autoAccept = false
if (autoAccept) {
    created.each { object ->
        Map m = metadata(object)
        m.put("review_status", "accepted")
        m.put("generation_status", "accepted")
        m.put("supersedes", m.get("derived_from") ?: "[]")
    }
    if (acceptedId != null) {
        tagged(acceptedId).each { metadata(it).put("generation_status", "superseded") }
        history = history.collect { value ->
            value.generation_id == acceptedId ? value + [status: "superseded"] : value
        }
    }
    retirePreviousTissueMaterial(generationId, acceptedId != null)
    fireHierarchyUpdate()
    current = generationMapping(generationId, "accepted", acceptedId)
    writeOutputs(current, history, [status: "completed", stage: STAGE,
        slide_id: envelope.slide_id, generation_id: generationId,
        interaction_reason: null, affected_object_ids: [], message: null,
        allowed_actions: [], resume_context: null, checkpoint_id: null,
        outputs: [generation_status: "accepted", tissue_detection_qc: qc] + resolutionOutputs,
        error: null])
} else {
    writeOutputs(current, history, [status: "needs_interaction", stage: STAGE,
        slide_id: envelope.slide_id, generation_id: generationId,
        interaction_reason: "tissue_section_review_required",
        affected_object_ids: created.collect { metadata(it).get("roi_id") },
        message: "QuPath detected ${created.size()} tissue sections.".toString(),
        allowed_actions: ["accept", "edit", "repeat"],
        resume_context: [schema_version: REVIEW_INTERACTION_CONTEXT_SCHEMA,
            checkpoint_id: "cp-${generationId}".toString(), generation_id: generationId,
            tissue_detection_qc: qc] + resolutionOutputs,
        checkpoint_id: "cp-${generationId}".toString(), outputs: null, error: null])
}
