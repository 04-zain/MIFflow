// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Retire admitted saved scientific selections before revised work. */
import org.mif.cells.NativeCellScope

if (!mifAuthenticatedHeadlessProjectStage)
    throw new IllegalStateException("Saved result retirement requires its authenticated analysis task")
if (args.size() != 3)
    throw new IllegalArgumentException("Expected retirement request, result and manifest paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path resultPath = Paths.get(args[1] as String).toAbsolutePath().normalize()
Path manifestPath = Paths.get(args[2] as String).toAbsolutePath().normalize()
def retirementJson = new GsonBuilder().serializeNulls().create()
Map envelope = retirementJson.fromJson(Files.readString(requestPath), Map.class) as Map
Map request = envelope.request as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "saved_result_retirement" ||
        request.schema_version != "roi-workflow-invocation/v1" ||
        !(request.retirement instanceof Map) || !(request.retirement.sources instanceof List) ||
        !(request.source_ref_sha256s instanceof List) || request.retirement.sources.isEmpty() ||
        request.source_ref_sha256s.size() != request.retirement.sources.size())
    throw new IllegalArgumentException("The saved result retirement request is invalid")
def project = getProject()
def image = getCurrentImageData()
def entry = getProjectEntry()
if (project == null || image == null || entry == null || entry.getID().toString() != request.project_entry_id ||
        project.getPath().toAbsolutePath().normalize() != Paths.get(request.project_file).toAbsolutePath().normalize() ||
        request.retirement.sources.any { it.scope.project_entry_id != request.project_entry_id || it.scope.sample_id != request.sample_id })
    throw new IllegalStateException("Saved result retirement differs from its authenticated image entry")
