// Numerical KRONOS pipeline over one explicitly supplied training image and domain.
// This module never publishes a batch classifier or changes source images/annotations.
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.awt.image.BufferedImage
import org.locationtech.jts.geom.Geometry
import org.bytedeco.opencv.opencv_ml.SVM
import org.bytedeco.opencv.opencv_ml.TrainData
import org.mif.classification.OpenCvTraining
import qupath.lib.classifiers.Normalization
import qupath.lib.images.ImageData
import qupath.lib.regions.RegionRequest
import org.mif.inference.MifKronosNativeFit
import org.mif.inference.FrozenFeatureCache

final class MifKronosPatchBackend implements AutoCloseable {
    final ImageData<BufferedImage> image
    final Geometry scope
    final double downsample
    final int patchSize
    final List<Integer> channelIndices
    final Map scopeIdentity
    final Object geometry
    final Map operations
    final Object client
    private final long featureCacheOwner=FrozenFeatureCache.newOwner()
    private final Map featureIdentity
    private final int gridRows,gridColumns
    long encodedPatchCount=0
    private boolean closed=false

    MifKronosPatchBackend(ImageData<BufferedImage> image,Geometry scope,double ds,List channels,
                          Map identity,Object geometry,Map operations,Object client) {
        int side=(client.ready.patch_size as Number).intValue()
        if(image==null || scope==null || scope.isEmpty() || !scope.isValid() || ds<1 || !Double.isFinite(ds)
                || Math.abs(side*ds-Math.rint(side*ds))>1e-9)
            throw new IllegalArgumentException("The patch grid needs a valid domain and exactly representable input footprints")
        if(channels.isEmpty() || channels.size()>32 || channels.toSet().size()!=channels.size() ||
                channels.any { !(it instanceof Integer) || it<0 || it>=image.getServer().nChannels() })
            throw new IllegalArgumentException("Select distinct, existing channel positions in explicit marker order")
        Set required=["patient_id","image_id","entry_id","split","fold_id","domain_id","domain_revision",
                      "tissue_component_id","eligible_parent_id"] as Set
        if(identity.keySet()!=required || identity.values().any { !(it instanceof String) || !it.trim() } || !(identity.split in ["training","validation","test","inference"]))
            throw new IllegalArgumentException("The patch backend requires an explicit image/domain/split scope")
        this.image=image;
        this.scope=scope.copy();
        downsample=ds;
        patchSize=side;
        channelIndices=List.copyOf(channels)
        scopeIdentity=Map.copyOf(identity);
        this.geometry=geometry;
        this.operations=operations;
        this.client=client
        // The prepared image generation is immutable. Labels, subjects, domains
        // and fitted graph parameters do not alter its frozen patch embeddings.
        def uris=image.server.getURIs().toList()
        if(uris.size()!=1 || uris.first().scheme!="file")
            throw new IOException("Frozen patch reuse requires the selected local prepared image generation")
        if(!(image.server instanceof org.mif.image.ProcessedImageServer))
            throw new IOException("Frozen patch reuse requires our prepared image server")
        featureIdentity=Collections.unmodifiableMap([image_uri:uris.first().toString(),
            image_metadata_sha256:image.server.getCompletedGenerationSha256(),
            width:image.server.width,height:image.server.height,downsample:ds,patch_size:patchSize,
            pixel_size_um:[image.server.pixelCalibration.pixelWidthMicrons,image.server.pixelCalibration.pixelHeightMicrons],
            channel_indices:channelIndices,package_sha256:client.ready.package_sha256,
            model_sha256:client.ready.model_sha256,panel:client.ready.panel])
        gridRows=(int)Math.ceil(image.server.height/(patchSize*ds))
        gridColumns=(int)Math.ceil(image.server.width/(patchSize*ds))
    }

    private String key(int row,int col) { row+":"+col }

    synchronized float[] embedding(int row,int col) {
        if(closed) throw new IOException("The KRONOS image backend is closed")
        if(!geometry.admitted(scope,image.getServer().getWidth(),image.getServer().getHeight(),row,col,downsample,patchSize))
            throw new IOException("The requested embedding patch is not in the supported image/domain")
        float[] cached=FrozenFeatureCache.get(featureCacheOwner,row,col)
        if(cached!=null) return cached
        float[] stored=client.withPatchTable(featureIdentity,gridRows,gridColumns) {table -> table.read(row,col)}
        if(stored!=null) {
            FrozenFeatureCache.put(featureCacheOwner,row,col,stored)
            return stored
        }
        def envelope=geometry.footprint(row,col,downsample,patchSize).getEnvelopeInternal()
        def request=RegionRequest.createInstance(image.getServer().getPath(),downsample,
            (int)Math.round(envelope.getMinX()),(int)Math.round(envelope.getMinY()),
            (int)Math.round(envelope.getWidth()),(int)Math.round(envelope.getHeight()))
        float[] values=org.mif.inference.MifKronosPatchProvider.readPatch(image,request,channelIndices,patchSize)
        float[] encoded=client.encode(values,channelIndices.size())[0]
        encodedPatchCount++
        encoded=client.withPatchTable(featureIdentity,gridRows,gridColumns) {table -> table.write(row,col,encoded)}
        FrozenFeatureCache.put(featureCacheOwner,row,col,encoded)
        return encoded
    }

