// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/**
 * Snapshot exact, explicitly published analysis-domain PathObject UUIDs.
 *
 * Arguments are request.json, stage-result.v1.json and
 * generation-manifest.v1.json.  The script always runs on the canonical mIF
 * project entry.  Moving-modality context is visual evidence only and never
 * changes annotation ownership or authorizes coordinate transfer.
 */

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.index.strtree.STRtree
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools
import qupath.lib.objects.PathObject

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "analysis_domain/01_snapshot_analysis_domains.groovy"
final String STAGE = "analysis_domain_selection"
final String STAGE_RESULT_SCHEMA = "roi-stage-result/v1"
final String GENERATION_MANIFEST_SCHEMA = "roi-generation-manifest/v1"
final String CANDIDATE_SCHEMA = "analysis-domain-candidate-manifest/v1"
final String DOMAIN_SCHEMA = "analysis-domain/v1"
final String GEOMETRY_DIGEST_POLICY = "jts-normalized-wkb-sha256/v1"
final int MAX_NORMALIZED_WKB_BYTES = 16 * 1024 * 1024
final int MAX_REVISIONS_PER_MANIFEST = 4096

if (args.size() != 3)
    throw new IllegalArgumentException("Expected request, stage-result and generation-manifest paths")
Path requestPath = Paths.get(args[0] as String)
Path resultPath = Paths.get(args[1] as String)
Path generationManifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance().newBuilder()
    .setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.LONG_OR_DOUBLE).create()
def canonicalGson = gson.newBuilder().serializeNulls().create()
def prettyGson = GsonTools.getInstance().newBuilder()
    .serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(requestPath), Map.class) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != STAGE)
    throw new IllegalArgumentException("Invalid analysis-domain request envelope")
Map request = envelope.request as Map
def boundProject = getProject()
def boundImageData = getCurrentImageData()
def boundEntry = getProjectEntry()
if (boundProject == null || boundImageData == null || boundEntry == null)
    throw new IllegalStateException("Stage execution requires an open saved canonical mIF project image")
Path boundProjectPath = boundProject.getPath().toAbsolutePath().normalize()
String requestedProjectFile = request.project_file as String
String requestedEntryId = request.project_entry_id as String
String requestedCanonicalImageId = request.canonical_image_id as String
String requestedCanonicalSha256 = request.canonical_image_sha256 as String
if (request.exchange_version != "roi-stage-request/v2" ||
        !requestedProjectFile || Paths.get(requestedProjectFile).toAbsolutePath().normalize() != boundProjectPath ||
        !requestedEntryId || boundEntry.getID().toString() != requestedEntryId ||
        !requestedCanonicalImageId ||
        !(requestedCanonicalSha256 ==~ /[0-9a-f]{64}/) ||
        !((request.script_bundle_sha256 as String) ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException("Exact canonical mIF project entry identity and script bundle are required")
// The mandatory bootstrap already authenticates the exact bundle, process,
// canonical image and saved entry. Do not reread/hash the same source here.
if (!mifAuthenticatedHeadlessProjectStage)
    throw new IllegalStateException("Analysis-region saving requires its authenticated analysis task")
Path projectRoot = boundProjectPath.parent

// The user-facing session must already have saved image data and closed QuPath
// before this separate headless command. Grouped execution commits only this
// bound entry from the stage-owned mutation branches below.
if (request.saved_closed_acknowledged != true)
    throw new IllegalStateException("Analysis-domain snapshot requires saved_closed_acknowledged")
if (!(request.action in [
        "review_analysis_rois", "accept_analysis_rois", "use_whole_tissue"
    ]))
    throw new IllegalArgumentException("Analysis-domain snapshot action is invalid")

Map originalMetadata = [:]
Map originalLocks = [:]
Map originalParents = [:]
Map originalOutputs = [(resultPath): Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null,
    (generationManifestPath): Files.exists(generationManifestPath) ? Files.readAllBytes(generationManifestPath) : null]
boolean saveAttempted = false
def rememberObject = { PathObject object ->
    if (!originalMetadata.containsKey(object)) {
        originalMetadata[object] = new LinkedHashMap(object.getMetadata())
        originalLocks[object] = object.isLocked()
        originalParents[object] = object.getParent()
    }
}
def writeBytes = { Path path, byte[] bytes ->
    Files.createDirectories(path.toAbsolutePath().parent)
    Path temporary = Files.createTempFile(path.toAbsolutePath().parent, ".analysis-regions-", ".tmp")
    try {
        java.nio.channels.FileChannel.open(temporary, java.nio.file.StandardOpenOption.WRITE).withCloseable { channel ->
            java.nio.ByteBuffer buffer = java.nio.ByteBuffer.wrap(bytes)
            while (buffer.hasRemaining()) channel.write(buffer)
            channel.force(true)
        }
        Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    } finally { Files.deleteIfExists(temporary) }
}
def atomicJson = { Path path, Object value ->
    writeBytes(path, (prettyGson.toJson(value) + "\n").getBytes(StandardCharsets.UTF_8))
}
def persistCurrentEntryState = {
    if (!boundImageData.isChanged()) return
    saveAttempted = true
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
def sha256Bytes = { byte[] bytes ->
    MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()
}
def sha256File = { Path path -> sha256Bytes(Files.readAllBytes(path)) }
def canonicalValue
canonicalValue = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap<String, Object>()
        (value as Map).each { key, nested -> sorted.put(key as String, canonicalValue(nested)) }
        return sorted
    }
    if (value instanceof List)
        return (value as List).collect { canonicalValue(it) }
    value
}
def canonicalSha256 = { Object value ->
    sha256Bytes(canonicalGson.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8))
}
def nonblank = { Object value, String field ->
    if (!(value instanceof String) || !(value as String).trim())
        throw new IllegalArgumentException("${field} must be a non-empty string")
    value as String
}

