// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Snapshot immutable compact evidence for live canonical-mIF training annotations. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.MessageDigest
import java.util.UUID
import org.locationtech.jts.geom.Geometry
import org.mif.roi.BiologicalParentSelector
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools
import com.google.gson.ToNumberPolicy
import qupath.lib.objects.PathObject

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "compartment_analysis/01_snapshot_training_annotations.groovy"
final String STAGE = "training_evidence_snapshot"
final String SNAPSHOT_SCHEMA = "training-evidence-snapshot/v2"
if (args.size() != 3)
    throw new IllegalArgumentException("Expected request, result and manifest paths")
Path requestPath = Paths.get(args[0] as String)
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
// We preserve integer identities and revision values while reading numeric metadata.
def gson = GsonTools.getInstance().newBuilder().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
def canonicalGson = gson.newBuilder().serializeNulls().create()
def wireGson = gson.newBuilder().serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(requestPath), Map.class) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != STAGE)
    throw new IllegalArgumentException("Invalid training-evidence request envelope")
Map request = envelope.request as Map
def boundProject = getProject()
def boundImageData = getCurrentImageData()
def boundEntry = getProjectEntry()
if (boundProject == null || boundImageData == null || boundEntry == null)
    throw new IllegalStateException("Stage execution requires an open saved canonical mIF project image")
Path boundProjectPath = boundProject.getPath().toAbsolutePath().normalize()
String requestedProjectFile = request.project_file as String
String requestedEntryId = request.project_entry_id as String
String requestedCanonicalSha256 = request.canonical_image_sha256 as String
if (request.exchange_version != "roi-stage-request/v2" ||
        !requestedProjectFile || Paths.get(requestedProjectFile).toAbsolutePath().normalize() != boundProjectPath ||
        !requestedEntryId || boundEntry.getID().toString() != requestedEntryId ||
        !(requestedCanonicalSha256 ==~ /[0-9a-f]{64}/) ||
        !((request.script_bundle_sha256 as String) ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException("Exact canonical mIF project entry identity and script bundle are required")
String requestedScriptBundleSha256 = request.script_bundle_sha256 as String
Path projectRoot = boundProjectPath.parent
Path scriptsRoot = projectRoot.resolve("scripts").normalize()
Path requestedBundleRoot = scriptsRoot.resolve("bundle-${requestedScriptBundleSha256}").normalize()
Path bundleManifestPath = requestedBundleRoot.resolve("qupath-script-bundle.v1.json").normalize()
Path dataRoot = projectRoot.resolve("data").normalize()
Path scriptBundleEvidencePath = dataRoot.resolve("qupath-script-bundle.evidence.json").normalize()
if (requestedBundleRoot.parent != scriptsRoot || bundleManifestPath.parent != requestedBundleRoot ||
        dataRoot.parent != projectRoot || scriptBundleEvidencePath.parent != dataRoot ||
        projectRoot.toRealPath() != projectRoot || scriptsRoot.toRealPath() != scriptsRoot ||
        requestedBundleRoot.toRealPath() != requestedBundleRoot ||
        bundleManifestPath.toRealPath() != bundleManifestPath ||
        dataRoot.toRealPath() != dataRoot || scriptBundleEvidencePath.toRealPath() != scriptBundleEvidencePath ||
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

def nonblank = { Object value, String field ->
    if (!(value instanceof String) || !(value as String).trim())
        throw new IllegalArgumentException("${field} must be a non-empty string")
    value as String
}
def sha256Bytes = { byte[] bytes ->
    MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()
}
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
    sha256Bytes(canonicalGson.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8))
}
def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
}

String slideId = nonblank(envelope.slide_id, "slide_id")
List<Map> pixelRequests = request.pixel_training_evidence_requests as List<Map>
Set<String> pixelRequestFields = [
    "training_plan_id", "classifier_role", "section_id", "generation_id",
    "generation_ids", "run_id", "analysis_domain_id",
    "analysis_domain_revision", "analysis_domain_revision_sha256",
    "analysis_domain_path_object_id", "analysis_domain_revision_evidence",
    "domain_set_sha256", "split", "training_labels", "eligible_parent_ids",
    "expected_evidence_sha256s"
] as Set
if (pixelRequests == null || pixelRequests.isEmpty() || pixelRequests.any { raw ->
        !(raw instanceof Map) || raw.keySet() != pixelRequestFields ||
            !((raw.training_plan_id as String) ==~ /[0-9a-f]{64}/) ||
            !(raw.classifier_role in ["primary_compartment", "secondary_structure"])
    })
    throw new IllegalArgumentException(
        "A closed nonempty pixel training plan-by-domain request matrix is required"
    )
