// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Run the selected detector once over one non-overlapping physical section union. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.UUID
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.operation.union.UnaryUnionOp
import org.mif.roi.RoiInventory
import java.util.function.Consumer
import qupath.lib.objects.PathObjects
import qupath.lib.roi.GeometryTools
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

// MIF_INSTANSEG_SCALE_GATE_BEGIN
final class MifInstanSegScaleGate {
    static Map validate(boolean metadataCalibrated,
                        qupath.lib.images.servers.PixelCalibration calibration, Path modelPath) {
        if (!metadataCalibrated || calibration == null || !calibration.hasPixelSizeMicrons())
            throw new IllegalStateException("InstanSeg scale requires physical X/Y image calibration")
        double imageX = calibration.getPixelWidthMicrons()
        double imageY = calibration.getPixelHeightMicrons()
        if (!Double.isFinite(imageX) || imageX <= 0.0d ||
                !Double.isFinite(imageY) || imageY <= 0.0d)
            throw new IllegalStateException("InstanSeg scale requires finite positive X/Y image calibration")

        // The pinned InstanSeg 0.1.7 extension reads the first output tensor's
        // X/Y axis scales, averages them, and uses one downsample for both axes.
        def model
        try {
            model = qupath.bioimageio.spec.Model.parse(modelPath)
        } catch (Exception error) {
            throw new IllegalStateException("InstanSeg scale model metadata is unreadable", error)
        }
        def outputs = model?.getOutputs()
        if (outputs == null || outputs.isEmpty())
            throw new IllegalStateException("InstanSeg scale model output is missing")
        def axes = outputs.first().getAxes()
        def xAxes = axes == null ? [] : axes.findAll { it.getID() == "x" }
        def yAxes = axes == null ? [] : axes.findAll { it.getID() == "y" }
        if (xAxes.size() != 1 || yAxes.size() != 1 ||
                xAxes.first().getType().name() != "X" ||
                yAxes.first().getType().name() != "Y" ||
                !(xAxes.first() instanceof qupath.bioimageio.spec.tensor.axes.ScaledAxis) ||
                !(yAxes.first() instanceof qupath.bioimageio.spec.tensor.axes.ScaledAxis) ||
                xAxes.first().getUnit()?.name() != "MICROMETER" ||
                yAxes.first().getUnit()?.name() != "MICROMETER")
            throw new IllegalStateException("InstanSeg scale model requires X/Y micrometre axes")
        double targetX = xAxes.first().getScale()
        double targetY = yAxes.first().getScale()
        if (!Double.isFinite(targetX) || targetX <= 0.0d ||
                !Double.isFinite(targetY) || targetY <= 0.0d)
            throw new IllegalStateException("InstanSeg scale model target X/Y is invalid")

        double downsample = qupath.ext.instanseg.core.InstanSegModel.fromPath(modelPath)
            .getPreferredDownsample(calibration)
        if (!Double.isFinite(downsample) || downsample <= 0.0d ||
                Math.abs(imageX * downsample - targetX) > 0.01d * targetX ||
                Math.abs(imageY * downsample - targetY) > 0.01d * targetY)
            throw new IllegalStateException("InstanSeg scale native resampling does not match model target")
        [image_x_um_per_px: imageX, image_y_um_per_px: imageY,
         target_x_um_per_px: targetX, target_y_um_per_px: targetY,
         downsample: downsample]
    }
}
// MIF_INSTANSEG_SCALE_GATE_END

// MIF_INSTANSEG_SELECTED_SETTINGS_BEGIN
final class MifInstanSegSelection {
    private static final Set<String> SETTINGS_FIELDS = [
        "model_path", "model_version", "device", "tile_size_px",
        "padding_px", "workers", "scope"
    ] as Set
    private static final Set<String> EVIDENCE_FIELDS = [
        "model_path", "model_version", "evidence_sha256", "model_files"
    ] as Set
    private static final Set<String> FILE_FIELDS = ["relative_path", "sha256"] as Set
    private static final int MAX_MODEL_FILES = 4096

    private static String digest(Object value) {
        def gson = new com.google.gson.GsonBuilder().disableHtmlEscaping()
            .serializeNulls().create()
        Object sorted = canonical(value)
        java.security.MessageDigest.getInstance("SHA-256")
            .digest(gson.toJson(sorted).getBytes(java.nio.charset.StandardCharsets.UTF_8))
            .encodeHex().toString()
    }

    private static Object canonical(Object value) {
        if (value instanceof Map) {
            Map result = new TreeMap()
            (value as Map).each { key, item ->
                if (!(key instanceof String))
                    throw new IllegalStateException("InstanSeg settings key is invalid")
                result[key] = canonical(item)
            }
            return result
        }
        if (value instanceof List) return (value as List).collect { canonical(it) }
        if (value instanceof Number) {
            double number = (value as Number).doubleValue()
            if (!Double.isFinite(number) || number != Math.rint(number) ||
                    number < Long.MIN_VALUE || number > Long.MAX_VALUE)
                throw new IllegalStateException("InstanSeg settings number is invalid")
            return (value as Number).longValue()
        }
        return value
    }

    private static boolean exactFields(Object value, Set<String> fields) {
        value instanceof Map && (value as Map).keySet() == fields
    }

    private static boolean sha256(Object value) {
        value instanceof String && value ==~ /[0-9a-f]{64}/
    }

    static Map select(Object selected, Object selectedSha256, Object admitted,
                      Object evidence, boolean excludedDomains, Object revision = null) {
        if (!exactFields(selected, SETTINGS_FIELDS) || !sha256(selectedSha256))
            throw new IllegalStateException("InstanSeg selected settings are absent or not closed")
        Map settings = selected as Map
        if (!(settings.model_path instanceof String) ||
                !(settings.model_version instanceof String) ||
                !(settings.device instanceof String) ||
                !(settings.scope in ["accepted_tissue", "configured_union"]) ||
                !settings.model_path || !settings.model_version || !settings.device) {
            throw new IllegalStateException("InstanSeg selected settings are invalid")
        }
        for (String field in ["tile_size_px", "padding_px", "workers"]) {
            Object value = settings[field]
            if (!(value instanceof Number) || !Double.isFinite((value as Number).doubleValue()) ||
                    (value as Number).doubleValue() != Math.rint((value as Number).doubleValue()) ||
                    (value as Number).doubleValue() < (field == "padding_px" ? 0 : 1) ||
                    (value as Number).doubleValue() > Integer.MAX_VALUE)
                throw new IllegalStateException("InstanSeg selected integer setting is invalid")
        }
        if (digest(settings) != selectedSha256)
            throw new IllegalStateException("InstanSeg selected settings digest differs")
        if (revision != null) {
            if (!exactFields(revision, ["predecessor_node_id", "revision", "settings"] as Set) ||
                    !((revision as Map).predecessor_node_id instanceof String) ||
                    !((revision as Map).predecessor_node_id ==~ /node-[0-9a-f]{48}/) ||
                    !((revision as Map).revision instanceof Number) ||
                    (revision as Map).revision < 1 ||
                    (revision as Map).revision != (revision as Map).revision.intValue() ||
                    !exactFields((revision as Map).settings, SETTINGS_FIELDS) ||
                    digest((revision as Map).settings) != selectedSha256)
                throw new IllegalStateException("InstanSeg revision differs from selected section settings")
        } else if (admitted != null &&
                (!exactFields(admitted, SETTINGS_FIELDS) || digest(admitted) != selectedSha256))
            throw new IllegalStateException("InstanSeg selected settings differ from admission")
        if (evidence == null) {
            if ((admitted == null || revision != null) && !excludedDomains)
                throw new IllegalStateException("InstanSeg selected settings lack model evidence")
        } else {
            if (!exactFields(evidence, EVIDENCE_FIELDS) ||
                    (evidence as Map).model_path != settings.model_path ||
                    (evidence as Map).model_version != settings.model_version ||
                    !sha256((evidence as Map).evidence_sha256))
                throw new IllegalStateException("InstanSeg model evidence differs from selection")
        }
        Map normalized = new LinkedHashMap(settings)
        for (String field in ["tile_size_px", "padding_px", "workers"])
            normalized[field] = (settings[field] as Number).intValue()
        return normalized
    }

