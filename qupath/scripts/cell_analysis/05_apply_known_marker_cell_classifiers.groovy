// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Import complete Python-owned marker-rule assignments and project one display view. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.MessageDigest
import java.util.UUID
import qupath.lib.io.GsonTools
import org.mif.cells.NativeCellScope
import org.mif.cells.PhenotypeComponents

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "cell_analysis/05_apply_known_marker_cell_classifiers.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "phenotyping")
    throw new IllegalArgumentException("Invalid phenotyping request envelope")
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
String slideId = envelope.slide_id as String
String runId = envelope.run_id as String
String sectionId = request.section_id as String
String generationId = request.generation_id as String
String phenotypeGenerationId = request.phenotype_generation_id as String
String selectedView = request.selected_display_view as String
if (!sectionId || !generationId || !phenotypeGenerationId)
    throw new IllegalArgumentException("Exact section, tissue generation and phenotype generation are required")
if (request.assignment_schema_version != "marker-rule-assignments/v2" || selectedView != "marker_rules")
    throw new IllegalArgumentException("Exact marker-rule assignment schema and selected display view are required")
if (!(request.materialize_display_classes instanceof Boolean))
    throw new IllegalArgumentException("materialize_display_classes must be explicit")
boolean materializeDisplayClasses = request.materialize_display_classes as Boolean

def md = { object -> object.getMetadata() }
List sections = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "tissue_detection" && m.get("generation_status") == "accepted" &&
        m.get("review_status") == "accepted" &&
        m.get("slide_id") == slideId && m.get("section_id") == sectionId &&
        m.get("generation_id") == generationId
}
if (sections.size() != 1) throw new IllegalStateException("Exactly one accepted current TissueSection is required")
// i) Resolve the accepted view metadata; cell values remain in its task or component.
def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    Files.newInputStream(path).withCloseable { input ->
        byte[] buffer = new byte[1024 * 1024]; int count
        while ((count = input.read(buffer)) >= 0) if (count > 0) digest.update(buffer, 0, count)
    }
    digest.digest().encodeHex().toString()
}
Map viewReference = request.phenotype_view_artifact as Map
if (viewReference?.schema_version != "phenotype-view-reference/v1" || viewReference.mode != selectedView ||
    viewReference.slide_id != slideId || viewReference.tissue_generation_id != generationId ||
    viewReference.section_id != sectionId || viewReference.canonical_image_sha256 != requestedCanonicalSha256)
    throw new IllegalArgumentException("The phenotype view belongs to a different current section")
Path viewArtifactPath = Paths.get(viewReference.artifact_path as String)
if (!viewArtifactPath.isAbsolute() || sha256(viewArtifactPath) != viewReference.artifact_sha256)
    throw new IllegalArgumentException("The selected phenotype metadata changed")
Map artifact = gson.fromJson(Files.readString(viewArtifactPath), Map)
if (artifact.schema_version != "phenotype-view-artifact/v1" ||
    ["mode", "slide_id", "canonical_image_sha256", "tissue_generation_id", "phenotype_generation_id",
     "section_id", "source_run_id", "cell_count", "cell_ids_sha256", "cell_to_domain_membership_sha256",
     "assignments_sha256"].any {artifact[it] != viewReference[it]})
    throw new IllegalArgumentException("The phenotype metadata differs from its accepted selection")
if (viewReference.phenotype_generation_id != phenotypeGenerationId)
    throw new IllegalArgumentException("Marker assignments target a different phenotype generation")
Set<String> expectedFields = [
    "cell_id", "analysis_domain_id", "primary_phenotype", "phenotype_source",
    "phenotype_components", "marker_states", "phenotype_confidence", "cluster_id", "cluster_label"
] as Set<String>
Map<String, Map> rowsById = PhenotypeComponents.readAssignments(
    request.phenotype_assignment_source as Map, viewReference.assignments_sha256 as String)
if (rowsById.size() != (viewReference.cell_count as Number).longValue())
    throw new IllegalArgumentException("Marker assignments differ from the prepared cell count")
def provider = NativeCellScope.open(boundImageData, sectionId).join()
if (request.segmentation_population_id != null && request.segmentation_population_id != provider.population)
    throw new IllegalStateException("Marker assignments target a stale segmentation population")
