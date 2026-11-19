// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/**
 * Checks supervised references, applies a saved native object classifier and records its cell predictions.
 *
 * We first resolve the selected QuPath project entry, accepted tissue section,
 * measured cell population and ordered classifier features. When a saved model
 * is absent, we retain the existing reference annotation and classifier
 * selection interaction until its saved native model has been supplied.
 *
 * For an existing model, we validate saved native reference IDs and their
 * class, role and region evidence before inference in the same operation.
 * Batch application uses the accepted model and current expected cell set,
 * then returns predictions for confidence review. The accepted request supplies
 * prepared phenotype assignments for those same cell IDs.
 *
 * Previous phenotype results remain stored by method and result version. We
 * update the selected displayed phenotype only after matching the accepted
 * result to the current cells. Native inference retains bounded batches and
 * accepted typed components reuse the canonical geometry. Failure handling
 * restores selected component pointers and prior output files.
 *
 * Inputs:
 *     args[0]: Current request JSON for the selected image, cells and model.
 *     args[1]: Result JSON path read by the Python analysis workflow.
 *     args[2]: Output-description JSON path for the calculated cell results.
 *
 * Outputs:
 *     Paused request, native predictions awaiting review, or the completed
 *     accepted per-cell phenotype update and its recorded model/result IDs.
 */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.ArrayList
import java.util.UUID
import java.util.function.Consumer
import org.mif.cells.NativeCellScope
import org.mif.cells.CellComponents
import org.mif.cells.PhenotypeComponents
import qupath.lib.classifiers.object.ObjectClassifiers
import qupath.lib.io.GsonTools
import qupath.lib.objects.classes.PathClass

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "cell_analysis/05_train_supervised_phenotypes.groovy"
final String INTERACTION_CONTEXT_SCHEMA = "supervised-phenotyping-interaction/v1"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
def project = getProject()
def imageData = getCurrentImageData()
def entry = getProjectEntry()
if (project == null || imageData == null || entry == null)
    throw new IllegalStateException("Supervised phenotyping requires a real QuPath project and current image entry")

Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
/**
 * Calculates a saved model or result file's checksum with one-megabyte reads.
 *
 * Args:
 *     path: Complete file whose content is compared with the selected model/result.
 *
 * Returns:
 *     Lowercase SHA-256 covering every file byte.
 */
def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { input ->
        DigestInputStream stream = new DigestInputStream(input, digest)
        byte[] buffer = new byte[1024 * 1024]
        while (stream.read(buffer) >= 0) { }
    }
    digest.digest().encodeHex().toString()
}
/**
 * Writes output bytes through a new temporary file beside their destination.
 *
 * We create the parent folder and a uniquely named temporary file, write the
 * complete bytes, then replace the destination using an atomic move when
 * supported. A filesystem without that move option uses the regular replacement
 * move. The temporary name is removed when the operation exits.
 *
 * Args:
 *     path: Result or output-description destination.
 *     content: Complete bytes to save.
 */
def atomicBytes = { Path path, byte[] content ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    try {
        Files.write(temp, content, StandardOpenOption.CREATE_NEW)
        try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
        catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
    } finally { Files.deleteIfExists(temp) }
}
/**
 * Encodes one result as UTF-8 JSON and saves it through the temporary-file writer.
 *
 * Args:
 *     path: Destination for the saved result.
 *     value: Result fields, including explicit nulls where required by the format.
 */
def atomicJson = { Path path, Object value ->
    atomicBytes(path, (wireGson.toJson(value) + "\n").getBytes(StandardCharsets.UTF_8))
}
/**
 * Restores the output file captured before an unsuccessful operation.
 *
 * Args:
 *     path: Result or output-description filename to restore.
 *     original: Previous bytes, or null when the file did not exist before this operation.
 */
def restoreOutput = { Path path, byte[] original ->
    if (original == null) Files.deleteIfExists(path)
    else atomicBytes(path, original)
}
Closure canonicalJson
/**
 * Orders nested JSON dictionary keys while retaining list order.
 *
 * Args:
 *     value: Native prediction or accepted phenotype-result fields.
 *
 * Returns:
 *     Nested TreeMap/list values used by the result-content checksum.
 */
canonicalJson = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap()
        (value as Map).each { key, item -> sorted[key as String] = canonicalJson(item) }
        return sorted
    }
    if (value instanceof List) return (value as List).collect { canonicalJson(it) }
    value
}
/**
 * Calculates a checksum of consistently ordered native prediction/result JSON.
 *
 * Args:
 *     value: Complete prediction rows or associated result-description fields.
 *
 * Returns:
 *     SHA-256 of the Gson-encoded canonicalJson(value) bytes.
 */
def contentDigest = { Object value ->
    MessageDigest.getInstance("SHA-256")
        .digest(gson.toJson(canonicalJson(value)).getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
}
Set<String> assignmentRequired = ["cell_id", "analysis_domain_id", "primary_phenotype", "phenotype_source",
    "phenotype_components", "marker_states"] as Set<String>
Set<String> assignmentNullable = ["phenotype_confidence", "cluster_id", "cluster_label"] as Set<String>
/**
 * Reads the current phenotype-assignment fields and fills absent optional values with null.
 *
 * Required fields identify the cell, analysis region, phenotype source,
 * components and marker states. Confidence and cluster values are optional
 * and receive explicit nulls in the returned copy. Completed component
 * descriptors own the shared accepted result and lineage identities.
 *
 * Args:
 *     raw: Assignment mapping read from a prepared result or cell metadata.
 *
 * Returns:
 *     Copied LinkedHashMap containing the checked field set.
 *
 * Raises:
 *     IllegalArgumentException: Required fields are missing or extra fields
 *         are supplied outside the current assignment representation.
 */
def normalizeAssignment = { Object raw ->
    Set<String> allowed = assignmentRequired + assignmentNullable
    if (!(raw instanceof Map) || !assignmentRequired.every {
            (raw as Map).containsKey(it)
        } || !allowed.containsAll((raw as Map).keySet()))
        throw new IllegalArgumentException("Phenotype assignment has an invalid typed shape")
    Map normalized = new LinkedHashMap(raw as Map)
    assignmentNullable.each { field ->
        if (!normalized.containsKey(field)) normalized.put(field, null)
    }
    normalized
}
// =============================================================================
// i) Read the selected image and native request
// =============================================================================
// We keep the original project, image data and entry references for this
// operation. The request must identify that same mIF image, its prepared
// script set, accepted tissue version and current analysis run.

Map envelope = gson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "supervised_phenotyping")
    throw new IllegalArgumentException("Invalid supervised phenotyping request envelope")