    static void verifyInventory(Map settings, Object evidence) {
        if (evidence == null) return
        if (!exactFields(evidence, EVIDENCE_FIELDS) ||
                (evidence as Map).model_path != settings.model_path ||
                (evidence as Map).model_version != settings.model_version ||
                !sha256((evidence as Map).evidence_sha256))
            throw new IllegalStateException("InstanSeg model evidence differs from selection")
        Object rawFiles = (evidence as Map).model_files
        if (!(rawFiles instanceof List) || rawFiles.isEmpty() ||
                rawFiles.size() > MAX_MODEL_FILES)
            throw new IllegalStateException("InstanSeg model inventory size is invalid")
        java.nio.file.Path root
        try { root = java.nio.file.Paths.get(settings.model_path as String) }
        catch (RuntimeException error) {
            throw new IllegalStateException("InstanSeg model root path is invalid", error)
        }
        if (!root.isAbsolute() || root != root.normalize() ||
                !java.nio.file.Files.isDirectory(root, java.nio.file.LinkOption.NOFOLLOW_LINKS) ||
                root.toRealPath() != root)
            throw new IllegalStateException("InstanSeg model root is unsafe")
        Map<String, String> expected = [:]
        (rawFiles as List).each { raw ->
            if (!exactFields(raw, FILE_FIELDS) ||
                    !(raw.relative_path instanceof String) ||
                    !sha256(raw.sha256))
                throw new IllegalStateException("InstanSeg model inventory entry is invalid")
            String name = raw.relative_path as String
            if (!name || name.length() > 4096 || name.startsWith("/") ||
                    name.contains("\\") || name.contains(":") ||
                    name.split("/", -1).any { it in ["", ".", ".."] } ||
                    expected.put(name, raw.sha256 as String) != null)
                throw new IllegalStateException("InstanSeg model inventory path is unsafe or duplicate")
        }
        if (!expected.containsKey("rdf.yaml"))
            throw new IllegalStateException("InstanSeg model inventory lacks rdf.yaml")
        Set<String> found = [] as Set
        java.nio.file.Files.walk(root).withCloseable { paths ->
            paths.forEach { java.nio.file.Path path ->
                if (path == root) return
                if (java.nio.file.Files.isSymbolicLink(path))
                    throw new IllegalStateException("InstanSeg model inventory contains a symbolic link")
                if (java.nio.file.Files.isDirectory(path, java.nio.file.LinkOption.NOFOLLOW_LINKS))
                    return
                if (!java.nio.file.Files.isRegularFile(path, java.nio.file.LinkOption.NOFOLLOW_LINKS))
                    throw new IllegalStateException("InstanSeg model inventory contains a nonregular file")
                String name = root.relativize(path).toString().replace("\\", "/")
                if (!expected.containsKey(name) || !found.add(name) ||
                        found.size() > MAX_MODEL_FILES || path.toRealPath() != path)
                    throw new IllegalStateException("InstanSeg model inventory contains an unexpected file")
                java.security.MessageDigest hash = java.security.MessageDigest.getInstance("SHA-256")
                java.nio.file.Files.newInputStream(path).withCloseable { input ->
                    byte[] buffer = new byte[1024 * 1024]
                    int count
                    while ((count = input.read(buffer)) != -1) hash.update(buffer, 0, count)
                }
                if (hash.digest().encodeHex().toString() != expected[name])
                    throw new IllegalStateException("InstanSeg model file bytes differ from evidence")
            }
        }
        if (found != expected.keySet())
            throw new IllegalStateException("InstanSeg model inventory has missing files")
    }
}
// MIF_INSTANSEG_SELECTED_SETTINGS_END

final String SCRIPT_ASSET_PATH = "cell_analysis/04_run_instanseg.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
// Task 6 revision digests are canonical JSON with explicit nulls.
def canonicalGson = gson.newBuilder().serializeNulls().create()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "segmentation")
    throw new IllegalArgumentException("Invalid segmentation request envelope")
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
Map config = request.resolved_config as Map
if (config == null) throw new IllegalArgumentException("Authenticated resolved_config is required")
String slideId = envelope.slide_id as String
String runId = envelope.run_id as String
String sectionId = request.section_id as String
String generationId = request.generation_id as String
if (!sectionId || !generationId) throw new IllegalArgumentException("Exact section and generation are required")
def md = { object -> object.getMetadata() }
List acceptedSections = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "tissue_detection" && m.get("generation_status") == "accepted" &&
        m.get("slide_id") == slideId
}
Map<String, Object> acceptedSectionsById = [:]
acceptedSections.each { candidate ->
    String candidateSectionId = md(candidate).get("section_id") as String
    if (!candidateSectionId || acceptedSectionsById.put(candidateSectionId, candidate) != null)
        throw new IllegalStateException("Accepted TissueSection inventory is ambiguous")
}
def section = acceptedSectionsById[sectionId]
if (section == null || md(section).get("generation_id") != generationId)
    throw new IllegalStateException("Exactly one accepted current TissueSection is required")
// Missing model evidence is permitted only for an exclusion-only request.
// Exact accepted-domain/table/live checks below must still authorize the skip.
boolean excludedPrimaryInputs = request.active_primary_outputs instanceof List &&
    !(request.active_primary_outputs as List).isEmpty() &&
    (request.active_primary_outputs as List).every { it instanceof Map &&
        it.domain_status in ["excluded_below_minimum", "excluded_by_primary_count_review"] }
def alternativeDetector = request.segmentation_settings?.method in ["cellpose", "stardist"] ?
    evaluate(requestedBundleRoot.resolve("cell_analysis/alternative_segmentation.groovy").toFile()) : null
