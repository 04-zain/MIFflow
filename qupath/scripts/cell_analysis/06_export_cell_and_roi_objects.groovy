// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Retain our current ROI geometry and canonical cell dataset selections. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.DigestOutputStream
import java.security.MessageDigest
import java.util.Collections
import java.util.IdentityHashMap
import java.util.UUID
import java.util.function.Consumer
import java.util.function.BiConsumer
import org.mif.cells.NativeCellScope
import org.mif.cells.SortedCellIds
import org.mif.roi.RoiInventory
import org.locationtech.jts.geom.MultiPolygon
import org.locationtech.jts.geom.Polygon
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "cell_analysis/06_export_cell_and_roi_objects.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls()
    .setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).setPrettyPrinting().create()
Map envelope = wireGson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "exports")
    throw new IllegalArgumentException("Invalid exports request envelope")
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
String generationId = request.generation_id as String
if (!generationId) throw new IllegalArgumentException("Exact accepted generation is required")
String requestedSectionId = request.section_id as String
String requestedDomainId = request.analysis_domain_id as String
List selectedMeasurementNames = (request.selected_measurement_names ?: []) as List
if (selectedMeasurementNames.any { !(it instanceof String) || !(it as String) } ||
    selectedMeasurementNames.toSet().size() != selectedMeasurementNames.size())
    throw new IllegalArgumentException("selected_measurement_names must contain unique exact names")
def md = { object -> object.getMetadata() }
def registrationQc = { object ->
    String raw = md(object).get("registration_qc") as String
    if (!raw) return null
    def parsed = gson.fromJson(raw, Object)
    if (!(parsed instanceof Map)) throw new IllegalStateException("registration_qc metadata must be JSON mapping")
    parsed
}
List sections
List rois
Map expectedPopulationIds = request.segmentation_population_ids instanceof Map ? request.segmentation_population_ids as Map : [:]
sections = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "tissue_detection" &&
        m.get("generation_status") == "accepted" &&
        m.get("review_status") == "accepted" &&
        m.get("slide_id") == slideId &&
        m.get("generation_id") == generationId &&
        (requestedSectionId ? m.get("section_id") == requestedSectionId :
            expectedPopulationIds.containsKey(m.get("section_id")))
}.sort { md(it).get("section_id") as String }
if (sections.isEmpty())
    throw new IllegalStateException(
        "Accepted authoritative TissueSections are required"
    )
Set<String> projectedSectionIds = sections.collect {
    md(it).get("section_id") as String
} as Set<String>
List excludedSectionList = request.excluded_section_ids == null ? [] : request.excluded_section_ids as List
Set<String> excludedSectionIds = excludedSectionList as Set<String>
if (excludedSectionIds.size() != excludedSectionList.size() ||
        excludedSectionIds.any { !(it instanceof String) || !projectedSectionIds.contains(it) })
    throw new IllegalStateException("Excluded section selection differs from accepted tissue")
rois = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") in ["primary_classifier", "secondary_classifier"] &&
        m.get("pipeline_active") == "true" && m.get("review_status") != "superseded" &&
        m.get("pipeline_run_id") == runId &&
        m.get("generation_id") == generationId &&
        m.get("slide_id") == slideId &&
        projectedSectionIds.contains(m.get("section_id")) && m.get("roi_id") &&
        (!requestedDomainId || m.get("analysis_domain_id") == requestedDomainId)
}.sort { md(it).get("roi_id") as String }
List<String> sectionIdsList = sections.collect { md(it).get("section_id") as String }
List<String> tissueRoiIds = sections.collect { md(it).get("roi_id") as String }
if (sectionIdsList.any { !it } ||
        sectionIdsList.toSet().size() != sectionIdsList.size() ||
        tissueRoiIds.any { !it } ||
        tissueRoiIds.toSet().size() != tissueRoiIds.size())
    throw new IllegalStateException(
        "Authoritative TissueSections require unique stable IDs"
    )
