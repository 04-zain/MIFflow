// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Apply one secondary proposal classifier only inside exact eligible parents. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.UUID
import org.mif.io.NativeClassifierFiles
import org.mif.roi.RoiInventory
import org.locationtech.jts.geom.Geometry
import org.mif.roi.BiologicalParentSelector
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.operation.union.UnaryUnionOp
import qupath.lib.io.GsonTools
import com.google.gson.ToNumberPolicy

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "compartment_analysis/03_apply_secondary_structure_classifiers.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
// Preserve revision integers and real-valued settings in owner content digests.
def gson = GsonTools.getInstance().newBuilder().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
def canonicalGson = gson.newBuilder().serializeNulls().create()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "secondary_classifier")
    throw new IllegalArgumentException("Invalid secondary-classifier request envelope")
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
String analysisDomainId = request.analysis_domain_id as String
String analysisDomainRevisionSha256 = request.analysis_domain_revision_sha256 as String
String analysisDomainPathObjectId = request.analysis_domain_path_object_id as String
String structureId = request.structure_id as String
List<String> parentIds = request.eligible_parent_ids as List<String>
String generationId = request.generation_id as String
String classifierReviewStatus = request.classifier_review_status as String
String configHash = request.config_hash as String
if (!sectionId || !(analysisDomainId ==~ /[0-9a-f]{64}/) ||
    !(analysisDomainRevisionSha256 ==~ /[0-9a-f]{64}/) || !analysisDomainPathObjectId ||
    !(request.analysis_domain_revision instanceof Number) ||
    (request.analysis_domain_revision as Number).intValue() < 1 ||
    (request.analysis_domain_revision as Number).doubleValue() !=
        (request.analysis_domain_revision as Number).intValue() ||
    !(request.domain_set_sha256 ==~ /[0-9a-f]{64}/) ||
    !structureId || parentIds == null || parentIds.isEmpty() ||
    parentIds.any { !(it instanceof String) || !it.trim() } ||
    parentIds.toSet().size() != parentIds.size() || !generationId ||
    !(classifierReviewStatus in ["accepted", "rejected", "needs_review"]) || !(configHash ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException("Exact domain, structure, parent inventory, generation and config hash are required")
String primaryDomainStatus = request.primary_domain_status as String
if (!(primaryDomainStatus in ["active", "excluded_below_minimum", "excluded_by_primary_count_review"]))
    throw new IllegalStateException("Secondary inference requires the committed primary domain disposition")
boolean primaryDomainExcluded = primaryDomainStatus != "active"
String predecessorResultRefSha256 = request.predecessor_result_ref_sha256 as String
if (!(predecessorResultRefSha256 ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException("Secondary inference requires its authenticated physical predecessor")
def canonicalValue
canonicalValue = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap<String, Object>()
        (value as Map).each { key, nested -> sorted.put(key as String, canonicalValue(nested)) }
        return sorted
    }
    if (value instanceof List) return (value as List).collect { canonicalValue(it) }
    value
}
def canonicalSha256 = { Object value ->
    MessageDigest.getInstance("SHA-256").digest(
        canonicalGson.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8)
    ).encodeHex().toString()
}
Map domainRevisionEvidence = request.analysis_domain_revision_evidence as Map
if (domainRevisionEvidence == null ||
        !(domainRevisionEvidence.kind in ["selected_roi", "whole_tissue_section"]) ||
        domainRevisionEvidence.analysis_domain_id != analysisDomainId ||
        domainRevisionEvidence.source_path_object_id != analysisDomainPathObjectId ||
        domainRevisionEvidence.project_entry_id != requestedEntryId ||
        domainRevisionEvidence.revision != request.analysis_domain_revision ||
        domainRevisionEvidence.content_sha256 != analysisDomainRevisionSha256)
    throw new IllegalStateException("Authenticated live domain revision evidence is invalid")
Map unsignedDomainRevisionEvidence = new TreeMap(domainRevisionEvidence)
unsignedDomainRevisionEvidence.remove("content_sha256")
// This is serialized geometry evidence, not part of the Task 6 revision digest.
unsignedDomainRevisionEvidence.remove("normalized_wkb_hex")
if (canonicalSha256(unsignedDomainRevisionEvidence) != analysisDomainRevisionSha256)
    throw new IllegalStateException("Authenticated live domain revision digest is stale")
def md = { object -> object.getMetadata() }

List sections = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "tissue_detection" && m.get("generation_status") == "accepted" &&
        m.get("review_status") == "accepted" &&
        m.get("slide_id") == slideId && m.get("section_id") == sectionId &&
        m.get("generation_id") == generationId
}
if (sections.size() != 1) throw new IllegalStateException("Exactly one accepted current TissueSection is required")
def section = sections.first()
def domain = getAnnotationObjects().find { object ->
    object.getID().toString() == analysisDomainPathObjectId
}
if (domain == null)
    throw new IllegalStateException("Exact analysis-domain PathObject is absent")