Map segmentation = (alternativeDetector ?: MifInstanSegSelection).select(
    request.segmentation_settings, request.segmentation_settings_sha256,
    config.segmentation, request.segmentation_model_evidence, excludedPrimaryInputs,
    request.segmentation_settings_revision
)
String canonicalImageId = request.canonical_image_id as String
String canonicalImageSha256 = request.canonical_image_sha256 as String
if (!canonicalImageId || !(canonicalImageSha256 ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException("Canonical image identity is required for stable segmentation")
Closure canonical
canonical = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap()
        (value as Map).each { key, item -> sorted[key as String] = canonical(item) }
        return sorted
    }
    if (value instanceof List) return (value as List).collect { canonical(it) }
    value
}
def contentDigest = { Object value ->
    MessageDigest.getInstance("SHA-256")
        .digest(canonicalGson.toJson(canonical(value)).getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
}

String domainSetSha256 = request.domain_set_sha256 as String
if (!(domainSetSha256 ==~ /[0-9a-f]{64}/) ||
        request.analysis_domain_selection_status != "accepted" ||
        !(request.analysis_domain_manifests instanceof List))
    throw new IllegalArgumentException("Segmentation requires the complete accepted Task 6 manifest set")
Set task6RevisionFields = [
    "analysis_domain_id", "kind", "slide_id", "section_id",
    "tissue_generation_id", "canonical_image_id", "canonical_image_sha256",
    "project_entry_id", "tissue_roi_id", "tissue_path_object_id",
    "source_path_object_id", "revision", "predecessor_sha256",
    "geometry_digest_policy", "geometry_sha256", "normalized_wkb_hex",
    "geometry_reference_path_object_id", "content_sha256",
] as Set
Set task6ManifestFields = [
    "schema_version", "canonical_image_id", "canonical_image_sha256",
    "project_entry_id", "slide_id", "section_id", "tissue_generation_id",
    "tissue_roi_id", "tissue_path_object_id", "revisions", "content_sha256",
] as Set
List<Map> requestedDomains = []
Set<String> manifestContentSha256s = [] as Set<String>
Set<String> manifestSourceKeys = [] as Set<String>
(request.analysis_domain_manifests as List).each { rawManifest ->
    if (!(rawManifest instanceof Map))
        throw new IllegalArgumentException("Task 6 manifest must be a mapping")
    Map manifest = rawManifest as Map
    if (manifest.keySet() != task6ManifestFields || manifest.schema_version != "analysis-domain/v1" ||
            !(manifest.content_sha256 as String ==~ /[0-9a-f]{64}/) ||
            !(manifest.revisions instanceof List) || (manifest.revisions as List).isEmpty())
        throw new IllegalStateException("Task 6 manifest schema is invalid")
    String manifestSectionId = manifest.section_id as String
    def manifestSection = acceptedSectionsById[manifestSectionId]
    if (manifestSection == null || manifest.slide_id != slideId ||
            manifest.canonical_image_id != canonicalImageId ||
            manifest.canonical_image_sha256 != canonicalImageSha256 ||
            manifest.project_entry_id != requestedEntryId ||
            manifest.tissue_generation_id != md(manifestSection).get("generation_id") ||
            manifest.tissue_roi_id != md(manifestSection).get("roi_id") ||
            manifest.tissue_path_object_id != manifestSection.getID().toString())
        throw new IllegalStateException("Task 6 manifest is outside accepted TissueSection scope")
    List<Map> history = (manifest.revisions as List).collect { rawRevision ->
        if (!(rawRevision instanceof Map))
            throw new IllegalStateException("Task 6 revision must be a mapping")
        rawRevision as Map
    }
    String firstDomainId = null
    String firstSourcePathObjectId = null
    String firstKind = null
    String predecessorSha256 = null
    history.eachWithIndex { revision, index ->
        if (revision.keySet() != task6RevisionFields)
            throw new IllegalStateException("Task 6 revision schema is invalid")
        Number revisionNumber = revision.revision as Number
        String domainId = revision.analysis_domain_id as String
        String kind = revision.kind as String
        String sourcePathObjectId = revision.source_path_object_id as String
        String revisionSha256 = revision.content_sha256 as String
        if (!(domainId ==~ /[0-9a-f]{64}/) || !(revisionSha256 ==~ /[0-9a-f]{64}/) ||
                !sourcePathObjectId || !(kind in ["selected_roi", "whole_tissue_section"]) ||
                revisionNumber == null || revisionNumber.intValue() != index + 1 ||
                revisionNumber.doubleValue() != revisionNumber.intValue() ||
                revision.predecessor_sha256 != predecessorSha256 ||
                revision.section_id != manifestSectionId || revision.slide_id != slideId ||
                revision.tissue_generation_id != manifest.tissue_generation_id ||
                revision.canonical_image_id != canonicalImageId ||
                revision.canonical_image_sha256 != canonicalImageSha256 ||
                revision.project_entry_id != requestedEntryId ||
                revision.tissue_roi_id != manifest.tissue_roi_id ||
                revision.tissue_path_object_id != manifest.tissue_path_object_id ||
                (firstDomainId != null && (domainId != firstDomainId ||
                    sourcePathObjectId != firstSourcePathObjectId || kind != firstKind)))
            throw new IllegalStateException("Task 6 revision history is stale or unordered")
        String expectedDomainId = contentDigest([
            canonical_image_sha256: canonicalImageSha256, kind: kind,
            schema_version: "analysis-domain/v1", section_id: manifestSectionId,
            source_path_object_id: sourcePathObjectId,
            tissue_generation_id: manifest.tissue_generation_id,
        ])
        String geometrySha256 = revision.geometry_sha256 as String
        String normalizedWkbHex = revision.normalized_wkb_hex as String
        String geometryReferencePathObjectId = revision.geometry_reference_path_object_id as String
        if (domainId != expectedDomainId ||
                revision.geometry_digest_policy != "jts-normalized-wkb-sha256/v1" ||
                (kind == "selected_roi" && (
                    !(geometrySha256 ==~ /[0-9a-f]{64}/) ||
                    !(normalizedWkbHex ==~ /(?:[0-9a-f]{2})+/) ||
                    geometryReferencePathObjectId != null ||
                    MessageDigest.getInstance("SHA-256").digest(normalizedWkbHex.decodeHex())
                        .encodeHex().toString() != geometrySha256
                )) ||
                (kind == "whole_tissue_section" && (
                    geometrySha256 != null || normalizedWkbHex != null ||
                    geometryReferencePathObjectId != sourcePathObjectId ||
                    sourcePathObjectId != manifest.tissue_path_object_id ||
                    revisionNumber.intValue() != 1
                )))
            throw new IllegalStateException("Task 6 revision geometry is invalid")
        Map revisionPayload = [
            analysis_domain_id: domainId, canonical_image_id: canonicalImageId,
            canonical_image_sha256: canonicalImageSha256,
            geometry_digest_policy: revision.geometry_digest_policy,
            geometry_reference_path_object_id: geometryReferencePathObjectId,
            geometry_sha256: geometrySha256, kind: kind,
            predecessor_sha256: predecessorSha256, project_entry_id: requestedEntryId,
            revision: revisionNumber.intValue(), section_id: manifestSectionId,
            slide_id: slideId, source_path_object_id: sourcePathObjectId,
            tissue_generation_id: manifest.tissue_generation_id,
            tissue_path_object_id: manifest.tissue_path_object_id,
            tissue_roi_id: manifest.tissue_roi_id,
        ]
        if (contentDigest(revisionPayload) != revisionSha256)
            throw new IllegalStateException("Task 6 revision digest is stale")
        firstDomainId = firstDomainId ?: domainId
        firstSourcePathObjectId = firstSourcePathObjectId ?: sourcePathObjectId
        firstKind = firstKind ?: kind
        predecessorSha256 = revisionSha256
    }
    Map manifestPayload = [
        canonical_image_id: manifest.canonical_image_id,
        canonical_image_sha256: manifest.canonical_image_sha256,
        project_entry_id: manifest.project_entry_id,
        revision_sha256s: history.collect { it.content_sha256 as String },
        schema_version: manifest.schema_version, section_id: manifest.section_id,
        slide_id: manifest.slide_id, tissue_generation_id: manifest.tissue_generation_id,
        tissue_path_object_id: manifest.tissue_path_object_id, tissue_roi_id: manifest.tissue_roi_id,
    ]
    String sourceKey = "${manifestSectionId}|${firstSourcePathObjectId}"
    if (contentDigest(manifestPayload) != manifest.content_sha256 ||
            !manifestContentSha256s.add(manifest.content_sha256 as String) ||
            !manifestSourceKeys.add(sourceKey))
        throw new IllegalStateException("Task 6 manifest content or inventory is stale")
    if (manifestSectionId == sectionId) requestedDomains << history.last()
}
if (requestedDomains.isEmpty() ||
        contentDigest([
            schema_version: "analysis-domain-set/v1",
            analysis_domain_manifest_sha256s: manifestContentSha256s.toList().sort(),
        ]) != domainSetSha256)
    throw new IllegalStateException("Task 6 global manifest set is incomplete or stale")
Map<String, Map> activeDomainsById = [:]
Set<String> activeDomainObjectIds = [] as Set<String>
List<Map> activeDomains = []
requestedDomains.each { revision ->
    if (revision.keySet() != task6RevisionFields)
        throw new IllegalStateException("Active analysis-domain revision schema is invalid")
    String domainId = revision.analysis_domain_id as String
    String kind = revision.kind as String
    String revisionSha256 = revision.content_sha256 as String
    String sourcePathObjectId = revision.source_path_object_id as String
    Number revisionNumber = revision.revision as Number
    if (!(domainId ==~ /[0-9a-f]{64}/) || !(revisionSha256 ==~ /[0-9a-f]{64}/) ||
            !sourcePathObjectId || !(kind in ["selected_roi", "whole_tissue_section"]) ||
            revisionNumber == null || revisionNumber.intValue() < 1 ||
            revisionNumber.doubleValue() != revisionNumber.intValue() ||
            revision.section_id != sectionId || revision.slide_id != slideId ||
            revision.tissue_generation_id != generationId ||
            revision.canonical_image_id != canonicalImageId ||
            revision.canonical_image_sha256 != canonicalImageSha256 ||
            revision.project_entry_id != requestedEntryId ||
            revision.tissue_roi_id != md(section).get("roi_id") ||
            revision.tissue_path_object_id != section.getID().toString())
        throw new IllegalStateException("Active analysis-domain revision is outside exact segmentation scope")
    String expectedDomainId = contentDigest([
        canonical_image_sha256: canonicalImageSha256, kind: kind,
        schema_version: "analysis-domain/v1", section_id: sectionId,
        source_path_object_id: sourcePathObjectId,
        tissue_generation_id: generationId,
    ])
    if (domainId != expectedDomainId)
        throw new IllegalStateException("Active analysis-domain ID differs from Task 6 identity")
    String geometrySha256 = revision.geometry_sha256 as String
    String normalizedWkbHex = revision.normalized_wkb_hex as String
    String geometryReferencePathObjectId = revision.geometry_reference_path_object_id as String
    String predecessorSha256 = revision.predecessor_sha256 as String
    if (revision.geometry_digest_policy != "jts-normalized-wkb-sha256/v1" ||
            (kind == "selected_roi" && (
                !(geometrySha256 ==~ /[0-9a-f]{64}/) ||
                !(normalizedWkbHex ==~ /(?:[0-9a-f]{2})+/) ||
                geometryReferencePathObjectId != null ||
                MessageDigest.getInstance("SHA-256").digest(normalizedWkbHex.decodeHex())
                    .encodeHex().toString() != geometrySha256 ||
                (revisionNumber.intValue() == 1 && predecessorSha256 != null) ||
                (revisionNumber.intValue() > 1 && !(predecessorSha256 ==~ /[0-9a-f]{64}/))
            )) ||
            (kind == "whole_tissue_section" && (
                geometrySha256 != null || normalizedWkbHex != null ||
                geometryReferencePathObjectId != sourcePathObjectId ||
                sourcePathObjectId != section.getID().toString() ||
                revisionNumber.intValue() != 1 || predecessorSha256 != null
            )))
        throw new IllegalStateException("Active analysis-domain revision geometry/lineage is invalid")
    Map revisionPayload = [
        analysis_domain_id: domainId,
        canonical_image_id: canonicalImageId,
        canonical_image_sha256: canonicalImageSha256,
        geometry_digest_policy: revision.geometry_digest_policy,
        geometry_reference_path_object_id: geometryReferencePathObjectId,
        geometry_sha256: geometrySha256,
        kind: kind, predecessor_sha256: predecessorSha256,
        project_entry_id: requestedEntryId, revision: revisionNumber.intValue(),
        section_id: sectionId, slide_id: slideId,
        source_path_object_id: sourcePathObjectId,
        tissue_generation_id: generationId,
        tissue_path_object_id: section.getID().toString(),
        tissue_roi_id: md(section).get("roi_id"),
    ]
    // Task 6 transmits normalized_wkb_hex for geometry attestation, but its
    // revision payload deliberately excludes that transport-only field.
    if (contentDigest(revisionPayload) != revisionSha256)
        throw new IllegalStateException("Active analysis-domain revision digest is stale")
    if (activeDomainsById.containsKey(domainId) || !activeDomainObjectIds.add(sourcePathObjectId))
        throw new IllegalStateException("Active analysis-domain identities are duplicated")
    def domain = kind == "whole_tissue_section" ? section : getAnnotationObjects().find {
        it.getID().toString() == sourcePathObjectId
    }
    if (domain == null || domain.getID().toString() != sourcePathObjectId ||
            domain.getROI()?.getGeometry() == null || domain.getROI().getGeometry().isEmpty() ||
            !domain.getROI().getGeometry().isValid() ||
            !domain.getROI().getGeometry().coveredBy(section.getROI().getGeometry()))
        throw new IllegalStateException("Active analysis-domain object is absent or outside exact tissue")
    Map domainMetadata = md(domain)
    boolean selectedDomain = kind == "selected_roi" && domain != section &&
        domainMetadata.get("pipeline_stage") == "analysis_domain_selection" &&
        domainMetadata.get("analysis_domain_status") == "accepted" &&
        domainMetadata.get("analysis_domain_active") == "true" &&
        domainMetadata.get("analysis_domain_id") == domainId &&
        domainMetadata.get("analysis_domain_revision") == Integer.toString(revisionNumber.intValue()) &&
        domainMetadata.get("analysis_domain_revision_sha256") == revisionSha256 &&
        domainMetadata.get("analysis_domain_set_sha256") == domainSetSha256 &&
        domainMetadata.get("analysis_domain_project_entry_id") == requestedEntryId &&
        domain.isLocked() && domain.getParent() == section
    boolean wholeTissueDomain = kind == "whole_tissue_section" && domain == section
    if (!(selectedDomain || wholeTissueDomain))
        throw new IllegalStateException("Active analysis-domain object is stale or inactive")
    if (geometrySha256 != null) {
        def normalized = domain.getROI().getGeometry().copy();
        normalized.normalize()
        String liveGeometrySha256 = MessageDigest.getInstance("SHA-256").digest(
            new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
        ).encodeHex().toString()
        if (liveGeometrySha256 != geometrySha256)
            throw new IllegalStateException("Live analysis-domain geometry differs from its accepted revision")
    }
    Map identity = [analysis_domain_id: domainId, kind: kind,
        revision: revisionNumber.intValue(), content_sha256: revisionSha256,
        source_path_object_id: sourcePathObjectId, geometry_sha256: geometrySha256]
    activeDomainsById[domainId] = [identity: identity, object: domain]
    activeDomains << activeDomainsById[domainId]
}
activeDomains.sort { left, right ->
    (left.identity.analysis_domain_id as String) <=> (right.identity.analysis_domain_id as String)
}
def activeDomainUnion = UnaryUnionOp.union(activeDomains.collect {
    it.object.getROI().getGeometry()
})
if (activeDomainUnion == null || activeDomainUnion.isEmpty() || !activeDomainUnion.isValid() ||
        !activeDomainUnion.coveredBy(section.getROI().getGeometry()))
    throw new IllegalStateException("Active analysis-domain union is empty, invalid, or outside its TissueSection")
def normalizedGeometrySha256 = { geometry ->
    if (geometry == null || geometry.isEmpty() || !geometry.isValid()) return null
    def normalized = geometry.copy();
    normalized.normalize()
    MessageDigest.getInstance("SHA-256").digest(
        new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
    ).encodeHex().toString()
}
def livePrimaryObjectRevisionSha256 = { object, Map domain ->
    Map metadata = md(object)
    def geometry = object.getROI()?.getGeometry()
    if (geometry == null || object.getParent() == null ||
            object.getROI().getImagePlane() != domain.object.getROI().getImagePlane() ||
            !geometry.coveredBy(domain.object.getROI().getGeometry())) return null
    String geometrySha256 = normalizedGeometrySha256(geometry) as String
    if (!(geometrySha256 ==~ /[0-9a-f]{64}/)) return null
    contentDigest([
        analysis_domain_id: domain.identity.analysis_domain_id,
        analysis_domain_revision_sha256: domain.identity.content_sha256,
        analysis_domain_set_sha256: domainSetSha256,
        classification: object.getClassification(),
        geometry_sha256: geometrySha256,
        parent_path_object_id: object.getParent().getID().toString(),
        path_object_id: object.getID().toString(),
        pipeline_active: metadata.get("pipeline_active"),
        type: "primary", type_id: metadata.get("primary_class_id"),
    ])
}
List primary = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    if (m.get("pipeline_stage") != "primary_classifier") return false
    Map domain = activeDomainsById[m.get("analysis_domain_id") as String]
    domain != null &&
        m.get("pipeline_active") == "true" && m.get("pipeline_run_id") == runId &&
        m.get("generation_id") == generationId && m.get("slide_id") == slideId &&
        m.get("section_id") == sectionId && m.get("analysis_domain_revision_sha256") ==
            domain.identity.content_sha256 && m.get("domain_set_sha256") == domainSetSha256 &&
        m.get("primary_class_id") != null && m.get("roi_id") &&
        object.getParent() == domain.object &&
        m.get("object_revision_sha256") == livePrimaryObjectRevisionSha256(object, domain)
}
// i) Match the accepted primary tables to the live annotations once. Count
// exclusions apply to both segmentation scopes, while the saved source rows
// remain available for review.
if (!(request.primary_classification_skipped instanceof Boolean) ||
        !(request.active_primary_outputs instanceof List))
    throw new IllegalArgumentException("Segmentation requires its accepted primary classification choice")