Set<String> sectionIds = sectionIdsList as Set
List<String> roiIds = rois.collect { md(it).get("roi_id") as String }
if (roiIds.toSet().size() != roiIds.size())
    throw new IllegalStateException("Stable current ROI IDs must be unique")
Map providers = [:]
sectionIds.each { currentSectionId ->
    def provider = NativeCellScope.open(boundImageData, currentSectionId as String).join()
    if (provider.getSectionId() != currentSectionId) throw new IllegalStateException("Selected cell base has a different section")
    providers[currentSectionId] = provider
    if (expectedPopulationIds.containsKey(currentSectionId) && expectedPopulationIds[currentSectionId] != provider.getPopulation())
        throw new IllegalStateException("Export cells do not match the checkpointed segmentation population")
}
Map<String, Object> roiById = rois.collectEntries { [(md(it).get("roi_id") as String): it] }
// Excluded sections have no semantic snapshot; authenticate their retained
// primary tables once against the live objects before publishing audit geometry.
List excludedPrimarySources = request.excluded_primary_sources == null ? [] : request.excluded_primary_sources as List
Set<String> excludedSourceDomains = new HashSet<>()
Map excludedLiveByDomain = [:]
rois.each { object ->
    Map metadata = md(object)
    if (excludedSectionIds.contains(metadata.get("section_id")))
        excludedLiveByDomain.computeIfAbsent(metadata.get("analysis_domain_id")) { [:] }[metadata.get("roi_id")] = object
}
excludedPrimarySources.each { rawSource ->
    if (!(rawSource instanceof Map) || rawSource.keySet() !=
            (["analysis_domain_id", "section_id", "producer_attempt_id", "roi_inventory"] as Set))
        throw new IllegalStateException("Excluded primary source schema differs")
    Map source = rawSource as Map
    String domainId = source.analysis_domain_id as String
    if (!domainId || !excludedSourceDomains.add(domainId) || !excludedSectionIds.contains(source.section_id) ||
            !(source.producer_attempt_id instanceof String) || !(source.roi_inventory instanceof Map))
        throw new IllegalStateException("Excluded primary source scope differs")
    Map live = excludedLiveByDomain[domainId] ?: [:]
    Set<String> observed = new HashSet<>()
    RoiInventory.read(MifProjectStageBootstrapV1.producerOutputRoot(request, config, source.producer_attempt_id as String), source.roi_inventory as Map,
            RoiInventory.member(source.producer_attempt_id as String), { RoiInventory.Row row ->
        def object = live[row.roiId()]
        Map metadata = object == null ? [:] : md(object)
        if (object == null || object.getID().toString() != row.pathObjectId() ||
                metadata.get("object_revision_sha256") != row.objectRevisionSha256() ||
                metadata.get("primary_class_id") != row.classId() ||
                metadata.get("pipeline_stage") != "primary_classifier" ||
                metadata.get("section_id") != source.section_id)
            throw new IllegalStateException("Excluded primary ROI changed after classifier acceptance")
        observed.add(row.roiId())
    } as Consumer<RoiInventory.Row>)
    if (observed != live.keySet())
        throw new IllegalStateException("Excluded primary table differs from complete live ROI set")
}
if (rois.any { excludedSectionIds.contains(md(it).get("section_id")) &&
        !excludedSourceDomains.contains(md(it).get("analysis_domain_id")) })
    throw new IllegalStateException("Excluded section contains an unauthenticated ROI domain")