Map domainMetadata = md(domain)
boolean selectedDomain =
    domainMetadata.get("pipeline_stage") == "analysis_domain_selection" &&
    domainMetadata.get("analysis_domain_status") == "accepted" &&
    domainMetadata.get("analysis_domain_active") == "true" &&
    domainMetadata.get("analysis_domain_id") == analysisDomainId &&
    domainMetadata.get("analysis_domain_revision") ==
        Integer.toString((request.analysis_domain_revision as Number).intValue()) &&
    domainMetadata.get("analysis_domain_revision_sha256") == analysisDomainRevisionSha256 &&
    domainMetadata.get("analysis_domain_set_sha256") == request.domain_set_sha256 &&
    domainMetadata.get("analysis_domain_project_entry_id") == requestedEntryId &&
    domain.isLocked() && domain.getParent() == section
boolean wholeTissueDomain = domain == section
if (!(selectedDomain || wholeTissueDomain) ||
        !domain.getROI().getGeometry().coveredBy(section.getROI().getGeometry()))
    throw new IllegalStateException("Classifier domain is stale, inactive, or outside exact tissue")
Geometry liveDomainGeometry = domain.getROI()?.getGeometry()
if (liveDomainGeometry == null || liveDomainGeometry.isEmpty() || !liveDomainGeometry.isValid())
    throw new IllegalStateException("Classifier live domain geometry is invalid")
if (domainRevisionEvidence.geometry_sha256 != null) {
    Geometry normalizedDomain = liveDomainGeometry.copy();
    normalizedDomain.normalize()
    String liveDomainGeometrySha256 = MessageDigest.getInstance("SHA-256").digest(
        new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedDomain)
    ).encodeHex().toString()
    if (liveDomainGeometrySha256 != domainRevisionEvidence.geometry_sha256)
        throw new IllegalStateException("Classifier live domain geometry differs from its accepted revision")
}

List<Map> parentSources = request.parent_sources as List<Map>
Set sourceFields = ["eligible_parent_id", "source_kind", "source_result_ref_sha256",
    "producer_attempt_id", "roi_inventory", "analysis_domain_path_object_id"] as Set
if (parentSources == null || parentSources.collect { it.eligible_parent_id } != parentIds ||
        parentSources.any { !(it instanceof Map) || it.keySet() != sourceFields ||
            !(it.source_result_ref_sha256 ==~ /[0-9a-f]{64}/) })
    throw new IllegalStateException("Secondary parent sources differ from the authenticated selection")
List<Map> configuredStructures = config.secondary_structures as List<Map>
if (configuredStructures == null) {
    Map selected = request.secondary_structure_settings as Map
    if (selected == null || selected.keySet() != ["structures", "selection_ref_sha256"] as Set ||
            !(selected.selection_ref_sha256 ==~ /[0-9a-f]{64}/) ||
            !(selected.structures instanceof List) || selected.structures.isEmpty())
        throw new IllegalStateException("Secondary inference requires its accepted definition choice")
    configuredStructures = selected.structures as List<Map>
} else if (request.secondary_structure_settings != null) {
    throw new IllegalStateException("An initial definition choice cannot replace configured structures")
}
int structureIndex = configuredStructures.findIndexOf { it.structure_id == structureId }
if (structureIndex < 0) throw new IllegalArgumentException("Unknown secondary structure")
Map structure = configuredStructures[structureIndex]
Object minimumAreaValue = structure.getOrDefault("minimum_candidate_area_um2", 0.0d)
if (!(minimumAreaValue instanceof Number) ||
        !Double.isFinite((minimumAreaValue as Number).doubleValue()) ||
        (minimumAreaValue as Number).doubleValue() < 0.0d)
    throw new IllegalArgumentException("Minimum candidate area must be a finite non-negative area")