Map request = envelope.request as Map
def boundProject = project
def boundImageData = imageData
def boundEntry = entry
boolean projectCheckpointPersistenceAttempted = false
boolean forwardPersistenceAttempted = false
/**
 * Saves project metadata when this operation runs as a prepared headless project step.
 *
 * We record that persistence was attempted before calling syncChanges(),
 * allowing the corresponding failure path to save restored project metadata
 * when needed. Interactive editing leaves saving to its existing desktop workflow.
 */
def persistProjectCheckpoint = {
    if (!mifAuthenticatedHeadlessProjectStage) return
    projectCheckpointPersistenceAttempted = true
    boundProject.syncChanges()
}
/**
 * Saves restored project metadata only when this operation already attempted a project write.
 *
 * The preceding failure handler restores the original declared class list
 * before invoking this callback.
 */
def persistRollbackProjectCheckpoint = {
    if (!projectCheckpointPersistenceAttempted) return
    boundProject.syncChanges()
}
/**
 * Saves the updated image objects and project for a prepared headless processing step.
 *
 * We record that persistence was attempted, save the bound image data through
 * its original project entry, then synchronize project metadata. Interactive
 * operations retain their existing desktop save sequence.
 */
def persistForwardCurrentEntry = {
    if (!mifAuthenticatedHeadlessProjectStage) return
    forwardPersistenceAttempted = true
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
/**
 * Saves restored image data when the unsuccessful operation had attempted persistence.
 *
 * The failure handler restores captured object metadata/classes before this
 * callback writes the image entry and synchronizes the project again.
 */
def persistRollbackCurrentEntry = {
    if (!forwardPersistenceAttempted) return
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
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
// Resolve native classifier support only from the authenticated script bundle.
Closure loadSupervisedAsset = {String relative ->
    List matches=authenticatedManifest.assets.findAll{it.path==relative}
    Path source=requestedBundleRoot.resolve(relative)
    if(matches.size()!=1 || source.toRealPath()!=source || Files.isSymbolicLink(source) ||
        !Files.isRegularFile(source) || Files.size(source)>1024*1024 || sha256(source)!=matches[0].sha256)
        throw new IllegalStateException("The supervised classifier support differs from the registered bundle")
    new GroovyShell(this.class.classLoader).evaluate(source.toFile()) as Map
}
Map nativeObjectSupport=loadSupervisedAsset("cell_analysis/supervised_classifier_support.groovy")
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
["slide_id", "run_id"].each { field ->
    if (!(envelope[field] instanceof String) || !(envelope[field] as String).trim())
        throw new IllegalArgumentException("${field} must be nonblank")
}
["section_id", "generation_id"].each { field ->
    if (!(request[field] instanceof String) || !(request[field] as String).trim())
        throw new IllegalArgumentException(
            field == "generation_id" ? "exact generation_id must be nonblank" : "${field} must be nonblank")
}
String slideId = envelope.slide_id as String
String runId = envelope.run_id as String
String sectionId = request.section_id as String
String generationId = request.generation_id as String
if (request.generation_ids != [generationId])
    throw new IllegalArgumentException("One exact section and generation are required")

/**
 * Reads the nonempty unique text list required for classes, cells or reference regions.
 *
 * Args:
 *     value: Supplied JSON list of IDs or names.
 *     field: List name included in a validation error.
 *
 * Returns:
 *     Ordered list of checked strings.
 *
 * Raises:
 *     IllegalArgumentException: The input is empty, not a list, has blank
 *         entries or repeats a value.
 */
def typedStrings = { value, String field ->
    if (!(value instanceof List) || (value.isEmpty() && field != "phenotyping_cell_ids") || value.any {
        !(it instanceof String) || !(it as String).trim()
    } || value.toSet().size() != value.size())
        throw new IllegalArgumentException("${field} must be unique nonblank strings")
    value.collect { it as String }
}
List<String> classIds = typedStrings(request.class_ids, "class_ids")
if (!(request.feature_schema instanceof List) || (request.feature_schema as List).isEmpty())
    throw new IllegalArgumentException("feature_schema must declare exact measurement keys and semantic roles")
List<Map> featureSchema = (request.feature_schema as List).collect { raw ->
    if (!(raw instanceof Map) || (raw as Map).keySet() as Set != ["measurement_name", "semantic_role"] as Set)
        throw new IllegalArgumentException("feature_schema entries require exact measurement_name and semantic_role")
    String measurement = raw.measurement_name as String
    String role = raw.semantic_role as String
    if (!measurement?.trim() || !role?.trim())
        throw new IllegalArgumentException("feature_schema measurement_name and semantic_role must be nonblank")
    [measurement_name: measurement, semantic_role: role]
}
List<String> featureIds = featureSchema.collect { it.measurement_name }
if (featureIds.toSet().size() != featureIds.size())
    throw new IllegalArgumentException("feature_schema measurement_name values must be unique")
if (!(request.current_validation_domain_id instanceof String) ||
    !(request.current_validation_domain_id as String).trim())
    throw new IllegalArgumentException("current_validation_domain_id must be nonblank")
String currentValidationDomainId = request.current_validation_domain_id as String
boolean batchDeployment = request.deployment_mode == "batch_deployment"
if (!batchDeployment && (!(request.point_selection_radius_level0 instanceof Number) ||
    !Double.isFinite((request.point_selection_radius_level0 as Number).doubleValue()) ||
    (request.point_selection_radius_level0 as Number).doubleValue() <= 0d))
    throw new IllegalArgumentException("A positive point_selection_radius_level0 is required")
double pointSelectionRadius = batchDeployment ? 1d :
    (request.point_selection_radius_level0 as Number).doubleValue()
if (!(request.materialize_display_classes instanceof Boolean))
    throw new IllegalArgumentException("materialize_display_classes must be explicit")
boolean materializeDisplayClasses = request.materialize_display_classes as Boolean
// =============================================================================
// ii) Retain the reference and model-selection interaction until a model is supplied
// =============================================================================
// We register the selected classes and return the existing paused interaction.
// Native reference training or explicit classifier import supplies the model
// identity before application. An error restores the class list and result file.

if (!(request.classifier instanceof Map)) {
    List originalDeclaredClasses = new ArrayList(project.getPathClasses())
    byte[] originalResult = Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null
    try {
        List declaredClasses = new ArrayList(originalDeclaredClasses)
        classIds.each { classId ->
            PathClass pathClass = PathClass.getInstance(classId)
            if (!declaredClasses.contains(pathClass)) declaredClasses.add(pathClass)
        }
        project.setPathClasses(declaredClasses)
        persistProjectCheckpoint()
        Map pause = [status: "needs_interaction", stage: envelope.stage, slide_id: slideId,
            generation_id: generationId, interaction_reason: "supervised_training_annotations_required",
            affected_object_ids: [], message: "Use point annotations and Set class for disjoint training/holdout regions, then select the saved classifier from native reference training or explicit import.",
            allowed_actions: ["annotate_supervised_training", "abort"],
            resume_context: [schema_version: INTERACTION_CONTEXT_SCHEMA,
                phase: "annotations_required", class_ids: classIds, feature_schema: featureSchema,
                current_validation_domain_id: currentValidationDomainId,
                point_selection_radius_level0: request.point_selection_radius_level0],
            checkpoint_id: "supervised-training-${generationId}".toString(), outputs: null, error: null]
        mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
            run_id: runId, stage: envelope.stage, slide_id: slideId, result: pause, generation_manifest: null])
    } catch (Throwable error) {
        try { restoreOutput(resultPath, originalResult) }
        catch (Throwable restoreError) { error.addSuppressed(restoreError) }
        try {
            project.setPathClasses(originalDeclaredClasses)
            persistRollbackProjectCheckpoint()
        }
        catch (Throwable restoreError) { error.addSuppressed(restoreError) }
        throw error
    }
    return
}
if (!(request.expected_cell_ids instanceof List))
    throw new IllegalArgumentException("expected_cell_ids must be a typed stable-ID list")