    /** Resolve one canonical row after admitting and preparing its image patch. */
    synchronized Map featureSource(int row,int col) {
        embedding(row,col)
        client.withPatchTable(featureIdentity,gridRows,gridColumns) {table -> table.reference(row,col)}
    }

    Map trainingData(List<Map> annotations,List<String> classOrder,boolean requireAllClasses=true) {
        if(scopeIdentity.split!="training") throw new IllegalArgumentException("Held-out images cannot supply training labels")
        if(classOrder.size()<2 || classOrder.size()>254 || classOrder.toSet().size()!=classOrder.size())
            throw new IllegalArgumentException("Declare distinct class names before graph training")
        if(annotations.isEmpty() || annotations.size()>10000)
            throw new IllegalArgumentException("Training requires a bounded set of explicit annotations")
        List<Map> selected=annotations.collect { row ->
            if(!(row.geometry instanceof Geometry) || !classOrder.contains(row.label))
                throw new IllegalArgumentException("A training annotation has an unselected class or invalid geometry")
            [id:row.id,label:row.label,geometry:(row.geometry as Geometry).intersection(scope)]
        }
        Set<String> candidates=new LinkedHashSet<>()
        for(Map annotation:selected) {
            def bounds=annotation.geometry.getEnvelopeInternal()
            if(annotation.geometry.isEmpty()) continue
            int firstRow=Math.max(0,(int)Math.floor(bounds.getMinY()/(patchSize*downsample)))
            int lastRow=(int)Math.ceil(bounds.getMaxY()/(patchSize*downsample))
            int firstCol=Math.max(0,(int)Math.floor(bounds.getMinX()/(patchSize*downsample)))
            int lastCol=(int)Math.ceil(bounds.getMaxX()/(patchSize*downsample))
            if(((long)lastRow-firstRow)*(lastCol-firstCol)>4096)
                throw new IllegalArgumentException("The annotated area exceeds the 4096-patch preview bound; reduce the training area or use a coarser recorded scale")
            for(int row=firstRow;row<lastRow;row++)for(int col=firstCol;col<lastCol;col++) {
                candidates.add(key(row,col))
                if(candidates.size()>4096) throw new IllegalArgumentException("Preview training exceeds 4096 candidate patches")
            }
        }
        Map<String,String> labels=new LinkedHashMap<>()
        Map<String,List> annotationIds=new LinkedHashMap<>()
        Set<String> context=new LinkedHashSet<>()
        for(String id:candidates) {
            def position=id.split(":").collect { Integer.parseInt(it) }
            int row=position[0],col=position[1]
            if(!geometry.admitted(scope,image.getServer().getWidth(),image.getServer().getHeight(),row,col,downsample,patchSize)) continue
            Map result=geometry.labelPatch(geometry.footprint(row,col,downsample,patchSize),selected)
            if(result.label==null) continue
            labels[id]=result.label;
            annotationIds[id]=result.annotation_ids;
            context.add(id)
            for(List neighbor:geometry.neighbors(scope,image.getServer().getWidth(),image.getServer().getHeight(),row,col,downsample,patchSize))
                context.add(key(neighbor[0] as int,neighbor[1] as int))
        }
        if(requireAllClasses && labels.values().toSet()!=classOrder.toSet())
            throw new IllegalArgumentException("Each class needs at least one completely labelled patch; partial or conflicting patches are context only")
        List<String> ordered=context.toList().sort { a,b ->
            def aa=a.split(":").collect { Integer.parseInt(it) },bb=b.split(":").collect { Integer.parseInt(it) }
            aa[0]<=>bb[0] ?: aa[1]<=>bb[1]
        }
        List<Map> nodes=ordered.collect { id ->
            def rc=id.split(":").collect { Integer.parseInt(it) }
            [node_id:id,grid_row:rc[0],grid_col:rc[1],scope:scopeIdentity]
        }
        return [nodes:nodes,labels:labels,class_order:classOrder,feature_width:768,annotation_ids:annotationIds]

    }