double minimumCandidateArea = (minimumAreaValue as Number).doubleValue()
if ((structure.eligible_parent_ids as List) != parentIds)
    throw new IllegalArgumentException("Secondary classifier eligible-parent inventory changed or reordered")
Set<String> primaryIds = config.primary_classes == null ?
    parentSources.findAll { it.source_kind == "primary" }.collect { it.eligible_parent_id as String } as Set :
    (config.primary_classes as List<Map>).collect { it.class_id as String } as Set
Set<String> earlierSecondaryIds = configuredStructures.take(structureIndex).collect { it.structure_id as String } as Set
def liveObjectRevisionSha256 = { object, String type, String typeId ->
    Map metadata = md(object)
    Geometry geometry = object.getROI()?.getGeometry()
    if (geometry == null || geometry.isEmpty() || !geometry.isValid() ||
            object.getParent() == null)
        return null
    Geometry normalized = geometry.copy();
    normalized.normalize()
    String geometrySha256 = MessageDigest.getInstance("SHA-256").digest(
        new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
    ).encodeHex().toString()
    canonicalSha256([
        analysis_domain_id: analysisDomainId,
        analysis_domain_revision_sha256: analysisDomainRevisionSha256,
        analysis_domain_set_sha256: request.domain_set_sha256,
        classification: object.getClassification(),
        geometry_sha256: geometrySha256,
        parent_path_object_id: object.getParent().getID().toString(),
        path_object_id: object.getID().toString(),
        pipeline_active: metadata.get("pipeline_active"),
        type: type,
        type_id: typeId,
    ])
}
Map<String, List> eligibleByType = new LinkedHashMap<>()
parentIds.each { parentId ->
    List matches
    if (parentId == "AnalysisDomain") {
        matches = [domain]
    } else if (primaryIds.contains(parentId)) {
        matches = getAnnotationObjects().findAll { object ->
            Map m = md(object)
            m.get("pipeline_stage") == "primary_classifier" && m.get("pipeline_run_id") == runId &&
                m.get("generation_id") == generationId && m.get("slide_id") == slideId &&
                m.get("section_id") == sectionId && m.get("analysis_domain_id") == analysisDomainId &&
                m.get("analysis_domain_revision_sha256") == analysisDomainRevisionSha256 &&
                m.get("domain_set_sha256") == request.domain_set_sha256 &&
                m.get("pipeline_active") == "true" && m.get("primary_class_id") == parentId &&
                object.getParent() == domain &&
                object.getROI()?.getGeometry()?.coveredBy(domain.getROI().getGeometry())
        }
    } else if (earlierSecondaryIds.contains(parentId)) {
        matches = getAnnotationObjects().findAll { object ->
            Map m = md(object)
            m.get("pipeline_stage") == "secondary_classifier" && m.get("pipeline_run_id") == runId &&
                m.get("generation_id") == generationId && m.get("slide_id") == slideId &&
                m.get("section_id") == sectionId && m.get("analysis_domain_id") == analysisDomainId &&
                m.get("analysis_domain_revision_sha256") == analysisDomainRevisionSha256 &&
                m.get("domain_set_sha256") == request.domain_set_sha256 &&
                m.get("pipeline_active") == "true" && m.get("structure_id") == parentId &&
                m.get("candidate_status") == "candidate" &&
                object.getClassification() == "Candidate_${parentId}".toString() && object.getParent() != null &&
                object.getROI()?.getGeometry()?.coveredBy(object.getParent().getROI().getGeometry()) &&
                object.getROI()?.getGeometry()?.coveredBy(domain.getROI().getGeometry())
        }
    } else {
        throw new IllegalArgumentException("Secondary parent is not AnalysisDomain, primary, or earlier secondary candidate")
    }
    eligibleByType[parentId] = matches.sort { a, b -> a.getID().toString() <=> b.getID().toString() }
}
// Our source list is bounded by configured parent types. Native membership
// comes from the accepted source tables and is compared with the live hierarchy.
Map<String, Set<String>> expectedByType = [:]
Map<String, List<Map>> tableSources = [:]
parentSources.each { source ->
    String parentId = source.eligible_parent_id as String
    String expectedKind = parentId == "AnalysisDomain" ? "analysis_domain" :
        (primaryIds.contains(parentId) ? "primary" : "secondary")
    if (source.source_kind != expectedKind)
        throw new IllegalStateException("Secondary parent source role differs from its configured parent")
    expectedByType[parentId] = [] as Set
    if (expectedKind == "analysis_domain") {
        if (source.producer_attempt_id != null || source.roi_inventory != null ||
                source.analysis_domain_path_object_id != analysisDomainPathObjectId)
            throw new IllegalStateException("Analysis-domain parent source differs from the accepted domain")
        expectedByType[parentId].add(analysisDomainPathObjectId)
    } else {
        if (!(source.roi_inventory instanceof Map) || source.analysis_domain_path_object_id != null ||
                !(source.producer_attempt_id instanceof String))
            throw new IllegalStateException("Classifier parent source lacks its exact accepted table")
        RoiInventory.member(source.producer_attempt_id as String)
        String sourceRef = source.source_result_ref_sha256 as String
        if (!tableSources.containsKey(sourceRef)) tableSources[sourceRef] = []
        tableSources[sourceRef].add(source)
    }
}
Map<String, Object> liveByPathId = getAnnotationObjects().collectEntries { object ->
    [(object.getID().toString()): object]
}
tableSources.values().each { sources ->
    Map first = sources.first()
    if (sources.any { it.source_kind != first.source_kind ||
            it.producer_attempt_id != first.producer_attempt_id || it.roi_inventory != first.roi_inventory })
        throw new IllegalStateException("One accepted parent output has inconsistent table commitments")
    Map<String, Map> selectedByClass = sources.collectEntries { source -> [(source.eligible_parent_id): source] }
    RoiInventory.read(MifProjectStageBootstrapV1.producerOutputRoot(request, config, first.producer_attempt_id as String), first.roi_inventory as Map,
        RoiInventory.member(first.producer_attempt_id as String), { RoiInventory.Row row ->
            Map source = selectedByClass[row.classId()]
            if (source == null) return
            def object = liveByPathId[row.pathObjectId()]
            String kind = source.source_kind as String
            String parentId = source.eligible_parent_id as String
            if (object == null || md(object).get("roi_id") != row.roiId() ||
                    md(object).get("object_revision_sha256") != row.objectRevisionSha256() ||
                    liveObjectRevisionSha256(object, kind, parentId) != row.objectRevisionSha256())
                throw new IllegalStateException("Accepted secondary parent table differs from live object identity or revision")
            // Earlier-secondary tables retain both candidate and Other outputs.
            // The revision binds classification; only live candidates can parent.
            if (kind == "primary" || (md(object).get("candidate_status") == "candidate" &&
                    object.getClassification() == "Candidate_${parentId}".toString()))
                expectedByType[parentId].add(row.pathObjectId())
        } as java.util.function.Consumer)
}
parentIds.each { parentId ->
    Set liveIds = eligibleByType[parentId].collect { it.getID().toString() } as Set
    if (liveIds != expectedByType[parentId])
        throw new IllegalStateException("Live secondary eligible parents differ from their accepted source tables")
}
List eligible = parentIds.collectMany { eligibleByType[it] }
if (eligible.isEmpty() && !primaryDomainExcluded) throw new IllegalStateException("No exact eligible parent objects exist")
if (eligible.any { it != domain && !(md(it).get("roi_id") as String) })
    throw new IllegalStateException("Eligible parent lacks stable roi_id")
