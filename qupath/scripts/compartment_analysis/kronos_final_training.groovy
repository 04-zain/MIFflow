// Final unique-patch fitting, called only after the existing native entry/evidence validator.
import java.nio.ByteBuffer
import java.nio.ByteOrder
import org.bytedeco.opencv.opencv_ml.SVM
import org.bytedeco.opencv.opencv_ml.TrainData
import org.mif.classification.OpenCvTraining
import qupath.lib.classifiers.Normalization
import qupath.lib.objects.classes.PathClass
import org.mif.inference.MifKronosNativeFit

final class MifKronosFinalTraining {
    static Map workerSettings(Map encoder) {
        [worker_command:encoder.worker_command,worker_release:encoder.worker_release,
         package_dir:encoder.package_directory,package_sha256:encoder.package_sha256,marker_names:encoder.marker_names,
         divisors:encoder.intensity_divisors,pixel_size_um:encoder.pixel_size_um,patch_size:encoder.patch_size,nuclear_channel:encoder.nuclear_channel,
         additional_markers:encoder.additional_markers,marker_text_model:encoder.marker_text_model,
         threads:encoder.threads,activation_budget_bytes:encoder.activation_budget_bytes]
    }
    static Map decodeGraph(Map result,Map settings,List classes) {
        if(result.header.kind!="graph_fit" || result.header.class_order!=classes)
            throw new IOException("The learned graph class order differs from the requested order")
        Map shapes=[self_weight:[768,settings.hidden_width],neighbour_weight:[768,settings.hidden_width],
            bias:[settings.hidden_width],head_weight:[settings.hidden_width,classes.size()],head_bias:[classes.size()]]
        if(result.header.arrays.collect {it.name}!=shapes.keySet().toList())throw new IOException("The graph parameter inventory differs")
        ByteBuffer buffer=ByteBuffer.wrap(result.body as byte[]).order(ByteOrder.LITTLE_ENDIAN)
        Map parameters=[:]
        for(Map item:result.header.arrays) {
            if(item.shape!=shapes[item.name])throw new IOException("The graph parameter dimensions differ")
            if(item.shape.size()==1) {
                float[] v=new float[item.shape[0] as int]
                for(int i=0;i<v.length;i++)v[i]=buffer.getFloat()
                parameters[item.name]=v
            } else {
                float[][] v=new float[item.shape[0] as int][item.shape[1] as int]
                for(int i=0;i<v.length;i++)for(int j=0;j<v[i].length;j++)v[i][j]=buffer.getFloat()
                parameters[item.name]=v
            }
        }
        if(buffer.hasRemaining())throw new IOException("The graph result contains undeclared values")
        [parameters:parameters,training_ids:result.header.training_node_ids,class_order:classes,
         label_sha256:result.header.label_assignments_sha256,loss_history:result.header.loss_history]
    }
    static Map prepare(Map request,List images,Map support) {
        Map config=request.model_configuration
        List<String> classes=List.copyOf(request.required_classes)
        List entries=request.training_entries
        if(entries.size()!=images.size() || entries.isEmpty() || entries.any { it.entry_state.split!="training" })
            throw new IllegalArgumentException("Final fitting requires exactly the declared training entries")
        List channels=request.feature_spec.ordered_channels
        if(channels.collect {it.canonical_channel_id}!=config.encoder.channel_ids)
            throw new IllegalArgumentException("The recorded channel and marker order differs")
        for(def image:images)for(Map channel:channels) {
            int index=(channel.qupath_channel_index as Number).intValue()
            if(index<0 || index>=image.server.nChannels() ||
                image.server.metadata.channels[index].name!=channel.observed_qupath_channel_name)
                throw new IOException("The training image channel name differs from the exact observation")
        }
        Map statistics=support.statistics.resolve(config,request,images)
        config=statistics.configuration
        Map nativeConfig=config.native_configuration
        Map worker=workerSettings(config.encoder)
        def client=support.workers.createTemporary(worker,"mif-kronos-final-fit-")
        List backends=[];
        List nodeRows=[];
        List<Map> featureSources=[];
        Map<String,String> labelRows=[:]
        Map<String,Map> targets=[:];
        Map<String,Integer> entryCounts=[:],classCounts=[:]
        Map<String,String> classNames=[:]
        MifKronosNativeFit.PreparedRows preparedRows=null
        try {
            entries.eachWithIndex { entry,int ei ->
                def image=images[ei];
                Map state=entry.entry_state
                String entryId=state.project_entry_id
                entryCounts[entryId]=0
                def calibration=image.server.pixelCalibration
                double px=calibration.pixelWidthMicrons,py=calibration.pixelHeightMicrons
                double ds=(config.encoder.pixel_size_um[0] as double)/px
                if(!Double.isFinite(ds) || ds<1 || !Double.isFinite(py) ||
                    Math.abs((config.encoder.pixel_size_um[1] as double)/py-ds)>1e-9)
                    throw new IOException("The training image cannot supply the recorded physical patch grid")
                for(Map channel:channels) {
                    if(image.server.metadata.channels[channel.qupath_channel_index as int].name!=channel.observed_qupath_channel_name)
                        throw new IOException("The training image channel name differs from the exact observation")
                }
                Map objects=image.hierarchy.annotationObjects.collectEntries {[(it.ID.toString()):it]}
                Map grouped=entry.evidence_rows.groupBy { [it.analysis_domain_id,it.analysis_domain_revision_sha256,it.material_parent_id] }
                for(def group:grouped.entrySet()) {
                    List rows=group.value
                    def parent=objects[rows[0].material_parent_id]
                    if(parent==null || parent.ROI==null || !parent.ROI.isArea())throw new IOException("The accepted training parent is absent")
                    List components=[]
                    def geometry=parent.ROI.geometry
                    for(int ci=0;ci<geometry.numGeometries;ci++)components.add(geometry.getGeometryN(ci))
                    components.sort {it.toText()}
                    components.eachWithIndex { component,int ci ->
                        Map identity=[patient_id:state.subject_id,image_id:state.image_identity_sha256,entry_id:entryId,split:"training",
                            fold_id:request.classifier_id,domain_id:rows[0].analysis_domain_id,domain_revision:rows[0].analysis_domain_revision_sha256,
                            tissue_component_id:parent.ID.toString()+":"+ci,eligible_parent_id:parent.ID.toString()]
                        def backend=support.backend.backend(image,component,ds,channels.collect {(it.qupath_channel_index as Number).intValue()},
                            identity,support.geometry,support.operations,client)
                        backends.add(backend)
                        List annotations=rows.collect { row ->
                            def object=objects[row.annotation_uuid]
                            if(object==null || object.parent!=parent || object.getClassification()!=row.path_class_id)
                                throw new IOException("A training annotation no longer matches the accepted entry")
                            String previous=classNames.putIfAbsent(row.class_id,row.path_class_id)
                            if(previous!=null && previous!=row.path_class_id)throw new IOException("A class has inconsistent PathClass names across subjects")
                            [id:row.annotation_uuid,label:row.class_id,geometry:object.ROI.geometry.copy()]
                        }
                        Map data=backend.trainingData(annotations,classes,false)
                        String prefix=entryId+"|"+rows[0].analysis_domain_id+"|"+parent.ID+"|"+ci+"|"
                        for(Map node:data.nodes) {
                            String id=prefix+node.node_id
                            nodeRows.add(node+[node_id:id]);
                            featureSources.add([backend:backend,row:node.grid_row,col:node.grid_col])
                            if(data.labels.containsKey(node.node_id)) {
                                labelRows[id]=data.labels[node.node_id]
                                targets[id]=[backend:backend,row:node.grid_row,col:node.grid_col,entry_id:entryId]
                                entryCounts[entryId]++
                            }
                        }
                        if(labelRows.size()>4096 || nodeRows.size()*768L*4>64L*1024*1024)
                            throw new IOException("The selected training annotations exceed the bounded patch-fit budget; reduce annotation coverage or choose a coarser scale")
                    }
                }
            }
            if(entryCounts.values().any {it<=0})throw new IOException("Every selected training image must contain at least one fully labelled supported patch")
            if(labelRows.values().toSet()!=classes.toSet())throw new IOException("The combined training annotations do not cover every selected class")
            Map result=client.trainGraph([nodes:nodeRows,labels:labelRows,class_order:classes,feature_width:768,settings:config.graph],{int index ->
                Map source=featureSources[index]
                source.backend.featureSource(source.row as int,source.col as int)
            })
            Map fitted=decodeGraph(result,config.graph,classes)
            List ids=labelRows.keySet().toList().sort()
            int hidden=(config.graph.hidden_width as Number).intValue()
            Map providers=backends.collectEntries {[(it):it.provider(fitted)]}
            try {
                List<MifKronosNativeFit.TrainingRow> trainingRows=ids.collect {String id ->
                    Map target=targets[id]
                    String label=labelRows[id]
                    classCounts[label]=(classCounts[label] ?: 0)+1
                    new MifKronosNativeFit.TrainingRow(providers[target.backend],
                        target.row as int,target.col as int,classes.indexOf(label))
                }
                def normalization=[none:Normalization.NONE,mean_variance:Normalization.MEAN_VARIANCE,min_max:Normalization.MIN_MAX][nativeConfig.normalization]
                if(normalization==null)throw new IOException("The native feature normalization is invalid")
                preparedRows=MifKronosNativeFit.prepareRows(trainingRows,hidden,normalization)
            } finally { providers.values().each {it.close()} }
            def preprocessor=preparedRows.preprocessor
            TrainData trainData=preparedRows.trainData
            Map convention=[package_sha256:config.encoder.package_sha256,panel_sha256:client.ready.panel_sha256,
                marker_names:config.encoder.marker_names,intensity_divisors:config.encoder.intensity_divisors,
                pixel_size_um:config.encoder.pixel_size_um,nuclear_channel:config.encoder.nuclear_channel,
                patch_size:config.encoder.patch_size,feature_width:768,graph_settings:config.graph]
            List outputRows=nativeConfig.output_labels.ordered_labels
            List outputCodes=classes.collect {id -> outputRows.find {it.canonical_class_id==id}.qupath_label}
            Map labelMap=outputRows.collectEntries {[(it.qupath_label as int):PathClass.fromString(classNames[it.canonical_class_id])]}+[(255):PathClass.NULL_CLASS]
            Map reviewPolicy=[schema:"reference-first-preview/v1",
                training_entry_ids:request.training_entries.collect {it.entry_state.project_entry_id}.sort(),
                path_classes:classes.collect {classNames[it]},
                parents_by_entry:request.training_entries.collectEntries {entry ->
                    List parents=entry.evidence_rows.collect {row ->
                        [path_object_id:row.material_parent_id,domain_id:row.analysis_domain_id,
                         domain_revision:row.analysis_domain_revision_sha256]
                    }.unique {it.path_object_id}.sort {it.path_object_id}
                    [(entry.entry_state.project_entry_id):parents]
                }]
            def ownedRows=preparedRows
            return [sample_count:ids.size(),feature_count:hidden,entry_counts:entryCounts,class_counts:new TreeMap(classCounts),
                configuration:config,marker_statistics_receipt:statistics.receipt,
                graph_nodes:nodeRows,train_data:trainData,convention:convention,fitted:fitted,
                classifier:{Object rawModel ->
                    String method=config.configuration_schema=="qupath-kronos-svm/v2"?"svm":"rf"
                    byte[] payload=support.codec.encode(fitted,rawModel,preprocessor,method,convention)
                    support.resources.create(payload,convention,worker,request.feature_spec,outputCodes,labelMap,reviewPolicy)
                },
                close:{ownedRows.close();
                backends.each {it.close()};
                client.close()}]
        } catch(Throwable error) {
            preparedRows?.close();
            backends.each {it.close()};
            client.close();
            throw error
        }
    }