Closure pixelRequestKey = { Map raw ->
    [raw.training_plan_id, raw.classifier_role, raw.split,
     raw.analysis_domain_id, raw.analysis_domain_revision_sha256]
}
List<List> pixelRequestKeys = pixelRequests.collect(pixelRequestKey)
// Match the Python producer's tuple order; Groovy's default List sort uses hashes.
List<List> canonicalPixelRequestKeys = pixelRequestKeys.sort(false) { List left, List right ->
    for (int index = 0; index < left.size(); index++) {
        int comparison = (left[index] as String) <=> (right[index] as String)
        if (comparison != 0) return comparison
    }
    0
}
if (pixelRequestKeys != canonicalPixelRequestKeys ||
        pixelRequestKeys.toSet().size() != pixelRequestKeys.size())
    throw new IllegalArgumentException(
        "Pixel training plan-by-domain requests must be canonical and unique"
    )
Set entryGenerationIds = pixelRequests.collect { it.generation_id }.toSet()
if (entryGenerationIds.size() != 1)
    throw new IllegalArgumentException(
        "Pixel training plan-by-domain requests must share one entry generation"
    )
String entryGenerationId = nonblank(entryGenerationIds.first(), "generation_id")
Map commonRequest = new LinkedHashMap(request)
commonRequest.remove("pixel_training_evidence_requests")
List<Map> planDomainResults = []
pixelRequests.each { Map pixelRequest ->
request = new LinkedHashMap(commonRequest)
request.putAll(pixelRequest)
String sectionId = nonblank(request.section_id, "section_id")
String generationId = nonblank(request.generation_id, "generation_id")
String domainId = nonblank(request.analysis_domain_id, "analysis_domain_id")
String domainPathObjectId = nonblank(request.analysis_domain_path_object_id, "analysis_domain_path_object_id")
String domainRevisionSha256 = nonblank(request.analysis_domain_revision_sha256, "analysis_domain_revision_sha256")
String domainSetSha256 = nonblank(request.domain_set_sha256, "domain_set_sha256")
String split = nonblank(request.split, "split")
if(split!="training" && boundImageData.getProperty("mif.annotation_feedback")=="model_assisted")
    throw new IllegalArgumentException("This subject received annotation-preview feedback. Use it for development and select unexposed subjects for held-out evaluation.")
if (!(domainId ==~ /[0-9a-f]{64}/) || !(domainRevisionSha256 ==~ /[0-9a-f]{64}/) ||
        !(domainSetSha256 ==~ /[0-9a-f]{64}/) ||
        !(split in ["training", "validation", "test"]) ||
        !(request.analysis_domain_revision instanceof Number) ||
        (request.analysis_domain_revision as Number).intValue() < 1 ||
        (request.analysis_domain_revision as Number).doubleValue() !=
            (request.analysis_domain_revision as Number).intValue() ||
        request.generation_ids != [generationId])
    throw new IllegalArgumentException("Exact domain revision, split and generation are required")
Map domainRevisionEvidence = request.analysis_domain_revision_evidence as Map
if (domainRevisionEvidence == null ||
        domainRevisionEvidence.analysis_domain_id != domainId ||
        domainRevisionEvidence.source_path_object_id != domainPathObjectId ||
        domainRevisionEvidence.project_entry_id != requestedEntryId ||
        domainRevisionEvidence.revision != request.analysis_domain_revision ||
        domainRevisionEvidence.content_sha256 != domainRevisionSha256)
    throw new IllegalStateException("Authenticated analysis-domain revision evidence is invalid")
Map unsignedDomainRevisionEvidence = new TreeMap(domainRevisionEvidence)
unsignedDomainRevisionEvidence.remove("content_sha256")
// This is serialized geometry evidence, not part of the Task 6 revision digest.
unsignedDomainRevisionEvidence.remove("normalized_wkb_hex")
if (canonicalSha256(unsignedDomainRevisionEvidence) != domainRevisionSha256)
    throw new IllegalStateException("Analysis-domain revision content digest is stale")

List<Map> labels = request.training_labels as List<Map>
if (labels == null || labels.isEmpty() || labels.any { raw ->
        !(raw instanceof Map) || raw.keySet() != ["class_id", "path_class_id"] as Set ||
            !(raw.class_id instanceof String) || !(raw.class_id as String).trim() ||
            !(raw.path_class_id instanceof String) || !(raw.path_class_id as String).trim()
    } || labels.collect { it.class_id }.toSet().size() != labels.size() ||
        labels.collect { it.path_class_id }.toSet().size() != labels.size())
    throw new IllegalArgumentException("Exact unique training class and PathClass IDs are required")
Map<String, String> classByPathClass = labels.collectEntries {
    [(it.path_class_id as String): it.class_id as String]
}

def md = { PathObject object -> object.getMetadata() }
Map<String, PathObject> annotationsById = getAnnotationObjects().collectEntries {
    [(it.getID().toString()): it]
}
PathObject domain = annotationsById[domainPathObjectId]
if (domain == null)
    throw new IllegalStateException("Exact live analysis-domain PathObject is absent")
List<PathObject> sections = getAnnotationObjects().findAll { object ->
    Map metadata = md(object)
    metadata.get("pipeline_stage") == "tissue_detection" &&
        metadata.get("generation_status") == "accepted" &&
        metadata.get("review_status") == "accepted" &&
        metadata.get("slide_id") == slideId && metadata.get("section_id") == sectionId &&
        metadata.get("generation_id") == generationId
}
if (sections.size() != 1)
    throw new IllegalStateException("Exactly one accepted current TissueSection is required")
PathObject section = sections.first()
Map domainMetadata = md(domain)
boolean acceptedSelectedDomain =
    domainMetadata.get("pipeline_stage") == "analysis_domain_selection" &&
    domainMetadata.get("analysis_domain_status") == "accepted" &&
    domainMetadata.get("analysis_domain_active") == "true" &&
    domainMetadata.get("analysis_domain_id") == domainId &&
    domainMetadata.get("analysis_domain_revision") ==
        Integer.toString((request.analysis_domain_revision as Number).intValue()) &&
    domainMetadata.get("analysis_domain_revision_sha256") == domainRevisionSha256 &&
    domainMetadata.get("analysis_domain_set_sha256") == domainSetSha256 &&
    domainMetadata.get("analysis_domain_project_entry_id") == requestedEntryId &&
    domain.isLocked() &&
    domain.getParent() == section
boolean acceptedWholeTissueDomain = domain == section
if (!(acceptedSelectedDomain || acceptedWholeTissueDomain))
    throw new IllegalStateException("Training snapshot domain is stale, inactive, or cross-image")

Geometry domainGeometry = domain.getROI()?.getGeometry()
if (domainGeometry == null || domainGeometry.isEmpty() || !domainGeometry.isValid())
    throw new IllegalStateException("Training snapshot domain geometry is invalid")
if (domainRevisionEvidence.geometry_sha256 != null) {
    Geometry normalizedDomain = domainGeometry.copy();
        normalizedDomain.normalize()
    String liveDomainGeometrySha256 = sha256Bytes(
        new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedDomain)
    )
    if (liveDomainGeometrySha256 != domainRevisionEvidence.geometry_sha256)
        throw new IllegalStateException("Live analysis-domain geometry differs from its accepted revision")
}
List<PathObject> peerDomains = acceptedSelectedDomain ? getAnnotationObjects().findAll { object ->
    Map metadata = md(object)
    metadata.get("pipeline_stage") == "analysis_domain_selection" &&
    metadata.get("analysis_domain_status") == "accepted" &&
    metadata.get("analysis_domain_active") == "true" &&
        metadata.get("analysis_domain_set_sha256") == domainSetSha256 &&
        metadata.get("analysis_domain_project_entry_id") == requestedEntryId &&
        object.isLocked() && object.getParent() == section
} : [domain]
if (!peerDomains.any { it.getID().toString() == domainPathObjectId } ||
        peerDomains.any { object ->
            Geometry geometry = object.getROI()?.getGeometry()
            geometry == null || geometry.isEmpty() || !geometry.isValid()
        })
    throw new IllegalStateException("Accepted peer analysis-domain inventory is invalid")