Closure reachesDomain = { object ->
    def cursor = object
    Set seen = [] as Set
    while (cursor != null && cursor != domain) {
        if (!seen.add(cursor.getID())) return false
        cursor = cursor.getParent()
    }
    cursor == domain
}
if (eligible.any { it != domain && !reachesDomain(it) })
    throw new IllegalStateException("Eligible parent hierarchy does not resolve to the exact live domain")
def eligibleUnion = primaryDomainExcluded ? null : UnaryUnionOp.union(eligible.collect { it.getROI().getGeometry() })
Map registrationContext = (request.registration_context ?: [:]) as Map
List assistanceScopes = (registrationContext.moving_assisted_scopes ?: []) as List
List matchingAssistance = assistanceScopes.findAll { scope ->
    scope instanceof Map && scope.stage == "secondary_classifier" &&
        scope.analysis_domain_id == analysisDomainId && scope.structure_id == structureId &&
        scope.eligible_parent_ids == parentIds
}
if (matchingAssistance.size() > 1)
    throw new IllegalStateException("Secondary classifier has ambiguous moving assistance provenance")
Map movingAssistance = matchingAssistance ? matchingAssistance.first() as Map : null
if (movingAssistance != null) {
    if (registrationContext.mode != "warpy" ||
        movingAssistance.keySet() != ["stage", "analysis_domain_id", "structure_id", "eligible_parent_ids",
                                  "registration_id", "registration_qc", "decision_id",
                                  "decision_source", "decided_by"] as Set ||
        !(movingAssistance.registration_id instanceof String) || !(movingAssistance.registration_id as String).trim() ||
        !(movingAssistance.decision_id instanceof String) || !(movingAssistance.decision_id as String).trim() ||
        !(movingAssistance.decision_source in ["user", "config", "training"]) ||
        !(movingAssistance.decided_by instanceof String) || !(movingAssistance.decided_by as String).trim() ||
        !(movingAssistance.registration_qc instanceof Map) || movingAssistance.registration_qc.status != "passed")
        throw new IllegalStateException("Explicit moving assistance requires exact passed Warpy provenance")
}