rois.each { object ->
    Map m = md(object)
    String parentRoiId = m.get("parent_roi_id") as String
    String analysisDomainId = m.get("analysis_domain_id") as String
    boolean isAnalysisDomainRoot = parentRoiId == analysisDomainId
    if (!parentRoiId || parentRoiId == m.get("roi_id") ||
        (!isAnalysisDomainRoot && !roiById.containsKey(parentRoiId)))
        throw new IllegalStateException("ROI parent_roi_id is missing, self-referential, or stale")
    if (m.get("pipeline_stage") == "primary_classifier" && !isAnalysisDomainRoot)
        throw new IllegalStateException("Primary ROI must be parented directly by its analysis domain")
    if (!isAnalysisDomainRoot) {
        Map parentMetadata = md(roiById[parentRoiId])
        if (parentMetadata.get("slide_id") != slideId ||
            parentMetadata.get("section_id") != m.get("section_id") ||
            parentMetadata.get("generation_id") != generationId ||
            parentMetadata.get("pipeline_run_id") != runId ||
            parentMetadata.get("analysis_domain_id") != analysisDomainId ||
            parentMetadata.get("analysis_domain_kind") != m.get("analysis_domain_kind"))
            throw new IllegalStateException("ROI parent_roi_id crosses domain, slide, section, generation, or run")
    }
    if (m.get("source") == "registered_reference_assisted" &&
        (!(m.get("registration_id") as String) || registrationQc(object)?.status != "passed"))
        throw new IllegalStateException("Registered-reference-assisted ROI lacks passed registration provenance")
}
def ring = { sequence -> sequence.toCoordinateArray().collect { coordinate -> [coordinate.x, coordinate.y] } }
def polygonCoordinates = { Polygon polygon ->
    List rings = [ring(polygon.exteriorRing.coordinateSequence)]
    (0..<polygon.numInteriorRing).each { index ->
        rings << ring(polygon.getInteriorRingN(index).coordinateSequence)
    }
    rings
}
def geometryJson = { geometry ->
    if (geometry.isEmpty() || !geometry.isValid() || !(geometry.area > 0))
        throw new IllegalStateException("Exported ROI requires valid, positive-area polygon geometry")
    if (geometry instanceof Polygon) return [type: "Polygon", coordinates: polygonCoordinates(geometry)]
    if (geometry instanceof MultiPolygon) return [type: "MultiPolygon", coordinates:
        (0..<geometry.numGeometries).collect { index -> polygonCoordinates(geometry.getGeometryN(index) as Polygon) }]
    throw new IllegalStateException("Unsupported polygon geometry ${geometry.geometryType}")
}
def normalizedGeometryDigest = { geometry ->
    def copy = geometry.copy();
    copy.normalize()
    MessageDigest.getInstance("SHA-256").digest(new WKBWriter(2, true).write(copy)).encodeHex().toString()
}
def roiRecord = { object ->
    Map m = md(object)
    Map regionDecision = m.get("semantic_region_decision") ?
        gson.fromJson(m.get("semantic_region_decision") as String, Map) : null
    double confidence = object.getClassProbability()
    String analysisDomainKind = m.get("analysis_domain_kind") as String
    if (!(analysisDomainKind in ["selected_roi", "whole_tissue_section"]))
        throw new IllegalStateException("ROI lacks authenticated analysis-domain kind")
    [roi_id: m.get("roi_id"), slide_id: slideId, section_id: m.get("section_id"),
     analysis_domain_id: m.get("analysis_domain_id"), analysis_domain_kind: analysisDomainKind,
     structure_id: m.get("structure_id"),
     path_object_id: object.getID().toString(), geometry_digest: normalizedGeometryDigest(object.getROI().getGeometry()),
     "class": object.getClassification(), roi_level: m.get("roi_level"),
     primary_class_id: m.get("primary_class_id"),
     parent_roi_id: m.get("parent_roi_id"), geometry: geometryJson(object.getROI().getGeometry()),
     source: m.get("source") ?: "classifier", classifier_id: m.get("classifier_id"),
     classifier_version: m.get("classifier_version"), classifier_confidence: Double.isFinite(confidence) ? confidence : null,
     semantic_profile: m.get("semantic_profile"), semantic_status: m.get("semantic_status") ?: "candidate",
     semantic_rule_results: m.get("semantic_rule_results") ? gson.fromJson(m.get("semantic_rule_results") as String, Object) : null,
     review_status: m.get("review_status"),
     semantic_classification_status: m.get("semantic_classification_status") ?: null,
     semantic_class_id: m.get("semantic_class_id") ?: null,
     review_rationale: regionDecision?.rationale,
     original_predicted_geometry: m.get("original_predicted_geometry") ?
        gson.fromJson(m.get("original_predicted_geometry") as String, Object) : null,
     generation_id: generationId, pipeline_stage: m.get("pipeline_stage"), pipeline_run_id: runId,
     classifier_status: m.get("classifier_status"), candidate_status: m.get("candidate_status"),
     registration_id: m.get("registration_id"), registration_qc: registrationQc(object),
     moving_assistance_decision_id: m.get("moving_assistance_decision_id"),
     moving_assistance_decision_source: m.get("moving_assistance_decision_source"),
     moving_assistance_decided_by: m.get("moving_assistance_decided_by")]
}
/** Reuse our admitted typed phenotype identities without reopening descriptors.
 * i) The native owner has already admitted the exact completed result metadata.
 * ii) We project one compact descriptor/SHA for each stored view and section;
 * recovery and the selected scientific view reuse the same admitted identities.
 */