List<String> expectedCellIds = typedStrings(request.expected_cell_ids, "expected_cell_ids").sort()
List<String> phenotypingCellIds = batchDeployment ? typedStrings(request.phenotyping_cell_ids, "phenotyping_cell_ids").sort() : expectedCellIds
Set<String> phenotypingCellIdSet = new HashSet<>(phenotypingCellIds)
if (!expectedCellIds.containsAll(phenotypingCellIds))
    throw new IllegalArgumentException("Phenotyping cells must belong to the current section")
if (!(request.classifier instanceof Map))
    throw new IllegalArgumentException("Exact saved classifier identity is required")
Map classifierIdentity = request.classifier as Map
Set classifierFields = batchDeployment ?
    (["classifier_id", "classifier_sha256", "qupath_version", "class_ids",
      "feature_schema", "validation_domain_id", "artifact_project_relative_path",
      "artifact_byte_count"] as Set) :
    (["classifier_id", "classifier_sha256", "qupath_version", "class_ids",
      "feature_schema", "training_cell_ids", "holdout_cell_ids",
      "validation_domain_id", "artifact_path"] as Set)
if ((classifierIdentity.keySet() as Set) != classifierFields)
    throw new IllegalArgumentException("Saved classifier identity has an invalid typed shape")
if (classifierIdentity.class_ids != classIds || classifierIdentity.feature_schema != featureSchema)
    throw new IllegalArgumentException("Classifier classes/features do not match the exact request order")
["classifier_id", "qupath_version", "validation_domain_id"].each { field ->
    if (!(classifierIdentity[field] instanceof String) || !(classifierIdentity[field] as String).trim())
        throw new IllegalArgumentException("Classifier ${field} must be nonblank")
}
if (classifierIdentity.qupath_version != qupath.lib.common.GeneralTools.getVersion())
    throw new IllegalStateException("Classifier QuPath version does not match the running runtime")
if (currentValidationDomainId != classifierIdentity.validation_domain_id)
    throw new IllegalStateException("Classifier validation domain does not match the current reuse domain")
List<String> trainingCellIds = batchDeployment ? [] : typedStrings(
    classifierIdentity.training_cell_ids, "training_cell_ids"
).sort()
List<String> holdoutCellIds = batchDeployment ? [] : typedStrings(
    classifierIdentity.holdout_cell_ids, "holdout_cell_ids"
).sort()
if (!trainingCellIds.intersect(holdoutCellIds).isEmpty())
    throw new IllegalArgumentException("Training and holdout stable cell IDs must be disjoint")