Map classifierConfig = structure.classifier as Map
def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { input ->
        DigestInputStream stream = new DigestInputStream(input, digest)
        byte[] buffer = new byte[1024 * 1024]
        while (stream.read(buffer) >= 0) { }
    }
    digest.digest().encodeHex().toString()
}
Map classifierApplication = request.classifier_application as Map
Set<String> classifierApplicationFields = [
    "classifier_record_set_ref_sha256", "classifier_record_set_sha256",
    "output_id", "classifier_record_sha256", "classifier_id",
    "classifier_version", "classifier_kind", "application_request_sha256",
    "target_project_identity_sha256", "deployment_binding_set_sha256",
    "target_evidence_set_sha256", "project_relative_path", "source_model_path",
    "native_classifier_sha256", "native_classifier_byte_count",
    "pixel_classifier_definition", "pixel_classifier_definition_sha256"
] as Set
if (classifierApplication == null ||
        classifierApplication.keySet() != classifierApplicationFields ||
        classifierApplication.output_id != classifierConfig.classifier_id ||
        classifierApplication.classifier_kind != "secondary_structure" ||
        classifierApplication.classifier_id != classifierConfig.classifier_id ||
        !(classifierApplication.native_classifier_byte_count instanceof Number) ||
        (classifierApplication.native_classifier_byte_count as Number).longValue() <= 0L ||
        classifierApplicationFields.findAll { it.endsWith("_sha256") }.any { name ->
            !(classifierApplication[name] ==~ /[0-9a-f]{64}/)
        } || !(classifierApplication.pixel_classifier_definition instanceof Map))
    throw new IllegalStateException("Validated secondary classifier application is invalid")
String projectRelativePath = classifierApplication.project_relative_path as String
if (!(projectRelativePath ==~ /classifiers\/pixel_classifiers\/mif-[0-9a-f]{64}\.json/))
    throw new IllegalStateException("Secondary classifier project-relative locator is invalid")
Path artifactPath = projectRoot.resolve(projectRelativePath).normalize()
// The authenticated application owns any imported source member. Active
// domains copy it under ordinary project-stage authority before application.
// Excluded domains retain their physical predecessor and accepted model identity.
String classifierSha = primaryDomainExcluded
    ? classifierApplication.native_classifier_sha256 as String
    : NativeClassifierFiles.ensureModel(
        projectRoot, projectRelativePath, classifierApplication.source_model_path as String,
        classifierApplication.native_classifier_sha256 as String,
        (classifierApplication.native_classifier_byte_count as Number).longValue())