Map phenotypeComponents = [:]
providers.values().each { provider ->
    provider.getSelectedComponentMetadata().each { Map descriptor ->
        Map view = descriptor.phenotype_view as Map
        if (view == null) return
        if (!(view.mode instanceof String) || !(view.phenotype_generation_id instanceof String) ||
                descriptor.scope_key != provider.getSectionId())
            throw new IllegalStateException("Saved phenotype identity differs from its selected section")
        Map generations = phenotypeComponents.computeIfAbsent(view.mode) { [:] }
        Map sectionReferences = generations.computeIfAbsent(view.phenotype_generation_id) { [:] }
        Map reference = [path:descriptor.path,sha256:descriptor.sha256]
        String section = descriptor.scope_key
        if (sectionReferences[section] != null && sectionReferences[section] != reference)
            throw new IllegalStateException("A saved phenotype view selects conflicting section components")
        sectionReferences[section] = reference
    }
}

String exportScope = requestedSectionId ?: "all-sections"
if (requestedDomainId) exportScope += "-${requestedDomainId}"
Path exportDirectory = MifProjectStageBootstrapV1.outputRoot(request, config).resolve("exports")
    .resolve(slideId).resolve(exportScope).toAbsolutePath().normalize()
Files.createDirectories(exportDirectory)
Path exportPath = exportDirectory.resolve(".pending-${UUID.randomUUID()}.json")
Path tempExport = exportPath.resolveSibling(".${exportPath.fileName}.${UUID.randomUUID()}.tmp")

/** We select our immutable bases and scalar components without copying cells.
 * i) Each reference belongs to the saved section population and current shape revision.
 * ii) Only sparse edits are published; clean geometry stays in its original base.
 * iii) The identity pass projects ID/domain columns without native geometry hydration.
 */