def domainSelector = new BiologicalParentSelector(peerDomains.collect { peer ->
    new BiologicalParentSelector.Parent(peer,peer.getROI().getGeometry(),peer.getID().toString())
})
List requestedParents = request.eligible_parent_ids instanceof List ? request.eligible_parent_ids as List : []
if (requestedParents.isEmpty() || requestedParents.any {
        !(it instanceof String) || !(it as String).trim()
    } || requestedParents.toSet().size() != requestedParents.size())
    throw new IllegalArgumentException("Exact eligible-parent type inventory is required")
List<PathObject> candidateParents = requestedParents.contains("AnalysisDomain") ? [domain] : []
Map<PathObject, Map> parentEvidence = [:]
if (candidateParents.contains(domain)) parentEvidence[domain] = [
    material_parent_type: "analysis_domain",
    material_parent_type_id: "AnalysisDomain",
    material_parent_revision_sha256: domainRevisionSha256,
]
Closure reachesDomain = { PathObject object ->
    PathObject cursor = object
    Set seen = [] as Set
    while (cursor != null && cursor != domain) {
        if (!seen.add(cursor.getID())) return false
        cursor = cursor.getParent()
    }
    cursor == domain
}
getAnnotationObjects().each { object ->
    Map metadata = md(object)
    String primaryId = metadata.get("primary_class_id") as String
    String structureId = metadata.get("structure_id") as String
    if (metadata.get("analysis_domain_id") == domainId &&
            metadata.get("pipeline_run_id") == request.run_id &&
            metadata.get("generation_id") == generationId &&
            metadata.get("pipeline_active") == "true" &&
            metadata.get("analysis_domain_revision_sha256") == domainRevisionSha256 &&
            metadata.get("domain_set_sha256") == domainSetSha256 &&
            object.getParent() != null &&
            reachesDomain(object as PathObject) &&
            ((metadata.get("pipeline_stage") == "primary_classifier" && requestedParents.contains(primaryId)) ||
             (metadata.get("pipeline_stage") == "secondary_classifier" &&
              metadata.get("candidate_status") == "candidate" && requestedParents.contains(structureId))))
        {
            candidateParents << object
            String parentType = metadata.get("pipeline_stage") == "primary_classifier" ?
                "primary" : "secondary"
            String parentTypeId = parentType == "primary" ? primaryId : structureId
            Geometry parentGeometry = object.getROI()?.getGeometry()
            if (parentGeometry == null || parentGeometry.isEmpty() || !parentGeometry.isValid())
                throw new IllegalStateException("Eligible training parent geometry is invalid")
            Geometry normalizedParent = parentGeometry.copy();
            normalizedParent.normalize()
            String parentGeometrySha256 = sha256Bytes(
                new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedParent)
            )
            parentEvidence[object] = [
                material_parent_type: parentType,
                material_parent_type_id: parentTypeId,
                material_parent_revision_sha256: canonicalSha256([
                    analysis_domain_id: domainId,
                    analysis_domain_revision_sha256: domainRevisionSha256,
                    analysis_domain_set_sha256: domainSetSha256,
                    classification: object.getClassification(),
                    geometry_sha256: parentGeometrySha256,
                    parent_path_object_id: object.getParent().getID().toString(),
                    path_object_id: object.getID().toString(),
                    pipeline_active: metadata.get("pipeline_active"),
                    type: parentType,
                    type_id: parentTypeId,
                ]),
            ]
            if (metadata.get("object_revision_sha256") !=
                    parentEvidence[object].material_parent_revision_sha256)
                throw new IllegalStateException("Eligible training parent live revision is stale")
        }
}
candidateParents = candidateParents.unique { it.getID().toString() }