    Map trainSpatial(List<Map> annotations,List<String> classOrder,Map settings,Closure progress) {
        Map data=trainingData(annotations,classOrder)
        Map request=[op:"train_graph",nodes:data.nodes,labels:data.labels,class_order:classOrder,feature_width:768,settings:settings]
        Map result=client.trainGraph(request,{int index ->
            Map node=data.nodes[index];featureSource(node.grid_row as int,node.grid_col as int)
        },progress)
        if(result.header.kind!="graph_fit" || result.header.class_order!=classOrder)
            throw new IOException("The graph fit does not match the declared class order")
        ByteBuffer buffer=ByteBuffer.wrap(result.body as byte[]).order(ByteOrder.LITTLE_ENDIAN)
        Map parameters=[:]
        Map shapes=[self_weight:[768,settings.hidden_width],neighbour_weight:[768,settings.hidden_width],
                    bias:[settings.hidden_width],head_weight:[settings.hidden_width,classOrder.size()],head_bias:[classOrder.size()]]
        if(result.header.arrays.collect { it.name }!=shapes.keySet().toList())
            throw new IOException("The fitted graph does not contain the exact layer/head parameter set")
        for(Map array:result.header.arrays) {
            if(array.shape!=shapes[array.name]) throw new IOException("The fitted graph parameter dimensions changed")
            if(array.shape.size()==1) {
                float[] vector=new float[array.shape[0] as int]
                for(int i=0;i<vector.length;i++) vector[i]=buffer.getFloat()
                parameters[array.name]=vector
            } else {
                float[][] matrix=new float[array.shape[0] as int][array.shape[1] as int]
                for(int i=0;i<matrix.length;i++)for(int j=0;j<matrix[i].length;j++)matrix[i][j]=buffer.getFloat()
                parameters[array.name]=matrix
            }
        }
        if(buffer.hasRemaining()) throw new IOException("The graph result contains undeclared extra values")
        return [parameters:parameters,training_ids:result.header.training_node_ids,class_order:classOrder,
                label_sha256:result.header.label_assignments_sha256,loss_history:result.header.loss_history]
    }

    Object provider(Map fitted,MifKronosNativeFit fit=null) {
        def provider=operations.provider(image,scope,downsample,
            {int row,int col -> embedding(row,col)},fitted.parameters,fitted.class_order.size(),patchSize)
        if(fit!=null) { provider.nativeClassifier=fit.wrapper;
            provider.preprocessor=fit.preprocessor }
        return provider
    }

    MifKronosNativeFit fitClassifier(Map fitted,List<Map> annotations,List<String> classOrder,String method,Map controls) {
        Map data=trainingData(annotations,classOrder)
        List<String> ids=data.labels.keySet().toList().sort()
        if(fitted.class_order!=classOrder || !(method in ["rf","svm"]))
            throw new IllegalArgumentException("The frozen graph, selected classifier and class order must agree")
        def frozen=provider(fitted)
        int width=(fitted.parameters.bias as float[]).length
        try {
            def normalization=[none:Normalization.NONE,mean_variance:Normalization.MEAN_VARIANCE,min_max:Normalization.MIN_MAX][controls.normalization]
            if(normalization==null) throw new IllegalArgumentException("Select an explicit native feature normalization")
            def trainer={TrainData training ->
                if(controls.configuration!=null && operations.fitNative!=null)
                    return operations.fitNative.call(controls.configuration,training,controls.seed as int)
                if(method=="rf")
                    return OpenCvTraining.fitForest(training,new OpenCvTraining.ForestSettings(
                        controls.max_depth as int,controls.min_sample_count as int,false,
                        controls.active_var_count as int,false,0,false,controls.max_trees as int,0d),
                        controls.seed as int)
                if(!(controls.kernel in ["LINEAR","RBF"])) throw new IllegalArgumentException("Select LINEAR or RBF for this preview")
                return OpenCvTraining.fitSvm(training,new OpenCvTraining.SvmSettings(
                    controls.kernel=="LINEAR" ? SVM.LINEAR : SVM.RBF,controls.c as double,
                    controls.kernel=="RBF" ? controls.gamma as double : 0d,
                    controls.max_iterations as int,controls.epsilon as double,false),controls.seed as int)
            } as java.util.function.Function
            return MifKronosNativeFit.fit(frozen,ids,data.labels,classOrder,width,normalization,method,trainer)
        } finally { frozen.close() }
    }

    @Override synchronized void close() {
        if(closed)return
        closed=true
        FrozenFeatureCache.release(featureCacheOwner)
    }
}
return [backend:{ImageData image,Geometry scope,double ds,List channels,Map identity,Object geometry,Map operations,Object client ->
    new MifKronosPatchBackend(image,scope,ds,channels,identity,geometry,operations,client)}]
