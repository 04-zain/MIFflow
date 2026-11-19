// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Atomically finalize semantic ROI results and canonical cell memberships. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.Collections
import java.util.IdentityHashMap
import java.util.UUID
import java.util.function.Function
import org.mif.cells.NativeCellScope
import org.mif.roi.SemanticMembershipIndex
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import org.locationtech.jts.geom.*
import org.locationtech.jts.io.WKBWriter
import qupath.lib.roi.GeometryTools
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "roi_validation/09_finalize_semantic_roi_results.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
def project = getProject()
def imageData = getCurrentImageData()
def entry = getProjectEntry()
if (project == null || imageData == null || entry == null)
    throw new IllegalStateException("Semantic reconciliation requires an open saved project image")
String envelopeText = Files.readString(Paths.get(args[0] as String))
JsonElement envelopeJson = JsonParser.parseString(envelopeText)
Map envelope = gson.fromJson(envelopeText, Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "semantic_validation")
    throw new IllegalArgumentException("Invalid semantic ROI request envelope")
Map request = envelope.request as Map
Map config = request.resolved_config as Map
if (config == null || !(config.output_dir instanceof String) || !config.output_dir)
    throw new IllegalArgumentException("Authenticated resolved_config output_dir is required")
def boundProject = project
def boundImageData = imageData
def boundEntry = entry
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
final String semanticAuthorityAssetPath =
    "roi_validation/00_authenticate_semantic_live_authority.groovy"
List semanticAuthorityEvidence = (authenticatedManifest.assets as List).findAll { raw ->
    raw instanceof Map && raw.keySet() == ["path", "sha256"] as Set &&
        raw.path == semanticAuthorityAssetPath
}
Path semanticAuthorityPath = requestedBundleRoot.resolve(
    semanticAuthorityAssetPath
).normalize()
if (semanticAuthorityEvidence.size() != 1 ||
        !((semanticAuthorityEvidence.first().sha256 as String) ==~ /[0-9a-f]{64}/) ||
        semanticAuthorityPath.toRealPath() != semanticAuthorityPath ||
        Files.isSymbolicLink(semanticAuthorityPath) ||
        !Files.isRegularFile(semanticAuthorityPath) ||
        MessageDigest.getInstance("SHA-256").digest(
            Files.readAllBytes(semanticAuthorityPath)
        ).encodeHex().toString() != semanticAuthorityEvidence.first().sha256)
    throw new IllegalStateException("Semantic live-authority helper is invalid")
Closure authenticateSemanticLiveAuthority =
    evaluate(semanticAuthorityPath.toFile()) as Closure
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
String slideId = envelope.slide_id as String
String runId = envelope.run_id as String
String sectionId = request.section_id as String
String generationId = request.generation_id as String
String analysisDomainId = request.analysis_domain_id as String
if (!sectionId || !generationId) throw new IllegalArgumentException("Exact section and generation are required")
String terminalReviewStatus = request.terminal_review_status as String
String requestedReviewAction = request.action in ["accept", "reject"] ?
    request.action as String : null
if (!(terminalReviewStatus in ["accepted", "rejected"]) ||
        (requestedReviewAction == "accept" && terminalReviewStatus != "accepted") ||
        (requestedReviewAction == "reject" && terminalReviewStatus != "rejected") ||
        (requestedReviewAction == null && terminalReviewStatus != "accepted"))
    throw new IllegalArgumentException(
        "Explicit terminal review status and review action are inconsistent"
    )
def sha256File = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { input ->
        DigestInputStream stream = new DigestInputStream(input, digest)
        byte[] buffer = new byte[1024 * 1024]
        while (stream.read(buffer) >= 0) { }
    }
    digest.digest().encodeHex().toString()
}
Closure canonicalJsonElement
Closure canonicalString = { String value ->
    StringBuilder builder = new StringBuilder("\"")
    for (int offset = 0; offset < value.length();) {
        int codePoint = value.codePointAt(offset)
        offset += Character.charCount(codePoint)
        switch (codePoint) {
            case 0x08: builder.append("\\b");
            break
            case 0x09: builder.append("\\t");
            break
            case 0x0a: builder.append("\\n");
            break
            case 0x0c: builder.append("\\f");
            break
            case 0x0d: builder.append("\\r");
            break
            case 0x22: builder.append("\\\"");
            break
            case 0x5c: builder.append("\\\\");
            break
            default:
                if (codePoint < 0x20 || codePoint > 0x7e) {
                    if (codePoint <= 0xffff) {
                        builder.append(String.format("\\u%04x", codePoint))
                    } else {
                        int shifted = codePoint - 0x10000
                        builder.append(String.format("\\u%04x", 0xd800 + (shifted >> 10)))
                        builder.append(String.format("\\u%04x", 0xdc00 + (shifted & 0x3ff)))
                    }
                } else {
                    builder.appendCodePoint(codePoint)
                }
        }
    }
    builder.append("\"").toString()
}
canonicalJsonElement = { JsonElement value ->
    if (value == null || value.isJsonNull()) return "null"
    if (value.isJsonPrimitive()) {
        def primitive = value.getAsJsonPrimitive()
        if (primitive.isString()) return canonicalString(primitive.getAsString())
        return primitive.toString()
    }
    if (value.isJsonArray()) {
        return "[" + value.getAsJsonArray().collect {
            canonicalJsonElement(it as JsonElement)
        }.join(",") + "]"
    }
    def object = value.getAsJsonObject()
    return "{" + object.keySet().toList().sort().collect { String key ->
        canonicalString(key) + ":" + canonicalJsonElement(object.get(key))
    }.join(",") + "}"
}
JsonElement requestJson = envelopeJson.getAsJsonObject().get("request")
JsonElement requestRowsJson
List<Map> rows
if (request.semantic_results_path) {
    String requestRowsText = Files.readString(Paths.get(request.semantic_results_path as String))
    requestRowsJson = JsonParser.parseString(requestRowsText)
    rows = gson.fromJson(requestRowsText, List) as List<Map>
} else {
    requestRowsJson = requestJson.getAsJsonObject().get("semantic_results")
    if (requestRowsJson == null) requestRowsJson = new JsonArray()
    rows = (request.semantic_results ?: []) as List<Map>
}
Map finalization = request.semantic_finalization as Map
Set finalizationFields = ["schema_version", "analysis_domain_id", "section_id",
    "tissue_generation_id", "pipeline_run_id", "semantic_result_set_sha256",
    "semantic_result_artifact", "active_secondary_roi_ids"] as Set