    /** Fit the selected native head; its caller retains data and model ownership. */
    static Object fitNative(Map config,TrainData data,int seed) {
        if(config.configuration_schema=="qupath-pixel-rtrees/v1") {
            Map p=config.rtrees;
            return OpenCvTraining.fitForest(data,new OpenCvTraining.ForestSettings(
                p.max_depth as int,p.min_sample_count as int,p.use_1se_rule as boolean,
                p.active_var_count as int,p.calculate_variable_importance as boolean,
                p.cv_folds as int,p.use_surrogates as boolean,p.max_trees as int,
                p.termination_epsilon as double),seed)
        } else {
            Map p=config.svm;
            return OpenCvTraining.fitSvm(data,new OpenCvTraining.SvmSettings(
                p.kernel=="LINEAR" ? SVM.LINEAR : SVM.RBF,p.c as double,
                p.kernel=="RBF" ? p.gamma as double : 0d,p.max_iterations as int,
                p.termination_epsilon as double,false),seed)
        }
    }
}
return [workerSettings:MifKronosFinalTraining.&workerSettings,prepare:{Map request,List images,Map support -> MifKronosFinalTraining.prepare(request,images,support)},
        fitNative:{Map config,TrainData data,int seed -> MifKronosFinalTraining.fitNative(config,data,seed)}]