Set<String> markerInventory = null
rowsById.each { cellId, raw ->
    if (!(raw instanceof Map) || ((raw as Map).keySet() as Set) != expectedFields)
        throw new IllegalArgumentException("Each phenotype assignment must have the exact typed row shape")
    Map row = raw as Map
    if (row.phenotype_confidence != null || row.cluster_id != null || row.cluster_label != null)
        throw new IllegalArgumentException("Marker-rule assignments cannot carry classifier or cluster results")
    if (!(row.primary_phenotype instanceof String) || !(row.primary_phenotype as String).trim() ||
        row.phenotype_source != "marker_rules")
        throw new IllegalArgumentException("Phenotype assignment ${cellId} lacks a typed marker-rule phenotype")
    if (!(row.marker_states instanceof Map) || (row.marker_states as Map).isEmpty() ||
        (row.marker_states as Map).any { marker, state ->
            !(marker instanceof String) || !(marker as String).trim() || !(state in ["positive", "negative", "unknown"])
        })
        throw new IllegalArgumentException("Phenotype assignment ${cellId} lacks complete marker states")
    Map states = row.marker_states as Map
    Set<String> rowInventory = states.keySet() as Set<String>
    if (markerInventory == null) markerInventory = new TreeSet(rowInventory)
    else if (rowInventory != markerInventory)
        throw new IllegalArgumentException("Every phenotype assignment must use one exact marker inventory")
    if (!(row.phenotype_components instanceof List))
        throw new IllegalArgumentException("Phenotype assignment ${cellId} components must be a typed list")
    List components = row.phenotype_components as List
    if (components.any { !(it instanceof String) || !(it as String).trim() } ||
        components.toSet().size() != components.size())
        throw new IllegalArgumentException("Phenotype assignment ${cellId} components must be unique nonblank strings")
    boolean outsidePopulation = row.primary_phenotype == "Not phenotyped"
    if (outsidePopulation && states.values().any { it != "unknown" })
        throw new IllegalArgumentException("Cells outside the phenotyping population require unknown marker states")
    List expectedComponents = outsidePopulation ? [] : new TreeMap(states).collect { marker, state ->
        "${marker}${state == "unknown" ? "?" : state == "positive" ? "+" : "-"}"
    }
    if (components != expectedComponents)
        throw new IllegalArgumentException("Phenotype assignment ${cellId} components do not match marker states")
}
def atomicBytes = { Path path, byte[] content ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    try {
        Files.write(temp, content, StandardOpenOption.CREATE_NEW)
        try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
        catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
    } finally {
        Files.deleteIfExists(temp)
    }
}
def atomicJson = { Path path, Object value ->
    atomicBytes(path, (wireGson.toJson(value) + "\n").getBytes(StandardCharsets.UTF_8))
}
def restoreOutput = { Path path, byte[] original ->
    if (original == null) Files.deleteIfExists(path)
    else atomicBytes(path, original)
}
Map outputs = [cell_count: rowsById.size(), section_id: sectionId,
    cell_ids_sha256: viewReference.cell_ids_sha256,
    cell_to_domain_membership_sha256: viewReference.cell_to_domain_membership_sha256,
    selected_display_view: selectedView, phenotype_generation_id: phenotypeGenerationId,
    phenotype_view_artifact: viewReference, materialized_display_classes: materializeDisplayClasses,
    coordinate_system: "global_level0"]
Map manifest = [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
    generations: [], object_outputs: outputs]
Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
    allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
byte[] originalManifest = Files.exists(manifestPath) ? Files.readAllBytes(manifestPath) : null
byte[] originalResult = Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null
Object originalComponents = boundImageData.getProperty("mif.cell_components")
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
    // ii) Save one narrow result component and select it for the current section.
    Path component = PhenotypeComponents.publish(boundImageData, sectionId,
        viewArtifactPath.parent.resolve("cell-results").resolve(UUID.randomUUID().toString()),
        viewReference, runId, rowsById)
    NativeCellScope.selectPhenotypeComponent(boundImageData, component.toString(), materializeDisplayClasses)
    outputs.component_descriptor = [path: component.toString(), sha256: sha256(component)]
    fireHierarchyUpdate()
    persistForwardCurrentEntry()
    atomicJson(manifestPath, manifest)
    mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
        generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]])
} catch (Exception error) {
    try { restoreOutput(resultPath, originalResult) }
    catch (Exception resultRestoreError) { error.addSuppressed(resultRestoreError) }
    try { restoreOutput(manifestPath, originalManifest) }
    catch (Exception manifestRestoreError) { error.addSuppressed(manifestRestoreError) }
    try {
        if (originalComponents == null) boundImageData.removeProperty("mif.cell_components")
        else boundImageData.setProperty("mif.cell_components", originalComponents)
        NativeCellScope.close(boundImageData)
    } catch (Exception restoreError) { error.addSuppressed(restoreError) }
    try { persistRollbackCurrentEntry() }
    catch (Exception hierarchyRestoreError) { error.addSuppressed(hierarchyRestoreError) }
    try {
        fireHierarchyUpdate()
    } catch (Exception rollbackEventError) {
        error.addSuppressed(rollbackEventError)
    }
    throw error
}
