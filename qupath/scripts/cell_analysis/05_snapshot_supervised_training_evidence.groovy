// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE

/** Export one immutable, current-image supervised-training evidence snapshot. */

import java.net.URI
import org.mif.cells.NativeCellScope
import org.mif.classification.ObjectTrainingRows
import java.util.function.Consumer
import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.UUID
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "cell_analysis/05_snapshot_supervised_training_evidence.groovy"
if (args.size() != 3)
    throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder()
    .serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(
    Files.readString(Paths.get(args[0] as String)), Map
) as Map
if (envelope.schema_version != "roi-stage-request/v2" ||
        envelope.stage != "supervised_training_evidence_snapshot")
    throw new IllegalArgumentException(
        "Invalid supervised training evidence snapshot request envelope"
    )
Map request = envelope.request as Map
Set specificationFields = [
    "training_plan_id", "reference_selection_sha256", "split", "sections", "class_ids", "feature_schema",
    "point_selection_radius_level0"
] as Set
List<Map> specifications =
    request.supervised_training_evidence_requests as List<Map>
if (specifications == null || specifications.isEmpty() ||
        specifications.any {
            !(it instanceof Map) || !((it.keySet() as Set) in
                [specificationFields, specificationFields + ["feature_source"]])
        })
    throw new IllegalArgumentException(
        "supervised_training_evidence_requests has an invalid typed shape"
    )

def boundProject = getProject()
def boundImageData = getCurrentImageData()
def boundEntry = getProjectEntry()
if (boundProject == null || boundImageData == null || boundEntry == null)
    throw new IllegalStateException(
        "Snapshot execution requires one open saved project image"
    )