Map currentClassifierDefinition = classifierApplication.pixel_classifier_definition as Map
if (canonicalSha256(currentClassifierDefinition) !=
        classifierApplication.pixel_classifier_definition_sha256 ||
        currentClassifierDefinition.classifier_id != classifierApplication.classifier_id ||
        currentClassifierDefinition.version != classifierApplication.classifier_version ||
        currentClassifierDefinition.kind != "secondary")
    throw new IllegalStateException("Secondary classifier definition differs from the selected record")
String candidateClass = "Candidate_${structureId}".toString()
String otherClass = "Other_${structureId}".toString()
Object declaredOutputClasses = currentClassifierDefinition.output_classes
if (!(declaredOutputClasses instanceof List) ||
        declaredOutputClasses.any { !(it instanceof String) || !it.trim() } ||
        declaredOutputClasses.toSet().size() != declaredOutputClasses.size() ||
        !declaredOutputClasses.contains(candidateClass))
    throw new IllegalStateException("Secondary classifier output classes lack the exact target candidate")
Set<String> allowed = declaredOutputClasses as Set<String>
String classifierVersion = classifierApplication.classifier_version as String
Map channelRoles = new TreeMap(currentClassifierDefinition.channel_roles as Map)
def classifier
Map kronosSupport=null

String owner = "secondary_classifier|${runId}|${generationId}|${analysisDomainId}|${structureId}"
def hierarchy = getCurrentHierarchy()
List prior = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_owner") == owner && m.get("pipeline_run_id") == runId &&
        m.get("generation_id") == generationId
}
Set before = getAnnotationObjects().collect { it.getID() } as Set
// Our authenticated attempt owns one absent final table member.
Path roiOutputRoot = MifProjectStageBootstrapV1.outputRoot(request, config)
RoiInventory.prepareOutput(roiOutputRoot, envelope.invocation_id as String)
List created = []
try {
    // Excluded domains retain their saved hierarchy and publish an empty table.
    if (!primaryDomainExcluded) {
        if(Files.readString(artifactPath).contains("data.op.mif-kronos-native-v1")) {
            String helperName="compartment_analysis/kronos_bundle_support.groovy"
            def helperRows=authenticatedManifest.assets.findAll {it.path==helperName}
            Path helperPath=requestedBundleRoot.resolve(helperName)
            if(helperRows.size()!=1 || helperPath.toRealPath()!=helperPath || Files.size(helperPath)>1024*1024)
                throw new IllegalStateException("The registered KRONOS loader is unavailable")
            byte[] helperBytes=Files.readAllBytes(helperPath)
            if(MessageDigest.getInstance("SHA-256").digest(helperBytes).encodeHex().toString()!=helperRows[0].sha256)
                throw new IllegalStateException("The KRONOS loader differs from the selected script bundle")
            Map loader=new GroovyShell(this.class.classLoader).evaluate(new String(helperBytes,StandardCharsets.UTF_8),"kronos_bundle_support.groovy") as Map
            kronosSupport=loader.load(requestedBundleRoot)
            kronosSupport.resources.bind(boundImageData,eligible.collect {parent -> [id:parent.ID.toString(),geometry:parent.ROI.geometry,revision:analysisDomainRevisionSha256,split:"inference"]})
        }
        classifier = loadPixelClassifier(artifactPath.toString())
        selectObjects(eligible)
        createAnnotationsFromPixelClassifier(classifier, minimumCandidateArea, 0.0d, "SPLIT")
        created = getAnnotationObjects().findAll { !before.contains(it.getID()) }
        Map parentsByObject = [:]
        def parentSelector = new BiologicalParentSelector(eligible.collect { parent ->
            String stableId = parent == domain ? analysisDomainId : md(parent).get("roi_id") as String
            new BiologicalParentSelector.Parent(parent,parent.getROI().getGeometry(),stableId)
        })
        created.each { object ->
            String classification = object.getClassification()
            if (!allowed.contains(classification))
                throw new IllegalStateException("Secondary classifier emitted ${classification}")
            def geometry = object.getROI().getGeometry()
            if (!geometry.coveredBy(eligibleUnion))
                throw new IllegalStateException("Secondary output crosses its eligible-parent boundary")
            def stableParent = parentSelector.smallestContaining(geometry)
            if (stableParent == null)
                throw new IllegalStateException("Secondary output has no single stable biological parent")
            if (md(stableParent).get("source") == "registered_reference_assisted") {
                String registrationId = md(stableParent).get("registration_id") as String
                def registrationQc = gson.fromJson(md(stableParent).get("registration_qc") as String, Object)
                if (!registrationId || !(registrationQc instanceof Map) || registrationQc.status != "passed")
                    throw new IllegalStateException("Registered-reference-assisted parent requires passed registration provenance")
            }
            parentsByObject[object] = stableParent
        }
        // Cells have already been measured and phenotyped. Replacing a region
        // must retain its child objects and their saved scientific identities.
        if (!prior.isEmpty()) hierarchy.removeObjects(prior, true)
        created.sort { a, b -> a.getROI().getGeometry().toText() <=> b.getROI().getGeometry().toText() }
        created.each { object ->
            def parent = parentsByObject[object]
            parent.addChildObject(object)
            if (object.getClassification() != candidateClass)
                object.setPathClass(getPathClass(otherClass))
            String classification = object.getClassification()
            String resolvedParentType = parentIds.find { eligibleByType[it].contains(parent) }
            Map m = md(object)
            String stableKey = "${owner}|${classification}|${object.getROI().getGeometry().toText()}"
            m.put("roi_id", "roi-${UUID.nameUUIDFromBytes(stableKey.getBytes(StandardCharsets.UTF_8))}")
            m.put("parent_roi_id", parent == domain ? analysisDomainId : md(parent).get("roi_id") as String)
            m.put("parent_path_object_id", parent.getID().toString())
            m.put("analysis_domain_id", analysisDomainId)
            m.put("analysis_domain_kind", domainRevisionEvidence.kind as String)
            m.put("analysis_domain_revision", Integer.toString(
                (request.analysis_domain_revision as Number).intValue()
            ))
            m.put("analysis_domain_revision_sha256", analysisDomainRevisionSha256)
            m.put("analysis_domain_path_object_id", analysisDomainPathObjectId)
            m.put("domain_set_sha256", request.domain_set_sha256 as String)
            m.put("material_parent_type", resolvedParentType == "AnalysisDomain" ?
                "analysis_domain" : (primaryIds.contains(resolvedParentType) ? "primary" : "secondary"))
            m.put("material_parent_type_id", resolvedParentType)
            m.put("material_parent_revision_sha256",
                resolvedParentType == "AnalysisDomain" ? analysisDomainRevisionSha256 :
                    md(parent).get("object_revision_sha256"))
            m.put("slide_id", slideId);
            m.put("section_id", sectionId);
            m.put("generation_id", generationId)
            m.put("pipeline_stage", "secondary_classifier");
            m.put("pipeline_owner", owner);
            m.put("pipeline_run_id", runId)
            m.put("classifier_status", classifierReviewStatus)
            m.put("review_status", "needs_review");
            m.put("pipeline_active", "true")
            m.put("roi_level", "secondary");
            m.put("structure_id", structureId)
            m.put("eligible_parent_id", resolvedParentType)
            m.put("eligible_parent_ids", gson.toJson(parentIds))
            m.put("candidate_status", classification == candidateClass ? "candidate" : "other")
            m.put("classifier_id", classifierApplication.classifier_id as String);
            m.put("classifier_version", classifierVersion)
            if (movingAssistance != null) {
                m.put("source", "registered_reference_assisted")
                m.put("registration_id", movingAssistance.registration_id as String)
                m.put("registration_qc", gson.toJson(movingAssistance.registration_qc))
                m.put("moving_assistance_decision_id", movingAssistance.decision_id as String)
                m.put("moving_assistance_decision_source", movingAssistance.decision_source as String)
                m.put("moving_assistance_decided_by", movingAssistance.decided_by as String)
            } else m.put("source", "classifier")
            m.put("coordinate_system", "global_level0")
            Geometry normalizedObject = object.getROI().getGeometry().copy();
            normalizedObject.normalize()
            String objectGeometrySha256 = MessageDigest.getInstance("SHA-256").digest(
                new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedObject)
            ).encodeHex().toString()
            m.put("geometry_sha256", objectGeometrySha256)
            m.put("object_revision_sha256", canonicalSha256([
                analysis_domain_id: analysisDomainId,
                analysis_domain_revision_sha256: analysisDomainRevisionSha256,
                analysis_domain_set_sha256: request.domain_set_sha256,
                classification: object.getClassification(),
                geometry_sha256: objectGeometrySha256,
                parent_path_object_id: object.getParent().getID().toString(),
                path_object_id: object.getID().toString(),
                pipeline_active: m.get("pipeline_active"),
                type: "secondary",
                type_id: structureId,
            ]))
        }
    }
} catch (Exception error) {
    if (!created.isEmpty()) hierarchy.removeObjects(created, true)
    throw error
} finally {
    if(kronosSupport!=null)kronosSupport.resources.closeImage(boundImageData)
}
if (!primaryDomainExcluded) fireHierarchyUpdate()