try {
List<Map> acceptedTissue = request.accepted_tissue_sections as List<Map>
Set acceptedTissueFields = [
    "section_id", "tissue_roi_id", "tissue_path_object_id", "tissue_generation_id"
] as Set
if (acceptedTissue == null || acceptedTissue.isEmpty() ||
        acceptedTissue.any { raw ->
            !(raw instanceof Map) || raw.keySet() != acceptedTissueFields ||
                nonblank(raw.section_id, "section_id") == null ||
                nonblank(raw.tissue_roi_id, "tissue_roi_id") == null ||
                nonblank(raw.tissue_path_object_id, "tissue_path_object_id") == null ||
                raw.tissue_generation_id != request.generation_id
        })
    throw new IllegalArgumentException("Exact accepted TissueSection inventory is required")
if (acceptedTissue.collect { it.section_id }.toSet().size() != acceptedTissue.size() ||
        acceptedTissue.collect { it.tissue_path_object_id }.toSet().size() != acceptedTissue.size())
    throw new IllegalArgumentException("Accepted TissueSection identities must be unique")
Map<String, Map> acceptedTissueBySection = acceptedTissue.collectEntries { tissue ->
    [(tissue.section_id as String): tissue]
}

Set candidateRequiredFields = [
    "schema_version", "selection_status", "canonical_image_id",
    "canonical_image_sha256", "project_entry_id", "tissue_generation_id",
    "analysis_domain_manifests", "overlap_evidence", "domain_set_sha256",
    "saved_closed_acknowledged", "selected_annotations_locked",
] as Set
Set candidateOptionalFields = ["assistance", "analysis_domain_assistance"] as Set
Set manifestFields = [
    "schema_version", "canonical_image_id", "canonical_image_sha256",
    "project_entry_id", "slide_id", "section_id", "tissue_generation_id",
    "tissue_roi_id", "tissue_path_object_id", "revisions", "content_sha256",
] as Set
Set revisionFields = [
    "analysis_domain_id", "kind", "slide_id", "section_id",
    "tissue_generation_id", "canonical_image_id", "canonical_image_sha256",
    "project_entry_id", "tissue_roi_id", "tissue_path_object_id",
    "source_path_object_id", "revision", "predecessor_sha256",
    "geometry_digest_policy", "geometry_sha256", "normalized_wkb_hex",
    "geometry_reference_path_object_id", "content_sha256",
] as Set
Set overlapFields = [
    "canonical_image_id", "canonical_image_sha256", "project_entry_id",
    "section_id", "tissue_generation_id", "tissue_roi_id",
    "tissue_path_object_id", "geometry_evidence", "positive_area_overlaps",
    "generated_by_spatial_index", "exact_jts_area_test",
] as Set
Set geometryFields = [
    "path_object_id", "parent_path_object_id", "tissue_roi_id",
    "tissue_path_object_id", "geometry_digest_policy", "geometry_sha256",
    "normalized_wkb_hex", "is_valid", "is_empty", "is_finite", "area",
    "covered_by_parent",
] as Set

Closure validateContextManifest = { Object value, String role, Set allowedStatuses ->
    if (value == null)
        return null
    if (!(value instanceof Map))
        throw new IllegalStateException("${role} must be an analysis-domain manifest object")
    Map context = value as Map
    if (!context.keySet().containsAll(candidateRequiredFields) ||
            !(context.keySet() - candidateRequiredFields - candidateOptionalFields).isEmpty() ||
            context.schema_version != CANDIDATE_SCHEMA ||
            context.canonical_image_id != requestedCanonicalImageId ||
            context.canonical_image_sha256 != requestedCanonicalSha256 ||
            context.project_entry_id != requestedEntryId ||
            context.tissue_generation_id != request.generation_id ||
            context.saved_closed_acknowledged != true ||
            !(context.analysis_domain_manifests instanceof List) ||
            (context.analysis_domain_manifests as List).isEmpty() ||
            !(context.overlap_evidence instanceof List))
        throw new IllegalStateException("${role} analysis-domain schema or scope is stale")
    if (!(context.selection_status in allowedStatuses)) {
        if (role == "candidate_manifest")
            throw new IllegalStateException("candidate_manifest must have candidate status")
        if (role == "accepted_retirement_predecessor")
            throw new IllegalStateException("accepted_retirement_predecessor must have accepted status")
        throw new IllegalStateException("lineage_predecessor_candidate status is invalid")
    }
    if (context.selected_annotations_locked !=
            (context.selection_status == "accepted"))
        throw new IllegalStateException("${role} lock state is invalid")

    Map<String, Map> manifestsBySource = [:]
    Map<String, Map> evidenceBySource = [:]
    List<String> manifestDigests = []
    (context.analysis_domain_manifests as List).each { rawManifest ->
        if (!(rawManifest instanceof Map) || rawManifest.keySet() != manifestFields ||
                rawManifest.schema_version != DOMAIN_SCHEMA ||
                !(rawManifest.revisions instanceof List) ||
                (rawManifest.revisions as List).isEmpty() ||
                (rawManifest.revisions as List).size() >
                    MAX_REVISIONS_PER_MANIFEST)
            throw new IllegalStateException("${role} analysis-domain manifest schema is invalid")
        Map manifest = rawManifest as Map
        Map tissue = acceptedTissueBySection[manifest.section_id as String]
        if (tissue == null || manifest.canonical_image_id != requestedCanonicalImageId ||
                manifest.canonical_image_sha256 != requestedCanonicalSha256 ||
                manifest.project_entry_id != requestedEntryId ||
                manifest.slide_id != envelope.slide_id ||
                manifest.tissue_generation_id != request.generation_id ||
                manifest.tissue_roi_id != tissue.tissue_roi_id ||
                manifest.tissue_path_object_id != tissue.tissue_path_object_id)
            throw new IllegalStateException("${role} analysis-domain manifest tissue scope is stale")

        String stableSourceId = null
        String stableDomainId = null
        String predecessorSha256 = null
        List<String> revisionDigests = []
        (manifest.revisions as List).eachWithIndex { rawRevision, index ->
            if (!(rawRevision instanceof Map) || rawRevision.keySet() != revisionFields)
                throw new IllegalStateException("${role} analysis-domain revision schema is invalid")
            Map revision = rawRevision as Map
            if (!(revision.revision instanceof Number) ||
                    !Double.isFinite((revision.revision as Number).doubleValue()) ||
                    (revision.revision as Number).doubleValue() != index + 1d ||
                    revision.kind != "selected_roi" ||
                    revision.canonical_image_id != requestedCanonicalImageId ||
                    revision.canonical_image_sha256 != requestedCanonicalSha256 ||
                    revision.project_entry_id != requestedEntryId ||
                    revision.slide_id != envelope.slide_id ||
                    revision.section_id != manifest.section_id ||
                    revision.tissue_generation_id != request.generation_id ||
                    revision.tissue_roi_id != tissue.tissue_roi_id ||
                    revision.tissue_path_object_id != tissue.tissue_path_object_id ||
                    revision.geometry_digest_policy != GEOMETRY_DIGEST_POLICY ||
                    revision.geometry_reference_path_object_id != null)
                throw new IllegalStateException("${role} analysis-domain revision scope is invalid")
            String sourceId = nonblank(
                revision.source_path_object_id, "${role} source_path_object_id"
            )
            String domainId = nonblank(
                revision.analysis_domain_id, "${role} analysis_domain_id"
            )
            if (stableSourceId == null) {
                stableSourceId = sourceId
                stableDomainId = domainId
            } else if (stableSourceId != sourceId || stableDomainId != domainId) {
                throw new IllegalStateException("${role} analysis-domain lineage changed identity")
            }
            String expectedDomainId = canonicalSha256([
                canonical_image_sha256: requestedCanonicalSha256,
                kind: "selected_roi", schema_version: DOMAIN_SCHEMA,
                section_id: manifest.section_id,
                source_path_object_id: sourceId,
                tissue_generation_id: request.generation_id,
            ])
            if (domainId != expectedDomainId ||
                    revision.predecessor_sha256 != predecessorSha256 ||
                    !((revision.geometry_sha256 as String) ==~ /[0-9a-f]{64}/) ||
                    !((revision.normalized_wkb_hex as String) ==~ /(?:[0-9a-f]{2})+/) ||
                    (revision.normalized_wkb_hex as String).length() >
                        MAX_NORMALIZED_WKB_BYTES * 2 ||
                    sha256Bytes((revision.normalized_wkb_hex as String).decodeHex()) !=
                        revision.geometry_sha256)
                throw new IllegalStateException("${role} analysis-domain revision lineage or geometry digest is invalid")
            Map revisionPayload = [
                analysis_domain_id: domainId,
                canonical_image_id: requestedCanonicalImageId,
                canonical_image_sha256: requestedCanonicalSha256,
                geometry_digest_policy: GEOMETRY_DIGEST_POLICY,
                geometry_reference_path_object_id: null,
                geometry_sha256: revision.geometry_sha256,
                kind: "selected_roi", predecessor_sha256: predecessorSha256,
                project_entry_id: requestedEntryId, revision: index + 1,
                section_id: manifest.section_id, slide_id: envelope.slide_id,
                source_path_object_id: sourceId,
                tissue_generation_id: request.generation_id,
                tissue_path_object_id: tissue.tissue_path_object_id,
                tissue_roi_id: tissue.tissue_roi_id,
            ]
            if (!((revision.content_sha256 as String) ==~ /[0-9a-f]{64}/) ||
                    canonicalSha256(revisionPayload) != revision.content_sha256)
                throw new IllegalStateException("${role} analysis-domain revision content SHA-256 is invalid")
            revisionDigests << revision.content_sha256 as String
            predecessorSha256 = revision.content_sha256 as String
        }
        if (manifestsBySource.containsKey(stableSourceId))
            throw new IllegalStateException("${role} selected-ROI inventory is duplicated")
        Map manifestPayload = [
            canonical_image_id: requestedCanonicalImageId,
            canonical_image_sha256: requestedCanonicalSha256,
            project_entry_id: requestedEntryId,
            revision_sha256s: revisionDigests,
            schema_version: DOMAIN_SCHEMA,
            section_id: manifest.section_id,
            slide_id: envelope.slide_id,
            tissue_generation_id: request.generation_id,
            tissue_path_object_id: tissue.tissue_path_object_id,
            tissue_roi_id: tissue.tissue_roi_id,
        ]
        if (!((manifest.content_sha256 as String) ==~ /[0-9a-f]{64}/) ||
                canonicalSha256(manifestPayload) != manifest.content_sha256)
            throw new IllegalStateException("${role} analysis-domain manifest content SHA-256 is invalid")
        manifestsBySource[stableSourceId] = manifest
        manifestDigests << manifest.content_sha256 as String
    }

    (context.overlap_evidence as List).each { rawEvidence ->
        if (!(rawEvidence instanceof Map) || rawEvidence.keySet() != overlapFields ||
                !(rawEvidence.geometry_evidence instanceof List) ||
                (rawEvidence.geometry_evidence as List).size() != 1 ||
                !(rawEvidence.positive_area_overlaps instanceof List) ||
                !(rawEvidence.positive_area_overlaps as List).isEmpty() ||
                rawEvidence.generated_by_spatial_index != true ||
                rawEvidence.exact_jts_area_test != true)
            throw new IllegalStateException("${role} overlap evidence schema is invalid")
        Map evidence = rawEvidence as Map
        Map rawGeometry = (evidence.geometry_evidence as List).first()
        if (!(rawGeometry instanceof Map) || rawGeometry.keySet() != geometryFields)
            throw new IllegalStateException("${role} geometry evidence schema is invalid")
        Map geometry = rawGeometry as Map
        String sourceId = nonblank(geometry.path_object_id, "${role} evidence path_object_id")
        Map manifest = manifestsBySource[sourceId]
        Map tissue = acceptedTissueBySection[evidence.section_id as String]
        Map latest = manifest == null ? null : (manifest.revisions as List<Map>).last()
        if (manifest == null || tissue == null || evidenceBySource.containsKey(sourceId) ||
                evidence.canonical_image_id != requestedCanonicalImageId ||
                evidence.canonical_image_sha256 != requestedCanonicalSha256 ||
                evidence.project_entry_id != requestedEntryId ||
                evidence.section_id != manifest.section_id ||
                evidence.tissue_generation_id != request.generation_id ||
                evidence.tissue_roi_id != tissue.tissue_roi_id ||
                evidence.tissue_path_object_id != tissue.tissue_path_object_id ||
                geometry.parent_path_object_id != tissue.tissue_path_object_id ||
                geometry.tissue_roi_id != tissue.tissue_roi_id ||
                geometry.tissue_path_object_id != tissue.tissue_path_object_id ||
                geometry.geometry_digest_policy != GEOMETRY_DIGEST_POLICY ||
                geometry.geometry_sha256 != latest.geometry_sha256 ||
                geometry.normalized_wkb_hex != latest.normalized_wkb_hex ||
                geometry.is_valid != true || geometry.is_empty != false ||
                geometry.is_finite != true || geometry.covered_by_parent != true ||
                !(geometry.area instanceof Number) ||
                !Double.isFinite((geometry.area as Number).doubleValue()) ||
                (geometry.area as Number).doubleValue() <= 0d)
            throw new IllegalStateException("${role} geometry evidence is stale")
        evidenceBySource[sourceId] = evidence
    }
    if (evidenceBySource.keySet() != manifestsBySource.keySet())
        throw new IllegalStateException("${role} overlap evidence inventory is stale")
    Map setPayload = [
        schema_version: "analysis-domain-set/v1",
        analysis_domain_manifest_sha256s: manifestDigests.sort(),
    ]
    if (!((context.domain_set_sha256 as String) ==~ /[0-9a-f]{64}/) ||
            canonicalSha256(setPayload) != context.domain_set_sha256)
        throw new IllegalStateException("analysis-domain context domain-set SHA-256 is invalid")
    context
}

Closure manifestsBySource = { Map context ->
    if (context == null)
        return [:]
    (context.analysis_domain_manifests as List<Map>).collectEntries { manifest ->
        Map latest = (manifest.revisions as List<Map>).last()
        [(latest.source_path_object_id as String): manifest]
    }
}
Closure validateLineageRelationship = { Map successor, Map predecessor, String relationship ->
    if (successor == null || predecessor == null)
        return
    Map predecessorBySource = manifestsBySource(predecessor)
    manifestsBySource(successor).each { sourceId, successorManifest ->
        Map predecessorManifest = predecessorBySource[sourceId]
        if (predecessorManifest == null)
            return
        List<Map> successorRevisions = successorManifest.revisions as List<Map>
        List<Map> predecessorRevisions = predecessorManifest.revisions as List<Map>
        boolean prefixMatches = successorRevisions.size() >= predecessorRevisions.size()
        predecessorRevisions.eachWithIndex { prior, index ->
            if (prefixMatches &&
                    prior.content_sha256 != successorRevisions[index].content_sha256)
                prefixMatches = false
        }
        if (!prefixMatches)
            throw new IllegalStateException("${relationship} analysis-domain lineage is incoherent")
    }
}

def priorCandidate = request.candidate_manifest
def lineagePredecessorCandidate = request.lineage_predecessor_candidate
def acceptedRetirementPredecessor = request.accepted_retirement_predecessor
priorCandidate = validateContextManifest(priorCandidate, "candidate_manifest", ["candidate"] as Set)
lineagePredecessorCandidate = validateContextManifest(lineagePredecessorCandidate, "lineage_predecessor_candidate", ["candidate", "accepted"] as Set)
acceptedRetirementPredecessor = validateContextManifest(acceptedRetirementPredecessor, "accepted_retirement_predecessor", ["accepted"] as Set)
validateLineageRelationship(priorCandidate as Map, lineagePredecessorCandidate as Map, "candidate-to-lineage")
validateLineageRelationship(lineagePredecessorCandidate as Map, acceptedRetirementPredecessor as Map, "lineage-to-retirement")
validateLineageRelationship(priorCandidate as Map, acceptedRetirementPredecessor as Map, "candidate-to-retirement")

Map<String, Map> candidateManifestBySource = manifestsBySource(priorCandidate as Map)
Map<String, Map> lineageManifestBySource = manifestsBySource(lineagePredecessorCandidate as Map)
Map<String, Map> priorManifestBySource = lineageManifestBySource
Map<String, Map> acceptedRetirementBySource = manifestsBySource(acceptedRetirementPredecessor as Map)

Closure expectedWholeTissueDomainSetSha256 = {
    List<String> wholeManifestDigests = acceptedTissue.collect { tissue ->
        String sourceId = tissue.tissue_path_object_id as String
        String domainId = canonicalSha256([
            canonical_image_sha256: requestedCanonicalSha256,
            kind: "whole_tissue_section", schema_version: DOMAIN_SCHEMA,
            section_id: tissue.section_id,
            source_path_object_id: sourceId,
            tissue_generation_id: request.generation_id,
        ])
        Map revisionPayload = [
            analysis_domain_id: domainId,
            canonical_image_id: requestedCanonicalImageId,
            canonical_image_sha256: requestedCanonicalSha256,
            geometry_digest_policy: GEOMETRY_DIGEST_POLICY,
            geometry_reference_path_object_id: sourceId,
            geometry_sha256: null,
            kind: "whole_tissue_section", predecessor_sha256: null,
            project_entry_id: requestedEntryId, revision: 1,
            section_id: tissue.section_id, slide_id: envelope.slide_id,
            source_path_object_id: sourceId,
            tissue_generation_id: request.generation_id,
            tissue_path_object_id: sourceId,
            tissue_roi_id: tissue.tissue_roi_id,
        ]
        String revisionSha256 = canonicalSha256(revisionPayload)
        canonicalSha256([
            canonical_image_id: requestedCanonicalImageId,
            canonical_image_sha256: requestedCanonicalSha256,
            project_entry_id: requestedEntryId,
            revision_sha256s: [revisionSha256],
            schema_version: DOMAIN_SCHEMA,
            section_id: tissue.section_id,
            slide_id: envelope.slide_id,
            tissue_generation_id: request.generation_id,
            tissue_path_object_id: sourceId,
            tissue_roi_id: tissue.tissue_roi_id,
        ])
    }
    canonicalSha256([
        schema_version: "analysis-domain-set/v1",
        analysis_domain_manifest_sha256s: wholeManifestDigests.sort(),
    ])
}

if (request.action == "accept_analysis_rois" && priorCandidate == null)
    throw new IllegalStateException("accept_analysis_rois requires candidate_manifest")
if (request.candidate_path_object_ids != null &&
        !(request.candidate_path_object_ids instanceof List))
    throw new IllegalArgumentException("candidate_path_object_ids must be a list")
List<String> candidatePathObjectIds = request.candidate_path_object_ids instanceof List ?
    (request.candidate_path_object_ids as List).collect {
        nonblank(it, "candidate_path_object_ids")
    } : []
if (request.action != "use_whole_tissue" &&
        candidatePathObjectIds.isEmpty() && priorCandidate != null)
    candidatePathObjectIds = candidateManifestBySource.keySet().toList()
// An interactive adapter may publish exact UUIDs to this bounded file.  The
// headless snapshot never infers candidates from name, class, or GUI selection.
if (request.action != "use_whole_tissue" &&
        candidatePathObjectIds.isEmpty() && request.saved_selection_path) {
    Path publication = Paths.get(request.saved_selection_path as String)
    if (!publication.isAbsolute()) publication = projectRoot.resolve(publication).normalize()
    if (publication.toRealPath() != publication || Files.isSymbolicLink(publication) ||
            !Files.isRegularFile(publication))
        throw new IllegalStateException("Published analysis-domain UUID file is invalid")
    Map published = gson.fromJson(Files.readString(publication), Map.class) as Map
    if (published.project_entry_id != requestedEntryId ||
            published.canonical_image_sha256 != requestedCanonicalSha256 ||
            published.tissue_generation_id != request.generation_id)
        throw new IllegalStateException("Published analysis-domain UUID scope is stale")
    if (published.schema_version == "analysis-domain-pathobject-publication/v1" &&
            published.candidate_path_object_ids instanceof List) {
        candidatePathObjectIds = (published.candidate_path_object_ids as List).collect {
            nonblank(it, "candidate_path_object_ids")
        }
    } else if (published.schema_version == CANDIDATE_SCHEMA) {
        Map publishedCandidate = validateContextManifest(
            published, "published_candidate_manifest", ["candidate", "accepted"] as Set
        )
        candidatePathObjectIds = manifestsBySource(publishedCandidate).keySet().toList()
    } else {
        throw new IllegalStateException("Published analysis-domain UUID schema is invalid")
    }
}
if (request.action != "use_whole_tissue" &&
        candidatePathObjectIds.isEmpty() && lineagePredecessorCandidate != null) {
    candidatePathObjectIds = lineageManifestBySource.keySet().toList()
}
if (request.action != "use_whole_tissue" &&
        (candidatePathObjectIds.isEmpty() ||
            candidatePathObjectIds.toSet().size() != candidatePathObjectIds.size()))
    throw new IllegalArgumentException("Exact unique candidate_path_object_ids are required")
if (request.action != "use_whole_tissue" && priorCandidate != null &&
        candidatePathObjectIds.toSet() != candidateManifestBySource.keySet())
    throw new IllegalStateException("candidate_manifest inventory does not match candidate_path_object_ids")

// This is the first hierarchy access, after authenticated bundle verification
// and saved/closed acknowledgement.
List<PathObject> annotations = getAnnotationObjects()
Map<String, PathObject> annotationsById = annotations.collectEntries { object ->
    [(object.getID().toString()): object]
}
Map<String, PathObject> tissueById = [:]
acceptedTissue.each { tissue ->
    String pathObjectId = nonblank(tissue.tissue_path_object_id, "tissue_path_object_id")
    PathObject object = annotationsById[pathObjectId]
    Map metadata = object?.getMetadata()
    if (object == null || metadata.get("pipeline_stage") != "tissue_detection" ||
            metadata.get("generation_status") != "accepted" ||
            metadata.get("review_status") != "accepted" ||
            metadata.get("section_id") != tissue.section_id ||
            metadata.get("roi_id") != tissue.tissue_roi_id ||
            metadata.get("generation_id") != tissue.tissue_generation_id)
        throw new IllegalStateException("Exact accepted TissueSection PathObject is stale")
    tissueById[pathObjectId] = object
}

if (request.action == "use_whole_tissue") {
    if (acceptedRetirementPredecessor == null)
        throw new IllegalStateException("Whole-tissue transition requires accepted_retirement_predecessor")
    String successorDomainSetSha256 =
        request.whole_tissue_successor_domain_set_sha256 as String
    if (!(successorDomainSetSha256 ==~ /[0-9a-f]{64}/) ||
            successorDomainSetSha256 != expectedWholeTissueDomainSetSha256())
        throw new IllegalStateException("Whole-tissue successor domain-set SHA-256 is invalid")
    List<Map> wholeTissueRetirementPlan = []
    List<String> absentSourcePathObjectIds = []
    acceptedRetirementBySource.keySet().sort().each { sourceId ->
        Map predecessorManifest = acceptedRetirementBySource[sourceId]
        Map predecessorIdentity = (predecessorManifest.revisions as List<Map>).last()
        PathObject retiredObject = annotationsById[sourceId]
        if (retiredObject == null) {
            absentSourcePathObjectIds << sourceId
            return
        }
        Map retiredMetadata = retiredObject.getMetadata()
        PathObject retiredParent = retiredObject.getParent()
        String currentStatus = retiredMetadata.get("analysis_domain_status") as String
        boolean acceptedActiveState = currentStatus == "accepted" &&
            retiredMetadata.get("analysis_domain_active") == "true" &&
            retiredMetadata.get("analysis_domain_audit_only") == null &&
            retiredMetadata.get("analysis_domain_retired_reason") == null &&
            retiredMetadata.get("analysis_domain_retired_by_set_sha256") == null
        boolean intendedRetiredState = currentStatus == "retired" &&
            retiredMetadata.get("analysis_domain_active") == "false" &&
            retiredMetadata.get("analysis_domain_audit_only") == "true" &&
            retiredMetadata.get("analysis_domain_retired_reason") ==
                "superseded_by_whole_tissue" &&
            retiredMetadata.get("analysis_domain_retired_by_set_sha256") ==
                successorDomainSetSha256
        if (!retiredObject.isAnnotation() ||
                retiredParent == null ||
                retiredParent.getID().toString() !=
                    predecessorIdentity.tissue_path_object_id ||
                !tissueById.containsKey(retiredParent.getID().toString()) ||
                retiredMetadata.get("pipeline_stage") != STAGE ||
                retiredMetadata.get("analysis_domain_id") != predecessorIdentity.analysis_domain_id ||
                !retiredObject.isLocked() ||
                !(acceptedActiveState || intendedRetiredState))
            throw new IllegalStateException("Whole-tissue predecessor PathObject identity is stale")
        wholeTissueRetirementPlan << [
            source_path_object_id: sourceId,
            object: retiredObject,
            metadata: retiredMetadata,
        ]
    }
    List<String> retiredSourcePathObjectIds = []
    wholeTissueRetirementPlan.each { retirement ->
        String sourceId = retirement.source_path_object_id as String
        PathObject retiredObject = retirement.object as PathObject
        Map retiredMetadata = retirement.metadata as Map
        rememberObject(retiredObject)
        retiredMetadata.put("analysis_domain_status", "retired")
        retiredMetadata.put("analysis_domain_active", "false")
        retiredMetadata.put("analysis_domain_audit_only", "true")
        retiredMetadata.put(
            "analysis_domain_retired_reason", "superseded_by_whole_tissue"
        )
        retiredMetadata.put(
            "analysis_domain_retired_by_set_sha256", successorDomainSetSha256
        )
        retiredObject.setLocked(true)
        retiredSourcePathObjectIds << sourceId
    }
    if (!retiredSourcePathObjectIds.isEmpty())
        boundImageData.getHierarchy().fireHierarchyChangedEvent(this)
    Map retirementOutput = [
        schema_version: "analysis-domain-retirement-attestation/v1",
        action: "use_whole_tissue",
        canonical_image_id: requestedCanonicalImageId,
        canonical_image_sha256: requestedCanonicalSha256,
        project_entry_id: requestedEntryId,
        tissue_generation_id: request.generation_id,
        accepted_retirement_predecessor_sha256:
            acceptedRetirementPredecessor.domain_set_sha256,
        successor_domain_set_sha256: successorDomainSetSha256,
        retired_source_path_object_ids: retiredSourcePathObjectIds,
        absent_source_path_object_ids: absentSourcePathObjectIds,
        retired_annotations_locked: true,
        audit_only: true,
        saved_closed_acknowledged: true,
    ]
    String generationId = request.generation_id as String
    Map generationManifest = [
        schema_version: GENERATION_MANIFEST_SCHEMA,
        invocation_id: envelope.invocation_id, run_id: envelope.run_id,
        stage: STAGE, slide_id: envelope.slide_id,
        generation_ids: [generationId], object_outputs: retirementOutput,
    ]
    persistCurrentEntryState()
    atomicJson(generationManifestPath, generationManifest)
    Map result = [
        status: "completed", stage: STAGE, slide_id: envelope.slide_id,
        generation_id: generationId, interaction_reason: null,
        affected_object_ids: retiredSourcePathObjectIds, message: null,
        allowed_actions: [], resume_context: null, checkpoint_id: null,
        outputs: retirementOutput, error: null,
    ]
    mifPublishProjectStageResult(resultPath, [
        schema_version: STAGE_RESULT_SCHEMA,
        invocation_id: envelope.invocation_id, run_id: envelope.run_id,
        stage: STAGE, slide_id: envelope.slide_id, result: result,
        generation_manifest: [
            path: generationManifestPath.fileName.toString(),
            sha256: sha256File(generationManifestPath),
        ],
    ])
    return
}

Map annotationPolicy = null
List<Map> live = []
candidatePathObjectIds.each { pathObjectId ->
    PathObject object = annotationsById[pathObjectId]
    if (object == null)
        throw new IllegalStateException("Published analysis-domain PathObject UUID is absent")
    if (!object.isAnnotation())
        throw new IllegalStateException("Selected analysis region must be an annotation")
    if (object.getMetadata().get("pipeline_stage") == "tissue_detection")
        throw new IllegalStateException("Draw an analysis region inside the tissue outline, or choose whole-section analysis.")
    if (!object.getChildObjects().isEmpty()) {
        // Accepted regions keep their admitted descendants. New regions may
        // contain ordinary annotations; only the selected region defines scope.
        Map priorAccepted = acceptedRetirementBySource[pathObjectId]
        Map priorIdentity = priorAccepted == null ? null : (priorAccepted.revisions as List<Map>).last()
        Map metadata = object.getMetadata()
        boolean exactPredecessor = priorIdentity != null && metadata.get("pipeline_stage") == STAGE &&
                metadata.get("analysis_domain_status") == "accepted" &&
                metadata.get("analysis_domain_active") == "true" &&
                metadata.get("analysis_domain_id") == priorIdentity.analysis_domain_id &&
                metadata.get("analysis_domain_set_sha256") == acceptedRetirementPredecessor.domain_set_sha256 &&
                metadata.get("analysis_domain_project_entry_id") == requestedEntryId
        if (!exactPredecessor) {
            if (annotationPolicy == null) {
                Path helper = projectRoot.resolve("scripts/bundle-${request.script_bundle_sha256}/general/06_cluster_annotations.groovy")
                annotationPolicy = new GroovyShell(this.class.classLoader).evaluate(helper.toFile()) as Map
            }
            Deque<PathObject> descendants = new ArrayDeque<>(object.getChildObjects())
            while (!descendants.isEmpty()) {
                PathObject descendant = descendants.removeFirst()
                if (!descendant.isAnnotation() || annotationPolicy.protectedObject.call(descendant))
                    throw new IllegalStateException("This region contains cells or protected analysis objects. Select a new region without these objects, or revise its previously accepted region.")
                descendants.addAll(descendant.getChildObjects())
            }
        }
    }
    def roi = object.getROI()
    if (roi == null || !roi.isArea())
        throw new IllegalStateException("Select an area annotation inside an accepted tissue section.")
    Geometry candidateGeometry = roi.getGeometry()
    boolean finite = candidateGeometry.getCoordinates().every { coordinate ->
        Double.isFinite(coordinate.x) && Double.isFinite(coordinate.y)
    }
    if (!candidateGeometry.isValid() || candidateGeometry.isEmpty() || !finite ||
            !Double.isFinite(candidateGeometry.getArea()) || candidateGeometry.getArea() <= 0d)
        throw new IllegalStateException("Selected analysis-domain geometry is invalid or non-finite")
    // QuPath may nest a new drawing under a superseded outline. Its scientific
    // parent is the unique accepted section containing the complete geometry.
    List<PathObject> containingTissue = tissueById.values().findAll { tissue ->
        def tissueRoi = tissue.getROI()
        tissueRoi != null && roi.getImagePlane() == tissueRoi.getImagePlane() &&
                candidateGeometry.coveredBy(tissueRoi.getGeometry())
    } as List<PathObject>
    if (containingTissue.size() != 1)
        throw new IllegalStateException("Each selected region must lie entirely inside exactly one accepted tissue section on the same image plane.")
    PathObject tissue = containingTissue[0]
    Geometry normalized = candidateGeometry.copy()
    normalized.normalize()
    byte[] normalizedWkb = new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
    String geometrySha256 = sha256Bytes(normalizedWkb)
    Map tissueMetadata = tissue.getMetadata()
    live << [object: object, geometry: candidateGeometry,
             normalized_wkb_hex: normalizedWkb.encodeHex().toString(),
             geometry_sha256: geometrySha256,
             section_id: tissueMetadata.get("section_id"),
             tissue_roi_id: tissueMetadata.get("roi_id"),
             tissue_path_object_id: tissue.getID().toString(),
             source_path_object_id: object.getID().toString()]
}

STRtree spatialIndex = new STRtree()
live.each { candidate -> spatialIndex.insert((candidate.geometry as Geometry).envelopeInternal, candidate) }
spatialIndex.build()
List<Map> overlapPairs = []
live.each { candidate ->
    Geometry candidateGeometry = candidate.geometry as Geometry
    spatialIndex.query(candidateGeometry.envelopeInternal).each { other ->
        if (candidate.source_path_object_id < other.source_path_object_id &&
                candidate.section_id == other.section_id) {
            double area = (other.geometry as Geometry).intersection(candidateGeometry).getArea()
            if (area > 0d)
                overlapPairs << [first: candidate.source_path_object_id,
                    second: other.source_path_object_id, area_px2: area]
        }
    }
}
if (!overlapPairs.isEmpty()) {
    double pixelArea = boundImageData.server.pixelCalibration.pixelWidthMicrons *
            boundImageData.server.pixelCalibration.pixelHeightMicrons
    String details = overlapPairs.take(12).collect { pair ->
        "${pair.first} and ${pair.second}: ${String.format(Locale.ROOT, '%.6g', pair.area_px2 * pixelArea)} µm²"
    }.join("; ")
    throw new IllegalStateException("${overlapPairs.size()} overlapping analysis-region pair(s). " +
        "Edit or deselect these regions in QuPath before saving again: ${details}" +
        (overlapPairs.size() > 12 ? "; additional pairs omitted" : ""))
}

List<Map> manifests = []
List<Map> overlapEvidence = []
acceptedTissue.each { tissue ->
    List<Map> sectionCandidates = live.findAll {
        it.section_id == tissue.section_id
    }.sort { left, right ->
        (left.source_path_object_id as String) <=>
            (right.source_path_object_id as String)
    }
    sectionCandidates.each { candidate ->
        List<Map> revisions = []
        if (request.action == "accept_analysis_rois") {
            Map currentManifest = candidateManifestBySource[
                candidate.source_path_object_id as String
            ]
            if (currentManifest == null)
                throw new IllegalStateException("candidate_manifest lacks a live selected ROI")
            List<Map> currentRevisions = (currentManifest.revisions as List<Map>).collect {
                new LinkedHashMap(it)
            }
            Map currentRevision = currentRevisions.last()
            if (currentRevision.geometry_sha256 != candidate.geometry_sha256 ||
                    currentRevision.normalized_wkb_hex != candidate.normalized_wkb_hex)
                throw new IllegalStateException("Reviewed analysis-domain geometry changed without an explicit edit")
            revisions.addAll(currentRevisions)
        } else {
            Map priorManifest = priorManifestBySource[
                candidate.source_path_object_id as String
            ]
            List<Map> priorRevisions = priorManifest == null ? [] :
                (priorManifest.revisions as List<Map>).collect { new LinkedHashMap(it) }
            Map latestPrior = priorRevisions.isEmpty() ? null : priorRevisions.last()
            boolean geometryUnchanged = latestPrior != null &&
                latestPrior.geometry_sha256 == candidate.geometry_sha256 &&
                latestPrior.normalized_wkb_hex == candidate.normalized_wkb_hex
            if (geometryUnchanged) {
                revisions.addAll(priorRevisions)
            } else {
                int revisionNumber = latestPrior == null ? 1 :
                    (latestPrior.revision as Number).intValue() + 1
                String predecessorSha256 = latestPrior?.content_sha256 as String
                String analysisDomainId = canonicalSha256([
                    canonical_image_sha256: requestedCanonicalSha256,
                    kind: "selected_roi", schema_version: DOMAIN_SCHEMA,
                    section_id: candidate.section_id,
                    source_path_object_id: candidate.source_path_object_id,
                    tissue_generation_id: request.generation_id,
                ])
                Map revisionPayload = [
                    analysis_domain_id: analysisDomainId,
                    canonical_image_id: requestedCanonicalImageId,
                    canonical_image_sha256: requestedCanonicalSha256,
                    geometry_digest_policy: GEOMETRY_DIGEST_POLICY,
                    geometry_reference_path_object_id: null,
                    geometry_sha256: candidate.geometry_sha256,
                    kind: "selected_roi", predecessor_sha256: predecessorSha256,
                    project_entry_id: requestedEntryId, revision: revisionNumber,
                    section_id: candidate.section_id, slide_id: envelope.slide_id,
                    source_path_object_id: candidate.source_path_object_id,
                    tissue_generation_id: request.generation_id,
                    tissue_path_object_id: candidate.tissue_path_object_id,
                    tissue_roi_id: candidate.tissue_roi_id,
                ]
                Map revision = [
                    analysis_domain_id: analysisDomainId, kind: "selected_roi",
                    slide_id: envelope.slide_id, section_id: candidate.section_id,
                    tissue_generation_id: request.generation_id,
                    canonical_image_id: requestedCanonicalImageId,
                    canonical_image_sha256: requestedCanonicalSha256,
                    project_entry_id: requestedEntryId,
                    tissue_roi_id: candidate.tissue_roi_id,
                    tissue_path_object_id: candidate.tissue_path_object_id,
                    source_path_object_id: candidate.source_path_object_id,
                    revision: revisionNumber, predecessor_sha256: predecessorSha256,
                    geometry_digest_policy: GEOMETRY_DIGEST_POLICY,
                    geometry_sha256: candidate.geometry_sha256,
                    normalized_wkb_hex: candidate.normalized_wkb_hex,
                    geometry_reference_path_object_id: null,
                    content_sha256: canonicalSha256(revisionPayload),
                ]
                revisions.addAll(priorRevisions)
                revisions << revision
            }
        }
        List<String> revisionDigests = revisions.collect { it.content_sha256 as String }
        Map manifestPayload = [
            canonical_image_id: requestedCanonicalImageId,
            canonical_image_sha256: requestedCanonicalSha256,
            project_entry_id: requestedEntryId,
            revision_sha256s: revisionDigests,
            schema_version: DOMAIN_SCHEMA,
            section_id: tissue.section_id,
            slide_id: envelope.slide_id,
            tissue_generation_id: tissue.tissue_generation_id,
            tissue_path_object_id: tissue.tissue_path_object_id,
            tissue_roi_id: tissue.tissue_roi_id,
        ]
        Map manifest = [
            schema_version: DOMAIN_SCHEMA,
            canonical_image_id: requestedCanonicalImageId,
            canonical_image_sha256: requestedCanonicalSha256,
            project_entry_id: requestedEntryId, slide_id: envelope.slide_id,
            section_id: tissue.section_id,
            tissue_generation_id: tissue.tissue_generation_id,
            tissue_roi_id: tissue.tissue_roi_id,
            tissue_path_object_id: tissue.tissue_path_object_id,
            revisions: revisions, content_sha256: canonicalSha256(manifestPayload),
        ]
        manifests << manifest
        overlapEvidence << [
            canonical_image_id: requestedCanonicalImageId,
            canonical_image_sha256: requestedCanonicalSha256,
            project_entry_id: requestedEntryId, section_id: tissue.section_id,
            tissue_generation_id: tissue.tissue_generation_id,
            tissue_roi_id: tissue.tissue_roi_id,
            tissue_path_object_id: tissue.tissue_path_object_id,
            geometry_evidence: [[path_object_id: candidate.source_path_object_id,
             parent_path_object_id: candidate.tissue_path_object_id,
             tissue_roi_id: candidate.tissue_roi_id,
             tissue_path_object_id: candidate.tissue_path_object_id,
             geometry_digest_policy: GEOMETRY_DIGEST_POLICY,
             geometry_sha256: candidate.geometry_sha256,
             normalized_wkb_hex: candidate.normalized_wkb_hex,
             is_valid: true, is_empty: false, is_finite: true,
             area: (candidate.geometry as Geometry).getArea(),
             covered_by_parent: true]],
            positive_area_overlaps: [], generated_by_spatial_index: true,
            exact_jts_area_test: true,
        ]
    }
}
manifests.sort { left, right ->
    String leftSource = (left.revisions as List<Map>).last().source_path_object_id as String
    String rightSource = (right.revisions as List<Map>).last().source_path_object_id as String
    int sectionOrder = (left.section_id as String) <=> (right.section_id as String)
    sectionOrder != 0 ? sectionOrder : leftSource <=> rightSource
}
overlapEvidence.sort { left, right ->
    String leftSource = ((left.geometry_evidence as List<Map>).first().path_object_id) as String
    String rightSource = ((right.geometry_evidence as List<Map>).first().path_object_id) as String
    int sectionOrder = (left.section_id as String) <=> (right.section_id as String)
    sectionOrder != 0 ? sectionOrder : leftSource <=> rightSource
}

String selectionStatus = request.action == "accept_analysis_rois" ? "accepted" : "candidate"
Map setPayload = [
    schema_version: "analysis-domain-set/v1",
    analysis_domain_manifest_sha256s: manifests.collect { it.content_sha256 as String }.sort(),
]
String domainSetSha256 = canonicalSha256(setPayload)
if (selectionStatus == "accepted") {
    Set<String> liveSourcePathObjectIds = live.collect {
        it.source_path_object_id as String
    } as Set<String>
    List<String> retiredSourcePathObjectIds = acceptedRetirementBySource.keySet()
        .findAll { !liveSourcePathObjectIds.contains(it) }
        .sort()
    retiredSourcePathObjectIds.each { sourceId ->
        PathObject retiredObject = annotationsById[sourceId]
        if (retiredObject == null)
            return
        Map retiredMetadata = retiredObject.getMetadata()
        Map predecessorManifest = acceptedRetirementBySource[sourceId]
        Map predecessorIdentity = (predecessorManifest.revisions as List<Map>).last()
        PathObject retiredParent = retiredObject.getParent()
        if (!retiredObject.isAnnotation() ||
                retiredParent == null ||
                retiredParent.getID().toString() !=
                    predecessorIdentity.tissue_path_object_id ||
                !tissueById.containsKey(retiredParent.getID().toString()) ||
                retiredMetadata.get("pipeline_stage") != STAGE ||
                !(retiredMetadata.get("analysis_domain_status") in ["accepted", "retired"]) ||
                retiredMetadata.get("analysis_domain_id") != predecessorIdentity.analysis_domain_id)
            throw new IllegalStateException("Omitted predecessor PathObject identity is stale")
        rememberObject(retiredObject)
        retiredMetadata.put("analysis_domain_status", "retired")
        retiredMetadata.put("analysis_domain_active", "false")
        retiredMetadata.put("analysis_domain_audit_only", "true")
        retiredMetadata.put(
            "analysis_domain_retired_reason", "omitted_from_reaccepted_inventory"
        )
        retiredMetadata.put("analysis_domain_retired_by_set_sha256", domainSetSha256)
        retiredObject.setLocked(true)
    }
    live.each { candidate ->
        PathObject object = candidate.object as PathObject
        rememberObject(object)
        tissueById[candidate.tissue_path_object_id].addChildObject(object)
        object.getMetadata().put("pipeline_stage", STAGE)
        object.getMetadata().put("analysis_domain_status", "accepted")
        object.getMetadata().put("analysis_domain_active", "true")
        object.getMetadata().remove("analysis_domain_audit_only")
        object.getMetadata().remove("analysis_domain_retired_reason")
        object.getMetadata().remove("analysis_domain_retired_by_set_sha256")
        Map acceptedManifest = manifests.find {
            (it.revisions as List<Map>).last().source_path_object_id ==
                candidate.source_path_object_id
        }
        object.getMetadata().put("analysis_domain_id",
            (acceptedManifest.revisions as List<Map>).last().analysis_domain_id as String)
        Map acceptedRevision = (acceptedManifest.revisions as List<Map>).last()
        object.getMetadata().put("analysis_domain_revision",
            Integer.toString((acceptedRevision.revision as Number).intValue()))
        object.getMetadata().put("analysis_domain_revision_sha256",
            acceptedRevision.content_sha256 as String)
        object.getMetadata().put("analysis_domain_set_sha256", domainSetSha256)
        object.getMetadata().put("analysis_domain_project_entry_id", requestedEntryId)
        object.getMetadata().put("analysis_domain_geometry_sha256",
            acceptedRevision.geometry_sha256 as String)
        object.setLocked(true)
    }
    boundImageData.getHierarchy().fireHierarchyChangedEvent(this)
}
Map output = [
    schema_version: CANDIDATE_SCHEMA, selection_status: selectionStatus,
    canonical_image_id: requestedCanonicalImageId,
    canonical_image_sha256: requestedCanonicalSha256,
    project_entry_id: requestedEntryId,
    tissue_generation_id: request.generation_id,
    analysis_domain_manifests: manifests,
    overlap_evidence: overlapEvidence,
    domain_set_sha256: domainSetSha256,
    saved_closed_acknowledged: true,
    selected_annotations_locked: selectionStatus == "accepted",
    assistance: request.analysis_domain_assistance,
]
String generationId = request.generation_id as String
Map generationManifest = [
    schema_version: GENERATION_MANIFEST_SCHEMA,
    invocation_id: envelope.invocation_id, run_id: envelope.run_id,
    stage: STAGE, slide_id: envelope.slide_id,
    generation_ids: [generationId], object_outputs: output,
]
persistCurrentEntryState()
atomicJson(generationManifestPath, generationManifest)
Map result = [
    status: "completed", stage: STAGE, slide_id: envelope.slide_id,
    generation_id: generationId, interaction_reason: null,
    affected_object_ids: candidatePathObjectIds, message: null,
    allowed_actions: [], resume_context: null, checkpoint_id: null,
    outputs: output, error: null,
]
mifPublishProjectStageResult(resultPath, [
    schema_version: STAGE_RESULT_SCHEMA,
    invocation_id: envelope.invocation_id, run_id: envelope.run_id,
    stage: STAGE, slide_id: envelope.slide_id, result: result,
    generation_manifest: [
        path: generationManifestPath.fileName.toString(),
        sha256: sha256File(generationManifestPath),
    ],
])

} catch (Exception primary) {
    try {
        originalMetadata.each { object, metadata ->
            object.getMetadata().clear()
            object.getMetadata().putAll(metadata)
            object.setLocked(originalLocks[object] as boolean)
            PathObject originalParent = originalParents[object] as PathObject
            if (object.getParent() != originalParent) {
                object.getParent()?.removeChildObject(object)
                originalParent?.addChildObject(object)
            }
        }
        if (!originalMetadata.isEmpty()) boundImageData.getHierarchy().fireHierarchyChangedEvent(this)
        if (saveAttempted) { boundEntry.saveImageData(boundImageData); boundProject.syncChanges() }
    } catch (Exception rollback) { primary.addSuppressed(rollback) }
    originalOutputs.each { path, bytes ->
        try {
            if (bytes == null) Files.deleteIfExists(path as Path)
            else writeBytes(path as Path, bytes as byte[])
        } catch (Exception rollback) { primary.addSuppressed(rollback) }
    }
    throw primary
}