Map basePaths = gson.fromJson(boundImageData.getProperty("mif.cell_datasets") as String, Map) as Map
List cellDatasets = []
SortedCellIds sortedCellIds = new SortedCellIds(Paths.get(System.getProperty("java.io.tmpdir")))
MessageDigest cellIdDigest = MessageDigest.getInstance("SHA-256")
MessageDigest cellDomainDigest = MessageDigest.getInstance("SHA-256")
long[] exportedCellCount = new long[1]
Map domainSummaries = [:]
try {
    sectionIds.toList().sort().each { String id ->
        Path basePath = Paths.get(basePaths[id] as String).toAbsolutePath().normalize()
        def provider = providers[id]
        boolean excludedSection = excludedSectionIds.contains(id)
        if (excludedSection && provider.getRowCount() != 0)
            throw new IllegalStateException("Excluded section contains active cells")
        Map components = excludedSection ? [:] : provider.getSelectedComponentReferences()
        Map selectedEdits = null
        if (excludedSection && (provider.getEditCount() != 0 || provider.getGeometryRevision() != 0))
            throw new IllegalStateException("Excluded section has unexpected cell edits")
        if (provider.getEditCount() > 0) {
            Path edits = provider.publishEdits(exportDirectory.resolve("cell-edits-${UUID.randomUUID()}"))
            selectedEdits = [path:edits.toString(),sha256:MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(edits)).encodeHex().toString()]
        }
        Map selectedView = null
        if (!excludedSection && (request.selected_phenotype_view != null || request.phenotype_generation_id != null)) {
            if (!(request.selected_phenotype_view instanceof String) || !(request.phenotype_generation_id instanceof String) ||
                    phenotypeComponents[request.selected_phenotype_view]?.get(request.phenotype_generation_id)?.get(id) == null)
                throw new IllegalStateException("Requested phenotype component is absent from this saved section")
            selectedView = [mode:request.selected_phenotype_view,phenotype_generation_id:request.phenotype_generation_id]
        }
        cellDatasets << [section_id:id,population_id:provider.getPopulation(),
            cell_dataset:[path:basePath.toString(),sha256:provider.getDescriptionSha256()],
            components:components,geometry_revision:provider.getGeometryRevision(),cell_edits:selectedEdits,phenotype_view:selectedView]
        provider.getDomainIds().each { domain ->
            if (!requestedDomainId || domain == requestedDomainId)
                domainSummaries.computeIfAbsent(domain) { [cell_count:0L,digest:MessageDigest.getInstance("SHA-256")] }
        }
        provider.forEachIdentity({ String cellId, String domain ->
            if (!requestedDomainId || domain == requestedDomainId) sortedCellIds.addIdentity(cellId, domain)
        } as BiConsumer)
    }
    cellIdDigest.update("[".getBytes(StandardCharsets.UTF_8))
    cellDomainDigest.update("{".getBytes(StandardCharsets.UTF_8))
    sortedCellIds.forEachIdentity({ String id, String domain ->
        if (exportedCellCount[0] > 0L) {
            cellIdDigest.update(",".getBytes(StandardCharsets.UTF_8));cellDomainDigest.update(",".getBytes(StandardCharsets.UTF_8))
        }
        byte[] encodedId = SortedCellIds.canonicalId(id).getBytes(StandardCharsets.UTF_8)
        cellIdDigest.update(encodedId)
        cellDomainDigest.update(encodedId);cellDomainDigest.update(":".getBytes(StandardCharsets.UTF_8))
        cellDomainDigest.update(SortedCellIds.canonicalId(domain).getBytes(StandardCharsets.UTF_8))
        Map summary = domainSummaries.computeIfAbsent(domain) { [cell_count:0L,digest:MessageDigest.getInstance("SHA-256")] }
        if (summary.cell_count == 0L) summary.digest.update("[".getBytes(StandardCharsets.UTF_8))
        else summary.digest.update(",".getBytes(StandardCharsets.UTF_8))
        summary.digest.update(encodedId);summary.cell_count++
        exportedCellCount[0]++
    } as BiConsumer)
    cellIdDigest.update("]".getBytes(StandardCharsets.UTF_8));cellDomainDigest.update("}".getBytes(StandardCharsets.UTF_8))
} finally { sortedCellIds.close() }
String cellIdsSha256 = cellIdDigest.digest().encodeHex().toString()
String cellDomainSha256 = cellDomainDigest.digest().encodeHex().toString()
Map domainInventories = new TreeMap()
domainSummaries.each { domain, summary ->
    if (summary.cell_count == 0L) summary.digest.update("[".getBytes(StandardCharsets.UTF_8))
    summary.digest.update("]".getBytes(StandardCharsets.UTF_8))
    domainInventories[domain] = [cell_count:summary.cell_count,cell_ids_sha256:summary.digest.digest().encodeHex().toString()]
}
Map cellScope = requestedDomainId ? [active_analysis_domain_ids:[requestedDomainId]] : [:]
MessageDigest exportDigest = MessageDigest.getInstance("SHA-256")
try {
    new DigestOutputStream(Files.newOutputStream(tempExport, StandardOpenOption.CREATE_NEW), exportDigest)
        .withCloseable { digestStream ->
            new BufferedWriter(new OutputStreamWriter(digestStream, StandardCharsets.UTF_8), 1024 * 1024)
                .withCloseable { writer ->
                    writer.write("{\"schema_version\":\"qupath-raw-objects/v1\",\"slide_id\":")
                    writer.write(gson.toJson(slideId));
            writer.write(",\"generation_id\":")
                    writer.write(gson.toJson(generationId));
                    writer.write(",\"pipeline_run_id\":");writer.write(gson.toJson(runId))
                    if (request.semantic_live_binding instanceof Map) {
                        writer.write(",\"semantic_live_binding\":")
                        writer.write(wireGson.toJson(request.semantic_live_binding))
                    }
                    writer.write(",\"phenotype_components\":")
                    writer.write(gson.toJson(phenotypeComponents))
                    writer.write(",\"cell_datasets\":");writer.write(wireGson.toJson(cellDatasets))
                    writer.write(",\"cell_scope\":");writer.write(gson.toJson(cellScope))
                    writer.write(",\"cell_summary\":")
                    writer.write(gson.toJson([row_count:exportedCellCount[0],cell_ids_sha256:cellIdsSha256,
                        domain_cell_mapping_sha256:cellDomainSha256,domain_inventories:domainInventories]))
            writer.write(",\"coordinate_system\":\"global_level0\",\"tissue_sections\":[")
                    sections.eachWithIndex { object, index ->
                        if (index) writer.write(",")
                        Map m = md(object)
                        writer.write(wireGson.toJson([slide_id: slideId, section_id: m.get("section_id"),
                            roi_id: m.get("roi_id"), generation_id: generationId,
                            revision: Integer.parseInt(m.get("revision") ?: "0"),
                            geometry: geometryJson(object.getROI().getGeometry())]))
                    }
                    writer.write("],\"rois\":[")
                    rois.eachWithIndex { object, index ->
                        if (index) writer.write(",")
                        writer.write(wireGson.toJson(roiRecord(object)))
                    }
                    writer.write("]}\n")
                }
        }
    try { Files.move(tempExport, exportPath, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(tempExport, exportPath, StandardCopyOption.REPLACE_EXISTING) }
} catch (Exception error) {
    Files.deleteIfExists(tempExport)
    throw error
}
String checksum = exportDigest.digest().encodeHex().toString()
Path immutableExportPath = exportDirectory.resolve("${checksum}.qupath-raw-objects.json")
if (Files.exists(immutableExportPath)) {
    boolean matches = Files.mismatch(immutableExportPath, exportPath) == -1L
    Files.deleteIfExists(exportPath)
    if (!matches) throw new IllegalStateException("Immutable raw export path collides with different bytes")
} else {
    try { Files.move(exportPath, immutableExportPath, StandardCopyOption.ATOMIC_MOVE) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(exportPath, immutableExportPath) }
}
exportPath = immutableExportPath

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
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
}
Map outputs = [path: exportPath.toString(), sha256: checksum, roi_count: rois.size(),
    cell_ids_sha256: cellIdsSha256,
    cell_count: exportedCellCount[0], tissue_section_count: sections.size(),
    selected_measurement_names: selectedMeasurementNames,
    coordinate_system: "global_level0"]
atomicJson(manifestPath, [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
    generations: [], object_outputs: outputs])
Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
    allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
    generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]])
