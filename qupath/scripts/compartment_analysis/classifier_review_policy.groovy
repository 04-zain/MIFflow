// Post-fit review provenance is recorded inside the newly reserved native model file.
// This does not wrap or refit its numerical classifier. Existing model files are never retagged.
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.LinkOption
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.nio.channels.FileChannel
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.DigestInputStream
import java.io.BufferedReader
import java.io.InputStreamReader
import java.nio.ByteBuffer
import com.google.gson.GsonBuilder
import com.google.gson.JsonParser
import com.google.gson.JsonObject
import com.google.gson.JsonElement
import com.google.gson.JsonArray
import com.google.gson.ToNumberPolicy
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.io.ByteOrderValues
import qupath.lib.io.GsonTools
import qupath.lib.images.servers.ImageServerBuilder
import qupath.lib.classifiers.pixel.PixelClassifier

final class MifClassifierReviewPolicy {
    static final String KEY="mif_review_scope"
    static final def GSON=new GsonBuilder().serializeNulls().disableHtmlEscaping().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
    static void require(boolean valid,String message){if(!valid)throw new IllegalArgumentException(message)}
    static JsonElement sorted(JsonElement value) {
        if(value.isJsonObject()) {
            JsonObject result=new JsonObject()
            value.asJsonObject.keySet().toList().sort().each {key->result.add(key,sorted(value.asJsonObject.get(key)))}
            return result
        }
        if(value.isJsonArray()){JsonArray result=new JsonArray();
            value.asJsonArray.each {result.add(sorted(it))};
            return result}
        value.deepCopy()
    }
    static String digest(byte[] value){MessageDigest.getInstance("SHA-256").digest(value).encodeHex().toString()}
    static String signature(Object value){digest(GSON.toJson(sorted(GSON.toJsonTree(value))).getBytes(StandardCharsets.UTF_8))}
    static Map shape(object) {
        def roi=object.ROI
        require(roi!=null && roi.isArea() && !roi.isEmpty() && roi.geometry.isValid(),"A recorded review region is missing or invalid.")
        def geometry=roi.geometry.copy();
        geometry.normalize()
        [id:object.ID.toString(),parent_id:object.parent?.ID?.toString(),path_class:object.pathClass?.toString(),
         geometry_sha256:digest(new WKBWriter(2,ByteOrderValues.LITTLE_ENDIAN,false).write(geometry)),
         z:roi.z,t:roi.t,c:roi.c]
    }
    static Map imageSignature(image) {
        def server=image.server
        def number={v->Double.isFinite(v as double)?v:null}
        String builder=GsonTools.getInstance().toJson(server.builder,ImageServerBuilder.ServerBuilder.class)
        [width:server.width,height:server.height,z:server.nZSlices(),t:server.nTimepoints(),
         pixel_type:server.pixelType.toString(),channels:server.metadata.channels.collect {it.name},
         pixel_width_um:number(server.pixelCalibration.pixelWidthMicrons),pixel_height_um:number(server.pixelCalibration.pixelHeightMicrons),
         builder_sha256:signature(JsonParser.parseString(builder)),
         source_files:server.URIs.collect {uri->
             if(uri.scheme!="file")throw new IllegalArgumentException("Interactive review requires the recorded local or mounted image.")
             Path path=Path.of(uri);
            def attributes=Files.readAttributes(path,java.nio.file.attribute.BasicFileAttributes.class)
             [uri:path.toAbsolutePath().normalize().toUri().toString(),size:attributes.size(),modified:attributes.lastModifiedTime().toString()]
         }]
    }
    static boolean inside(object,domains) {
        if(object.ROI==null || !object.ROI.isArea())return false
        domains.any {parent->object.ROI.z==parent.ROI.z && object.ROI.t==parent.ROI.t && object.ROI.geometry.coveredBy(parent.ROI.geometry)}
    }
    static List<Map> references(image,List names,List parents,List forcedIds=[]) {
        image.hierarchy.annotationObjects.findAll {object->
            forcedIds.contains(object.ID.toString()) ||
            (object.metadata.get("pipeline_stage")==null && names.contains(object.pathClass?.toString()) && inside(object,parents))
        }.collect {shape(it)}.sort {it.id}
    }
    static String domainSignature(Map value) {
        // Analysis-domain identities use Python's canonical ASCII JSON, including
        // escaped UTF-16 pairs for non-ASCII sample and image identifiers.
        String json=GSON.toJson(sorted(GSON.toJsonTree(value)))
        StringBuilder ascii=new StringBuilder()
        for(int i=0;i<json.length();i++) {
            int code=json.charAt(i)
            ascii.append(code>=127 ? String.format("\\u%04x",code) : Character.toString((char)code))
        }
        digest(ascii.toString().getBytes(StandardCharsets.US_ASCII))
    }
    static String wholeTissueDomainId(object) {
        Map m=object.metadata
        if(m.pipeline_stage!="tissue_detection" || m.generation_status!="accepted" ||
            m.review_status!="accepted" || !object.isLocked() ||
            ["canonical_image_id","canonical_image_sha256","section_id","generation_id","roi_id","slide_id"].any {key->
                !(m[key] instanceof String) || m[key].isBlank()})return null
        domainSignature([canonical_image_sha256:m.canonical_image_sha256,
            kind:"whole_tissue_section",schema_version:"analysis-domain/v1",section_id:m.section_id,
            source_path_object_id:object.ID.toString(),tissue_generation_id:m.generation_id])
    }
    static boolean wholeTissueDomain(Map binding,Map state,object) {
        Map m=object.metadata
        String domainId=wholeTissueDomainId(object)
        if(domainId==null || domainId!=binding.analysis_domain_id || binding.analysis_domain_revision!=1 ||
            m.canonical_image_sha256!=state.image_identity_sha256 || m.slide_id!=binding.slide_id)return false
        String id=object.ID.toString()
        String revision=domainSignature([analysis_domain_id:domainId,canonical_image_id:m.canonical_image_id,
            canonical_image_sha256:m.canonical_image_sha256,geometry_digest_policy:"jts-normalized-wkb-sha256/v1",
            geometry_reference_path_object_id:id,geometry_sha256:null,kind:"whole_tissue_section",
            predecessor_sha256:null,project_entry_id:state.project_entry_id,revision:1,section_id:m.section_id,
            slide_id:m.slide_id,source_path_object_id:id,tissue_generation_id:m.generation_id,
            tissue_path_object_id:id,tissue_roi_id:m.roi_id])
        revision==binding.analysis_domain_revision_sha256
    }
    static List domainParents(Map request,Map entry,image) {
        String entryId=entry.entry_state.project_entry_id
        List objects=image.hierarchy.annotationObjects.toList()
        List domains=[]
        List bindings=request.training_analysis_domain_bindings.findAll {it.project_entry_id==entryId}
        require(!bindings.isEmpty(),"The fitted image has no recorded analysis domain.")
        for(Map binding:bindings) {
            List selected=objects.findAll {object->
                Map m=object.metadata
                m.pipeline_stage=="analysis_domain_selection" && m.analysis_domain_id==binding.analysis_domain_id &&
                m.analysis_domain_status=="accepted" && m.analysis_domain_active=="true" &&
                m.analysis_domain_revision_sha256==binding.analysis_domain_revision_sha256 &&
                m.analysis_domain_set_sha256==binding.domain_set_sha256 && object.isLocked()
            }
            if(selected.isEmpty()) {
                // Whole-tissue domains use the TissueSection itself. They need
                // not contain training examples: match their exact accepted
                // identity and revision, independently of reference ancestry.
                selected=objects.findAll {wholeTissueDomain(binding,entry.entry_state,it)}
            }
            require(selected.size()==1,"A selected analysis domain is missing, changed or ambiguous. Refit the classifier from the current reference set.")
            shape(selected.first());
            domains.add(selected.first())
        }
        if(request.classifier_kind=="primary")return domains.unique {it.ID.toString()}
        require(request.classifier_kind=="secondary","Only primary and secondary classifiers have this review scope.")
        // Secondary material IDs name actual biological parents or AnalysisDomain.
        List names=request.eligible_material_parent_type_ids
        List parents=objects.findAll {object->
            Map m=object.metadata
            String id=m.pipeline_stage=="primary_classifier"?m.primary_class_id:m.pipeline_stage=="secondary_classifier"?m.structure_id:null
            m.pipeline_active=="true" && id!=null && names.contains(id) && object.isLocked() &&
            bindings.any {binding->m.analysis_domain_id==binding.analysis_domain_id && m.analysis_domain_revision_sha256==binding.analysis_domain_revision_sha256 && m.domain_set_sha256==binding.domain_set_sha256} &&
            inside(object,domains)
        }
        if(names.contains("AnalysisDomain"))parents.addAll(domains)
        require(!parents.isEmpty(),"No current eligible parent regions are available for this secondary classifier.")
        parents.unique {it.ID.toString()}
    }
    static Map capture(Map request,List images) {
        // The authenticated role determines review kind; parent names never do.
        String kind=[primary_compartment:"primary",secondary_structure:"secondary"][request.classifier_role]
        require(request.classifier_kind=="pixel" && kind!=null,"The model is not a primary or secondary classifier.")
        request=request+[classifier_kind:kind]
        require(request.training_entries.size()==images.size() && !images.isEmpty(),"The fitting image inventory differs from the review scope.")
        Map entries=[:]
        request.training_entries.eachWithIndex {entry,int index->
            require(entry.entry_state.split=="training","Only development images can receive pre-test model feedback.")
            def image=images[index];
            List parents=domainParents(request,entry,image)
            List names=request.training_entries.collectMany {it.evidence_rows.collect {row->row.path_class_id}}.unique().sort()
            List refs=references(image,names,parents,entry.evidence_rows.collect {it.annotation_uuid})
            require(!refs.isEmpty(),"The fitted classifier has no reference annotations.")
            entries[entry.entry_state.project_entry_id]=[image:imageSignature(image),image_identity_sha256:entry.entry_state.image_identity_sha256,
                path_classes:names,reference_ids:entry.evidence_rows.collect {it.annotation_uuid}.unique().sort(),
                parents:parents.collect {object->[shape:shape(object),metadata_sha256:signature(new TreeMap(object.metadata))]}.sort {it.shape.id},references:refs]
        }
        [schema:"mif-reference-first-review/v1",fit_execution_sha256:request.fit_execution_sha256,
         classifier_id:request.classifier_id,classifier_kind:request.classifier_kind,entries:new TreeMap(entries)]
    }
    static def modelAttributes(Path path) {
        def attributes=Files.readAttributes(path,java.nio.file.attribute.BasicFileAttributes.class,LinkOption.NOFOLLOW_LINKS)
        require(attributes.isRegularFile() && !attributes.isSymbolicLink(),"The saved classifier is unavailable.")
        attributes
    }
    static void unchanged(Path path,def before) {
        def after=modelAttributes(path)
        require(after.size()==before.size() && after.fileKey()==before.fileKey() &&
            after.lastModifiedTime()==before.lastModifiedTime(),"The saved classifier changed while it was being read.")
    }
    /** Read only review metadata; numerical arrays are skipped by the streaming parser. */
    static Map scan(Path path,boolean hashFile=false) {
        def before=modelAttributes(path)
        Map result=[model_path:path,attributes:before,byte_count:before.size(),has_scope:false,kronos:false,members:0]
        MessageDigest hash=hashFile ? MessageDigest.getInstance("SHA-256") : null
        Files.newInputStream(path,LinkOption.NOFOLLOW_LINKS).withCloseable {input->
            def stream=hash==null ? input : new DigestInputStream(input,hash)
            new JsonReader(new BufferedReader(new InputStreamReader(stream,StandardCharsets.UTF_8))).withCloseable {reader->
                require(reader.peek()==JsonToken.BEGIN_OBJECT,"The saved classifier file is invalid.")
                reader.beginObject()
                while(reader.hasNext()) {
                    String key=reader.nextName();result.members++
                    if(key==KEY) {
                        require(!result.has_scope,"The classifier contains duplicate review scopes.")
                        result.has_scope=true
                        result.scope=GSON.fromJson(reader,Map.class)
                    } else if(key=="op" && reader.peek()==JsonToken.BEGIN_OBJECT) {
                        // Saved KRONOS producers install this final ImageDataOp directly;
                        // appendOps rejects wrappers. Preview-only operations are not saved fits.
                        reader.beginObject()
                        while(reader.hasNext()) {
                            String field=reader.nextName()
                            if(field=="type" && reader.peek()==JsonToken.STRING)
                                result.kronos=reader.nextString()=="data.op.mif-kronos-native-v1"
                            else reader.skipValue()
                        }
                        reader.endObject()
                    } else reader.skipValue()
                }
                reader.endObject()
                require(reader.peek()==JsonToken.END_DOCUMENT,"The saved classifier contains trailing JSON.")
            }
        }
        unchanged(path,before)
        if(hash!=null)result.sha256=hash.digest().encodeHex().toString()
        result
    }
    static void transfer(FileChannel source,FileChannel target,long start,long count) {
        while(count>0) {
            long copied=source.transferTo(start,count,target)
            require(copied>0,"The saved classifier could not be copied.")
            start+=copied;count-=copied
        }
    }
    static void attach(Path path,Map scope) {
        Map source=scan(path)
        require(!source.has_scope,"The classifier already contains a recorded review scope; it cannot be replaced.")
        byte[] encoded=((source.members ? "," : "")+GSON.toJson(KEY)+":"+GSON.toJson(scope)+"}").getBytes(StandardCharsets.UTF_8)
        Path temporary=Files.createTempFile(path.parent,".model-review-", ".json")
        try {
            FileChannel.open(path,StandardOpenOption.READ,LinkOption.NOFOLLOW_LINKS).withCloseable {input->
                long closing=source.byte_count-1
                ByteBuffer one=ByteBuffer.allocate(1)
                while(closing>=0) {
                    one.clear();require(input.read(one,closing)==1,"The saved classifier changed while copying.")
                    int value=one.array()[0]&255
                    if(!(value in [9,10,13,32])){require(value==125,"The saved classifier file is invalid.");break}
                    closing--
                }
                require(closing>=0,"The saved classifier file is empty.")
                FileChannel.open(temporary,StandardOpenOption.WRITE).withCloseable {output->
                    transfer(input,output,0,closing)
                    ByteBuffer addition=ByteBuffer.wrap(encoded)
                    while(addition.hasRemaining())output.write(addition)
                    transfer(input,output,closing+1,source.byte_count-closing-1)
                    output.force(true)
                }
            }
            unchanged(path,source.attributes)
            Files.move(temporary,path,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING)
        }finally{Files.deleteIfExists(temporary)}
    }
    static Map read(Path path) {snapshot(path).scope}
    static Map snapshot(Path path,boolean hashFile=false) {
        Map result=scan(path,hashFile)
        require(result.has_scope,"This model has no recorded reference-first review. Fit it through the classifier step before opening predictions.")
        Map scope=result.scope
        Set required=["schema","fit_execution_sha256","classifier_id","classifier_kind","entries"] as Set
        require(scope!=null && scope.keySet().containsAll(required) &&
            (scope.keySet()-required-["fold_resources","model_sha256"]).isEmpty() &&
            scope.schema=="mif-reference-first-review/v1" && scope.classifier_kind in ["primary","secondary"],
            "The classifier has an invalid recorded reference scope.")
        result
    }
    static PixelClassifier loadClassifier(Map snapshot) {
        Path path=snapshot.model_path
        unchanged(path,snapshot.attributes)
        PixelClassifier classifier=Files.newInputStream(path,LinkOption.NOFOLLOW_LINKS).withCloseable {input->
            new BufferedReader(new InputStreamReader(input,StandardCharsets.UTF_8)).withCloseable {reader->
                GsonTools.getInstance().fromJson(reader,PixelClassifier.class)
            }
        }
        unchanged(path,snapshot.attributes)
        classifier
    }
    /** Select the original held-out model; never fall back to the final refit. */
    static Map foldSnapshot(Path finalModel,Map reference) {
        Map deployed=snapshot(finalModel)
        Map index=deployed.scope.fold_resources
        require(index!=null && index.schema=="mif-cv-fold-resources/v1" &&
            index.fit_execution_sha256==deployed.scope.fit_execution_sha256 &&
            reference.classifier_id==deployed.scope.classifier_id && reference.split=="cross_validation" &&
            reference.candidate instanceof Number && reference.fold instanceof Number &&
            reference.candidate==index.selected_candidate_index,
            "The original cross-validation fold model is not available for this reference.")
        int fold=(reference.fold as Number).intValue()
        require(reference.fold==fold,"The selected cross-validation fold is invalid.")
        List matches=index.models.findAll {it.fold==fold}
        require(matches.size()==1 && matches.first().held_out_subject_ids.contains(reference.subject_id),
            "This subject does not belong to the selected held-out fold.")
        Map resource=matches.first()
        String expected="classifiers/cv_review/mif-${index.fit_execution_sha256}-fold-${fold}.json"
        require(resource.project_relative_path==expected,"The fold model resource path differs from its recorded fit.")
        Path root=finalModel.parent.parent.parent
        Path path=root.resolve(expected)
        require(!Files.isSymbolicLink(path.parent) && !Files.isSymbolicLink(path.parent.parent),
            "The fold review resource directory is unsafe.")
        Map selected=snapshot(path,true)
        require(selected.byte_count==resource.byte_count && selected.sha256==resource.sha256 &&
            selected.scope.fit_execution_sha256==deployed.scope.fit_execution_sha256 &&
            selected.scope.classifier_id==deployed.scope.classifier_id &&
            selected.scope.classifier_kind==deployed.scope.classifier_kind &&
            selected.scope.entries.containsKey(reference.project_entry_id) &&
            selected.scope.entries[reference.project_entry_id].reference_ids.contains(reference.annotation_uuid) &&
            selected.scope.entries[reference.project_entry_id].references.any {
                it.id==reference.annotation_uuid && it.path_class==reference.reference_class},
            "The saved fold classifier or its original held-out reference scope changed.")
        selected
    }
    /** Open the same saved model on a reference from completed validation or testing.
     * The recorded review request identifies the model and annotation; the parent
     * supplies the original spatial context needed by a KRONOS graph prediction.
     */
    static List evaluationScope(Map record,image,String entryId,Map reference) {
        require(reference.project_entry_id==entryId && reference.classifier_id==record.classifier_id &&
            reference.split in ["validation","test"],"Select a reference from this classifier's recorded evaluation.")
        def object=image.hierarchy.annotationObjects.find {it.ID.toString()==reference.annotation_uuid}
        require(object!=null && object.ROI!=null && object.ROI.isArea() &&
            object.pathClass?.toString()==reference.reference_class,
            "The evaluated reference is unavailable or its label has changed. Return to the classifier step to review the references.")
        def parent=object.parent
        require(parent?.ROI!=null && parent.ROI.isArea() &&
            object.ROI.z==parent.ROI.z && object.ROI.t==parent.ROI.t &&
            object.ROI.geometry.coveredBy(parent.ROI.geometry),
            "The evaluated reference no longer lies inside its analysis region. Review the analysis region before continuing.")
        [[id:parent.ID.toString(),geometry:parent.ROI.geometry.copy(),
          revision:parent.metadata.analysis_domain_revision_sha256 ?: shape(parent).geometry_sha256,
          split:reference.split,z:parent.ROI.z,t:parent.ROI.t]]
    }
    static List validate(Map record,image,String entryId) {
        Map expected=record.entries[entryId]
        require(expected!=null,"This image is not in the recorded development set. Reserved test images are reviewed through the independent evaluation step.")
        require(signature(imageSignature(image))==signature(expected.image),"The current image or its channel/calibration settings differ from the fitted image.")
        Map objects=image.hierarchy.annotationObjects.collectEntries {[(it.ID.toString()):it]}
        List parents=expected.parents.collect {row->
            def object=objects[row.shape.id]
            require(object!=null && signature(shape(object))==signature(row.shape) && signature(new TreeMap(object.metadata))==row.metadata_sha256,
                "A recorded tissue or parent region changed. Return to the pipeline before reviewing this model.")
            object
        }
        require(signature(references(image,expected.path_classes,parents,expected.reference_ids))==signature(expected.references),
            "The reference annotations changed after fitting. Return to the classifier step to record a new reference set and refit.")
        parents.collect {object->[id:object.ID.toString(),geometry:object.ROI.geometry.copy(),revision:signature(shape(object)),split:"training",z:object.ROI.z,t:object.ROI.t]}
    }
}
return [capture:MifClassifierReviewPolicy.&capture,attach:MifClassifierReviewPolicy.&attach,loadClassifier:MifClassifierReviewPolicy.&loadClassifier,
        wholeTissueDomainId:MifClassifierReviewPolicy.&wholeTissueDomainId,
        read:MifClassifierReviewPolicy.&read,foldSnapshot:MifClassifierReviewPolicy.&foldSnapshot,evaluationScope:MifClassifierReviewPolicy.&evaluationScope,validate:MifClassifierReviewPolicy.&validate,snapshot:MifClassifierReviewPolicy.&snapshot]
