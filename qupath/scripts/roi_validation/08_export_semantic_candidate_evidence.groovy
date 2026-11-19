// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Export immutable, provisional candidate-to-cell relations for semantic review.
 *
 * This script intentionally does not alter PathObjects.  Final canonical cell
 * memberships are owned by the later atomic semantic-finalization operation.
 */
import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.MessageDigest
import java.security.DigestOutputStream
import java.util.UUID
import org.mif.roi.SemanticCandidateEvidence
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "roi_validation/08_export_semantic_candidate_evidence.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
Map envelope = gson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "semantic_validation")
    throw new IllegalArgumentException("Invalid semantic evidence request envelope")
Map request = envelope.request as Map
String slideId = request.slide_id as String
String sectionId = request.section_id as String
String domainId = request.analysis_domain_id as String
String runId = request.run_id as String
String generationId = request.generation_id as String
String populationId = request.segmentation_population_id as String
String rawExportPath = request.raw_export_path as String
String rawExportSha256 = request.raw_export_sha256 as String
Map physicalCalibration = request.physical_calibration as Map
Map phenotypeView = request.phenotype_view as Map
List evidenceSelectors = (request.evidence_selectors ?: []) as List
if (![slideId, sectionId, domainId, runId, generationId, populationId, rawExportPath].every { it } ||
        !(rawExportSha256 ==~ /[0-9a-f]{64}/) ||
        !(physicalCalibration?.x_um_per_px instanceof Number) || !(physicalCalibration?.y_um_per_px instanceof Number) ||
        !((physicalCalibration.x_um_per_px as double) > 0.0d) || !((physicalCalibration.y_um_per_px as double) > 0.0d) ||
        !phenotypeView?.mode || !phenotypeView?.generation_id || !((phenotypeView?.inventory_sha256 as String) ==~ /[0-9a-f]{64}/) ||
        evidenceSelectors.any { !(it instanceof Map) || !((it as Map).type instanceof String) || !((it as Map).selector instanceof Map) })
    throw new IllegalArgumentException("Semantic evidence requires exact slide/section/domain/run/generation/population identity")