List<Map> snapshots = []
Map<String, Integer> observedAnnotationClassCounts = new TreeMap<>()
getAnnotationObjects().sort { it.getID().toString() }.each { annotation ->
    if (candidateParents.contains(annotation)) return
    Map annotationMetadata = md(annotation)
    if (annotationMetadata.get("pipeline_owner") != null ||
            annotationMetadata.get("pipeline_stage") in [
                "tissue_detection", "analysis_domain_selection",
                "primary_classifier", "secondary_classifier"
            ]) return
    String pathClassId = annotation.getClassification()
    String observedClass = pathClassId ?: "(unclassified)"
    observedAnnotationClassCounts[observedClass] =
        (observedAnnotationClassCounts[observedClass] ?: 0) + 1
    if (!classByPathClass.containsKey(pathClassId)) return
    Geometry geometry = annotation.getROI()?.getGeometry()
    if (geometry == null || geometry.isEmpty()) return
    if (!geometry.isValid() || !geometry.getCoordinates().every { coordinate ->
            Double.isFinite(coordinate.x) && Double.isFinite(coordinate.y)
        })
        throw new IllegalStateException("Training annotation geometry is invalid or outside its domain")
    if (!geometry.coveredBy(domainGeometry)) return
    PathObject owningDomain = domainSelector.smallestContaining(geometry)
    if (owningDomain == null || owningDomain.getID().toString() != domainPathObjectId)
        return
    PathObject materialParent = annotation.getParent()
    if (materialParent == null || !candidateParents.contains(materialParent) ||
            !geometry.coveredBy(materialParent.getROI().getGeometry()))
        throw new IllegalStateException(
            "Training annotation actual hierarchy parent is not an exact eligible material parent"
        )
    Map exactParentEvidence = parentEvidence[materialParent] as Map
    Geometry normalized = geometry.copy()
    normalized.normalize()
    byte[] normalizedWkb = new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
    List<PathObject> descendants = []
    Closure collectDescendants
    collectDescendants = { PathObject parent ->
        parent.getChildObjects().each { child ->
            descendants << child
            collectDescendants(child as PathObject)
        }
    }
    collectDescendants(annotation)
    List<PathObject> selectedCells = descendants.findAll { it.isCell() }
    List<Map> cellEvidence = selectedCells.collect { cell ->
        Map cellMetadata = md(cell)
        String stableCellId = nonblank(cellMetadata.get("cell_id"), "selected cell stable ID")
        Geometry cellGeometry = cell.getROI()?.getGeometry()
        if (cellMetadata.get("pipeline_stage") != "segmentation" ||
                !(cellMetadata.get("analysis_domain_id") in [null, domainId]) ||
                cellGeometry == null || cellGeometry.isEmpty() || !cellGeometry.isValid() ||
                !cellGeometry.coveredBy(geometry) || cell.getParent() == null)
            throw new IllegalStateException("Selected training cell is stale or outside its hierarchy annotation")
        if (!(cellMetadata.get("generation_id") instanceof String) ||
                !(cellMetadata.get("generation_id") as String).trim() ||
                !(cellMetadata.get("measurement_generation_id") instanceof String) ||
                !(cellMetadata.get("measurement_generation_id") as String).trim())
            throw new IllegalStateException(
                "Selected training cells require exact segmentation and measurement generations"
            )
        Geometry normalizedCell = cellGeometry.copy();
            normalizedCell.normalize()
        String cellGeometrySha256 = sha256Bytes(
            new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedCell)
        )
        Map cellPayload = [
            stable_cell_id: stableCellId,
            path_object_id: cell.getID().toString(),
            path_class_id: cell.getClassification(),
            normalized_roi_sha256: cellGeometrySha256,
            parent_path_object_id: cell.getParent().getID().toString(),
            segmentation_generation_id: cellMetadata.get("generation_id"),
            segmentation_population_id: cellMetadata.get("segmentation_population_id"),
            segmentation_config_sha256: cellMetadata.get("segmentation_config_sha256"),
            segmentation_domain_sha256: cellMetadata.get("segmentation_domain_sha256"),
            measurement_generation_id: cellMetadata.get("measurement_generation_id"),
        ]
        [stable_cell_id: stableCellId, evidence_sha256: canonicalSha256(cellPayload)]
    }.sort { it.stable_cell_id }
    List<String> cellIds = cellEvidence.collect { it.stable_cell_id as String }
    List<String> cellEvidenceSha256s = cellEvidence.collect { it.evidence_sha256 as String }
    if (cellIds.toSet().size() != cellIds.size() ||
            cellEvidenceSha256s.toSet().size() != cellEvidenceSha256s.size())
        throw new IllegalStateException("Selected training cells require unique stable and live identities")
    Set segmentationGenerations = selectedCells.collect {
        md(it).get("generation_id")
    }.findAll { it != null }.toSet()
    Set measurementGenerations = selectedCells.collect {
        md(it).get("measurement_generation_id")
    }.findAll { it != null }.toSet()
    if (segmentationGenerations.size() > 1 || measurementGenerations.size() > 1)
        throw new IllegalStateException("Selected training cells cross segmentation or measurement generations")
    String segmentationGenerationId = segmentationGenerations ? segmentationGenerations.first() as String : null
    String measurementGenerationId = measurementGenerations ? measurementGenerations.first() as String : null
    String cellSelectionRule = "explicit-hierarchy-descendant-cells/v1"
    Map selectionPayload = [
        cell_selection_rule: cellSelectionRule,
        cell_ids: cellIds,
        cell_evidence_sha256s: cellEvidenceSha256s,
        segmentation_generation_id: segmentationGenerationId,
        measurement_generation_id: measurementGenerationId,
    ]
    Map payload = [
        schema_version: SNAPSHOT_SCHEMA,
        project_entry_id: requestedEntryId,
        annotation_uuid: annotation.getID().toString(),
        class_id: classByPathClass[pathClassId],
        path_class_id: pathClassId,
        normalized_roi_sha256: sha256Bytes(normalizedWkb),
        material_parent_id: materialParent.getID().toString(),
        material_parent_type: exactParentEvidence.material_parent_type,
        material_parent_type_id: exactParentEvidence.material_parent_type_id,
        material_parent_revision_sha256: exactParentEvidence.material_parent_revision_sha256,
        split: split,
        analysis_domain_id: domainId,
        analysis_domain_revision_sha256: domainRevisionSha256,
        domain_set_sha256: domainSetSha256,
        cell_ids: cellIds,
        cell_evidence_sha256s: cellEvidenceSha256s,
        segmentation_generation_id: segmentationGenerationId,
        measurement_generation_id: measurementGenerationId,
        cell_selection_rule: cellSelectionRule,
        cell_selection_sha256: canonicalSha256(selectionPayload),
    ]
    snapshots << payload + [evidence_sha256: canonicalSha256(payload)]
}