def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
}
def persistCurrentEntryState = {
    if (!mifAuthenticatedHeadlessProjectStage ||
            !boundImageData.isChanged()) return
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
String reviewStatus = classifierReviewStatus
// We sort the existing collection once and stream its four typed columns.
// The completed table precedes saved-state and envelope publication.
created.sort { a, b -> (md(a).get("roi_id") as String) <=> (md(b).get("roi_id") as String) }
Map roiInventory = RoiInventory.write(roiOutputRoot, envelope.invocation_id as String, created,
    { object -> new RoiInventory.Row(md(object).get("roi_id") as String,
        object.getID().toString(), md(object).get("object_revision_sha256") as String,
        md(object).get("structure_id") as String) } as java.util.function.Function)
Map outputs = [
    roi_inventory: roiInventory,
    object_count: created.size(),
    section_id: sectionId, analysis_domain_id: analysisDomainId,
    analysis_domain_revision: (request.analysis_domain_revision as Number).intValue(),
    analysis_domain_revision_sha256: analysisDomainRevisionSha256,
    analysis_domain_path_object_id: analysisDomainPathObjectId,
    domain_set_sha256: request.domain_set_sha256,
    structure_id: structureId, eligible_parent_ids: parentIds,
    eligible_parent_ids_sha256: canonicalSha256(parentIds),
    parent_sources: parentSources,
    predecessor_result_ref_sha256: predecessorResultRefSha256,
    primary_domain_status: primaryDomainStatus,
    coordinate_system: "global_level0",
    classifier_id: classifierApplication.classifier_id, classifier_version: classifierVersion,
    classifier_kind: classifierApplication.classifier_kind,
    classifier_sha256: classifierSha,
    classifier_record_set_ref_sha256: classifierApplication.classifier_record_set_ref_sha256,
    classifier_record_set_sha256: classifierApplication.classifier_record_set_sha256,
    selected_output_id: classifierApplication.output_id,
    selected_classifier_record_sha256: classifierApplication.classifier_record_sha256,
    application_request_sha256: classifierApplication.application_request_sha256,
    target_project_identity_sha256: classifierApplication.target_project_identity_sha256,
    deployment_binding_set_sha256: classifierApplication.deployment_binding_set_sha256,
    target_evidence_set_sha256: classifierApplication.target_evidence_set_sha256,
    project_relative_path: projectRelativePath,
    native_classifier_sha256: classifierApplication.native_classifier_sha256,
    native_classifier_byte_count: classifierApplication.native_classifier_byte_count,
    pixel_classifier_definition_sha256: classifierApplication.pixel_classifier_definition_sha256,
    validation_domain_id: currentClassifierDefinition.domain_id, channel_roles: channelRoles,
    material_context_hash: configHash, review_status: reviewStatus,
    validation_status: classifierConfig.validation_status,
    validation_record_id: classifierConfig.validation_record_id,
    qc_passed: classifierConfig.qc_passed,
    qc_record_id: classifierConfig.qc_record_id,
    material_context_matches: true,
    review_action: reviewStatus == "accepted" ? "accept" : null]
outputs.classifier_definition = currentClassifierDefinition
persistCurrentEntryState()
atomicJson(manifestPath, [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
    generations: [], object_outputs: outputs])
String manifestSha = sha256(manifestPath)
Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: generationId, interaction_reason: null, affected_object_ids: [],
    message: null, allowed_actions: [], resume_context: null, checkpoint_id: null,
    outputs: outputs, error: null]
mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
    generation_manifest: [path: manifestPath.fileName.toString(), sha256: manifestSha]])