boolean primaryClassificationSkipped = request.primary_classification_skipped as boolean
if (primaryClassificationSkipped && (segmentation.scope != "accepted_tissue" ||
        !(request.active_primary_outputs as List).isEmpty()))
    throw new IllegalArgumentException("Skipped primary classification requires accepted analysis regions without primary outputs")
Set primaryOutputFields = [
    "analysis_domain_id", "analysis_domain_revision", "analysis_domain_revision_sha256",
    "analysis_domain_path_object_id", "domain_set_sha256", "section_id",
    "producer_attempt_id", "roi_inventory", "domain_status",
] as Set
Map primaryByDomain = activeDomainsById.collectEntries { domainId, domain -> [(domainId): [:]] }
primary.each { object ->
    Map metadata = md(object)
    Map rows = primaryByDomain[metadata.get("analysis_domain_id")]
    if (rows.put(metadata.get("roi_id"), object) != null)
        throw new IllegalStateException("Live primary ROI IDs are duplicated")
}
Set selectedDomainIds = new HashSet()
Set suppliedDomainIds = new HashSet()
List eligiblePrimary = []
(request.active_primary_outputs as List).each { raw ->
    if (!(raw instanceof Map) || raw.keySet() != primaryOutputFields)
        throw new IllegalStateException("Segmentation primary output schema is invalid")
    Map output = raw as Map
    String domainId = output.analysis_domain_id as String
    Map domain = activeDomainsById[domainId]
    if (domain == null || !suppliedDomainIds.add(domainId) || output.section_id != sectionId ||
            !(output.analysis_domain_revision instanceof Number) ||
            (output.analysis_domain_revision as Number).intValue() != (domain.identity.revision as Number).intValue() ||
            (output.analysis_domain_revision as Number).doubleValue() != (output.analysis_domain_revision as Number).intValue() ||
            output.analysis_domain_revision_sha256 != domain.identity.content_sha256 ||
            output.analysis_domain_path_object_id != domain.identity.source_path_object_id ||
            output.domain_set_sha256 != domainSetSha256 || !(output.roi_inventory instanceof Map) ||
            !(output.producer_attempt_id instanceof String) ||
            !(output.domain_status in ["active", "excluded_below_minimum", "excluded_by_primary_count_review"]))
        throw new IllegalStateException("Segmentation primary result differs from the accepted analysis region")
    boolean included = output.domain_status == "active"
    if (included) selectedDomainIds.add(domainId)
    Set observed = new HashSet()
    RoiInventory.read(MifProjectStageBootstrapV1.producerOutputRoot(request, config, output.producer_attempt_id as String), output.roi_inventory as Map,
            RoiInventory.member(output.producer_attempt_id as String), { RoiInventory.Row row ->
        def object = primaryByDomain[domainId][row.roiId()]
        Map metadata = object == null ? [:] : md(object)
        if (object == null || object.getID().toString() != row.pathObjectId() ||
                metadata.get("object_revision_sha256") != row.objectRevisionSha256() ||
                metadata.get("primary_class_id") != row.classId())
            throw new IllegalStateException("Segmentation primary ROI changed after classifier acceptance")
        observed.add(row.roiId())
        if (included && row.classId() != "Ignore") eligiblePrimary.add(object)
    } as Consumer<RoiInventory.Row>)
    if (observed != primaryByDomain[domainId].keySet())
        throw new IllegalStateException("Segmentation primary table does not match the complete live ROI set")
}
if (primaryClassificationSkipped) {
    selectedDomainIds.addAll(activeDomainsById.keySet())
} else if (suppliedDomainIds != activeDomainsById.keySet()) {
    throw new IllegalStateException("Segmentation primary results do not cover the section's analysis regions")
}
boolean skippedExcludedDomains = selectedDomainIds.isEmpty()
List includedDomains = activeDomains.findAll { selectedDomainIds.contains(it.identity.analysis_domain_id) }
def unionGeometry = skippedExcludedDomains ? section.getROI().getGeometry().getFactory().createPolygon() :
    UnaryUnionOp.union(includedDomains.collect { it.object.getROI().getGeometry() })