String expectedDigest = classifierIdentity.classifier_sha256 as String
if (!(expectedDigest ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException("Classifier digest must be lowercase complete-file SHA-256")
Path artifactPath
if (batchDeployment) {
    String relativeText = classifierIdentity.artifact_project_relative_path as String
    Path relative = Paths.get(relativeText).normalize()
    Path classifierRoot = projectRoot.resolve("classifiers").normalize()
    artifactPath = projectRoot.resolve(relative).normalize()
    if (!relativeText || relative.isAbsolute() || relative.toString().contains("..") ||
        !artifactPath.startsWith(classifierRoot) ||
        !(classifierIdentity.artifact_byte_count instanceof Number) ||
        (classifierIdentity.artifact_byte_count as Number).longValue() <= 0L)
        throw new IllegalArgumentException("Batch classifier locator is not project-relative under classifiers")
} else {
    artifactPath = Paths.get(classifierIdentity.artifact_path as String).toAbsolutePath().normalize()
}
if (!Files.isRegularFile(artifactPath))
    throw new IllegalArgumentException("Saved native QuPath object-classifier artifact is required")
if (sha256(artifactPath) != expectedDigest ||
    (batchDeployment && Files.size(artifactPath) !=
        (classifierIdentity.artifact_byte_count as Number).longValue()))
    throw new IllegalStateException("Saved classifier complete-file digest mismatch")

// Acceptance consumes the saved prediction rows and their model identity.
// Only initial inference or the reference-model route needs the model loaded.
def selectedNativeClassifier = materializeDisplayClasses ?
    null : ObjectClassifiers.readClassifier(artifactPath)
// i) We own this opened native model until the current operation exits.
// ii) Validation failures, batch returns and completed application all release
// the model; the retained support API owns no individual classifier instance.
try {
Map selectedFeatureSource=nativeObjectSupport.featureSource(selectedNativeClassifier) as Map

/**
 * Retrieves the metadata map attached to a QuPath object.
 *
 * Args:
 *     object: Selected tissue annotation, reference point or measured cell.
 *
 * Returns:
 *     The object's metadata map, used for reading its analysis IDs or writing
 *     the currently accepted phenotype result.
 */
def metadata = { object -> object.getMetadata() }
// =============================================================================
// iii) Select the accepted tissue section and complete measured cell population
// =============================================================================
// We match the tissue and cells by image, section and accepted generation.
// Every expected cell must exist once, belong to the recorded analysis region
// and contain every measurement selected by the saved model.

List sections = getAnnotationObjects().findAll { object ->
    Map m = metadata(object)
    m.get("pipeline_stage") == "tissue_detection" && m.get("generation_status") == "accepted" &&
        m.get("review_status") == "accepted" &&
        m.get("slide_id") == slideId && m.get("section_id") == sectionId &&
        m.get("generation_id") == generationId
}
if (sections.size() != 1)
    throw new IllegalStateException("Exactly one accepted current TissueSection is required")
def cellProvider = NativeCellScope.open(imageData, sectionId).join()
String populationId = request.segmentation_population_id as String
if (!populationId || !cellProvider.ownsPopulation(populationId))
    throw new IllegalStateException("Supervised classifier targets a stale segmentation population")
Map<String, String> liveDomainMapping = new TreeMap()
// i) We verify every current identity while retaining only the protocol's scalar mapping.
if (!batchDeployment && materializeDisplayClasses) {
    // Reviewed values already passed finite-feature checks. We compare their
    // dataset/revision/component identities below and project only current IDs.
    cellProvider.forEachIdentity({ String cellId, String domainId ->
        if (!cellId || !domainId || liveDomainMapping.containsKey(cellId))
            throw new IllegalStateException("Reviewed supervised cells require unique current domain identities")
        liveDomainMapping[cellId] = domainId
    } as java.util.function.BiConsumer)
} else {
cellProvider.forEach({ cell ->
    Map values = metadata(cell)
    String cellId = values.get("cell_id") as String
    String domainId = values.get("analysis_domain_id") as String
    if (!cellId || liveDomainMapping.containsKey(cellId) ||
        values.get("generation_id") != generationId || values.get("slide_id") != slideId ||
        values.get("section_id") != sectionId || values.get("segmentation_population_id") != populationId ||
        !(values.get("analysis_domain_kind") in ["selected_roi", "whole_tissue_section"]) || !domainId)
        throw new IllegalStateException("Supervised cells require unique current section, population and domain identities")
    if (values.get("scientific_measurements_stale") == "true" || values.get("mif.measurements_stale") == "true")
        throw new IllegalStateException("Supervised classifier requires current measurements after geometry edits")
    liveDomainMapping[cellId] = domainId
    if (selectedFeatureSource == null && !materializeDisplayClasses && phenotypingCellIdSet.contains(cellId)) featureIds.each { name ->
        if (!cell.measurementList.containsKey(name) || !Double.isFinite(cell.measurementList.get(name)))
            throw new IllegalStateException("Cell ${cellId} lacks exact finite selected feature ${name}")
    }
} as Consumer)
}
if (liveDomainMapping.keySet().toList() != expectedCellIds)
    throw new IllegalStateException("Expected stable cell IDs must cover the exact current section")
if (!(request.cell_id_to_analysis_domain_id instanceof Map) ||
    new TreeMap(request.cell_id_to_analysis_domain_id as Map) != liveDomainMapping)
    throw new IllegalStateException("Supervised request analysis-domain mapping is stale or incomplete")

/**
 * We classify at most 256 current native cells together. Optional frozen features
 * share one encoder for this operation; each batch's finite features are checked
 * before the native model runs. We extract the original class/confidence values
 * from those same instances, then release them. Only prediction rows survive.
 * Invalid features, undeclared classes or unavailable confidence stop inference.
 */
Closure<List<Map>> predictCurrentCells = {
    if (phenotypingCellIdSet.isEmpty()) return []
    Map featureSupport = selectedFeatureSource == null ? null : loadSupervisedAsset("cell_analysis/kronos_cell_features.groovy")
    Map workers = featureSupport == null ? null : loadSupervisedAsset("compartment_analysis/kronos_worker_client.groovy")
    Map session = null
    List batch = []
    List<Map> predictions = []
    try {
        if (featureSupport != null) {
            if (featureSupport.featureSchema() != featureSchema)
                throw new IllegalStateException("The saved cell encoder and selected feature schema differ")
            session = featureSupport.open(selectedFeatureSource, resultPath.toAbsolutePath().parent, workers)
        }
        Closure flush = {
            if (batch.isEmpty()) return
            // ii) Features and native predictions use the same bounded instances.
            if (featureSupport != null) featureSupport.prepare(imageData, batch, selectedFeatureSource,
                [image_sha256: requestedCanonicalSha256, segmentation_generation_id: generationId],
                resultPath.toAbsolutePath().parent, workers, session)
            batch.each { cell -> featureIds.each { name ->
                if (!cell.measurementList.containsKey(name) || !Double.isFinite(cell.measurementList.get(name)))
                    throw new IllegalStateException("Cell ${metadata(cell).get('cell_id')} lacks exact finite selected feature ${name}")
            }}
            selectedNativeClassifier.classifyObjects(imageData, batch, true)
            batch.each { cell ->
                String cellId = metadata(cell).get("cell_id") as String
                String classId = cell.getPathClass()?.toString()
                double confidence = cell.getClassProbability()
                if (!(classId in classIds) || !Double.isFinite(confidence) || confidence < 0d || confidence > 1d)
                    throw new IllegalStateException("Native classifier returned an undeclared class or invalid confidence for ${cellId}")
                predictions.add([cell_id: cellId, class_id: classId, confidence: confidence])
            }
            batch.clear()
        }
        cellProvider.forEach({ cell ->
            if (!phenotypingCellIdSet.contains(metadata(cell).get("cell_id") as String)) return
            batch.add(cell)
            if (batch.size() == 256) flush()
        } as Consumer)
        flush()
        predictions.sort { it.cell_id }
    } finally {
        batch.clear()
        if (session != null) featureSupport.close(session)
    }
}

/**
 * We publish checked accepted assignments as aligned typed fields and select the
 * completed descriptor only after publication succeeds. The saved-image owner
 * retains previous component pointers for transaction rollback; clean geometry
 * and historical native objects are never copied into the hierarchy.
 */
Closure<Path> publishAcceptedView = { Map reference, List<Map> assignments ->
    Path destination = resultPath.toAbsolutePath().parent.resolve("phenotype-components")
        .resolve("supervised-" + UUID.randomUUID().toString())
    Map<String, Map> rows = assignments.collectEntries { [(it.cell_id as String): it] }
    Path descriptor = PhenotypeComponents.publish(imageData, sectionId, destination, reference,
        runId, rows)
    NativeCellScope.selectPhenotypeComponent(imageData, descriptor.toString(), materializeDisplayClasses)
    descriptor
}

// =============================================================================
// iv) Apply the saved model to a new eligible cell population
// =============================================================================
// We classify bounded current instances and collect their original class and
// confidence values. Confidence review reopens these stored prediction rows;
// acceptance publishes aligned typed view fields and selects their descriptor.

if (batchDeployment) {
    List originalProjectClasses = new ArrayList(project.getPathClasses())
    Object originalComponents = imageData.getProperty(CellComponents.PROPERTY)
    byte[] originalManifest = Files.exists(manifestPath) ?
        Files.readAllBytes(manifestPath) : null
    byte[] originalResult = Files.exists(resultPath) ?
        Files.readAllBytes(resultPath) : null
    try {
        List projectClasses = new ArrayList(originalProjectClasses)
        classIds.each { classId ->
            PathClass pathClass = PathClass.getInstance(classId)
            if (!projectClasses.contains(pathClass)) projectClasses.add(pathClass)
        }
        project.setPathClasses(projectClasses)
        if (materializeDisplayClasses != (request.stored_prediction instanceof Map))
            throw new IllegalArgumentException(
                "Reviewed application requires its stored prediction file"
            )
        Map predictionReuse = loadSupervisedAsset("cell_analysis/supervised_prediction_reuse.groovy")
        Map selectedPredictions = predictionReuse.readOrPredict.call(
            request.stored_prediction as Map, request.prediction_identity as Map,
            phenotypingCellIds, classIds, {
                predictCurrentCells()
            }
        )
        List<Map> predictions = selectedPredictions.predictions as List<Map>
        List<String> exactPhenotypedIds = predictions.collect {
            it.cell_id as String
        }
        String cellIdsSha256 = MessageDigest.getInstance("SHA-256")
            .digest(gson.toJson(exactPhenotypedIds)
                .getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
        if (!(request.prediction_identity instanceof Map))
            throw new IllegalArgumentException(
                "Batch prediction identity authority is required"
            )
        Map predictionIdentity = new LinkedHashMap(
            request.prediction_identity as Map
        )
        Set predictionIdentityFields = [
            "schema_version", "sample_id", "project_entry_id", "slide_id",
            "section_id", "tissue_generation_id",
            "classifier_record_sha256", "classifier_id", "class_ids",
            "analysis_domain_bindings", "measurement_result_ref_sha256",
            "cell_ids_sha256", "cell_to_domain_membership_sha256"
        ] as Set
        if ((predictionIdentity.keySet() as Set) != predictionIdentityFields ||
            predictionIdentity.schema_version !=
                "mif-batch-supervised-native-prediction/v1" ||
            predictionIdentity.project_entry_id != requestedEntryId ||
            predictionIdentity.slide_id != slideId ||
            predictionIdentity.section_id != sectionId ||
            predictionIdentity.tissue_generation_id != generationId ||
            predictionIdentity.classifier_id != classifierIdentity.classifier_id ||
            predictionIdentity.class_ids != classIds ||
            predictionIdentity.cell_ids_sha256 != cellIdsSha256)
            throw new IllegalArgumentException(
                "Batch prediction identity differs from the live section"
            )
        predictionIdentity.analysis_domain_bindings =
            (predictionIdentity.analysis_domain_bindings as List).collect { raw ->
                if (!(raw instanceof Map) ||
                    !((raw as Map).analysis_domain_revision instanceof Number))
                    throw new IllegalArgumentException(
                        "Batch prediction domain binding is invalid"
                    )
                double revision = ((raw as Map).analysis_domain_revision as Number)
                    .doubleValue()
                if (!Double.isFinite(revision) || revision <= 0d ||
                    revision != Math.rint(revision) || revision > Integer.MAX_VALUE)
                    throw new IllegalArgumentException(
                        "Batch prediction domain revision is invalid"
                    )
                Map normalized = new LinkedHashMap(raw as Map)
                normalized.analysis_domain_revision = (int) revision
                normalized
            }
        String rowsSha256
        String predictionSha256
        int predictionByteCount
        if (materializeDisplayClasses) {
            rowsSha256 = selectedPredictions.rows_sha256 as String
            predictionSha256 = selectedPredictions.prediction_sha256 as String
            predictionByteCount = (selectedPredictions.prediction_byte_count as Number).intValue()
        } else {
            rowsSha256 = contentDigest(predictions)
            Map predictionUnsigned = new LinkedHashMap(predictionIdentity)
            predictionUnsigned.cell_count = predictions.size()
            predictionUnsigned.rows = predictions
            predictionUnsigned.rows_sha256 = rowsSha256
            predictionSha256 = contentDigest(predictionUnsigned)
            predictionByteCount = gson.toJson(canonicalJson(predictionUnsigned))
                .getBytes(StandardCharsets.UTF_8).length
        }

        if (!materializeDisplayClasses) {
            if (request.expected_prediction_sha256 != null ||
                request.phenotype_view_artifact != null ||
                request.phenotype_assignment_source != null)
                throw new IllegalArgumentException(
                    "Unreviewed batch prediction cannot carry accepted view authority"
                )
            Map outputs = [
                predictions: predictions,
                prediction_sha256: predictionSha256,
                prediction_rows_sha256: rowsSha256,
                cell_ids_sha256: cellIdsSha256,
                section_id: sectionId,
                materialized_display_classes: false
            ]
            Map manifest = [
                schema_version: "roi-generation-manifest/v1",
                invocation_id: envelope.invocation_id,
                run_id: runId,
                stage: envelope.stage,
                slide_id: slideId,
                generation_ids: [generationId],
                generations: [],
                object_outputs: outputs
            ]
            Map pause = [
                status: "needs_interaction", stage: envelope.stage,
                slide_id: slideId, generation_id: generationId,
                interaction_reason: "supervised_classifier_review_required",
                affected_object_ids: [],
                message: "Review the current section supervised predictions and confidence threshold.",
                allowed_actions: ["review_supervised_classifier", "abort"],
                resume_context: [
                    schema_version: INTERACTION_CONTEXT_SCHEMA,
                    phase: "prediction_review", sha256: predictionSha256,
                    byte_count: predictionByteCount,
                    cell_count: predictions.size(),
                    score_kind: nativeObjectSupport.scoreKind(selectedNativeClassifier),
                    class_count: request.classifier.class_ids.size(),
                    selected_display_view: "supervised_classifier"
                ],
                checkpoint_id: "supervised-prediction-${predictionSha256}".toString(),
                outputs: null, error: null
            ]
            project.setPathClasses(originalProjectClasses)
            fireHierarchyUpdate()
            atomicJson(manifestPath, manifest)
            mifPublishProjectStageResult(resultPath, [
                schema_version: "roi-stage-result/v1",
                invocation_id: envelope.invocation_id,
                run_id: runId, stage: envelope.stage, slide_id: slideId,
                result: pause, generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]
            ])
            return
        }

        if (request.expected_prediction_sha256 != predictionSha256 ||
            !(request.phenotype_assignment_source instanceof Map))
            throw new IllegalStateException(
                "Reviewed prediction digest or prepared assignments changed"
            )
        Map viewReference = request.phenotype_view_artifact as Map
        List<Map> viewAssignments = PhenotypeComponents.readAssignments(request.phenotype_assignment_source as Map,
            viewReference.assignments_sha256 as String).values().collect { normalizeAssignment(it) as Map }
        if (viewAssignments.collect { it.cell_id as String } != expectedCellIds)
            throw new IllegalArgumentException("Prepared supervised assignments do not cover the exact cells")
        Set referenceFields = [
            "schema_version", "mode", "slide_id", "canonical_image_sha256",
            "tissue_generation_id", "phenotype_generation_id", "section_id",
            "source_run_id", "artifact_path", "artifact_sha256",
            "lineage_sha256", "cell_ids_sha256", "cell_count", "cell_to_domain_membership_sha256", "assignments_sha256"
        ] as Set
        if (!(viewReference instanceof Map) ||
            (viewReference.keySet() as Set) != referenceFields ||
            viewReference.schema_version != "phenotype-view-reference/v1" ||
            viewReference.mode != "supervised_classifier" ||
            viewReference.slide_id != slideId ||
            viewReference.canonical_image_sha256 != requestedCanonicalSha256 ||
            viewReference.tissue_generation_id != generationId ||
            viewReference.section_id != sectionId ||
            viewReference.cell_ids_sha256 != contentDigest(expectedCellIds) ||
            viewReference.cell_to_domain_membership_sha256 != contentDigest(liveDomainMapping) ||
            viewReference.cell_count != expectedCellIds.size())
            throw new IllegalArgumentException(
                "Prepared supervised phenotype-view reference is stale"
            )
        Path viewArtifactPath = Paths.get(viewReference.artifact_path as String)
            .toAbsolutePath().normalize()
        if (!Files.isRegularFile(viewArtifactPath) ||
            sha256(viewArtifactPath) != viewReference.artifact_sha256)
            throw new IllegalArgumentException(
                "Prepared supervised phenotype-view artifact is unavailable"
            )
        Map viewArtifact = gson.fromJson(
            Files.readString(viewArtifactPath), Map
        ) as Map
        if (viewArtifact.schema_version != "phenotype-view-artifact/v1" ||
            viewArtifact.mode != "supervised_classifier" ||
            viewArtifact.phenotype_generation_id !=
                viewReference.phenotype_generation_id ||
            viewArtifact.source_run_id != viewReference.source_run_id ||
            viewArtifact.cell_count != expectedCellIds.size() ||
            viewArtifact.cell_ids_sha256 != contentDigest(expectedCellIds) ||
            viewArtifact.cell_to_domain_membership_sha256 != viewReference.cell_to_domain_membership_sha256 ||
            viewArtifact.assignments_sha256 != viewReference.assignments_sha256 ||
            !(viewArtifact.lineage instanceof Map) ||
            contentDigest(viewArtifact.lineage) != viewReference.lineage_sha256)
            throw new IllegalArgumentException(
                "Prepared supervised phenotype-view content differs"
            )
        // =============================================================================
        // v) Write the accepted per-cell phenotype result
        // =============================================================================
        // Each checked assignment resolves its original cell by ID. We retain the
        // previous per-method results, add the accepted supervised result and update
        // the currently selected phenotype metadata and QuPath display class together.

        Path phenotypeComponent = publishAcceptedView(viewReference, viewAssignments)
        Map outputs = [
            prediction_sha256: predictionSha256,
            prediction_rows_sha256: rowsSha256,
            phenotype_view_artifact: viewReference,
            component_descriptor: [path: phenotypeComponent.toString(), sha256: sha256(phenotypeComponent)],
            section_id: sectionId, cell_ids_sha256: cellIdsSha256,
            materialized_display_classes: true
        ]
        Map manifest = [
            schema_version: "roi-generation-manifest/v1",
            invocation_id: envelope.invocation_id,
            run_id: runId, stage: envelope.stage, slide_id: slideId,
            generation_ids: [generationId], generations: [],
            object_outputs: outputs
        ]
        fireHierarchyUpdate()
        persistForwardCurrentEntry()
        Map completed = [
            status: "completed", stage: envelope.stage, slide_id: slideId,
            generation_id: generationId, interaction_reason: null,
            affected_object_ids: [], message: null,
            allowed_actions: [], resume_context: null, checkpoint_id: null,
            outputs: outputs, error: null
        ]
        atomicJson(manifestPath, manifest)
        mifPublishProjectStageResult(resultPath, [
            schema_version: "roi-stage-result/v1",
            invocation_id: envelope.invocation_id,
            run_id: runId, stage: envelope.stage, slide_id: slideId,
            result: completed,
            generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]
        ])
        return
    } catch (Throwable error) {
        try { restoreOutput(resultPath, originalResult) }
        catch (Throwable restoreError) { error.addSuppressed(restoreError) }
        try { restoreOutput(manifestPath, originalManifest) }
        catch (Throwable restoreError) { error.addSuppressed(restoreError) }
        try {
            if (originalComponents == null) imageData.removeProperty(CellComponents.PROPERTY)
            else imageData.setProperty(CellComponents.PROPERTY, originalComponents)
            NativeCellScope.close(imageData)
            project.setPathClasses(originalProjectClasses)
            fireHierarchyUpdate()
            persistRollbackCurrentEntry()
        } catch (Throwable restoreError) { error.addSuppressed(restoreError) }
        throw error
    }
}
// The native image owns reference-to-cell associations. Coordinate-only
// annotations are bound once; saved references follow their cell IDs.
// =============================================================================
// vi) Export point references for the original model review
// =============================================================================
// We require points from this image, section and run, carrying an explicit
// training or holdout role and a reference-region ID. Their coordinates and
// classes remain linked to their saved native cell and population identities.

List<Map> pointEvidence = []
Set<String> pointIds = [] as Set<String>
Map referenceSupport = loadSupervisedAsset("cell_analysis/supervised_references.groovy") as Map
Map referenceResolver = referenceSupport.create.call(
    boundEntry.getID().toString(), populationId, cellProvider, pointSelectionRadius
) as Map
getAnnotationObjects().findAll { point ->
    Map m = metadata(point)
    point.getROI()?.isPoint() && m.get("pipeline_stage") == "supervised_training" &&
        m.get("pipeline_run_id") == runId && m.get("generation_id") == generationId &&
        m.get("slide_id") == slideId && m.get("section_id") == sectionId
}.each { point ->
    Map m = metadata(point)
    String evidenceSet = m.get("supervised_evidence_set") as String
    if (!(evidenceSet in ["training", "holdout"]))
        throw new IllegalStateException("Scoped supervised points require an explicit training or holdout evidence set")
    String pointId = m.get("point_id") as String
    String regionId = m.get("spatial_region_id") as String
    String classId = point.getPathClass()?.toString()
    if (!pointId || !pointIds.add(pointId) || !regionId || !(classId in classIds))
        throw new IllegalStateException("Training points require unique IDs, declared classes, and explicit spatial regions")
    pointEvidence << [point_id: pointId, cell_id: referenceResolver.bind.call(point), slide_id: slideId, section_id: sectionId, generation_id: generationId, run_id: runId,
        x_level0: point.getROI().getCentroidX(), y_level0: point.getROI().getCentroidY(),
        class_id: classId, evidence_set: evidenceSet, spatial_region_id: regionId]
}
if (!pointEvidence.any { it.evidence_set == "training" } ||
    !pointEvidence.any { it.evidence_set == "holdout" })
    throw new IllegalStateException("Explicit training and spatially separated holdout point evidence are required")
pointEvidence.sort { it.point_id }
if (referenceResolver.stats.new_bindings > 0) {
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}

// =============================================================================
// vii) Check our native reference roles before inference
// =============================================================================
// i) Saved reference IDs select bounded current cells. We retain the researchers'
// exact class/role/region evidence and require the model's original reference IDs.
// ii) The current dataset and selected measurement components bind the scientific
// values; no full-population centroid export or Python approval roundtrip is needed.

List<String> mappedTrainingCellIds = []
List<String> mappedHoldoutCellIds = []
Set<String> mappedTrainingRegionIds = [] as Set<String>
Set<String> mappedHoldoutRegionIds = [] as Set<String>
pointEvidence.each { point ->
    String cellId = point.cell_id as String
    def cell = cellProvider.lookup(cellId)
    if (cell == null || !Double.isFinite((point.x_level0 as Number).doubleValue()) ||
        !Double.isFinite((point.y_level0 as Number).doubleValue()) ||
        !Double.isFinite(cell.getROI().getCentroidX()) ||
        !Double.isFinite(cell.getROI().getCentroidY()))
        throw new IllegalStateException("Saved supervised reference lacks its finite current cell")
    if (point.evidence_set == "training") {
        mappedTrainingCellIds.add(cellId)
        mappedTrainingRegionIds.add(point.spatial_region_id as String)
    } else {
        mappedHoldoutCellIds.add(cellId)
        mappedHoldoutRegionIds.add(point.spatial_region_id as String)
    }
}
if (mappedTrainingCellIds.sort() != trainingCellIds ||
    mappedHoldoutCellIds.sort() != holdoutCellIds ||
    mappedTrainingCellIds.toSet().size() != mappedTrainingCellIds.size() ||
    mappedHoldoutCellIds.toSet().size() != mappedHoldoutCellIds.size() ||
    !mappedTrainingRegionIds.intersect(mappedHoldoutRegionIds).isEmpty())
    throw new IllegalStateException("Current supervised references differ from the model's disjoint cell/region roles")
String validationSha256 = contentDigest([
    classifier_sha256: expectedDigest, canonical_image_sha256: requestedCanonicalSha256,
    population_id: populationId, slide_id: slideId, section_id: sectionId,
    generation_id: generationId, run_id: runId,
    validation_domain_id: currentValidationDomainId,
    class_ids: classIds, feature_schema: featureSchema,
    cell_dataset_sha256: cellProvider.getDescriptionSha256(),
    geometry_revision: cellProvider.getGeometryRevision(),
    components: cellProvider.getSelectedComponentMetadata().collect { it.sha256 }.sort(),
    point_evidence: pointEvidence])

// =============================================================================
// viii) Run the checked model and save the requested result
// =============================================================================
// We retain the selected component pointers and output bytes before inference.
// Accepted phenotype views are checked against their saved file and model
// identity before selecting the typed component and saving the image entry.
// Review-only calculation returns prediction rows from transient instances.

List originalProjectClasses = new ArrayList(project.getPathClasses())
Object originalComponents = imageData.getProperty(CellComponents.PROPERTY)
byte[] originalManifest = Files.exists(manifestPath) ? Files.readAllBytes(manifestPath) : null
byte[] originalResult = Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null
List projectClasses = new ArrayList(originalProjectClasses)
classIds.each { classId ->
    PathClass pathClass = PathClass.getInstance(classId)
    if (!projectClasses.contains(pathClass)) {
        projectClasses.add(pathClass)
    }
}

try {
    project.setPathClasses(projectClasses)
    String phenotypeGenerationId = "supervised-classifier-view-v1-${expectedDigest}"
    if (materializeDisplayClasses != (request.stored_prediction instanceof Map))
        throw new IllegalArgumentException("Reviewed supervised application requires its stored prediction file")
    Map predictionReuse = loadSupervisedAsset("cell_analysis/supervised_prediction_reuse.groovy")
    Map reviewIdentity = [schema_version: "supervised-phenotype-review/v1",
        section_id: sectionId, generation_id: phenotypeGenerationId,
        classifier: classifierIdentity, class_ids: classIds, feature_schema: featureSchema,
        validated_evidence_sha256: validationSha256,
        cell_ids_sha256: contentDigest(expectedCellIds),
        cell_id_to_analysis_domain_id: liveDomainMapping]
    Map selectedPredictions = predictionReuse.readOrPredict.call(
        request.stored_prediction as Map, reviewIdentity, expectedCellIds, classIds,
        { predictCurrentCells() }, "predictions")
    List<Map> predictions = selectedPredictions.predictions as List<Map>
    List<String> exactPhenotypedIds = predictions.collect { it.cell_id as String }
    String cellIdsSha256 = MessageDigest.getInstance("SHA-256")
        .digest(gson.toJson(exactPhenotypedIds).getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
    List<Map> viewAssignments = predictions.collect { prediction ->
        [cell_id: prediction.cell_id,
         analysis_domain_id: liveDomainMapping[prediction.cell_id as String],
         primary_phenotype: prediction.class_id,
         phenotype_source: "supervised_qupath", phenotype_components: [],
         marker_states: [:], phenotype_confidence: prediction.confidence,
         cluster_id: null, cluster_label: null]
    }
    Map viewReference = request.phenotype_view_artifact as Map
    Path phenotypeComponent = null
    if (materializeDisplayClasses) {
        Set referenceFields = ["schema_version", "mode", "slide_id", "canonical_image_sha256",
            "tissue_generation_id", "phenotype_generation_id", "section_id", "source_run_id",
            "artifact_path", "artifact_sha256", "lineage_sha256",
            "cell_ids_sha256", "cell_count", "cell_to_domain_membership_sha256", "assignments_sha256"] as Set
        if (!(viewReference instanceof Map) || (viewReference.keySet() as Set) != referenceFields ||
            viewReference.schema_version != "phenotype-view-reference/v1" ||
            viewReference.mode != "supervised_classifier" || viewReference.slide_id != slideId ||
            viewReference.tissue_generation_id != generationId ||
            viewReference.phenotype_generation_id != phenotypeGenerationId ||
            viewReference.section_id != sectionId ||
            viewReference.cell_ids_sha256 != cellIdsSha256 ||
            viewReference.cell_to_domain_membership_sha256 != contentDigest(liveDomainMapping) ||
            viewReference.cell_count != exactPhenotypedIds.size() ||
            !(viewReference.artifact_sha256 ==~ /[0-9a-f]{64}/) ||
            !(viewReference.lineage_sha256 ==~ /[0-9a-f]{64}/))
            throw new IllegalArgumentException("Supervised phenotype-view artifact reference is stale or invalid")
        Path viewArtifactPath = Paths.get(viewReference.artifact_path as String).toAbsolutePath().normalize()
        if (!Files.isRegularFile(viewArtifactPath) || sha256(viewArtifactPath) != viewReference.artifact_sha256)
            throw new IllegalArgumentException("Supervised phenotype-view artifact is missing or tampered")
        Map viewArtifact = gson.fromJson(Files.readString(viewArtifactPath), Map) as Map
        List<Map> acceptedAssignments = PhenotypeComponents.readAssignments(request.phenotype_assignment_source as Map,
            viewReference.assignments_sha256 as String).values().collect { normalizeAssignment(it) as Map }
        if (acceptedAssignments != viewAssignments)
            throw new IllegalArgumentException("Accepted supervised assignments differ from the checked native predictions")
        if (viewArtifact.schema_version != "phenotype-view-artifact/v1" ||
            viewArtifact.mode != "supervised_classifier" || viewArtifact.slide_id != slideId ||
            viewArtifact.tissue_generation_id != generationId ||
            viewArtifact.phenotype_generation_id != phenotypeGenerationId ||
            viewArtifact.section_id != sectionId || viewArtifact.source_run_id != viewReference.source_run_id ||
            viewArtifact.cell_ids_sha256 != cellIdsSha256 || viewArtifact.cell_count != exactPhenotypedIds.size() ||
            viewArtifact.cell_to_domain_membership_sha256 != contentDigest(liveDomainMapping) ||
            viewArtifact.assignments_sha256 != viewReference.assignments_sha256 || !(viewArtifact.lineage instanceof Map) ||
            contentDigest(viewArtifact.lineage) != viewReference.lineage_sha256)
            throw new IllegalArgumentException("Supervised phenotype-view artifact content or lineage is stale")
        phenotypeComponent = publishAcceptedView(viewReference, viewAssignments)
    } else if (viewReference != null) {
        throw new IllegalArgumentException("Unaccepted supervised inference cannot publish a phenotype view")
    }
    Map outputs = [section_id: sectionId, coordinate_system: "global_level0",
        classifier_id: classifierIdentity.classifier_id,
        classifier_sha256: expectedDigest, qupath_version: classifierIdentity.qupath_version,
        current_validation_domain_id: currentValidationDomainId,
        artifact_validation_domain_id: classifierIdentity.validation_domain_id,
        class_ids: classIds, feature_schema: featureSchema, feature_ids: featureIds, training_cell_ids: trainingCellIds,
        holdout_cell_ids: holdoutCellIds, point_evidence: pointEvidence,
        validated_evidence_sha256: validationSha256,
        phenotype_generation_id: phenotypeGenerationId,
        phenotype_mode: "supervised_classifier",
        selected_display_view: "supervised_classifier",
        phenotype_view_artifact: viewReference,
        component_descriptor: phenotypeComponent == null ? null : [path: phenotypeComponent.toString(), sha256: sha256(phenotypeComponent)],
        materialized_display_classes: materializeDisplayClasses,
        cell_ids_sha256: cellIdsSha256]
    if (!materializeDisplayClasses) outputs.putAll([predictions: predictions,
        prediction_rows_sha256: contentDigest(predictions),
        predicted_cell_ids: exactPhenotypedIds, updated_cell_ids: exactPhenotypedIds,
        cell_id_to_analysis_domain_id: liveDomainMapping])
    Map manifest = [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
        generations: [], object_outputs: outputs]
    Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
        generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
        allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
    if (materializeDisplayClasses) {
        fireHierarchyUpdate()
        persistForwardCurrentEntry()
    } else {
        project.setPathClasses(originalProjectClasses)
    }
    atomicJson(manifestPath, manifest)
    mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
        run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
        generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]])
} catch (Throwable error) {
    try {
        if (originalComponents == null) imageData.removeProperty(CellComponents.PROPERTY)
        else imageData.setProperty(CellComponents.PROPERTY, originalComponents)
        NativeCellScope.close(imageData)
        project.setPathClasses(originalProjectClasses)
        fireHierarchyUpdate()
    } catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    try { persistRollbackCurrentEntry() }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    try { restoreOutput(resultPath, originalResult) }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    try { restoreOutput(manifestPath, originalManifest) }
    catch (Throwable restoreError) { error.addSuppressed(restoreError) }
    throw error
}
} finally {
    nativeObjectSupport.close(selectedNativeClassifier)
}
