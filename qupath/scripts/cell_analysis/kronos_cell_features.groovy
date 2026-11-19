/**
 * Prepare masked single-cell KRONOS2 inputs for supervised RF, SVM or LR.
 *
 * Input: processed image, current cells with stable IDs and nucleus/cell ROIs,
 * saved image-to-marker mapping, image/segmentation identity, cache directory,
 * and the registered model-preparation client. Output: 768 ordered measurements
 * per cell and a receipt binding them to the actual pixels, masks and encoder.
 *
 * The crop contains the saved number of samples per axis at the saved physical pixel size.
 * Its center is the nucleus centroid. Accepted v1 inputs use nearest native
 * samples. New v2 inputs area-average source-cell pixels when either axis is
 * coarser, retaining native-grid behavior and the target-centre ROI mask.
 * Outside-cell/image contributions are zero before normalization. The
 * exported model applies saved marker normalization and returns its class token.
 */
import java.awt.image.BufferedImage
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import org.locationtech.jts.io.WKBWriter
import org.mif.image.CellImageSamples
import qupath.lib.images.ImageData
import qupath.lib.regions.RegionRequest

final class MifKronosCellFeatures {
    static final Set BIOLOGY_FIELDS = ["marker_name","marker_full_name","compartment","family","family_desc"].toSet()
    static final String STATISTICS_SOURCE = "kronos-native-tissue-sample-moments/v1:sha256:"
    static final String INPUT_SCHEMA = "kronos-cell-input-evidence/v1"
    static final Map LEGACY_POLICY = Collections.unmodifiableMap([
        schema:"kronos-cell-extraction/v1",
        center:"nucleus-centroid", mask:"cell-roi",
        outside:"zero-before-normalization", pooling:"x_norm_clstoken",
        interpolation:"nearest-level0", edge_policy:"zero-pad",
        field_of_view:"saved-samples-at-recorded-pixel-size"
    ])
    static final Map POLICY = Collections.unmodifiableMap(new LinkedHashMap(LEGACY_POLICY) + [
        schema:"kronos-cell-extraction/v2", mask:"native-pixel-and-target-center-cell-roi",
        interpolation:"area-overlap-on-coarsening"
    ])
    static List<Map> featureSchema() {
        (0..<768).collect { int i -> [measurement_name:String.format(Locale.ROOT,
            "KRONOS2: cell embedding: %04d",i), semantic_role:"kronos_cell_embedding"] }
    }
    static String sha(byte[] bytes) {
        MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()
    }
    static void require(boolean condition, String message) {
        if(!condition) throw new IllegalArgumentException(message)
    }
    /** Language-independent canonical identity; all numeric source values are binary64. */
    static void digestValue(MessageDigest digest,Object value) {
        if(value==null) {digest.update((byte)'n');return}
        if(value instanceof Boolean) {digest.update((byte)(value?'t':'f'));return}
        if(value instanceof Number) {
            double number=(value as Number).doubleValue()
            require(Double.isFinite(number),"Input identity contains a nonfinite number")
            digest.update((byte)'d');digest.update(ByteBuffer.allocate(8).putDouble(number).array());return
        }
        if(value instanceof String || value instanceof GString) {
            byte[] bytes=value.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8)
            digest.update((byte)'s');digest.update(ByteBuffer.allocate(4).putInt(bytes.length).array());digest.update(bytes);return
        }
        if(value instanceof List) {
            digest.update((byte)'l');digest.update(ByteBuffer.allocate(4).putInt(value.size()).array())
            value.each {digestValue(digest,it)};return
        }
        require(value instanceof Map && value.keySet().every {it instanceof String},"Input identity contains unsupported values")
        List keys=value.keySet().toList().sort {left,right ->
            Arrays.compareUnsigned(left.getBytes(java.nio.charset.StandardCharsets.UTF_8),right.getBytes(java.nio.charset.StandardCharsets.UTF_8))
        }
        digest.update((byte)'m');digest.update(ByteBuffer.allocate(4).putInt(keys.size()).array())
        keys.each {key -> digestValue(digest,key);digestValue(digest,value[key])}
    }
    static String canonicalDigest(Object value) {
        MessageDigest digest=MessageDigest.getInstance("SHA-256");digestValue(digest,value)
        digest.digest().encodeHex().toString()
    }
    static List pendingMarkerNames(Map source) {
        List rows=source?.encoder?.additional_markers ?: []
        List names=rows.findAll {it instanceof Map && it.keySet()==BIOLOGY_FIELDS}.collect {it.marker_name}
        (source?.encoder?.marker_names ?: []).findAll {names.contains(it)}
    }
    static String sourceIntentSha256(Map source,List automaticNames) {
        validateSource(source)
        require(automaticNames instanceof List && !automaticNames.isEmpty() && automaticNames.toSet().size()==automaticNames.size(),
            "Deferred cell inputs require distinct automatically fitted markers")
        List rows=source.encoder.additional_markers
        require(rows instanceof List && automaticNames==source.encoder.marker_names.findAll {automaticNames.contains(it)} &&
            rows.count {automaticNames.contains(it.marker_name)}==automaticNames.size() &&
            pendingMarkerNames(source).every {automaticNames.contains(it)},"Deferred marker identities differ from the source")
        List projected=rows.collect {row ->
            if(!automaticNames.contains(row.marker_name))return row
            require(row.keySet()==BIOLOGY_FIELDS ||
                (row.keySet()==BIOLOGY_FIELDS+["mean","std","statistics_source"] &&
                    row.statistics_source instanceof String &&
                    (row.statistics_source==~ /kronos-native-tissue-sample-moments\/v1:sha256:[0-9a-f]{64}/)),
                "Deferred inputs cannot replace explicit marker statistics")
            row.findAll {key,value -> BIOLOGY_FIELDS.contains(key)}
        }
        canonicalDigest(source+[encoder:source.encoder+[additional_markers:projected]])
    }
    static void validateInputEvidence(Map evidence,Map source,Collection ids) {
        require(evidence!=null && evidence.keySet()==["schema","source_intent_sha256","automatic_marker_names","cell_input_sha256"].toSet() &&
            evidence.schema==INPUT_SCHEMA && evidence.automatic_marker_names instanceof List && evidence.cell_input_sha256 instanceof Map &&
            evidence.source_intent_sha256==sourceIntentSha256(source,evidence.automatic_marker_names as List) &&
            evidence.cell_input_sha256.keySet()==ids.toSet() &&
            evidence.cell_input_sha256.values().every {it instanceof String && (it==~ /[0-9a-f]{64}/)},
            "Deferred KRONOS cell input evidence differs from the source or exact cell inventory")
    }
    static Map workerSettings(Map encoder) {
        [worker_command:encoder.worker_command,
         worker_release:encoder.worker_release, package_dir:encoder.package_directory,
         package_sha256:encoder.package_sha256, marker_names:encoder.marker_names,
         divisors:encoder.intensity_divisors, pixel_size_um:encoder.pixel_size_um, patch_size:encoder.patch_size,
         nuclear_channel:encoder.nuclear_channel, threads:encoder.threads,
         activation_budget_bytes:encoder.activation_budget_bytes,
         additional_markers:encoder.additional_markers,marker_text_model:encoder.marker_text_model]
    }
    static void validateSource(Map source) {
        require(source!=null && source.keySet()==(["schema","encoder","channel_names","extraction"] as Set),
            "KRONOS cell feature source is incomplete or contains unknown fields")
        require(source.schema=="kronos-cell-feature-source/v1" && source.encoder instanceof Map &&
            (source.extraction==POLICY || source.extraction==LEGACY_POLICY),
            "The saved KRONOS cell extraction policy is unsupported")
        Map encoder=source.encoder
        require(encoder.keySet()==["package_directory","package_sha256","worker_command","worker_release","channel_ids","marker_names","intensity_divisors",
            "pixel_size_um","patch_size","nuclear_channel","threads","activation_budget_bytes",
            "additional_markers","marker_text_model"].toSet(),
            "KRONOS cell encoder fields are incomplete or unknown")
        // Gson transports JSON numbers as Double; Groovy's collection membership
        // does not match those values to Integer entries. Keep exact validation
        // before converting so fractional and overflowing values remain invalid.
        require(encoder.patch_size instanceof Number &&
            [32d,64d,128d,256d].contains((encoder.patch_size as Number).doubleValue()),
            "KRONOS image size must be 32, 64, 128 or 256")
        List names=source.channel_names
        require(names!=null && !names.isEmpty() && names.size()<=32 &&
            names.toSet().size()==names.size() && names.every {it instanceof String && it.trim()},
            "Select distinct exact processed-image names for the KRONOS channels")
        [encoder.channel_ids,encoder.marker_names,encoder.intensity_divisors].each {
            require(it instanceof List && it.size()==names.size(),"KRONOS cell channel and marker orders disagree")
        }
        [encoder.channel_ids,encoder.marker_names].each { values ->
            require(values.toSet().size()==values.size() && values.every {it instanceof String && it.trim()},
                "KRONOS channel and marker identities must be explicit and distinct")
        }
        require(encoder.pixel_size_um instanceof List && encoder.pixel_size_um.size()==2,
            "KRONOS sampling needs independent X and Y physical pixel sizes")
        (encoder.pixel_size_um+encoder.intensity_divisors).each {
            require(it instanceof Number && Double.isFinite(it.doubleValue()) && it.doubleValue()>0,
                "KRONOS pixel sizes and intensity divisors must be positive finite values")
        }
    }

    /** Sample one cell; the raw image and mask remain separate native inputs. */
    static Map sample(ImageData<BufferedImage> image, Object cell, List<Integer> indices,
                      double dx, double dy, String generation, int patchSize, Map extraction) {
        Map metadata=cell.getMetadata()
        String id=metadata.cell_id
        require(cell.isCell() && id instanceof String && id.trim() &&
            metadata.generation_id==generation,
            "KRONOS input cells must retain their stable ID and selected segmentation generation")
        def roi=cell.getROI(), nucleus=cell.getNucleusROI()
        require(roi!=null && nucleus!=null && roi.isArea() && nucleus.isArea() &&
            !roi.isEmpty() && !nucleus.isEmpty() && roi.getImagePlane()==nucleus.getImagePlane(),
            "KRONOS cell features require real cell and nucleus ROIs on the same image plane")
        double cx=nucleus.getCentroidX(),cy=nucleus.getCentroidY()
        double width=patchSize*(double)dx,height=patchSize*(double)dy,x0=cx-width/2.0,y0=cy-height/2.0
        require([cx,cy,width,height,x0,y0].every {Double.isFinite(it as double)},
            "KRONOS cell crop coordinates must be finite")
        require(roi.getBoundsX()>=x0 && roi.getBoundsY()>=y0 &&
            roi.getBoundsX()+roi.getBoundsWidth()<=x0+width &&
            roi.getBoundsY()+roi.getBoundsHeight()<=y0+height,
            "A cell exceeds the selected ${patchSize}-sample field of view; choose a larger image size or physical sampling size in classifier setup, or correct its segmentation")
        def server=image.getServer()
        boolean areaPolicy=extraction==POLICY
        int[] bounds=CellImageSamples.kronosReadBounds(roi,server.getWidth(),server.getHeight(),
            x0,y0,dx,dy,patchSize,areaPolicy)
        int x1=bounds[0],y1=bounds[1],x2=bounds[2],y2=bounds[3]
        require(x2>x1 && y2>y1,"The cell crop does not contain image pixels")
        long sourcePixels=(long)(x2-x1)*(y2-y1)
        require(sourcePixels<=4L*1024*1024 && sourcePixels*server.nChannels()*8L<=256L*1024*1024,
            "This physical cell crop exceeds the bounded full-resolution read; use a smaller sampling size")
        def request=RegionRequest.createInstance(server.getPath(),1.0,x1,y1,x2-x1,y2-y1,roi.getZ(),roi.getT())
        BufferedImage patch=server.readRegion(request)
        require(patch!=null && patch.getWidth()==x2-x1 && patch.getHeight()==y2-y1,
            "The processed image server did not return the exact full-resolution cell rectangle")
        def sampled=areaPolicy ? CellImageSamples.sampleKronosArea(patch.getRaster(),x1,y1,
            server.getWidth(),server.getHeight(),roi,indices,x0,y0,dx,dy,patchSize) :
            CellImageSamples.sampleKronos(patch.getRaster(),x1,y1,
                server.getWidth(),server.getHeight(),roi,indices,x0,y0,dx,dy,patchSize)
        require(sampled.selectedCount()>0,"No sample center intersects this cell; use finer physical sampling or inspect its ROI")
        WKBWriter writer=new WKBWriter()
        [cell:cell,cell_id:id,pixels:sampled.pixels(),mask:sampled.mask(),
         geometry:[center_x:cx,center_y:cy,patch_x:x0,patch_y:y0,patch_width:width,patch_height:height,
                   z:roi.getZ(),t:roi.getT(),cell_roi_sha256:sha(writer.write(roi.getGeometry())),
                   nucleus_roi_sha256:sha(writer.write(nucleus.getGeometry()))]]
    }

    /** Own one native encoder across bounded batches of an operation. */
    static Map open(Map source, Path cacheRoot, Map workers) {
        validateSource(source)
        require(pendingMarkerNames(source).isEmpty(),"Fit automatic marker statistics before opening the cell encoder")
        require(cacheRoot!=null && cacheRoot.isAbsolute(),"KRONOS requires an absolute owned cache directory")
        Path directory=cacheRoot.resolve("kronos-cell-features")
        Files.createDirectories(directory)
        require(!Files.isSymbolicLink(directory),"KRONOS cache directory must not be a symbolic link")
        [client:workers.create(workerSettings(source.encoder as Map),directory),source:source,closed:false]
    }

    static void close(Map session) {
        if(session!=null && !session.closed) {
            session.closed=true
            session.client.close()
        }
    }

    /**
     * Write the fixed feature measurements only after a complete validated batch.
     * A failed image run does not authorize any subsequent fit or prediction.
     * Successful batches are safe to reuse because receipts bind exact inputs.
     */
    static Map inputContext(ImageData<BufferedImage> image,List cells,Map source,Map scope) {
        validateSource(source)
        require(image!=null && cells!=null && !cells.isEmpty(),"KRONOS preparation requires an image and current cells")
        require(scope!=null && scope.keySet()==(["image_sha256","segmentation_generation_id"] as Set) &&
            scope.image_sha256 instanceof String && (scope.image_sha256==~ /[0-9a-f]{64}/) &&
            scope.segmentation_generation_id instanceof String && scope.segmentation_generation_id.trim(),
            "KRONOS preparation requires exact image and segmentation identity")
        Map encoder=source.encoder
        def server=image.getServer()
        List observed=server.getMetadata().getChannels().collect {it.getName()}
        List<Integer> indices=source.channel_names.collect { String name ->
            require(observed.count(name)==1,"Processed image lacks one unambiguous KRONOS channel: "+name)
            observed.indexOf(name)
        }
        double px=server.pixelCalibration.pixelWidthMicrons,py=server.pixelCalibration.pixelHeightMicrons
        require(Double.isFinite(px) && px>0 && Double.isFinite(py) && py>0,
            "KRONOS cell extraction requires recorded physical image calibration")
        double dx=(encoder.pixel_size_um[0] as double)/px,dy=(encoder.pixel_size_um[1] as double)/py
        require(dx>=1.0 && dy>=1.0 && Double.isFinite(dx) && Double.isFinite(dy),
            "KRONOS sampling must be no finer than the acquired image resolution")
        Set ids=[] as Set
        cells.each { cell ->
            String id=cell.getMetadata().get("cell_id")
            require(id!=null && ids.add(id),"KRONOS preparation requires distinct stable cell IDs")
        }
        [indices:indices,px:px,py:py,dx:dx,dy:dy,ids:ids]
    }

    static String sampledInputSha256(Map row,Map source,Map scope,Map context,String intent) {
        MessageDigest digest=MessageDigest.getInstance("SHA-256")
        digestValue(digest,[schema:INPUT_SCHEMA,source_intent_sha256:intent,cell_id:row.cell_id,
            scope:scope,channel_indices:context.indices,source_pixel_size_um:[context.px,context.py],geometry:row.geometry])
        float[] pixels=row.pixels as float[]
        ByteBuffer bytes=ByteBuffer.allocate(8192).order(ByteOrder.LITTLE_ENDIAN)
        for(float value:pixels) {
            require(Float.isFinite(value),"KRONOS cell raw input is not finite")
            bytes.putFloat(value)
            if(!bytes.hasRemaining()) {digest.update(bytes.array());bytes.clear()}
        }
        if(bytes.position()>0)digest.update(bytes.array(),0,bytes.position())
        digest.update(row.mask as byte[])
        digest.digest().encodeHex().toString()
    }

    /** Capture only immutable raw input evidence; no encoder or normalization is opened. */
    static Map inputEvidence(ImageData<BufferedImage> image,List cells,Map source,Map scope) {
        List names=pendingMarkerNames(source)
        require(!names.isEmpty(),"Deferred input evidence requires pending automatic marker statistics")
        Map context=inputContext(image,cells,source,scope)
        String intent=sourceIntentSha256(source,names)
        Map hashes=new TreeMap()
        for(def cell:cells) {
            if(Thread.currentThread().isInterrupted())throw new InterruptedException("KRONOS cell input capture cancelled")
            Map row=sample(image,cell,context.indices as List<Integer>,context.dx as double,context.dy as double,
                scope.segmentation_generation_id as String,source.encoder.patch_size as int,source.extraction as Map)
            hashes[row.cell_id]=sampledInputSha256(row,source,scope,context,intent)
        }
        [schema:INPUT_SCHEMA,source_intent_sha256:intent,automatic_marker_names:names,cell_input_sha256:hashes]
    }

    static Map prepare(ImageData<BufferedImage> image,List cells,Map source,Map scope,
                       Path cacheRoot,Map workers,Map sharedSession=null,Map expectedInputs=null) {
        Map context=inputContext(image,cells,source,scope)
        require(pendingMarkerNames(source).isEmpty(),"Fit automatic marker statistics before opening the cell encoder")
        if(expectedInputs!=null)validateInputEvidence(expectedInputs,source,context.ids as Set)
        Map encoder=source.encoder
        List<Integer> indices=context.indices
        double px=context.px,py=context.py,dx=context.dx,dy=context.dy
        Map session=sharedSession==null ? open(source,cacheRoot,workers) : sharedSession
        require(session.source==source && !session.closed && session.client.isAlive(),
            "KRONOS feature batches require their current shared encoder session")
        def client=session.client
        List<Map> schema=featureSchema()
        Map<String,String> digests=new LinkedHashMap<>()
        String spec=null
        int hits=0,count=source.channel_names.size(),patchSize=(encoder.patch_size as Number).intValue()
        long accumulatorBytes=(source.extraction==POLICY && (dx>1d || dy>1d)) ?
            (long)patchSize*patchSize*count*8L : 0L
        int batchSize=Math.min(16,(int)((64L*1024*1024-accumulatorBytes)/((long)patchSize*patchSize*(4L*count+1))))
        try {
            for(int first=0;first<cells.size();first+=batchSize) {
                if(Thread.currentThread().isInterrupted()) throw new InterruptedException("KRONOS cell encoding cancelled")
                List<Map> batch=cells.subList(first,Math.min(cells.size(),first+batchSize)).collect {
                    sample(image,it,indices,dx,dy,scope.segmentation_generation_id as String,patchSize,source.extraction as Map)
                }
                if(expectedInputs!=null)batch.each {row ->
                    require(sampledInputSha256(row,source,scope,context,expectedInputs.source_intent_sha256 as String)==
                        expectedInputs.cell_input_sha256[row.cell_id],"Raw KRONOS cell input changed after evidence capture: "+row.cell_id)
                }
                Map header=[op:"encode_cells",shape:[batch.size(),count,patchSize,patchSize],mask_shape:[batch.size(),patchSize,patchSize],
                    cell_ids:batch.collect {it.cell_id},scope:scope,channel_ids:encoder.channel_ids,
                    channel_names:source.channel_names,channel_indices:indices,source_pixel_size_um:[px,py],
                    extraction:source.extraction,geometry:batch.collect {it.geometry}]
                Map reply=client.encodeCells(header,batch)
                require(reply.header.cell_ids==header.cell_ids && reply.header.panel_sha256==client.ready.panel_sha256 &&
                    reply.header.package_sha256==encoder.package_sha256,
                    "KRONOS returned a different cell, image panel or model package")
                String current=reply.header.feature_spec_sha256
                require((current==~ /[0-9a-f]{64}/) && (spec==null || spec==current),
                    "KRONOS cell feature definition changed during extraction")
                spec=current;hits+=(reply.header.cache_hits as Number).intValue()
                batch.eachWithIndex { Map row,int index ->
                    float[] values=reply.features[index]
                    require(values.length==768 && values.every {Float.isFinite(it as float)},
                        "KRONOS cell encoder must return exactly 768 finite values")
                    def measurements=row.cell.getMeasurementList()
                    for(int column=0;column<768;column++)measurements.put(schema[column].measurement_name as String,values[column])
                    measurements.close()
                    String digest=reply.header.feature_sha256[index]
                    require(digest==~ /[0-9a-f]{64}/,"KRONOS cell input identity is invalid")
                    row.cell.getMetadata().put("mif.kronos_cell_feature_spec_sha256",spec)
                    row.cell.getMetadata().put("mif.kronos_cell_feature_sha256",digest)
                    if(expectedInputs!=null)
                        row.cell.getMetadata().put("mif.kronos_cell_input_sha256",expectedInputs.cell_input_sha256[row.cell_id] as String)
                    digests[row.cell_id as String]=digest
                }
            }
            [feature_schema:schema,feature_spec_sha256:spec,cell_feature_sha256:digests,
             encoded_cell_count:cells.size(),cache_hits:hits]
        } finally {if(sharedSession==null)close(session)}
    }
}

return [prepare:{ImageData image,List cells,Map source,Map scope,Path cacheRoot,Map workers,Map session=null,Map expectedInputs=null ->
    MifKronosCellFeatures.prepare(image,cells,source,scope,cacheRoot,workers,session,expectedInputs)},
    inputEvidence:{ImageData image,List cells,Map source,Map scope -> MifKronosCellFeatures.inputEvidence(image,cells,source,scope)},
    pendingMarkerNames:{Map source -> MifKronosCellFeatures.pendingMarkerNames(source)},
    sourceIntentSha256:{Map source,List names -> MifKronosCellFeatures.sourceIntentSha256(source,names)},
    validateInputEvidence:{Map evidence,Map source,Collection ids -> MifKronosCellFeatures.validateInputEvidence(evidence,source,ids)},
    open:{Map source,Path cacheRoot,Map workers -> MifKronosCellFeatures.open(source,cacheRoot,workers)},
    close:{Map session -> MifKronosCellFeatures.close(session)},
    featureSchema:{ -> MifKronosCellFeatures.featureSchema()}]