if (segmentation.scope == "configured_union") {
    if (!skippedExcludedDomains) {
        if (eligiblePrimary.isEmpty()) throw new IllegalStateException("No non-Ignore primary region is available for configured segmentation")
        unionGeometry = unionGeometry.intersection(UnaryUnionOp.union(eligiblePrimary.collect { it.getROI().getGeometry() }))
    }
} else if (segmentation.scope != "accepted_tissue") {
    throw new IllegalArgumentException("Unsupported segmentation scope")
}
if (unionGeometry == null || !unionGeometry.isValid() ||
        (!skippedExcludedDomains && (unionGeometry.isEmpty() || !unionGeometry.coveredBy(section.getROI().getGeometry()))))
    throw new IllegalStateException("Segmentation union is empty, invalid, or outside its TissueSection")
def normalizedUnion = unionGeometry.copy();
normalizedUnion.normalize()
String unionGeometrySha256 = MessageDigest.getInstance("SHA-256").digest(
    new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedUnion)
).encodeHex().toString()
def unionParent = skippedExcludedDomains ? null : PathObjects.createAnnotationObject(
    GeometryTools.geometryToROI(unionGeometry, section.getROI().getImagePlane()))
List<Map> domainIdentity = activeDomains.collect { new TreeMap(it.identity as Map) }
String segmentationConfigSha256 = contentDigest(segmentation)
Map segmentationDomainIdentity = [
    scope: segmentation.scope, domain_set_sha256: domainSetSha256,
    active_domain_revisions: domainIdentity, union_geometry_sha256: unionGeometrySha256,
]
if (request.segmentation_settings_revision != null) {
    Map revision = request.segmentation_settings_revision as Map
    // A revised detection is a distinct population even when its settings return
    // to an earlier value. Old component/layout and edit references stay historical.
    // Gson reads request numbers as doubles; hash the typed revision's integers.
    segmentationDomainIdentity.segmentation_settings_revision_sha256 = contentDigest([
        predecessor_node_id: revision.predecessor_node_id,
        revision: (revision.revision as Number).longValue(), settings: segmentation,
    ])
}
String segmentationDomainSha256 = contentDigest(segmentationDomainIdentity)
String populationDigest = contentDigest([
    schema_version: "segmentation-population/v2", canonical_image_id: canonicalImageId,
    canonical_image_sha256: canonicalImageSha256, slide_id: slideId,
    tissue_generation_id: generationId, section_id: sectionId,
    tissue_roi_id: md(section).get("roi_id"),
    segmentation_config_sha256: segmentationConfigSha256,
    segmentation_domain_sha256: segmentationDomainSha256,
])
String populationId = "segmentation-population/v2/${populationDigest}"
if (request.segmentation_population_id != null && request.segmentation_population_id != populationId)
    throw new IllegalStateException("Stored segmentation population does not match current config/domain")