List expectedEvidenceSha256s = request.expected_evidence_sha256s instanceof List ?
    request.expected_evidence_sha256s as List : null
if (expectedEvidenceSha256s == null || expectedEvidenceSha256s.any {
        !(it instanceof String) || !((it as String) ==~ /[0-9a-f]{64}/)
    } || expectedEvidenceSha256s != expectedEvidenceSha256s.sort(false) ||
        expectedEvidenceSha256s.toSet().size() != expectedEvidenceSha256s.size())
    throw new IllegalArgumentException(
        "Expected split evidence hashes must be an exact deterministic inventory"
    )
List actualEvidenceSha256s = snapshots.collect {
    it.evidence_sha256 as String
}.sort()
if (!expectedEvidenceSha256s.isEmpty() &&
        actualEvidenceSha256s != expectedEvidenceSha256s)
    throw new IllegalStateException(
        "Live split evidence differs from its immutable requested inventory"
    )

Map snapshotSet = [
    schema_version: "training-evidence-snapshot-set/v2",
    project_entry_id: requestedEntryId,
    analysis_domain_id: domainId,
    analysis_domain_revision: (request.analysis_domain_revision as Number).intValue(),
    analysis_domain_revision_sha256: domainRevisionSha256,
    domain_set_sha256: domainSetSha256,
    split: split,
    snapshots: snapshots,
    snapshot_count: snapshots.size(),
    snapshot_set_sha256: canonicalSha256(snapshots),
    live_geometry_authority: "qpdata",
    biological_sufficiency_claimed: false,
    observed_annotation_class_counts: observedAnnotationClassCounts,
]
planDomainResults << [
    training_plan_id: request.training_plan_id,
    classifier_role: request.classifier_role,
    snapshot_set: snapshotSet,
]
}
Map outputs = [
    schema_version: "pixel-training-evidence-entry/v1",
    project_entry_id: requestedEntryId,
    plan_domain_results: planDomainResults,
]
atomicJson(manifestPath, [
    schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: envelope.run_id, stage: envelope.stage, slide_id: slideId,
    generation_ids: [entryGenerationId], generations: [], object_outputs: outputs,
])
String manifestSha = sha256Bytes(Files.readAllBytes(manifestPath))
Map result = [
    status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: entryGenerationId, interaction_reason: null, affected_object_ids: [],
    message: null, allowed_actions: [], resume_context: null, checkpoint_id: null,
    outputs: outputs, error: null,
]
mifPublishProjectStageResult(resultPath, [
    schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
    run_id: envelope.run_id, stage: envelope.stage, slide_id: slideId,
    result: result,
    generation_manifest: [path: manifestPath.fileName.toString(), sha256: manifestSha],
])
