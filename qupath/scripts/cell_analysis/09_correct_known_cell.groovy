// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Publish one reviewed set of known-cell corrections through the native task owner. */
import org.mif.cells.NativeCellScope

if(!mifAuthenticatedHeadlessProjectStage || args.size()!=3)
    throw new IllegalStateException("Cell correction requires its authenticated analysis task")
Path requestPath=Paths.get(args[0]),resultPath=Paths.get(args[1]),manifestPath=Paths.get(args[2])
def json=new GsonBuilder().serializeNulls().create()
Map envelope=json.fromJson(Files.readString(requestPath),Map.class)
Map request=envelope.request as Map
if(envelope.schema_version!="roi-stage-request/v2" || envelope.stage!="segmentation_correction" ||
        request.schema_version!="roi-workflow-invocation/v1" || !(request.scope instanceof Map) ||
        !(request.correction_request instanceof Map) || !(request.outline instanceof Map))
    throw new IllegalArgumentException("The cell correction request is invalid")
def project=getProject(),image=getCurrentImageData(),entry=getProjectEntry()
if(project==null || image==null || entry==null || entry.ID.toString()!=request.project_entry_id ||
        project.path.toAbsolutePath().normalize()!=Paths.get(request.project_file).toAbsolutePath().normalize())
    throw new IllegalStateException("The cell correction differs from its authenticated image")
Path bundle=project.path.parent.resolve("scripts/bundle-${request.script_bundle_sha256}")
Map catalogue=json.fromJson(Files.readString(bundle.resolve("qupath-script-bundle.v1.json")),Map.class)
String helperName="general/06_cluster_annotations.groovy"
def helperRecord=catalogue.assets.findAll {it.path==helperName}
byte[] helperBytes=Files.readAllBytes(bundle.resolve(helperName))
if(helperRecord.size()!=1 || MifProjectStageBootstrapV1.mifSha256(helperBytes)!=helperRecord.first().sha256)
    throw new IllegalStateException("The native cell correction helper changed")
def helper=new GroovyShell(this.class.classLoader).evaluate(new String(helperBytes,StandardCharsets.UTF_8))
Map scope=request.scope as Map
if(helper.correctionOperations.call(scope)!=helper.correctionOperations.call(request.correction_request as Map))
    throw new IllegalArgumentException("The requested correction changed its selected cells or operations")
Object previous=image.getProperty("mif.cell_edits")
boolean saveAttempted=false
def write={Path destination,Map value ->
    byte[] bytes=(json.toJson(value)+"\n").getBytes(StandardCharsets.UTF_8)
    Files.createDirectories(destination.parent)
    Path pending=Files.createTempFile(destination.parent,".cell-correction-",".tmp")
    try {
        java.nio.channels.FileChannel.open(pending,java.nio.file.StandardOpenOption.WRITE).withCloseable {channel ->
            ByteBuffer buffer=ByteBuffer.wrap(bytes)
            while(buffer.hasRemaining())channel.write(buffer)
            channel.force(true)
        }
        Files.move(pending,destination,java.nio.file.StandardCopyOption.ATOMIC_MOVE,
            java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    } finally {Files.deleteIfExists(pending)}
}
try {
    def provider=NativeCellScope.open(image,scope.section_id as String).join()
    if(provider.geometryRevision!=(scope.geometry_revision as Number).longValue())
        throw new IllegalArgumentException("The saved cell edits changed after correction review")
    if(scope.cell_edits!=null)
        provider.selectEdits(image,Paths.get(scope.cell_edits.path as String),scope.cell_edits.sha256 as String)
    Map output=helper.correctKnownCells.call(image,scope,request.outline as Map,true)
    if(output.changes.isEmpty())throw new IllegalArgumentException("The correction contains no changed cells")
    Path checkpoint=provider.publishEdits(Paths.get(request.operational_output_dir as String)
        .resolve("batch/cell-corrections").resolve(envelope.invocation_id as String))
    output.cell_edits=[path:checkpoint.toString(),sha256:MifProjectStageBootstrapV1.mifSha256(Files.readAllBytes(checkpoint))]
    saveAttempted=true
    entry.saveImageData(image);project.syncChanges()
    Map manifest=[schema_version:"roi-generation-manifest/v1",invocation_id:envelope.invocation_id,
        run_id:envelope.run_id,stage:envelope.stage,slide_id:envelope.slide_id,
        generation_ids:[envelope.invocation_id],generations:[],object_outputs:output]
    write(manifestPath,manifest)
    Map result=[status:"completed",stage:envelope.stage,slide_id:envelope.slide_id,
        generation_id:envelope.invocation_id,interaction_reason:null,affected_object_ids:[],message:null,
        allowed_actions:[],resume_context:null,checkpoint_id:null,outputs:output,error:null]
    mifPublishProjectStageResult(resultPath,[schema_version:"roi-stage-result/v1",
        invocation_id:envelope.invocation_id,run_id:envelope.run_id,stage:envelope.stage,
        slide_id:envelope.slide_id,result:result,generation_manifest:[path:manifestPath.fileName.toString(),
            sha256:MifProjectStageBootstrapV1.mifSha256(Files.readAllBytes(manifestPath))]])
} catch(Exception failure) {
    try {
        if(previous==null)image.removeProperty("mif.cell_edits")
        else image.setProperty("mif.cell_edits",previous)
        NativeCellScope.close(image)
        if(saveAttempted){entry.saveImageData(image);project.syncChanges()}
    } catch(Exception rollback){failure.addSuppressed(rollback)}
    throw failure
} finally {NativeCellScope.close(image)}