String owner = "segmentation|${populationId}"
Map unionMetadata = unionParent == null ? [:] : md(unionParent)
unionMetadata.put("pipeline_stage", "segmentation_union_temp")
unionMetadata.put("pipeline_owner", owner);
unionMetadata.put("pipeline_run_id", runId)
unionMetadata.put("generation_id", generationId);
unionMetadata.put("slide_id", slideId)
unionMetadata.put("section_id", sectionId)
String selectProcessingDevice(String requested) {
    if (!(requested in ["auto","cpu","gpu","mps"]))
        throw new IllegalArgumentException("Select automatic, CPU, NVIDIA GPU or Apple Metal segmentation.")
    org.mif.inference.NativeModelRuntime.prepareDevice(requested)
    def engine = ai.djl.engine.Engine.getInstance()
    List candidates = requested == "auto" ? [] : [requested]
    if(requested == "auto") {
        if(engine.getGpuCount()>0)candidates.add("gpu")
        if(System.getProperty("os.name").toLowerCase().contains("mac") && System.getProperty("os.arch") in ["aarch64","arm64"])candidates.add("mps")
        candidates.add("cpu")
    }
    for(String candidate:candidates) {
        try {
            def manager=engine.newBaseManager(ai.djl.Device.fromName(candidate))
            try {
                def probe=manager.ones(new ai.djl.ndarray.types.Shape(4,4))
                if(probe.sum().getFloat()!=16f)throw new IllegalStateException("Device arithmetic check failed")
            } finally {manager.close()}
            println("InstanSeg execution device: "+candidate)
            return candidate
        } catch(Throwable error) {
            if(requested!="auto")throw new IllegalArgumentException("The requested segmentation device is unavailable in the installed native runtime.",error)
            println("Segmentation device "+candidate+" unavailable: "+error.message)
        }
    }
    throw new IllegalStateException("No working segmentation device was found.")
}

def hierarchy = getCurrentHierarchy()
def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    Files.newInputStream(path).withCloseable { input ->
        byte[] buffer = new byte[1024 * 1024]; int count
        while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count)
    }
    digest.digest().encodeHex().toString()
}
def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
}
Path baseRoot = request.containsKey("operational_output_dir") ?
    MifProjectStageBootstrapV1.outputRoot(request, config).resolve("cell-datasets/segmentation") :
    Paths.get(request.cell_dataset_directory as String).toAbsolutePath().normalize()
if (!(request.cell_dataset_directory instanceof String) || !Paths.get(request.cell_dataset_directory as String).isAbsolute())
    throw new IllegalArgumentException("Segmentation requires its durable cell dataset directory")
Files.createDirectories(baseRoot)
Path baseDirectory = baseRoot.resolve(populationDigest)
Path descriptorPath = baseDirectory.resolve("dataset.json")
Map originalProperties = ["mif.cell_datasets", "mif.cell_components", "mif.cell_edits", "mif.segmentation_populations"].collectEntries { [(it): boundImageData.getProperty(it)] }
byte[] originalManifestBytes = Files.exists(manifestPath) ? Files.readAllBytes(manifestPath) : null
byte[] originalResultBytes = Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null
boolean unionParentAttached = false
boolean persistenceAttempted = false
Path privateWork = null
def results = null
boolean reused = Files.isRegularFile(descriptorPath)
Map description
Map datasetReference
Map domainScope = activeDomains.collectEntries { domain ->
    Map identity = domain.identity as Map
    [(identity.analysis_domain_id as String): [
        pipeline_stage: "segmentation", slide_id: slideId, section_id: sectionId,
        generation_id: generationId, tissue_generation_id: generationId,
        segmentation_population_id: populationId, segmentation_config_sha256: segmentationConfigSha256,
        segmentation_domain_sha256: segmentationDomainSha256, domain_set_sha256: domainSetSha256,
        analysis_domain_id: identity.analysis_domain_id, analysis_domain_kind: identity.kind,
        analysis_domain_revision: (identity.revision as Number).intValue(),
        analysis_domain_revision_sha256: identity.content_sha256,
        tissue_roi_id: md(section).get("roi_id")]]
}
Map scope = [population_id: populationId, segmentation_population_id: populationId,
    canonical_image_id: canonicalImageId, canonical_image_sha256: canonicalImageSha256,
    sample_id: request.sample_id ?: slideId, image_entry_id: requestedEntryId,
    project_entry_id: requestedEntryId, slide_id: slideId, section_id: sectionId,
    pipeline_stage: "segmentation", pipeline_owner: owner, pipeline_run_id: runId,
    generation_id: generationId, tissue_generation_id: generationId,
    tissue_roi_id: md(section).get("roi_id"), tissue_path_object_id: section.getID().toString(),
    segmentation_source_run_id: runId, segmentation_config_sha256: segmentationConfigSha256,
    segmentation_domain_sha256: segmentationDomainSha256, domain_set_sha256: domainSetSha256,
    union_geometry_sha256: unionGeometrySha256, domains: domainScope,
    native_geometry_factory: "qupath-default-packed-float",
    x_um_per_px: boundImageData.getServer().getPixelCalibration().getPixelWidthMicrons(),
    y_um_per_px: boundImageData.getServer().getPixelCalibration().getPixelHeightMicrons()]