Path boundProjectPath = boundProject.getPath().toAbsolutePath().normalize()
String requestedProjectFile = request.project_file as String
String requestedEntryId = request.project_entry_id as String
String requestedCanonicalSha256 = request.canonical_image_sha256 as String
if (request.exchange_version != "roi-stage-request/v2" ||
        !requestedProjectFile ||
        Paths.get(requestedProjectFile).toAbsolutePath().normalize() != boundProjectPath ||
        !requestedEntryId || boundEntry.getID().toString() != requestedEntryId ||
        !(requestedCanonicalSha256 ==~ /[0-9a-f]{64}/) ||
        !((request.script_bundle_sha256 as String) ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException(
        "Exact project entry identity and script bundle are required"
    )
String requestedScriptBundleSha256 = request.script_bundle_sha256 as String
Path projectRoot = boundProjectPath.parent
Path scriptsRoot = projectRoot.resolve("scripts").normalize()
Path requestedBundleRoot = scriptsRoot
    .resolve("bundle-${requestedScriptBundleSha256}").normalize()
Path bundleManifestPath = requestedBundleRoot
    .resolve("qupath-script-bundle.v1.json").normalize()
Path dataRoot = projectRoot.resolve("data").normalize()
Path scriptBundleEvidencePath = dataRoot
    .resolve("qupath-script-bundle.evidence.json").normalize()
if (requestedBundleRoot.parent != scriptsRoot ||
        bundleManifestPath.parent != requestedBundleRoot ||
        dataRoot.parent != projectRoot ||
        scriptBundleEvidencePath.parent != dataRoot ||
        projectRoot.toRealPath() != projectRoot ||
        scriptsRoot.toRealPath() != scriptsRoot ||
        requestedBundleRoot.toRealPath() != requestedBundleRoot ||
        bundleManifestPath.toRealPath() != bundleManifestPath ||
        dataRoot.toRealPath() != dataRoot ||
        scriptBundleEvidencePath.toRealPath() != scriptBundleEvidencePath ||
        Files.isSymbolicLink(scriptsRoot) || !Files.isDirectory(scriptsRoot) ||
        Files.isSymbolicLink(requestedBundleRoot) ||
        !Files.isDirectory(requestedBundleRoot) ||
        Files.isSymbolicLink(bundleManifestPath) ||
        !Files.isRegularFile(bundleManifestPath) ||
        Files.isSymbolicLink(dataRoot) || !Files.isDirectory(dataRoot) ||
        Files.isSymbolicLink(scriptBundleEvidencePath) ||
        !Files.isRegularFile(scriptBundleEvidencePath))
    throw new IllegalStateException(
        "Immutable QuPath script bundle path ancestry is invalid"
    )
byte[] authenticatedManifestBytes = Files.readAllBytes(bundleManifestPath)
String authenticatedManifestSha256 = MessageDigest.getInstance("SHA-256")
    .digest(authenticatedManifestBytes).encodeHex().toString()
if (authenticatedManifestSha256 != requestedScriptBundleSha256)
    throw new IllegalStateException(
        "Immutable QuPath script bundle manifest does not match the request"
    )
Map authenticatedManifest = gson.fromJson(
    new String(authenticatedManifestBytes, StandardCharsets.UTF_8), Map.class
) as Map
if (authenticatedManifest == null ||
        authenticatedManifest.keySet() != ["schema_version", "assets"] as Set ||
        authenticatedManifest.schema_version != "qupath-script-bundle/v1" ||
        !(authenticatedManifest.assets instanceof List) ||
        (authenticatedManifest.assets as List).isEmpty())
    throw new IllegalStateException(
        "Authenticated QuPath script bundle manifest is invalid"
    )
final String verifierAssetPath = "general/00_verify_stage_script_bundle.groovy"
List verifierEvidence = (authenticatedManifest.assets as List).findAll { raw ->
    raw instanceof Map &&
        raw.keySet() == ["path", "sha256"] as Set &&
        raw.path == verifierAssetPath
}
Path verifierPath = requestedBundleRoot.resolve(verifierAssetPath).normalize()
if (verifierEvidence.size() != 1 ||
        !((verifierEvidence.first().sha256 as String) ==~ /[0-9a-f]{64}/) ||
        verifierPath.toRealPath() != verifierPath ||
        Files.isSymbolicLink(verifierPath) ||
        !Files.isRegularFile(verifierPath) ||
        MessageDigest.getInstance("SHA-256")
            .digest(Files.readAllBytes(verifierPath)).encodeHex().toString() !=
                verifierEvidence.first().sha256)
    throw new IllegalStateException(
        "Immutable QuPath script bundle verifier is invalid"
    )
Closure verifyCurrentScriptBundle = evaluate(verifierPath.toFile()) as Closure
verifyCurrentScriptBundle(request, boundProjectPath, SCRIPT_ASSET_PATH, gson)
// Frozen native references select the cells whose features enter training.
// The same source description is embedded in the fitted native object model.
// Load only helpers whose bytes belong to this authenticated script bundle.
def loadCellFeatureSupport = { String relative ->
    List rows = (authenticatedManifest.assets as List).findAll { it.path == relative }
    Path path = requestedBundleRoot.resolve(relative).normalize()
    if (rows.size() != 1 || !path.startsWith(requestedBundleRoot) ||
            path.toRealPath() != path || Files.isSymbolicLink(path) || !Files.isRegularFile(path))
        throw new IllegalStateException("The KRONOS cell support asset is absent or linked: " + relative)
    byte[] bytes = Files.readAllBytes(path)
    if (MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString() != rows[0].sha256)
        throw new IllegalStateException("The KRONOS cell support asset changed: " + relative)
    Object loaded = new GroovyShell(this.class.classLoader).evaluate(new String(bytes, StandardCharsets.UTF_8))
    if (!(loaded instanceof Map)) throw new IllegalStateException("Invalid KRONOS cell support: " + relative)
    loaded as Map
}
Map cellFeatureSupport = null
Map cellWorkerSupport = null
Map supervisedReferences = loadCellFeatureSupport("cell_analysis/supervised_references.groovy")
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
Object liveDigest = boundEntry.getMetadataMap()
    .get("mif_pipeline.source_image_sha256")
if (canonicalUri.scheme != "file" || canonicalUri.query != null ||
        canonicalUri.fragment != null ||
        canonicalUri.toASCIIString() != requestedCanonicalUri ||
        liveRole != "primary_mif" || liveDigest != requestedCanonicalSha256 ||
        liveCanonicalUris != [requestedCanonicalUri])
    throw new IllegalStateException(
        "Request image authority does not match the open canonical mIF entry"
    )

def typedStrings = { value, String field ->
    if (!(value instanceof List) || (value as List).isEmpty() ||
            (value as List).any {
                !(it instanceof String) || !(it as String).trim()
            } || (value as List).toSet().size() != (value as List).size())
        throw new IllegalArgumentException(
            "${field} must be unique nonblank strings"
        )
    (value as List).collect { it as String }
}
String slideId = envelope.slide_id as String
String runId = envelope.run_id as String
if (!slideId?.trim() || !runId?.trim())
    throw new IllegalArgumentException(
        "Snapshot slide and run identities must be explicit"
    )
List<Map> snapshotSets = specifications.collect { Map specification ->
String trainingPlanId = specification.training_plan_id as String
String referenceSelectionSha256 = specification.reference_selection_sha256 as String
String split = specification.split as String
if (!trainingPlanId?.trim() || !(referenceSelectionSha256 ==~ /[0-9a-f]{64}/) ||
        !(split in ["training", "validation", "test"]))
    throw new IllegalArgumentException(
        "Snapshot plan and role identities must be explicit"
    )
List<String> classIds =
    typedStrings(specification.class_ids, "class_ids")
if (!(specification.feature_schema instanceof List) ||
        (specification.feature_schema as List).isEmpty())
    throw new IllegalArgumentException(
        "feature_schema must declare exact measurements"
    )
List<Map> featureSchema = (specification.feature_schema as List).collect { raw ->
    if (!(raw instanceof Map) ||
            (raw as Map).keySet() as Set !=
                ["measurement_name", "semantic_role"] as Set ||
            !(raw.measurement_name instanceof String) ||
            !(raw.measurement_name as String).trim() ||
            !(raw.semantic_role instanceof String) ||
            !(raw.semantic_role as String).trim())
        throw new IllegalArgumentException(
            "feature_schema entries require exact measurement_name and semantic_role"
        )
    [measurement_name: raw.measurement_name as String,
     semantic_role: raw.semantic_role as String]
}
List<String> measurementNames = featureSchema.collect {
    it.measurement_name as String
}
if (measurementNames.toSet().size() != measurementNames.size())
    throw new IllegalArgumentException(
        "feature_schema measurement names must be unique"
    )
if (!(specification.point_selection_radius_level0 instanceof Number) ||
        !Double.isFinite(
            (specification.point_selection_radius_level0 as Number).doubleValue()
        ) ||
        (specification.point_selection_radius_level0 as Number).doubleValue() <= 0d)
    throw new IllegalArgumentException(
        "point_selection_radius_level0 must be positive"
    )
double pointSelectionRadius =
    (specification.point_selection_radius_level0 as Number).doubleValue()

def metadata = { object -> object.getMetadata() }
Set sectionFields = [
    "section_id", "segmentation_generation_id",
    "measurement_generation_id", "analysis_domain_ids"
] as Set
if (!(specification.sections instanceof List) ||
        (specification.sections as List).isEmpty())
    throw new IllegalArgumentException(
        "sections must contain the complete authorized entry scope"
    )
List<Map> sectionSpecifications =
    (specification.sections as List).collect { raw ->
        if (!(raw instanceof Map) || (raw.keySet() as Set) != sectionFields)
            throw new IllegalArgumentException(
                "sections contain an invalid typed scope"
            )
        String sectionId = raw.section_id as String
        String segmentationGenerationId =
            raw.segmentation_generation_id as String
        String measurementGenerationId =
            raw.measurement_generation_id as String
        if (!sectionId?.trim() || !segmentationGenerationId?.trim() ||
                !measurementGenerationId?.trim())
            throw new IllegalArgumentException(
                "Section and generation identities must be explicit"
            )
        List<String> domainIds = typedStrings(
            raw.analysis_domain_ids, "analysis_domain_ids"
        )
        if (domainIds != domainIds.toSorted())
            throw new IllegalArgumentException(
                "analysis_domain_ids must be canonically ordered"
            )
        [
            section_id: sectionId,
            segmentation_generation_id: segmentationGenerationId,
            measurement_generation_id: measurementGenerationId,
            analysis_domain_ids: domainIds
        ]
    }
List sectionIdentities = sectionSpecifications.collect { raw ->
    [raw.section_id, raw.segmentation_generation_id,
     raw.measurement_generation_id]
}
List sortedSectionIdentities = sectionIdentities.toSorted { left, right ->
    (left[0] <=> right[0]) ?: (left[1] <=> right[1]) ?:
        (left[2] <=> right[2])
}
if (sectionIdentities.toSet().size() != sectionIdentities.size() ||
        sectionIdentities != sortedSectionIdentities)
    throw new IllegalArgumentException(
        "sections must be unique and canonically ordered"
    )

Map featureSession=null
List<Map> snapshots
try {
snapshots = (specification.sections as List).collect { raw ->
    Map section = sectionSpecifications.find { candidate ->
        candidate.section_id == raw.section_id &&
            candidate.segmentation_generation_id ==
                raw.segmentation_generation_id &&
            candidate.measurement_generation_id == raw.measurement_generation_id
    }
    String sectionId = section.section_id as String
    String segmentationGenerationId =
        section.segmentation_generation_id as String
    String measurementGenerationId =
        section.measurement_generation_id as String
    List<String> analysisDomainIds =
        section.analysis_domain_ids as List<String>
    // Read the saved associations before expensive feature or geometry work.
    // Empty sections remain part of the plan without opening an encoder.
    Map reference = [training_plan_id: trainingPlanId, reference_selection_sha256: referenceSelectionSha256,
        project_entry_id: requestedEntryId,
        slide_id: slideId, split: split, reference_role: split == "training" ? "training" : "holdout",
        class_ids: classIds, sections: [section]]
    List<Map> pointEvidence = supervisedReferences.read.call(boundImageData, reference)[sectionId]
    Set<String> referenceCellIds = pointEvidence.collect { it.cell_id as String } as Set
    Closure eligibleCell = { object ->
        Map values = metadata(object)
        object.isCell() &&
            values.get("pipeline_stage") == "segmentation" &&
            values.get("slide_id") == slideId &&
            values.get("section_id") == sectionId &&
            values.get("generation_id") == segmentationGenerationId &&
            values.get("measurement_generation_id") == measurementGenerationId &&
            analysisDomainIds.contains(values.get("analysis_domain_id") as String)
    }
    if (specification.feature_source != null) {
        if (!(specification.feature_source instanceof Map))
            throw new IllegalArgumentException("KRONOS cell feature_source must be an explicit record")
        if (cellFeatureSupport == null) {
            cellFeatureSupport = loadCellFeatureSupport("cell_analysis/kronos_cell_features.groovy")
            cellWorkerSupport = loadCellFeatureSupport("compartment_analysis/kronos_worker_client.groovy")
        }
        if (featureSchema != cellFeatureSupport.featureSchema.call())
            throw new IllegalArgumentException("KRONOS cell snapshots require the complete ordered 768-feature schema")
    }
    List automaticMarkers=specification.feature_source==null ? [] :
        cellFeatureSupport.pendingMarkerNames.call(specification.feature_source as Map)
    Map deferredInputs=automaticMarkers.isEmpty() ? null :
        [schema:"kronos-cell-input-evidence/v1",automatic_marker_names:automaticMarkers,
         source_intent_sha256:cellFeatureSupport.sourceIntentSha256.call(specification.feature_source as Map,automaticMarkers),
         cell_input_sha256:new TreeMap()]
    Set<String> populationIds = new TreeSet()
    List<Map> eligibleCells = []
    Closure recordCell = { cell ->
        Map values = metadata(cell)
        String cellId = values.get("cell_id") as String
        String domainId = values.get("analysis_domain_id") as String
        String domainKind = values.get("analysis_domain_kind") as String
        if (!cellId?.trim() ||
                !domainId?.trim() ||
                !(domainKind in ["selected_roi", "whole_tissue_section"]))
            throw new IllegalStateException(
                "Eligible cells require unique stable IDs and exact domain identity"
            )
        Map measurements = new TreeMap()
        (deferredInputs==null ? measurementNames : []).each { measurementName ->
            Object value = cell.getMeasurements().get(measurementName)
            if (!(value instanceof Number) ||
                    !Double.isFinite((value as Number).doubleValue()))
                throw new IllegalStateException(
                    "Cell ${cellId} lacks finite feature ${measurementName}"
                )
            measurements[measurementName] = (value as Number).doubleValue()
        }
        String primaryCompartment =
            values.get("primary_compartment") as String ?:
            cell.getPathClass()?.toString()
        Map row = [
            cell_id: cellId,
            slide_id: slideId,
            section_id: sectionId,
            analysis_domain_id: domainId,
            analysis_domain_kind: domainKind,
            x_level0: cell.getROI().getCentroidX(),
            y_level0: cell.getROI().getCentroidY(),
            cell_geometry: ObjectTrainingRows.geometry(cell.getROI().getGeometry()),
            nucleus_geometry: cell.getNucleusROI() == null ? [:] :
                ObjectTrainingRows.geometry(cell.getNucleusROI().getGeometry()),
            primary_compartment: primaryCompartment,
            marker_measurements: measurements
        ]
        eligibleCells << row
        String populationId = values.get("segmentation_population_id") as String
        if (!populationId?.trim())
            throw new IllegalStateException("Reference cells require one current segmentation population")
        populationIds.add(populationId)
    }
    List featureBatch = []
    Closure flushFeatures = {
        if (featureBatch.isEmpty()) return
        Map inputScope=[image_sha256:requestedCanonicalSha256,segmentation_generation_id:segmentationGenerationId]
        if(deferredInputs!=null) {
            Map batchInputs=cellFeatureSupport.inputEvidence.call(boundImageData,featureBatch,specification.feature_source as Map,inputScope)
            if(batchInputs.source_intent_sha256!=deferredInputs.source_intent_sha256)
                throw new IllegalStateException("KRONOS input definition changed during snapshot capture")
            deferredInputs.cell_input_sha256.putAll(batchInputs.cell_input_sha256)
        } else {
            if(featureSession==null)
                featureSession=cellFeatureSupport.open.call(specification.feature_source as Map,
                    resultPath.toAbsolutePath().parent.resolve("cell-feature-cache"),cellWorkerSupport)
            cellFeatureSupport.prepare.call(boundImageData, featureBatch, specification.feature_source as Map,
                inputScope,resultPath.toAbsolutePath().parent.resolve("cell-feature-cache"),cellWorkerSupport,featureSession)
        }
        featureBatch.each { recordCell(it) }
        featureBatch.clear()
    }
    if (!referenceCellIds.isEmpty()) {
        NativeCellScope.open(boundImageData, sectionId).join().forEach({ cell ->
            if (!referenceCellIds.contains(metadata(cell).get("cell_id") as String) ||
                    !eligibleCell(cell)) return
            if (specification.feature_source == null) recordCell(cell)
            else {
                featureBatch.add(cell)
                if (featureBatch.size() == 1024) flushFeatures()
            }
        } as Consumer)
    }
    if (specification.feature_source != null) flushFeatures()
    if (!eligibleCells.isEmpty() && populationIds.size() != 1)
        throw new IllegalStateException("Reference cells require one current segmentation population")
    eligibleCells.sort { it.cell_id }
    for (int i = 1; i < eligibleCells.size(); i++) {
        if (eligibleCells[i - 1].cell_id == eligibleCells[i].cell_id)
            throw new IllegalStateException("Eligible snapshot cells contain a duplicate finalized identity")
    }
    if (eligibleCells.collect { it.cell_id }.toSet() != referenceCellIds)
        throw new IllegalStateException(
            "Saved reference cells are absent from the current measured section"
        )

    Map snapshot=[
        section_id: sectionId,
        pipeline_run_id: runId,
        segmentation_generation_id: segmentationGenerationId,
        measurement_generation_id: measurementGenerationId,
        coordinate_system: "global_level0",
        point_selection_radius_level0: pointSelectionRadius,
        class_ids: classIds,
        feature_schema: featureSchema,
        point_evidence: pointEvidence,
        eligible_cells: eligibleCells
    ]
    if(deferredInputs!=null) {
        cellFeatureSupport.validateInputEvidence.call(deferredInputs,specification.feature_source as Map,referenceCellIds)
        snapshot.deferred_feature_evidence=deferredInputs
    }
    snapshot
}
} finally {
    if(featureSession!=null)cellFeatureSupport.close.call(featureSession)
}
[
    training_plan_id: trainingPlanId,
    split: split,
    snapshots: snapshots
]
}
Map<String, Integer> splitRank = [training: 0, validation: 1, test: 2]
List roleKeys = snapshotSets.collect { item ->
    [item.training_plan_id, item.split]
}
List canonicalRoleKeys = roleKeys.toSorted { left, right ->
    (left[0] <=> right[0]) ?:
        (splitRank[left[1] as String] <=> splitRank[right[1] as String])
}
if (roleKeys.toSet().size() != roleKeys.size() ||
        roleKeys != canonicalRoleKeys ||
        snapshotSets.collect { it.training_plan_id }.toSet().size() != 1)
    throw new IllegalArgumentException(
        "supervised training evidence roles must be unique and canonical"
    )
Map outputs = [
    supervised_training_evidence_snapshot_sets: snapshotSets
]
def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { input ->
        DigestInputStream stream = new DigestInputStream(input, digest)
        byte[] buffer = new byte[1024 * 1024]
        while (stream.read(buffer) >= 0) { }
    }
    digest.digest().encodeHex().toString()
}
def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(
        ".${path.fileName}.${UUID.randomUUID()}.tmp"
    )
    Files.writeString(
        temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8
    )
    try {
        Files.move(
            temp, path,
            StandardCopyOption.ATOMIC_MOVE,
            StandardCopyOption.REPLACE_EXISTING
        )
    } catch (AtomicMoveNotSupportedException ignored) {
        Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING)
    }
}
atomicJson(manifestPath, [
    schema_version: "roi-generation-manifest/v1",
    invocation_id: envelope.invocation_id,
    run_id: runId,
    stage: envelope.stage,
    slide_id: slideId,
    generation_ids: snapshotSets.collect { it.training_plan_id }.toSet().toList().sort(),
    generations: [],
    // The result owns the snapshot rows; the manifest binds its plan generation.
    object_outputs: [:]
])
Map result = [
    status: "completed",
    stage: envelope.stage,
    slide_id: slideId,
    generation_id: snapshotSets[0].training_plan_id,
    interaction_reason: null,
    affected_object_ids: [],
    message: null,
    allowed_actions: [],
    resume_context: null,
    checkpoint_id: null,
    outputs: outputs,
    error: null
]
mifPublishProjectStageResult(resultPath, [
    schema_version: "roi-stage-result/v1",
    invocation_id: envelope.invocation_id,
    run_id: runId,
    stage: envelope.stage,
    slide_id: slideId,
    result: result,
    generation_manifest: [
        path: manifestPath.fileName.toString(),
        sha256: sha256(manifestPath)
    ]
])
