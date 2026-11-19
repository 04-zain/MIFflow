// Resolve only the known helper inventory from an already owner-selected immutable script bundle.
import java.nio.file.Path
import java.nio.file.Files
import java.nio.file.LinkOption
import java.security.MessageDigest

return [load:{Path bundle ->
    if(!bundle.isAbsolute() || bundle.toRealPath()!=bundle || Files.isSymbolicLink(bundle))
        throw new IOException("The KRONOS script bundle path is invalid")
    Path manifestPath=bundle.resolve("qupath-script-bundle.v1.json")
    if(!Files.isRegularFile(manifestPath,LinkOption.NOFOLLOW_LINKS) || Files.size(manifestPath)>1024*1024)
        throw new IOException("The KRONOS script manifest is unavailable")
    byte[] bytes=Files.readAllBytes(manifestPath)
    String hash=MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()
    if(bundle.fileName.toString()!="bundle-"+hash)throw new IOException("The script bundle manifest changed")
    Map manifest=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().fromJson(new String(bytes,java.nio.charset.StandardCharsets.UTF_8),Map.class)
    if(manifest.schema_version!="qupath-script-bundle/v1" || !(manifest.assets instanceof List))
        throw new IOException("The script bundle schema is unsupported")
    Map assets=[:]
    for(Map row:manifest.assets) {
        if(row.keySet()!=["path","sha256"] as Set || assets.put(row.path,row.sha256)!=null)
            throw new IOException("The script asset inventory is ambiguous")
    }
    Map sources=[:]
    for(String name:["patch_geometry","kronos_patch_operation","kronos_worker_client","kronos_patch_backend",
                     "kronos_model_codec","kronos_classifier_resource","kronos_marker_statistics","kronos_final_training","kronos_live_preview","prediction_display","native_prediction_display","annotation_tools","reference_first_guard","classifier_review_policy","classifier_review","native_annotation_preview","annotation_preview_workbench"]) {
        String relative=(name in ["prediction_display","native_prediction_display","annotation_tools","reference_first_guard"]?"general/":"compartment_analysis/")+name+".groovy"
        Path source=bundle.resolve(relative)
        if(!Files.isRegularFile(source,LinkOption.NOFOLLOW_LINKS) || source.toRealPath()!=source || Files.size(source)>1024*1024)
            throw new IOException("A required KRONOS helper is missing or linked: "+name)
        byte[] content=Files.readAllBytes(source)
        if(MessageDigest.getInstance("SHA-256").digest(content).encodeHex().toString()!=assets[relative])
            throw new IOException("A KRONOS helper differs from the registered bundle: "+name)
        sources[name]=new String(content,java.nio.charset.StandardCharsets.UTF_8)
    }
    GroovyShell shell=new GroovyShell(this.class.classLoader)
    Map loaded=[:]
    sources.each {name,content -> loaded[name]=shell.evaluate(content,name+".groovy")}
    Map result=[geometry:loaded.patch_geometry.geometry,operations:loaded.kronos_patch_operation,
        workers:loaded.kronos_worker_client,backend:loaded.kronos_patch_backend,codec:loaded.kronos_model_codec,
        resources:loaded.kronos_classifier_resource,statistics:loaded.kronos_marker_statistics,
        training:loaded.kronos_final_training,preview:loaded.kronos_live_preview]
    result.annotationPreview=loaded.annotation_preview_workbench
    result.nativeAnnotation=loaded.native_annotation_preview
    result.referenceFirst=loaded.reference_first_guard
    result.reviewPolicy=loaded.classifier_review_policy
    result.classifierReview=loaded.classifier_review
    result.displayPolicy=loaded.prediction_display
    result.nativeDisplay=loaded.native_prediction_display
    result.annotationTools=loaded.annotation_tools
    result.operations.annotationTools=loaded.annotation_tools
    result.operations.display=loaded.prediction_display.create
    result.resources.configure(result.geometry,result.operations,result.workers,result.backend,result.codec)
    return result
}]