if (alternativeDetector != null) {
    scope.segmentation_method = segmentation.method
    scope.cell_boundary_source = segmentation.method == "cellpose" ? "learned_cell" :
        (segmentation.parameters.cell_expansion_um as Number).doubleValue() > 0 ? "estimated_nuclear_expansion" : "nuclear_outline"
    scope.nucleus_boundary_source = segmentation.method == "cellpose" ? "not_segmented" : "stardist_nucleus"
}
def domainIndex = new org.locationtech.jts.index.strtree.STRtree()
includedDomains.each { domain -> def geometry = domain.object.getROI().getGeometry(); domainIndex.insert(geometry.envelopeInternal, [domain: domain, geometry: geometry]) }
domainIndex.build()
def compartmentIndex = new org.locationtech.jts.index.strtree.STRtree()
primary.each { object ->
    Map values = md(object)
    def region = new org.mif.cells.PrimaryCompartmentMembership.Region(
        values.get("roi_id"), values.get("primary_class_id"), object.getROI())
    compartmentIndex.insert(region.geometry().envelopeInternal,
        [domain_id: values.get("analysis_domain_id"), region: region])
}
compartmentIndex.build()
try {
    if (!skippedExcludedDomains && alternativeDetector == null) {
        MifInstanSegSelection.verifyInventory(segmentation, request.segmentation_model_evidence)
        MifInstanSegScaleGate.validate(boundImageData.getServerMetadata().pixelSizeCalibrated(),
            boundImageData.getServer().getPixelCalibration(), Paths.get(segmentation.model_path as String))
    }
    if (!reused) {
        privateWork = Files.createTempDirectory(baseRoot, ".${populationDigest}.native-")
        def stream = new org.mif.segmentation.NativeCellStream(privateWork.resolve("cells"),
            gson.toJsonTree(scope).asJsonObject, populationDigest)
        def outsideDomain = new java.util.concurrent.atomic.AtomicLong()
        try {
            if (!skippedExcludedDomains) {
            def acceptedCell = { cell ->
                    def centroid = cell.getROI().getGeometry().centroid
                    if (centroid.isEmpty() || !Double.isFinite(centroid.x) || !Double.isFinite(centroid.y))
                        throw new IllegalStateException("The detector returned a cell outline without a finite centroid")
                    List matchingDomains = domainIndex.query(centroid.envelopeInternal).findAll { centroid.coveredBy(it.geometry) }.collect { it.domain }
                    if (matchingDomains.isEmpty()) {
                        outsideDomain.incrementAndGet()
                        return
                    }
                    if (matchingDomains.size() > 1)
                        throw new IllegalStateException("Cell centroid at (${centroid.x}, ${centroid.y}) lies in multiple included analysis regions: " +
                            matchingDomains.collect { it.identity.analysis_domain_id }.join(", "))
                    Map domain = matchingDomains.first() as Map
                    List compartmentCandidates = compartmentIndex.query(centroid.envelopeInternal)
                        .findAll { it.domain_id == domain.identity.analysis_domain_id }.collect { it.region }
                    String primaryCompartment = org.mif.cells.PrimaryCompartmentMembership.select(centroid, compartmentCandidates)
                    String geometryKey = contentDigest([cell_geometry_wkt: cell.getROI().getGeometry().toText(),
                        nucleus_geometry_wkt: cell.getNucleusROI()?.getGeometry()?.toText() ?: ""])
                    String cellId = "cell-${UUID.nameUUIDFromBytes((populationId + '|' + geometryKey).getBytes(StandardCharsets.UTF_8))}"
                    Map metadataValues = md(cell)
                    metadataValues.putAll(domainScope[domain.identity.analysis_domain_id as String].collectEntries { key, value -> [(key): value.toString()] })
                    metadataValues.putAll([cell_id: cellId, pipeline_owner: owner, pipeline_run_id: runId,
                        segmentation_source_run_id: runId, segmentation_model_version: segmentation.model_version as String,
                        x_level0: Double.toString(centroid.x), y_level0: Double.toString(centroid.y),
                        primary_compartment: primaryCompartment,
                        secondary_roi_ids: "[]", coordinate_system: "global_level0"])
                    stream.write(cell)
                } as java.util.function.Consumer
            if (alternativeDetector != null) {
                results = alternativeDetector.detect(segmentation, boundImageData, unionParent, privateWork, acceptedCell)
            } else {
            String processingDevice = selectProcessingDevice(segmentation.device as String)
            section.addChildObject(unionParent); unionParentAttached = true
            def detector = qupath.ext.instanseg.core.InstanSeg.builder()
                .modelPath(segmentation.model_path as String).device(processingDevice)
                .nThreads((segmentation.workers as Number).intValue())
                .tileDims((segmentation.tile_size_px as Number).intValue())
                .interTilePadding((segmentation.padding_px as Number).intValue())
                .makeMeasurements(false).randomColors(false).build()
            long operationBytes = Long.parseLong(System.getenv("MIF_COMPUTE_MEMORY_BYTES") ?: "1073741824")
            long objects = operationBytes / 4L
            if (objects < 8L * 1024 * 1024) throw new IllegalStateException("Segmentation admission requires at least one bounded native object group")
            long metadata = Long.parseLong(System.getenv("MIF_METADATA_CACHE_BYTES") ?: "33554432")
            results = org.mif.segmentation.NativeInstanSeg.detect(detector, boundImageData, unionParent,
                privateWork.resolve("frontier.bin"), (segmentation.workers as Number).intValue(), objects, metadata,
                acceptedCell)
            }
            if (results == null || results.wasInterrupted() || results.nTilesFailed() > 0 ||
                    results.nTilesProcessed() <= 0 || results.nPixelsProcessed() <= 0)
                throw new IllegalStateException("Segmentation did not complete every required tile")
            }
            Path streamPath = stream.complete()
            String commandJson = System.getenv("MIF_CELL_DATASET_IMPORT_COMMAND")
            List command = commandJson == null ? null : gson.fromJson(commandJson, List.class)
            if (command == null || command.isEmpty() || command.any { !(it instanceof String) || !it })
                throw new IllegalStateException("The installed runtime did not provide its canonical cell importer")
            command = new ArrayList(command)
            Path responsePath = privateWork.resolve("import-result.json")
            command.addAll([streamPath.toString(), baseDirectory.toString(), "--source-sha256", populationDigest, "--result-json", responsePath.toString()])
            Process process = new ProcessBuilder(command as List<String>)
                .redirectOutput(privateWork.resolve("import-stdout.log").toFile()).redirectError(privateWork.resolve("import.log").toFile()).start()
            try {
                if (process.waitFor() != 0) throw new IllegalStateException("Canonical cell publication failed; see " + privateWork.resolve("import.log"))
            } catch (InterruptedException interrupted) {
                process.destroyForcibly(); process.waitFor(); Thread.currentThread().interrupt(); throw interrupted
            }
            Map imported = gson.fromJson(Files.readString(responsePath), Map.class) as Map
            if (Paths.get(imported.dataset as String).toAbsolutePath().normalize() != descriptorPath ||
                    (imported.row_count as Number).longValue() + outsideDomain.get() != (skippedExcludedDomains ? 0 : results.nObjectsDetected()) ||
                    imported.sha256 != sha256(descriptorPath))
                throw new IllegalStateException("Canonical cell publication does not match finalized native cells")
        } finally { stream.close() }
    }
    description = gson.fromJson(Files.readString(descriptorPath), Map.class) as Map
    if (description.schema_version != "mif-cell-dataset/v1" || description.source_sha256 != populationDigest ||
            description.scope.population_id != populationId || description.scope.section_id != sectionId ||
            description.scope.canonical_image_id != canonicalImageId || description.scope.canonical_image_sha256 != canonicalImageSha256 ||
            !(description.domain_inventories instanceof Map) ||
            (description.domain_inventories as Map).keySet() != domainScope.keySet())
        throw new IllegalStateException("Completed cell dataset belongs to another scientific population")
    datasetReference = [path: descriptorPath.toString(), sha256: sha256(descriptorPath),
        schema_version: description.schema_version, row_count: (description.row_count as Number).longValue(), source_sha256: populationDigest]
    List<Map> domainCellInventories = activeDomains.collect { domain ->
        String domainId = domain.identity.analysis_domain_id as String
        Map summary = description.domain_inventories[domainId] as Map
        [analysis_domain_id: domainId, kind: domain.identity.kind,
            revision: (domain.identity.revision as Number).intValue(), content_sha256: domain.identity.content_sha256,
            cell_count: (summary.cell_count as Number).longValue(), cell_ids_sha256: summary.cell_ids_sha256]
    }
    String cellEvidenceSha256 = datasetReference.sha256
    String sectionPopulationSha256 = contentDigest([segmentation_population_id: populationId,
        cell_dataset: datasetReference, domain_cell_inventories: domainCellInventories,
        domain_cell_mapping_sha256: description.domain_cell_mapping_sha256, cell_evidence_sha256: cellEvidenceSha256])
    Map segmentationPopulation = [schema_version: "segmentation-population/v2", segmentation_population_id: populationId,
        canonical_image_id: canonicalImageId, canonical_image_sha256: canonicalImageSha256, slide_id: slideId,
        tissue_generation_id: generationId, section_id: sectionId, tissue_roi_id: md(section).get("roi_id"),
        segmentation_config_sha256: segmentationConfigSha256, segmentation_domain_sha256: segmentationDomainSha256,
        domain_set_sha256: domainSetSha256, union_geometry_sha256: unionGeometrySha256, active_domain_revisions: domainIdentity,
        cell_dataset: datasetReference,
        domain_cell_inventories: domainCellInventories, domain_cell_mapping_sha256: description.domain_cell_mapping_sha256,
        cell_evidence_sha256: cellEvidenceSha256, section_population_sha256: sectionPopulationSha256]
    Map outputs = segmentationPopulation.subMap(["segmentation_population_id", "segmentation_config_sha256",
        "segmentation_domain_sha256", "domain_set_sha256", "union_geometry_sha256", "cell_dataset",
        "domain_cell_inventories", "domain_cell_mapping_sha256", "cell_evidence_sha256", "section_population_sha256"])
    outputs.putAll([cell_count: datasetReference.row_count, cell_ids_sha256: description.cell_ids_sha256])
    outputs.putAll([section_id: sectionId, segmentation_passes: reused || skippedExcludedDomains ? 0 : 1,
        segmentation_skipped_excluded_domains: skippedExcludedDomains,
        segmentation_reused_without_recomputation: reused,
        domain_cell_counts: domainCellInventories.collectEntries { [(it.analysis_domain_id): it.cell_count] },
        segmentation_population: segmentationPopulation,
        segmentation_status: datasetReference.row_count == 0 ? "completed_zero_cells" : "completed_with_cells",
        tiles_processed: reused || skippedExcludedDomains ? 0 : results.nTilesProcessed(),
        pixels_processed: reused || skippedExcludedDomains ? 0 : results.nPixelsProcessed(),
        coordinate_system: "global_level0"])
    if (!skippedExcludedDomains && alternativeDetector == null) MifInstanSegSelection.verifyInventory(segmentation, request.segmentation_model_evidence)
    Map accepted = originalProperties["mif.segmentation_populations"] == null ? [:] : gson.fromJson(originalProperties["mif.segmentation_populations"] as String, Map.class)
    accepted[sectionId] = segmentationPopulation
    boundImageData.setProperty("mif.segmentation_populations", canonicalGson.toJson(accepted))
    org.mif.cells.NativeCellScope.bind(boundImageData, descriptorPath.toString(), sectionId)
    org.mif.cells.NativeCellScope.open(boundImageData, sectionId).join()
    if (unionParentAttached) { hierarchy.removeObject(unionParent, false); unionParentAttached = false }
    fireHierarchyUpdate()
    if (mifAuthenticatedHeadlessProjectStage && boundImageData.isChanged()) {
        persistenceAttempted = true; boundEntry.saveImageData(boundImageData); boundProject.syncChanges()
    }
    atomicJson(manifestPath, [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId], generations: [], object_outputs: outputs])
    Map result = [status: "completed", stage: envelope.stage, slide_id: slideId, generation_id: generationId,
        interaction_reason: null, affected_object_ids: [], message: null, allowed_actions: [],
        resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
    mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
        generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]])
} catch (Throwable error) {
    org.mif.cells.NativeCellScope.close(boundImageData)
    originalProperties.each { key, value -> if (value == null) boundImageData.removeProperty(key) else boundImageData.setProperty(key, value) }
    [ (resultPath): originalResultBytes, (manifestPath): originalManifestBytes ].each { path, bytes ->
        if (bytes == null) Files.deleteIfExists(path) else Files.write(path, bytes)
    }
    if (persistenceAttempted) {
        try { boundEntry.saveImageData(boundImageData); boundProject.syncChanges() }
        catch (Throwable rollbackError) { error.addSuppressed(rollbackError) }
    }
    throw error
} finally {
    if (unionParentAttached) hierarchy.removeObject(unionParent, false)
    if (privateWork != null && Files.isRegularFile(descriptorPath)) Files.walk(privateWork).withCloseable { paths ->
        paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) }
    }
}