if (!analysisDomainId || !(finalization instanceof Map) ||
        finalization.keySet() != finalizationFields ||
        finalization.schema_version != "semantic-finalization/v2" ||
        finalization.analysis_domain_id != analysisDomainId ||
        finalization.section_id != sectionId ||
        finalization.tissue_generation_id != generationId ||
        finalization.pipeline_run_id != runId ||
        !((finalization.semantic_result_set_sha256 as String) ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException(
        "Exact semantic-finalization/v2 identity is required before mutation"
    )
String semanticResultSetSha256 = finalization.semantic_result_set_sha256 as String
Map semanticResultArtifact = finalization.semantic_result_artifact as Map
if (!(semanticResultArtifact instanceof Map) ||
        semanticResultArtifact.keySet() != ["schema_version", "path", "sha256", "record_count",
                                            "semantic_result_set_sha256"] as Set ||
        semanticResultArtifact.schema_version != "semantic-result-set/v1" ||
        semanticResultArtifact.semantic_result_set_sha256 != semanticResultSetSha256 ||
        !((semanticResultArtifact.sha256 as String) ==~ /[0-9a-f]{64}/) ||
        !(semanticResultArtifact.path instanceof String))
    throw new IllegalArgumentException("Exact semantic result artifact identity is required")
Path semanticResultArtifactPath = Paths.get(semanticResultArtifact.path as String)
    .toAbsolutePath().normalize()
if (!Files.isRegularFile(semanticResultArtifactPath) || Files.isSymbolicLink(semanticResultArtifactPath) ||
        sha256File(semanticResultArtifactPath) != semanticResultArtifact.sha256)
    throw new IllegalStateException("Immutable semantic result artifact checksum is invalid")
String semanticResultArtifactText = Files.readString(semanticResultArtifactPath)
JsonElement semanticResultArtifactJson = JsonParser.parseString(
    semanticResultArtifactText
)
Map semanticResultArtifactPayload = gson.fromJson(
    semanticResultArtifactText, Map
) as Map
boolean unifiedRegionReview = (semanticResultArtifactPayload?.semantic_results instanceof List) &&
    semanticResultArtifactPayload.semantic_results.every { it instanceof Map && it.schema_version == "semantic-result/v3" }
if (unifiedRegionReview) {
    if (terminalReviewStatus != "accepted" || requestedReviewAction != null)
        throw new IllegalArgumentException("Unified region review requires completed individual decisions")
} else if (rows.any { !(it instanceof Map) || it.review_status != terminalReviewStatus })
    throw new IllegalArgumentException("Every semantic mutation row must agree with terminal_review_status")
Set mutationRowFields = ["roi_id", "slide_id", "section_id", "analysis_domain_id",
    "path_object_id", "structure_id", "parent_roi_id", "semantic_profile",
    "semantic_status", "semantic_rule_results", "semantic_classification_status",
    "semantic_class_id", "semantic_classification_rule_results", "review_status",
    "profile_id", "profile_version", "profile_content_sha256",
    "evidence_snapshot_sha256", "geometry_sha256"] as Set
if (unifiedRegionReview) mutationRowFields.add("region_decision")
Set artifactRowFields = (mutationRowFields + [
    "schema_version", "tissue_generation_id", "pipeline_run_id"
]) as Set
if (unifiedRegionReview) artifactRowFields.add("measurement_results")
List artifactRows = semanticResultArtifactPayload?.semantic_results as List
if (!(semanticResultArtifactPayload instanceof Map) ||
        semanticResultArtifactPayload.keySet() != ["schema_version",
            "semantic_result_set_sha256", "semantic_results"] as Set ||
        semanticResultArtifactPayload.schema_version != "semantic-result-set/v1" ||
        semanticResultArtifactPayload.semantic_result_set_sha256 != semanticResultSetSha256 ||
        !(artifactRows instanceof List) ||
        semanticResultArtifact.record_count != artifactRows.size() ||
        !semanticResultArtifactJson.isJsonObject() ||
        artifactRows.any { !(it instanceof Map) || it.keySet() != artifactRowFields ||
            it.schema_version != (unifiedRegionReview ? "semantic-result/v3" : "semantic-result/v2") })
    throw new IllegalStateException("Immutable semantic result artifact content is invalid")
def semanticResultArtifactJsonObject = semanticResultArtifactJson.getAsJsonObject()
String canonicalArtifactResultSet = "{" +
    canonicalString("schema_version") + ":" +
    canonicalJsonElement(semanticResultArtifactJsonObject.get("schema_version")) + "," +
    canonicalString("semantic_results") + ":" +
    canonicalJsonElement(semanticResultArtifactJsonObject.get("semantic_results")) + "}"
String recomputedSemanticResultSetSha256 = MessageDigest.getInstance("SHA-256")
    .digest(canonicalArtifactResultSet.getBytes(StandardCharsets.UTF_8))
    .encodeHex().toString()
if (recomputedSemanticResultSetSha256 != semanticResultSetSha256)
    throw new IllegalStateException("Immutable semantic result artifact digest is invalid")
def artifactJsonRows = semanticResultArtifactJsonObject
    .getAsJsonArray("semantic_results")
JsonArray artifactMutationJsonRows = new JsonArray()
artifactJsonRows.each { JsonElement artifactJsonRow ->
    if (!artifactJsonRow.isJsonObject())
        throw new IllegalStateException("Immutable semantic result artifact row is invalid")
    JsonObject projected = new JsonObject()
    mutationRowFields.each { String field ->
        projected.add(field, artifactJsonRow.getAsJsonObject().get(field))
    }
    artifactMutationJsonRows.add(projected)
}
String canonicalArtifactMutationRows = canonicalJsonElement(artifactMutationJsonRows)
String canonicalRequestMutationRows = canonicalJsonElement(requestRowsJson)
if (!requestRowsJson.isJsonArray() ||
        rows.any { !(it instanceof Map) || it.keySet() != mutationRowFields } ||
        canonicalArtifactMutationRows != canonicalRequestMutationRows)
    throw new IllegalStateException(
        "Immutable semantic result artifact and request mutation rows differ"
    )
Map<String, JsonElement> acceptanceRuleJsonByRoi = [:]
Map<String, JsonElement> classificationRuleJsonByRoi = [:]
for (int index = 0; index < artifactRows.size(); index++) {
    acceptanceRuleJsonByRoi[artifactRows[index].roi_id as String] =
        artifactJsonRows.get(index).getAsJsonObject()
            .get("semantic_rule_results")
    classificationRuleJsonByRoi[artifactRows[index].roi_id as String] =
        artifactJsonRows.get(index).getAsJsonObject()
            .get("semantic_classification_rule_results")
}
List activeSecondaryIds = finalization.active_secondary_roi_ids as List
if (!(activeSecondaryIds instanceof List) ||
        activeSecondaryIds.any { !(it instanceof String) || !(it as String) } ||
        activeSecondaryIds.toSet().size() != activeSecondaryIds.size() ||
        activeSecondaryIds != activeSecondaryIds.toList().sort())
    throw new IllegalArgumentException("active_secondary_roi_ids must be sorted unique stable ROI IDs")
Set activeSecondaryIdSet = activeSecondaryIds as Set
def md = { object -> object.getMetadata() }
if (!analysisDomainId)
    throw new IllegalStateException("Semantic finalization requires its accepted analysis domain")
Map authenticatedSemanticAuthority = authenticateSemanticLiveAuthority(
    request, slideId, runId, generationId, requestedEntryId, gson
) as Map
def section = authenticatedSemanticAuthority.section
Map<String, Object> byId = authenticatedSemanticAuthority.roi_by_id as Map
Collection rois = byId.values()
Closure forEachCell = authenticatedSemanticAuthority.for_each_cell as Closure
long cellCount = authenticatedSemanticAuthority.cell_count as long
def cellProvider = authenticatedSemanticAuthority.cell_provider
String tissueRoiId = md(section).get("roi_id") as String
String biologicalRootId = analysisDomainId ?: tissueRoiId
List<String> existingIds = rois.collect { md(it).get("roi_id") as String }
if (existingIds.toSet().size() != existingIds.size())
    throw new IllegalStateException("Existing current biological ROIs contain duplicate roi_id")
List ids = rows.collect { it.roi_id }
if (ids.any { !(it instanceof String) || !(it as String) } || ids.toSet().size() != ids.size())
    throw new IllegalArgumentException("Semantic results contain blank or duplicate roi_id")

def ring = { sequence -> sequence.toCoordinateArray().collect { coordinate -> [coordinate.x, coordinate.y] } }
def polygonCoordinates = { Polygon polygon ->
    List rings = [ring(polygon.exteriorRing.coordinateSequence)]
    (0..<polygon.numInteriorRing).each { index ->
        rings << ring(polygon.getInteriorRingN(index).coordinateSequence)
    }
    rings
}
def geometryJson = { geometry ->
    if (geometry instanceof Polygon) return [type: "Polygon", coordinates: polygonCoordinates(geometry)]
    if (geometry instanceof MultiPolygon) return [type: "MultiPolygon", coordinates:
        (0..<geometry.numGeometries).collect { index -> polygonCoordinates(geometry.getGeometryN(index) as Polygon) }]
    throw new IllegalStateException("Unsupported polygon geometry ${geometry.geometryType}")
}
GeometryFactory factory = new GeometryFactory()
def point = { value ->
    if (!(value instanceof List) || value.size() != 2 ||
        value.any { !(it instanceof Number) || !Double.isFinite((it as Number).doubleValue()) })
        throw new IllegalArgumentException("Geometry positions must contain exactly two finite numbers")
    new Coordinate((value[0] as Number).doubleValue(), (value[1] as Number).doubleValue())
}
def polygon = { value ->
    if (!(value instanceof List) || value.isEmpty())
        throw new IllegalArgumentException("Polygon coordinates require at least one ring")
    List<LinearRing> parsedRings = value.collect { rawRing ->
        if (!(rawRing instanceof List) || rawRing.size() < 4)
            throw new IllegalArgumentException("Polygon rings require at least four positions")
        Coordinate[] coordinates = rawRing.collect { point(it) } as Coordinate[]
        if (!coordinates.first().equals2D(coordinates.last()))
            throw new IllegalArgumentException("Polygon rings must be closed")
        factory.createLinearRing(coordinates)
    }
    factory.createPolygon(parsedRings.first(), parsedRings.drop(1) as LinearRing[])
}
def parseGeometry = { value ->
    if (!(value instanceof Map) || value.keySet() != ["type", "coordinates"] as Set)
        throw new IllegalArgumentException("Corrected geometry must be exact GeoJSON Polygon or MultiPolygon")
    def geometry
    if (value.type == "Polygon") geometry = polygon(value.coordinates)
    else if (value.type == "MultiPolygon") {
        if (!(value.coordinates instanceof List) || value.coordinates.isEmpty())
            throw new IllegalArgumentException("MultiPolygon coordinates must be non-empty")
        geometry = factory.createMultiPolygon(value.coordinates.collect { polygon(it) } as Polygon[])
    } else throw new IllegalArgumentException("Corrected geometry must be Polygon or MultiPolygon")
    if (geometry.isEmpty() || !geometry.isValid() || geometry.dimension != 2)
        throw new IllegalArgumentException("Corrected geometry must be non-empty, valid and two-dimensional")
    geometry
}

Set requiredRowFields = ["roi_id", "slide_id", "section_id", "analysis_domain_id",
                         "path_object_id", "structure_id", "parent_roi_id",
                         "semantic_profile", "semantic_status", "semantic_rule_results",
                         "semantic_classification_status", "semantic_class_id",
                         "semantic_classification_rule_results",
                         "review_status", "profile_id", "profile_version",
                         "profile_content_sha256", "evidence_snapshot_sha256",
                         "geometry_sha256"] as Set
if (unifiedRegionReview) requiredRowFields.add("region_decision")
Set optionalRowFields = ["corrected_geometry", "original_predicted_geometry", "source",
                         "registration_id", "registration_qc"] as Set
Set allowed = requiredRowFields + optionalRowFields
Set semanticStatuses = ["accepted", "rejected", "indeterminate"] as Set
Set semanticClassificationStatuses = (unifiedRegionReview ?
    ["not_configured", "not_evaluated", "assigned", "cannot_assess"] :
    ["not_configured", "not_evaluated", "classified", "unclassified", "indeterminate"]) as Set
Set reviewStatuses = ["accepted", "rejected", "needs_review", "not_reviewed"] as Set
Map<String, Geometry> correctedById = [:]
rows.each { row ->
    if (!(row instanceof Map) || !requiredRowFields.every { row.containsKey(it) } ||
            !allowed.containsAll(row.keySet()))
        throw new IllegalArgumentException("Semantic result fields do not match the mutation schema")
    def object = byId[row.roi_id]
    if (object == null || row.slide_id != slideId || row.section_id != sectionId)
        throw new IllegalArgumentException("Semantic result contains unknown, tissue, stale, or cross-boundary roi_id")
    Map current = md(object)
    if (row.path_object_id != object.getID().toString())
        throw new IllegalArgumentException("Semantic result PathObject identity is stale")
    if (row.structure_id != current.get("structure_id"))
        throw new IllegalArgumentException("Semantic result structure identity is stale")
    if (!((row.profile_content_sha256 as String) ==~ /[0-9a-f]{64}/) ||
            !((row.evidence_snapshot_sha256 as String) ==~ /[0-9a-f]{64}/) ||
            !((row.geometry_sha256 as String) ==~ /[0-9a-f]{64}/))
        throw new IllegalArgumentException("Semantic result evidence digests are invalid")
    Geometry currentGeometry = object.getROI().getGeometry()
    Geometry normalized = currentGeometry.copy();
    normalized.normalize()
    String currentGeometrySha256 = MessageDigest.getInstance("SHA-256")
        .digest(new WKBWriter(2, true).write(normalized)).encodeHex().toString()
    if (row.geometry_sha256 != currentGeometrySha256)
        throw new IllegalArgumentException("Semantic result geometry digest is stale")
    if (analysisDomainId && (row.analysis_domain_id != analysisDomainId ||
            current.get("analysis_domain_id") != analysisDomainId))
        throw new IllegalArgumentException("Semantic result crosses its exact analysis domain")
    if (!object.getROI().getGeometry().coveredBy(section.getROI().getGeometry()))
        throw new IllegalStateException("Candidate ROI is outside its canonical mIF TissueSection")
    if (!(row.semantic_status instanceof String) || !semanticStatuses.contains(row.semantic_status))
        throw new IllegalArgumentException("semantic_status is invalid")
    String classificationStatus = row.semantic_classification_status as String
    String semanticClassId = row.semantic_class_id as String
    List classificationResults = row.semantic_classification_rule_results as List
    if (!semanticClassificationStatuses.contains(classificationStatus) ||
            !(classificationResults instanceof List))
        throw new IllegalArgumentException("semantic classification fields are invalid")
    if (row.semantic_class_id != null && (!(row.semantic_class_id instanceof String) || !semanticClassId))
        throw new IllegalArgumentException("semantic classification class ID is invalid")
    if (unifiedRegionReview) {
        Map decision = row.region_decision instanceof Map ? row.region_decision as Map : null
        Set decisionFields = ["schema_version", "slide_id", "section_id", "analysis_domain_id",
            "candidate_roi_id", "profile_id", "profile_version", "profile_content_sha256",
            "geometry_digest", "evidence_digest", "checkpoint_id", "reviewer_id", "action",
            "rationale", "subtype_status", "semantic_class_id"] as Set
        if (decision == null || decision.keySet() != decisionFields ||
                decision.schema_version != "semantic-region-decision/v2" ||
                !(decision.action in ["accept", "reject"]) ||
                !(decision.reviewer_id instanceof String) || !(decision.reviewer_id as String).trim() ||
                !(decision.checkpoint_id instanceof String) || !(decision.checkpoint_id as String).trim() ||
                (decision.rationale != null && (!(decision.rationale instanceof String) || !(decision.rationale as String).trim())))
            throw new IllegalArgumentException("Region review decision is invalid")
        Map expectedDecision = [slide_id: row.slide_id, section_id: row.section_id,
            analysis_domain_id: row.analysis_domain_id, candidate_roi_id: row.roi_id,
            profile_id: row.profile_id, profile_version: row.profile_version,
            profile_content_sha256: row.profile_content_sha256,
            geometry_digest: row.geometry_sha256, evidence_digest: row.evidence_snapshot_sha256,
            subtype_status: classificationStatus, semantic_class_id: row.semantic_class_id]
        if (expectedDecision.any { key, value -> decision[key] != value } ||
                row.semantic_status != (decision.action == "accept" ? "accepted" : "rejected") ||
                row.review_status != row.semantic_status)
            throw new IllegalArgumentException("Region result differs from its exact reviewer decision")
        if (classificationResults.any { !(it instanceof Map) || it.keySet() != ["class_id", "result"] as Set ||
                !(it.class_id instanceof String) || !(it.class_id as String).trim() })
            throw new IllegalArgumentException("Region subtype measurement result is invalid")
        List declaredSubtypeIds = classificationResults.collect { it.class_id }
        if (declaredSubtypeIds.toSet().size() != declaredSubtypeIds.size())
            throw new IllegalArgumentException("Region subtype measurements contain duplicates")
        if (decision.action == "reject") {
            if (classificationStatus != "not_evaluated" || semanticClassId != null)
                throw new IllegalArgumentException("Rejected region must clear its subtype")
        } else if (declaredSubtypeIds.isEmpty()) {
            if (classificationStatus != "not_configured" || semanticClassId != null)
                throw new IllegalArgumentException("This structure has no declared subtypes")
        } else if (!(classificationStatus in ["assigned", "cannot_assess"]) ||
                (classificationStatus == "assigned" ? !declaredSubtypeIds.contains(semanticClassId) : semanticClassId != null))
            throw new IllegalArgumentException("Choose a declared subtype or Cannot assess")
        List measurements = [row.semantic_rule_results] + classificationResults.collect { it.result }
        if (measurements.any { it != null && (!(it instanceof Map) ||
                !(it.outcome in ["available", "pass", "fail", "unavailable"]) ||
                it.evidence_digest != row.evidence_snapshot_sha256) })
            throw new IllegalArgumentException("Region measurement evidence is stale or invalid")
    } else {
        if (classificationResults.any { !(it instanceof Map) ||
                it.keySet() != ["class_id", "result"] as Set ||
                !(it.class_id instanceof String) || !(it.class_id as String) ||
                !(it.result instanceof Map) ||
                !(it.result.outcome in ["pass", "fail", "unavailable", "needs_review"]) })
            throw new IllegalArgumentException("semantic classification rule result is invalid")
        List<String> classificationIds = classificationResults.collect { it.class_id as String }
        List<String> classificationOutcomes = classificationResults.collect { it.result.outcome as String }
        int passCount = classificationOutcomes.count("pass")
        if (classificationIds.toSet().size() != classificationIds.size() ||
                classificationOutcomes.contains("needs_review") || passCount >= 2)
            throw new IllegalArgumentException("semantic classification is unresolved or ambiguous")
        if (classificationStatus == "not_configured") {
            if (row.semantic_class_id != null || !classificationResults.isEmpty())
                throw new IllegalArgumentException("not-configured semantic classification is inconsistent")
        } else if (classificationStatus == "not_evaluated") {
            if (row.semantic_status == "accepted" || row.semantic_class_id != null ||
                    !classificationResults.isEmpty())
                throw new IllegalArgumentException("not-evaluated semantic classification is inconsistent")
        } else if (classificationStatus == "classified") {
            if (row.semantic_status != "accepted" || row.semantic_class_id == null ||
                    classificationResults.isEmpty() || passCount != 1 ||
                    classificationOutcomes.contains("unavailable") ||
                    semanticClassId != classificationIds[classificationOutcomes.indexOf("pass")])
                throw new IllegalArgumentException("classified semantic result is inconsistent")
        } else if (classificationStatus == "unclassified") {
            if (row.semantic_status != "accepted" || row.semantic_class_id != null ||
                    classificationResults.isEmpty() ||
                    classificationOutcomes.any { it != "fail" })
                throw new IllegalArgumentException("unclassified semantic result is inconsistent")
        } else if (classificationStatus == "indeterminate") {
            if (row.semantic_status != "accepted" || row.semantic_class_id != null ||
                    classificationResults.isEmpty() ||
                    !classificationOutcomes.contains("unavailable"))
                throw new IllegalArgumentException("indeterminate semantic result is inconsistent")
        }
    }
    if (!(row.review_status instanceof String) || !reviewStatuses.contains(row.review_status))
        throw new IllegalArgumentException("exact prospective review_status is required")
    String currentParent = current.get("parent_roi_id") as String
    if (!currentParent || currentParent == row.roi_id)
        throw new IllegalStateException("Current ROI parent identity is missing or self-referential")
    if (row.containsKey("parent_roi_id") && row.parent_roi_id != currentParent)
        throw new IllegalArgumentException("Semantic import cannot reparent a canonical biological ROI")
    if (currentParent != biologicalRootId && !byId.containsKey(currentParent))
        throw new IllegalStateException("Current ROI parent is not in the exact canonical hierarchy")
    if (row.containsKey("source") && row.source != current.get("source"))
        throw new IllegalArgumentException("Semantic import cannot rewrite ROI source provenance")
    if (current.get("source") == "registered_reference_assisted") {
        def currentRegistrationQc = current.get("registration_qc") ?
            gson.fromJson(current.get("registration_qc") as String, Object) : null
        if (!current.get("registration_id") || !(currentRegistrationQc instanceof Map) ||
            currentRegistrationQc.status != "passed")
            throw new IllegalStateException("Registered-reference-assisted ROI lacks passed registration provenance")
        if (row.containsKey("registration_id") && row.registration_id != current.get("registration_id"))
            throw new IllegalArgumentException("Semantic import registration_id conflicts with candidate provenance")
        if (row.containsKey("registration_qc") && gson.toJson(row.registration_qc) != gson.toJson(currentRegistrationQc))
            throw new IllegalArgumentException("Semantic import registration_qc conflicts with candidate provenance")
    } else if (row.containsKey("registration_id") || row.containsKey("registration_qc")) {
        throw new IllegalArgumentException("Registration provenance is allowed only on registered-reference-assisted ROIs")
    }
    if (row.corrected_geometry != null) {
        Geometry corrected = parseGeometry(row.corrected_geometry)
        if (!corrected.coveredBy(section.getROI().getGeometry()))
            throw new IllegalArgumentException("Corrected geometry crosses its TissueSection")
        correctedById[row.roi_id as String] = corrected
        if (row.containsKey("original_predicted_geometry") && current.get("original_predicted_geometry")) {
            parseGeometry(row.original_predicted_geometry)
            def existingOriginal = gson.fromJson(current.get("original_predicted_geometry") as String, Object)
            if (gson.toJson(existingOriginal) != gson.toJson(row.original_predicted_geometry))
                throw new IllegalArgumentException("Original predicted geometry provenance cannot be replaced")
        } else if (row.containsKey("original_predicted_geometry")) {
            Geometry declaredOriginal = parseGeometry(row.original_predicted_geometry)
            if (!declaredOriginal.equalsTopo(object.getROI().getGeometry()))
                throw new IllegalArgumentException("Declared original geometry does not match the current prediction")
        }
    } else if (row.containsKey("original_predicted_geometry")) {
        throw new IllegalArgumentException("original_predicted_geometry is accepted only with corrected_geometry")
    }
}
Map<String, String> semanticStatusById = rows.collectEntries { row ->
    [(row.roi_id as String): (row.semantic_status as String)]
}
Map<String, String> reviewStatusById = rows.findAll { it.containsKey("review_status") }.collectEntries { row ->
    [(row.roi_id as String): (row.review_status as String)]
}
// Validate the complete existing parent graph before any geometry or metadata mutation.
byId.each { roiId, object ->
    Set<String> visited = [] as Set
    String cursor = roiId
    while (cursor != biologicalRootId) {
        if (!visited.add(cursor)) throw new IllegalStateException("ROI hierarchy contains a cycle")
        def node = byId[cursor]
        if (node == null) throw new IllegalStateException("ROI hierarchy contains an unknown parent")
        cursor = md(node).get("parent_roi_id") as String
        if (!cursor) throw new IllegalStateException("ROI hierarchy contains a blank parent")
    }
}
// Validate the prospective graph after every correction has been parsed.  This
// deliberately uses a corrected parent geometry when parent and child are
// updated in the same atomic import, rather than validating against stale
// pre-import geometry or merely against the enclosing TissueSection.
byId.each { roiId, object ->
    String parentRoiId = md(object).get("parent_roi_id") as String
    Geometry geometry = correctedById[roiId] ?: object.getROI().getGeometry()
    Geometry parentGeometry
    if (parentRoiId == biologicalRootId) {
        if (analysisDomainId) {
            List domainObjects = getAnnotationObjects().findAll { candidate ->
                candidate.getID().toString() == request.analysis_domain_path_object_id
            }
            if (domainObjects.size() != 1)
                throw new IllegalStateException("Exact analysis-domain PathObject is absent")
            parentGeometry = domainObjects.first().getROI().getGeometry()
        } else parentGeometry = section.getROI().getGeometry()
    }
    else {
        def parent = byId[parentRoiId]
        if (parent == null)
            throw new IllegalStateException("Current ROI parent is not in the exact canonical hierarchy")
        parentGeometry = correctedById[parentRoiId] ?: parent.getROI().getGeometry()
    }
    if (!geometry.coveredBy(parentGeometry))
        throw new IllegalArgumentException("Prospective ROI geometry crosses its exact stable biological parent")
}

List prospectivePrimaryObjects = rois.findAll {
    Map m = md(it)
    String roiId = m.get("roi_id") as String
    String review = reviewStatusById.getOrDefault(roiId, m.get("review_status") as String)
    m.get("pipeline_stage") == "primary_classifier" && m.get("classifier_status") == "accepted" &&
        review == "accepted"
}
prospectivePrimaryObjects.each { object ->
    Map m = md(object)
    if (m.get("roi_level") != "primary" || m.get("parent_roi_id") != biologicalRootId ||
        (analysisDomainId && m.get("analysis_domain_id") != analysisDomainId))
        throw new IllegalStateException("Accepted primary ROI must parent directly to the exact analysis domain")
}
Map<String, Geometry> prospectivePrimary = prospectivePrimaryObjects.collectEntries { object ->
    String roiId = md(object).get("roi_id") as String
    [(roiId): (correctedById[roiId] ?: object.getROI().getGeometry())]
}
def primaryMembership = new org.locationtech.jts.index.strtree.STRtree()
prospectivePrimaryObjects.each { object ->
    Map values = md(object)
    Geometry corrected = correctedById[values.get("roi_id") as String]
    def roi = corrected == null ? object.getROI() :
        GeometryTools.geometryToROI(corrected, object.getROI().getImagePlane())
    def region = new org.mif.cells.PrimaryCompartmentMembership.Region(
        values.get("roi_id"), values.get("primary_class_id"), roi)
    primaryMembership.insert(region.geometry().getEnvelopeInternal(), region)
}
primaryMembership.build()
forEachCell { cell ->
    def centroid = cell.getROI().getGeometry().centroid
    org.mif.cells.PrimaryCompartmentMembership.select(
        centroid, primaryMembership.query(centroid.getEnvelopeInternal()))
}
List prospectiveSecondaryObjects = rois.findAll {
    Map m = md(it)
    String roiId = m.get("roi_id") as String
    String semanticStatus = semanticStatusById.getOrDefault(roiId, m.get("semantic_status") as String)
    String review = reviewStatusById.getOrDefault(roiId, m.get("review_status") as String)
    m.get("pipeline_stage") == "secondary_classifier" && m.get("classifier_status") == "accepted" &&
        m.get("candidate_status") == "candidate" &&
        semanticStatus == "accepted" && review == "accepted" &&
        activeSecondaryIdSet.contains(roiId)
}
Set prospectiveSecondaryIds = prospectiveSecondaryObjects.collect {
    md(it).get("roi_id") as String
} as Set
if (prospectiveSecondaryIds != activeSecondaryIdSet)
    throw new IllegalArgumentException("active_secondary_roi_ids do not match final accepted secondary ROIs")
prospectiveSecondaryObjects.each { object ->
    Map m = md(object)
    String parentRoiId = m.get("parent_roi_id") as String
    if (m.get("roi_level") != "secondary" ||
        (analysisDomainId && m.get("analysis_domain_id") != analysisDomainId))
        throw new IllegalStateException("Accepted secondary ROI has an invalid biological level")
    Set<String> visited = [] as Set
    String cursor = parentRoiId
    while (cursor != biologicalRootId) {
        if (!visited.add(cursor))
            throw new IllegalStateException("Accepted secondary ancestry contains a cycle")
        def parent = byId[cursor]
        Map parentMetadata = parent == null ? [:] : md(parent)
        boolean acceptedPrimaryParent = prospectivePrimary.containsKey(cursor) &&
            parentMetadata.get("pipeline_stage") == "primary_classifier" &&
            parentMetadata.get("roi_level") == "primary"
        boolean acceptedSecondaryParent = prospectiveSecondaryIds.contains(cursor) &&
            parentMetadata.get("pipeline_stage") == "secondary_classifier" &&
            parentMetadata.get("roi_level") == "secondary"
        if (parent == null || !(acceptedPrimaryParent || acceptedSecondaryParent) ||
            (analysisDomainId && parentMetadata.get("analysis_domain_id") != analysisDomainId))
            throw new IllegalStateException(
                "Accepted secondary ROI requires every exact accepted ancestor"
            )
        cursor = parentMetadata.get("parent_roi_id") as String
        if (!cursor) throw new IllegalStateException("Accepted secondary ancestry is incomplete")
    }
}
Map<String, Geometry> prospectiveSecondary = prospectiveSecondaryObjects.collectEntries { object ->
    String roiId = md(object).get("roi_id") as String
    [(roiId): (correctedById[roiId] ?: object.getROI().getGeometry())]
}
def secondaryMembership = new SemanticMembershipIndex(activeSecondaryIds.collect { prospectiveSecondary[it] })

def atomicBytes = { Path path, byte[] content ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    try {
        Files.write(temp, content, StandardOpenOption.CREATE_NEW)
        try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
        catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
    } finally { Files.deleteIfExists(temp) }
}
def atomicJson = { Path path, Object value ->
    atomicBytes(path, (wireGson.toJson(value) + "\n").getBytes(StandardCharsets.UTF_8))
}
def restoreOutput = { Path path, byte[] original ->
    if (original == null) Files.deleteIfExists(path)
    else atomicBytes(path, original)
}
String reconciledCellIdsSha256 = authenticatedSemanticAuthority.cell_ids_sha256 as String
String reviewStatus = terminalReviewStatus
Map finalizationReceipt = [schema_version: "semantic-finalization/v2",
    analysis_domain_id: analysisDomainId, section_id: sectionId,
    tissue_generation_id: generationId, pipeline_run_id: runId,
    semantic_result_set_sha256: semanticResultSetSha256,
    semantic_result_artifact: semanticResultArtifact,
    active_secondary_roi_ids: activeSecondaryIds,
    accepted_secondary_roi_ids: prospectiveSecondary.keySet().toList().sort(),
    accepted_secondary_count: prospectiveSecondary.size(),
    reconciled_cell_ids_sha256: reconciledCellIdsSha256, membership_cell_count: cellCount]
Map outputs = [updated_roi_ids: ids.sort(), roi_count: ids.size(), section_id: sectionId,
    coordinate_system: "global_level0", review_status: reviewStatus,
    review_action: requestedReviewAction,
    reconciled_cell_ids_sha256: reconciledCellIdsSha256, cell_count: cellCount,
    semantic_finalization: finalizationReceipt]
Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
    allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
byte[] originalManifest = Files.exists(manifestPath) ? Files.readAllBytes(manifestPath) : null
byte[] originalResult = Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null
Map<Object, Object> originalRois = correctedById.collectEntries { roiId, ignored ->
    def object = byId[roiId]
    [(object): object.getROI()]
}
Map<Object, Map> originalMetadata = rows.collect { byId[it.roi_id] }
    .toSet().collectEntries { object -> [(object): new LinkedHashMap(md(object))] }
Object originalCellComponents = boundImageData.getProperty("mif.cell_components")
Path membershipComponent = null
boolean forwardPersistenceAttempted = false
def persistForwardCurrentEntry = {
    if (!mifAuthenticatedHeadlessProjectStage) return
    forwardPersistenceAttempted = true
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
def persistRollbackCurrentEntry = {
    if (!forwardPersistenceAttempted) return
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
try {
    rows.each { row ->
        def object = byId[row.roi_id]
        Map current = md(object)
        Geometry corrected = correctedById[row.roi_id as String]
        if (corrected != null) {
            if (!current.get("original_predicted_geometry"))
                current.put("original_predicted_geometry", gson.toJson(geometryJson(object.getROI().getGeometry())))
            object.setROI(GeometryTools.geometryToROI(corrected, object.getROI().getImagePlane()))
            Closure liveGeometrySha256 =
                authenticatedSemanticAuthority.live_geometry_sha256 as Closure
            Closure canonicalSha256 =
                authenticatedSemanticAuthority.canonical_sha256 as Closure
            String correctedGeometrySha256 = liveGeometrySha256(
                object.getROI().getGeometry()
            ) as String
            current.put("geometry_sha256", correctedGeometrySha256)
            String type = current.get("pipeline_stage") == "primary_classifier" ?
                "primary" : "secondary"
            String typeId = type == "primary" ?
                current.get("primary_class_id") as String :
                current.get("structure_id") as String
            current.put("object_revision_sha256", canonicalSha256([
                analysis_domain_id: analysisDomainId,
                analysis_domain_revision_sha256:
                    request.analysis_domain_revision_sha256,
                analysis_domain_set_sha256: request.domain_set_sha256,
                classification: object.getClassification(),
                geometry_sha256: correctedGeometrySha256,
                parent_path_object_id: object.getParent().getID().toString(),
                path_object_id: object.getID().toString(),
                pipeline_active: current.get("pipeline_active"),
                type: type,
                type_id: typeId,
            ]))
        }
        ["semantic_profile", "semantic_status", "review_status"].each { key ->
            if (row.containsKey(key)) current.put(key, row[key] == null ? "" : (row[key] as String))
        }
        current.put("semantic_rule_results",
            canonicalJsonElement(acceptanceRuleJsonByRoi[row.roi_id as String]))
        current.put("semantic_classification_status", row.semantic_classification_status as String)
        current.put("semantic_class_id", row.semantic_class_id == null ? "" : row.semantic_class_id as String)
        current.put("semantic_classification_rule_results",
            canonicalJsonElement(classificationRuleJsonByRoi[row.roi_id as String]))
        if (unifiedRegionReview) current.put("semantic_region_decision", wireGson.toJson(row.region_decision))
        current.put("semantic_result_set_sha256", semanticResultSetSha256)
        current.put("semantic_run_id", runId);
        current.put("semantic_generation_id", generationId)
    }
    // i) We freeze active ROI IDs as Int32 codes and retain explicit null colours.
    List membershipEntries = activeSecondaryIds.withIndex().collect { roiId, index ->
        [code:index, label_id:roiId, name:roiId, color:null]
    }
    Map membershipDictionaries = [secondary_roi_ids:[dictionary_id:("${semanticResultSetSha256}:secondary_roi_ids" as String),
        version:1, field:"secondary_roi_ids", entries:membershipEntries]]
    Map<String,String> membershipColumns = [secondary_roi_ids:"list_int32",
        membership_run_id:"string", membership_generation_id:"string"]
    Path membershipRoot = MifProjectStageBootstrapV1.outputRoot(request, config).resolve("cell-results")
        .resolve("semantic-${UUID.randomUUID()}")
    String membershipRequestSha = (authenticatedSemanticAuthority.canonical_sha256 as Closure)(request)
    long appliedCells = 0
    membershipComponent = cellProvider.writeComponent(membershipRoot, membershipRequestSha,
        analysisDomainId, null, ["mif.measurements_stale"] as Set<String>, membershipColumns,
        wireGson.toJsonTree(membershipDictionaries).getAsJsonObject(), { cell ->
            if (md(cell).get("analysis_domain_id") != analysisDomainId) return null
            if (md(cell).get("scientific_measurements_stale") == "true")
                throw new IllegalStateException("Semantic membership measurements changed before publication")
            appliedCells++
            [secondary_roi_ids:secondaryMembership.secondaryCodes(cell.getROI().getGeometry().centroid), membership_run_id:runId,
             membership_generation_id:generationId]
        } as Function)
    if (appliedCells != cellCount)
        throw new IllegalStateException("Semantic reconciliation changed the selected stable cell population")
    outputs.component_descriptor = [path:membershipComponent.toString(),
        sha256:sha256File(membershipComponent)]
    NativeCellScope.selectComponent(boundImageData, membershipComponent.toString())
    fireHierarchyUpdate()
    persistForwardCurrentEntry()
    atomicJson(manifestPath, [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
        generations: [], object_outputs: outputs])
    mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
        generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256File(manifestPath)]])
} catch (Throwable error) {
    try {
        boundImageData.setProperty("mif.cell_components", originalCellComponents)
        NativeCellScope.close(boundImageData)
    } catch (Throwable restoreError) { error.addSuppressed(restoreError) }

    try { restoreOutput(resultPath, originalResult) }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    try { restoreOutput(manifestPath, originalManifest) }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    originalRois.each { object, roi ->
        try { object.setROI(roi) }
        catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    }
    originalMetadata.each { object, snapshot ->
        try { md(object).clear();
            md(object).putAll(snapshot) }
        catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    }
    try { fireHierarchyUpdate() }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    try { persistRollbackCurrentEntry() }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    throw error
}