def project = getProject();
def imageData = getCurrentImageData();
def entry = getProjectEntry()
if (project == null || imageData == null || entry == null) throw new IllegalStateException("Stage execution requires an open saved project image")
Path projectPath = project.getPath().toAbsolutePath().normalize()
String requestedProjectFile = request.project_file as String
String requestedEntryId = request.project_entry_id as String
String requestedCanonicalSha256 = request.canonical_image_sha256 as String
String requestedScriptBundleSha256 = request.script_bundle_sha256 as String
if (!requestedProjectFile || Paths.get(requestedProjectFile).toAbsolutePath().normalize() != projectPath ||
        !requestedEntryId || entry.getID().toString() != requestedEntryId ||
        !(requestedCanonicalSha256 ==~ /[0-9a-f]{64}/) || !(requestedScriptBundleSha256 ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException("Exact project entry identity and script bundle are required")
Path scriptsRoot = projectPath.parent.resolve("scripts").normalize()
Path requestedBundleRoot = scriptsRoot.resolve("bundle-${requestedScriptBundleSha256}").normalize()
Path bundleManifestPath = requestedBundleRoot.resolve("qupath-script-bundle.v1.json").normalize()
Path dataRoot = projectPath.parent.resolve("data").normalize()
Path scriptBundleEvidencePath = dataRoot.resolve("qupath-script-bundle.evidence.json").normalize()
if (!Files.isRegularFile(bundleManifestPath) || !Files.isRegularFile(scriptBundleEvidencePath) ||
        dataRoot.toRealPath() != dataRoot || Files.isSymbolicLink(bundleManifestPath))
    throw new IllegalStateException("Immutable QuPath script bundle path ancestry is invalid")
byte[] authenticatedManifestBytes = Files.readAllBytes(bundleManifestPath)
String authenticatedManifestSha256 = MessageDigest.getInstance("SHA-256").digest(authenticatedManifestBytes).encodeHex().toString()
if (authenticatedManifestSha256 != requestedScriptBundleSha256)
    throw new IllegalStateException("Immutable QuPath script bundle manifest does not match the request")
Map authenticatedManifest = gson.fromJson(new String(authenticatedManifestBytes, StandardCharsets.UTF_8), Map.class) as Map
final String verifierAssetPath = "general/00_verify_stage_script_bundle.groovy"
List verifierEvidence = (authenticatedManifest.assets as List).findAll { raw -> raw instanceof Map && raw.path == verifierAssetPath }
Path verifierPath = requestedBundleRoot.resolve(verifierAssetPath).normalize()
String verifierDigest = verifierEvidence ? verifierEvidence[0].sha256 as String : null
if (verifierEvidence.size() != 1 || !Files.isRegularFile(verifierPath) ||
        !(verifierDigest ==~ /[0-9a-f]{64}/) ||
        MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(verifierPath)).encodeHex().toString() != verifierDigest)
    throw new IllegalStateException("Immutable QuPath script bundle verifier is invalid")
Closure verifyCurrentScriptBundle = evaluate(verifierPath.toFile()) as Closure
verifyCurrentScriptBundle(request, projectPath, SCRIPT_ASSET_PATH, gson)
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
List<String> liveCanonicalUris = entry.getURIs()
    .collect { URI uri -> MifProjectStageBootstrapV1.mifCanonicalUri(uri) }
    .toSet().toList().sort()
Object liveRole = entry.getMetadataMap().get("mif_pipeline.image_role")
Object liveDigest = entry.getMetadataMap().get(
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

def md = { object -> object.getMetadata() }
Closure canonicalJson
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
Map authenticatedSemanticAuthority = authenticateSemanticLiveAuthority(
    request, slideId, runId, generationId, requestedEntryId, gson
) as Map
// Semantic live authority authenticated before any evidence export is published.
Collection authenticatedRois = (authenticatedSemanticAuthority.roi_by_id as Map).values()
// A new review needs evidence for every detected candidate, including regions
// accepted in an earlier review before measurements or phenotypes changed.
List candidates = authenticatedRois.findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "secondary_classifier" &&
        m.get("candidate_status") == "candidate"
}.sort { canonicalString(md(it).get("roi_id") as String) }
List primaries = authenticatedRois.findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "primary_classifier"
}.sort { canonicalString(md(it).get("roi_id") as String) }
Closure forEachCell = authenticatedSemanticAuthority.for_each_cell as Closure
long cellCount = authenticatedSemanticAuthority.cell_count as long
canonicalJson = { Object value ->
    if (value == null) return "null"
    if (value instanceof CharSequence) return canonicalString(value.toString())
    if (value instanceof Boolean) return value ? "true" : "false"
    if (value instanceof Number) return value.toString()
    if (value instanceof Map) {
        return "{" + (value as Map).keySet().collect { it.toString() }.sort().collect { String key ->
            canonicalString(key) + ":" + canonicalJson((value as Map).get(key))
        }.join(",") + "}"
    }
    if (value instanceof Iterable) {
        return "[" + (value as Iterable).collect { canonicalJson(it) }.join(",") + "]"
    }
    throw new IllegalArgumentException("Unsupported semantic digest value")
}
def digest = { Object value -> MessageDigest.getInstance("SHA-256").digest(canonicalJson(value).getBytes(StandardCharsets.UTF_8)).encodeHex().toString() }
def geometryDigest = { Geometry geometry ->
    // Normalized JTS geometry is the identity contract, never a display serialization.
    Geometry copy = geometry.copy();
    copy.normalize()
    MessageDigest.getInstance("SHA-256").digest(new WKBWriter(2, true).write(copy)).encodeHex().toString()
}
List primaryInventory = primaries.collect { primary ->
    Map m = md(primary)
    new TreeMap([
        roi_id: m.get("roi_id"), geometry_digest: geometryDigest(primary.getROI().getGeometry()),
        primary_class_id: m.get("primary_class_id"),
    ])
}
String livePrimaryGeometryInventorySha256 = digest(
    new TreeMap(["primary_geometry_inventory/v1": primaryInventory])
)
if (livePrimaryGeometryInventorySha256 != request.primary_geometry_inventory_sha256)
    throw new IllegalStateException("Live primary geometry inventory does not match the raw export")
Map<String, List<Geometry>> primariesByClass = primaries.groupBy {
    md(it).get("primary_class_id") as String
}.collectEntries { String classId, List objects ->
    [(classId): objects.collect { it.getROI().getGeometry() }]
}
List candidateInventory = candidates.collect { candidate ->
    Map m = md(candidate)
    [candidate_roi_id: m.get("roi_id"), candidate_path_object_id: candidate.getID().toString(),
     geometry_digest: geometryDigest(candidate.getROI().getGeometry())]
}
String evidenceRoot = request.containsKey("operational_output_dir") ?
    MifProjectStageBootstrapV1.outputRoot(request, request.resolved_config as Map).resolve("semantic-evidence").toString() :
    request.semantic_evidence_output_dir as String
if (!evidenceRoot) evidenceRoot = projectPath.parent.resolve("data").resolve("semantic-evidence").toString()
Path output = Paths.get(evidenceRoot)
    .resolve(slideId).resolve(sectionId).resolve(domainId).resolve("candidate-membership-${UUID.randomUUID()}.json")