Map originalProperties = ["mif.cell_components", "mif.cell_datasets", "mif.segmentation_populations"].collectEntries {
    [(it): image.getProperty(it)]
}
Map originalMetadata = [:]
Map originalNames = [:]
List generated = []
// Match the existing typed producer identity; no geometry/table hash pass is needed
// to retire its selection. Original object geometry and manual descendants stay saved.
request.retirement.sources.each { source ->
    if (!(source.artifact_kind in ["primary_inference_result", "secondary_inference_result"])) return
    Map identity = source.owning_identity as Map
    String stage = source.artifact_kind == "primary_inference_result" ? "primary_classifier" : "secondary_classifier"
    Map domains = source.scope.analysis_domain_bindings.collectEntries { [(it.analysis_domain_id): it] }
    List matching = getAnnotationObjects().findAll { object ->
        Map m = object.getMetadata()
        m.pipeline_stage == stage && m.section_id == identity.section_id && identity.generation_ids.contains(m.generation_id) &&
            m.pipeline_run_id == identity.pipeline_run_id && domains.containsKey(m.analysis_domain_id) &&
            m.analysis_domain_path_object_id == identity.analysis_domain_path_object_id &&
            m.pipeline_active == "true" && m.classifier_id == identity.classifier_id &&
            m.classifier_version == identity.classifier_version &&
            (stage != "secondary_classifier" || m.structure_id == identity.structure_id)
    }
    if (matching.size() != (identity.roi_inventory.row_count as Number).longValue())
        throw new IllegalStateException("The saved generated region inventory changed for section ${identity.section_id}; refresh before revising it")
    matching.each { object ->
        Map m = object.getMetadata()
        Map domain = domains[m.analysis_domain_id] as Map
        String owner = "${stage}|${identity.pipeline_run_id}|${m.generation_id}|${m.analysis_domain_id}" +
            (stage == "secondary_classifier" ? "|${identity.structure_id}" : "")
        if (m.pipeline_owner != owner || m.pipeline_run_id != identity.pipeline_run_id ||
                m.slide_id != source.scope.slide_id || m.analysis_domain_revision_sha256 != domain.analysis_domain_revision_sha256 ||
                m.domain_set_sha256 != domain.domain_set_sha256 ||
                m.analysis_domain_path_object_id != identity.analysis_domain_path_object_id ||
                m.classifier_id != identity.classifier_id || m.classifier_version != identity.classifier_version ||
                m.pipeline_active != "true" ||
                (stage == "secondary_classifier" ? m.classifier_status : m.review_status) != "accepted")
            throw new IllegalStateException("The saved generated region differs from its admitted producer for section ${identity.section_id}")
        if (originalMetadata.containsKey(object))
            throw new IllegalStateException("A generated region appears in more than one retirement source")
        originalMetadata[object] = new LinkedHashMap(m)
        originalNames[object] = object.getName()
        generated.add(object)
    }
}
byte[] originalManifest = Files.exists(manifestPath) ? Files.readAllBytes(manifestPath) : null
byte[] originalResult = Files.exists(resultPath) ? Files.readAllBytes(resultPath) : null
boolean saveAttempted = false
def writeBytes = { Path destination, byte[] bytes ->
    Files.createDirectories(destination.parent)
    Path temporary = Files.createTempFile(destination.parent, ".retirement-", ".tmp")
    try {
        java.nio.channels.FileChannel.open(temporary, java.nio.file.StandardOpenOption.WRITE).withCloseable { channel ->
            ByteBuffer buffer = ByteBuffer.wrap(bytes)
            while (buffer.hasRemaining()) channel.write(buffer)
            channel.force(true)
        }
        Files.move(temporary, destination, java.nio.file.StandardCopyOption.ATOMIC_MOVE,
            java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    } finally { Files.deleteIfExists(temporary) }
}
def restoreOutput = { Path destination, byte[] bytes ->
    if (bytes == null) Files.deleteIfExists(destination)
    else writeBytes(destination, bytes)
}
try {
    NativeCellScope.retireSavedCellResults(image, request.retirement.sources as List)
    generated.each { object ->
        object.getMetadata().put("pipeline_active", "false")
        object.getMetadata().put("review_status", "superseded")
        object.getMetadata().put("classifier_status", "superseded")
        object.getMetadata().put("retired_for_revision_of_node_id", request.retirement.predecessor_node_id as String)
        object.setName((object.getName() ?: "Generated region") + " (previous result)")
    }
    if (!generated.isEmpty()) fireHierarchyUpdate()
    saveAttempted = true
    entry.saveImageData(image)
    project.syncChanges()
    Map outputs = [retired_artifact_ref_sha256s: request.source_ref_sha256s]
    String generationId = envelope.invocation_id
    Map manifest = [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
        run_id: envelope.run_id, stage: envelope.stage, slide_id: envelope.slide_id,
        generation_ids: [generationId], generations: [], object_outputs: outputs]
    writeBytes(manifestPath, (retirementJson.toJson(manifest) + "\n").getBytes(StandardCharsets.UTF_8))
    Map result = [status: "completed", stage: envelope.stage, slide_id: envelope.slide_id,
        generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
        allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
    mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1",
        invocation_id: envelope.invocation_id, run_id: envelope.run_id, stage: envelope.stage,
        slide_id: envelope.slide_id, result: result,
        generation_manifest: [path: manifestPath.fileName.toString(),
            sha256: MifProjectStageBootstrapV1.mifSha256(Files.readAllBytes(manifestPath))]])
} catch (Exception primary) {
    try {
        originalProperties.each { key, value ->
            if (value == null) image.removeProperty(key as String)
            else image.setProperty(key as String, value)
        }
        originalMetadata.each { object, metadata ->
            object.getMetadata().clear()
            object.getMetadata().putAll(metadata)
            object.setName(originalNames[object])
        }
        if (!generated.isEmpty()) fireHierarchyUpdate()
        NativeCellScope.close(image)
        if (saveAttempted) { entry.saveImageData(image); project.syncChanges() }
    } catch (Exception rollback) { primary.addSuppressed(rollback) }
    try { restoreOutput(resultPath, originalResult) }
    catch (Exception rollback) { primary.addSuppressed(rollback) }
    try { restoreOutput(manifestPath, originalManifest) }
    catch (Exception rollback) { primary.addSuppressed(rollback) }
    throw primary
}