Files.createDirectories(output.parent)
Path temporary = output.resolveSibling(".${output.fileName}.${UUID.randomUUID()}.tmp")
Map candidateEvidence = new TreeMap()
MessageDigest payloadDigest = MessageDigest.getInstance("SHA-256")
try {
    // The source-authority pass remains independent. This single second pass
    // checks the selected phenotype view and indexes all candidate memberships.
    // One bounded spill orders every candidate without retaining cell objects.
    try (SemanticCandidateEvidence evidence = new SemanticCandidateEvidence(
            candidates.collect { it.getROI().getGeometry() }, primariesByClass,
            physicalCalibration.x_um_per_px as double,
            physicalCalibration.y_um_per_px as double, output.parent)) {
        forEachCell { cell ->
            Map m = md(cell)
            if (m.get("pipeline_run_id") != runId ||
                    m.get("selected_phenotype_view") != phenotypeView.mode ||
                    m.get("phenotyping_generation_id") != phenotypeView.generation_id)
                throw new IllegalStateException("Candidate evidence cell crosses its accepted run or phenotype view")
            evidence.include(cell.getROI().getGeometry().getCentroid(),
                m.get("cell_id") as String, cell.getNucleusROI() != null)
        }
        new BufferedWriter(new OutputStreamWriter(new DigestOutputStream(
            Files.newOutputStream(temporary, StandardOpenOption.CREATE_NEW), payloadDigest),
            StandardCharsets.UTF_8)).withCloseable { writer ->
            writer.write("{\"schema_version\":\"semantic-candidate-membership/v1\",\"slide_id\":" + gson.toJson(slideId))
            writer.write(",\"section_id\":" + gson.toJson(sectionId) + ",\"analysis_domain_id\":" + gson.toJson(domainId))
            writer.write(",\"generation_id\":" + gson.toJson(generationId) + ",\"pipeline_run_id\":" + gson.toJson(runId))
            writer.write(",\"segmentation_population_id\":" + gson.toJson(populationId) + ",\"relations\":[")
            boolean first = true
            List<SemanticCandidateEvidence.Membership> memberships = evidence.forEachRelation({
                    int ordinal, String memberId, boolean hasNucleus ->
                if (!first) writer.write(",")
                first = false
                writer.write(gson.toJson([
                    candidate_roi_id: candidateInventory[ordinal].candidate_roi_id,
                    cell_id: memberId, has_valid_nucleus: hasNucleus,
                ]))
            } as SemanticCandidateEvidence.RelationConsumer)
            candidates.eachWithIndex { candidate, int ordinal ->
                Map m = md(candidate);
                Geometry candidateGeometry = candidate.getROI().getGeometry()
                def membership = memberships[ordinal]
                def geometryFacts = evidence.geometryFacts(ordinal)
                Map facts = [
                    candidate_path_object_id: candidate.getID().toString(), geometry_digest: geometryDigest(candidateGeometry),
                    candidate_area_px2: geometryFacts.areaPx2(), geometry_component_count: geometryFacts.componentCount(),
                    classifier_confidence: candidate.getClassProbability(), member_cell_inventory_sha256: membership.cellInventorySha256(),
                    member_cell_count: membership.cellCount(), primary_geometry_inventory_sha256: livePrimaryGeometryInventorySha256,
                    physical_calibration: physicalCalibration,
                    distance_to_primary_um: geometryFacts.distanceToPrimaryUm(),
                    overlap_fraction_with_primary: geometryFacts.overlapFractionWithPrimary(),
                ]
                if (geometryFacts.minimumBoundaryDistanceUm() != null)
                    facts.minimum_boundary_distance_um = geometryFacts.minimumBoundaryDistanceUm()
                else facts.unavailable_evidence = [minimum_boundary_distance_um: [reason: "primary_geometry_source_absent"]]
                candidateEvidence[m.get("roi_id") as String] = facts
            }
            writer.write("],\"raw_export_path\":" + gson.toJson(rawExportPath) + ",\"raw_export_sha256\":" + gson.toJson(rawExportSha256))
            writer.write(",\"evidence_selectors\":" + gson.toJson(evidenceSelectors))
            writer.write(",\"phenotype_view\":" + gson.toJson(phenotypeView))
            writer.write(",\"candidate_count\":" + candidates.size())
            writer.write(",\"candidate_inventory_sha256\":" + gson.toJson(digest(candidateInventory)))
            writer.write(",\"candidate_evidence\":" + gson.toJson(candidateEvidence) + "}\n")
        }
    }
    Files.move(temporary, output, StandardCopyOption.ATOMIC_MOVE)
} catch (AtomicMoveNotSupportedException ignored) { Files.move(temporary, output, StandardCopyOption.REPLACE_EXISTING) }
catch (Exception error) { Files.deleteIfExists(temporary);
    throw error }
String sha = payloadDigest.digest().encodeHex().toString()
Map outputs = [path: output.toString(), artifact_sha256: sha, schema_version: "semantic-candidate-membership/v1",
    candidate_count: candidates.size(), cell_count: cellCount, coordinate_system: "global_level0"]
Files.writeString(manifestPath, wireGson.toJson([schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId], generations: [], object_outputs: outputs]) + "\n")
mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, result: [status: "completed", stage: envelope.stage,
    slide_id: slideId, generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
    allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null],
    generation_manifest: [path: manifestPath.fileName.toString(), sha256: MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(manifestPath)).encodeHex().toString()]])
