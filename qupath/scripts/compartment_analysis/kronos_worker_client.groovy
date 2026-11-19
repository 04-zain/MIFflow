// Native frozen inference; Python runs only preparation and graph fitting.
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.Callable
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

final class MifKronosPreparationClient implements AutoCloseable {
    private final Process process
    private final DataInputStream input
    private final DataOutputStream output
    private final ExecutorService reader = Executors.newSingleThreadExecutor()
    final Path sessionDirectory
    final Map ready
    private long nextId = 0
    private volatile long activeId = 0
    private volatile boolean closed = false

    MifKronosPreparationClient(Map settings, Path directory, boolean fitting=false) {
        List command=settings.worker_command
        if (!(command instanceof List) || command.isEmpty() || command.size()>8 ||
                command.any { !(it instanceof String) || it.isEmpty() || it.contains("\u0000") } ||
                !Path.of(command[0]).isAbsolute() || !Files.isExecutable(Path.of(command[0])) ||
                !directory.isAbsolute() || !Files.isDirectory(directory))
            throw new IllegalArgumentException("The application worker or session folder is unavailable; repair Software setup and retry")
        for(String key:["threads","nuclear_channel","activation_budget_bytes"]) {
            Object raw=settings[key]
            if(raw!=null && (!(raw instanceof Number) || !Double.isFinite(raw.doubleValue()) || raw.doubleValue()!=Math.rint(raw.doubleValue())))
                throw new IllegalArgumentException("Worker integer setting is not an exact integer: "+key)
        }
        if(settings.worker_release!=null && settings.worker_release!="mif-pipeline-prototype-7")
            throw new IOException("The configured KRONOS worker release differs from this application")
        sessionDirectory=directory
        ProcessBuilder builder=new ProcessBuilder(command as List<String>)
        builder.environment().put("PYTHONDONTWRITEBYTECODE","1")
        builder.environment().put("OPENBLAS_NUM_THREADS","1")
        builder.environment().put("OMP_NUM_THREADS",(settings.threads ?: 1).toString())
        if(!fitting) builder.environment().put("MIF_COMPUTE_DEVICE","cpu")
        builder.environment().put("HF_HUB_OFFLINE","1")
        builder.environment().put("TRANSFORMERS_OFFLINE","1")
        builder.redirectError(directory.resolve("worker.log").toFile())
        process=builder.start()
        input=new DataInputStream(new BufferedInputStream(process.getInputStream()))
        output=new DataOutputStream(new BufferedOutputStream(process.getOutputStream()))
        try {
            Map init=fitting ? [op:"initialize_fit",threads:(settings.threads as Number).intValue(),session_dir:directory.toString()] :
                     [op:"initialize",package_dir:settings.package_dir,package_sha256:settings.package_sha256,
                      marker_names:settings.marker_names,divisors:settings.divisors,pixel_size_um:settings.pixel_size_um,
                      patch_size:(settings.patch_size as Number).intValue(),
                      additional_markers:settings.additional_markers,marker_text_model:settings.marker_text_model,
                      nuclear_channel:settings.nuclear_channel==null ? null : (settings.nuclear_channel as Number).intValue(),
                      threads:(settings.threads as Number).intValue(),session_dir:directory.toString()]
            if(!fitting && settings.activation_budget_bytes!=null)
                init.activation_budget_bytes=(settings.activation_budget_bytes as Number).longValue()
            ready=exchange(init,new byte[0],300,null).header
            if (ready.kind!="ready" || (!fitting && ready.package_sha256!=settings.package_sha256) ||
                ready.worker_release!="mif-pipeline-prototype-7" || ready.worker_protocol!="mif-kronos-worker/v3")
                throw new IOException("The local encoder did not confirm the selected model revision")
        } catch (Throwable error) { close();
            throw error }
    }

    boolean isAlive() { !closed && process.isAlive() }

    private Map readFrame() {
        int size=input.readInt()
        if(size<1 || size>8*1024*1024) throw new IOException("Worker metadata frame is too large")
        byte[] metadata=new byte[size];
        input.readFully(metadata)
        Object parsed=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().fromJson(new String(metadata,StandardCharsets.UTF_8),Object.class)
        if(!(parsed instanceof Map)) throw new IOException("Worker metadata is not an object")
        Map header=parsed as Map
        if(!(header.byte_count instanceof Number) || !Double.isFinite(header.byte_count.doubleValue()) ||
                header.byte_count.doubleValue()!=Math.rint(header.byte_count.doubleValue()))
            throw new IOException("Worker payload has no exact integer byte count")
        long length=(header.byte_count as Number).longValue()
        if(length<0 || length>64*1024*1024) throw new IOException("Worker binary frame is too large")
        byte[] body=new byte[(int)length];
        input.readFully(body)
        return [header:header,body:body]
    }

    synchronized Map exchange(Map fields, byte[] body, int seconds=120, Closure onProgress=null) {
        if(closed) throw new IOException("The KRONOS worker is closed")
        if(body.length>64*1024*1024) throw new IOException("Worker input exceeds the binary-frame budget")
        long requestId=++nextId;
        activeId=requestId
        byte[] metadata=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().toJson(fields+[request_id:requestId,byte_count:body.length]).getBytes(StandardCharsets.UTF_8)
        if(metadata.length>8*1024*1024) throw new IOException("Worker input exceeds the metadata-frame budget")
        output.writeInt(metadata.length);
        output.write(metadata);
        output.write(body);
        output.flush()
        long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(seconds)
        try {
            while(true) {
                long remaining=deadline-System.nanoTime()
                if(remaining<=0) throw new TimeoutException("The KRONOS worker exceeded its deadline")
                def future=reader.submit({ -> readFrame() } as Callable<Map>)
                Map response=future.get(remaining,TimeUnit.NANOSECONDS)
                if(response.header.request_id!=requestId) throw new IOException("Worker reply belongs to a different request")
                if(response.header.kind=="error") throw new IOException(response.header.message as String)
                if(response.header.kind=="progress") {
                    if(onProgress!=null) onProgress(response.header)
                    continue
                }
                return response
            }
        } catch(Throwable error) {
            // A partial/late reply cannot be paired with another request.
            // Retire this owned process before allowing its caller to retry.
            process.destroy()
            try { close() } catch(Throwable cleanup) { error.addSuppressed(cleanup) }
            throw error
        } finally { activeId=0 }
    }

    void cancelActive() {
        long id=activeId
        if(id>0 && !closed) Files.writeString(sessionDirectory.resolve("cancel-"+id),"cancel")
    }

    @Override void close() {
        if(closed) return
        closed=true
        try { output?.close() } catch(Exception ignored) {}
        try {
            if(!process.waitFor(10,TimeUnit.SECONDS)) {
                process.destroy()
                if(!process.waitFor(5,TimeUnit.SECONDS)) process.destroyForcibly().waitFor(5,TimeUnit.SECONDS)
            }
        } finally {
            try { input?.close() } catch(Exception ignored) {}
            reader.shutdownNow()
        }
    }
}

/** Owns one native model and a bounded persistent, label-independent feature cache. */
final class MifNativeKronosClient implements AutoCloseable {
    final Map ready
    private final Map settings
    private final Path directory
    private final Path cache
    private final boolean ownsDirectory
    private final Path model
    private final String devicePreference
    private org.mif.inference.FrozenEncoder encoder
    private MifKronosPreparationClient fitter
    private final LinkedHashMap<String,org.mif.inference.FrozenPatchTable> patchTables=new LinkedHashMap<>(16,0.75f,true)
    private final LinkedHashMap<String,Path> entries = new LinkedHashMap<>(64,0.75f,true)
    private volatile boolean closed=false
    private volatile boolean cancelled=false
    private int lastCacheHit=0
    private final ByteBuffer digestBuffer=ByteBuffer.allocate(64*1024).order(ByteOrder.LITTLE_ENDIAN)

    static String json(Object value) {
        Object ordered=value
        if(value instanceof Map) ordered=new TreeMap(value.collectEntries { k,v -> [(k):canonical(v)] })
        else ordered=canonical(value)
        new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().create().toJson(ordered)
    }
    private static Object canonical(Object value) {
        if(value instanceof Map)return new TreeMap(value.collectEntries { k,v -> [(k):canonical(v)] })
        if(value instanceof Collection)return value.collect { canonical(it) }
        value
    }
    static String sha(byte[] value) { java.security.MessageDigest.getInstance("SHA-256").digest(value).encodeHex().toString() }
    static MifNativeKronosClient temporary(Map settings,String prefix) {
        Path directory=Files.createTempDirectory(prefix)
        try { return new MifNativeKronosClient(settings,directory,true) }
        catch(Throwable error) {
            try { deleteDirectory(directory) } catch(Throwable cleanup) { error.addSuppressed(cleanup) }
            throw error
        }
    }
    private static void deleteDirectory(Path directory) {
        // Files.walk does not follow symbolic links; remove only this owned session.
        Files.walk(directory).withCloseable { paths ->
            paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) }
        }
    }
    MifNativeKronosClient(Map settings,Path directory,boolean ownsDirectory=false) {
        this.settings=new LinkedHashMap(settings);this.directory=directory
        this.ownsDirectory=ownsDirectory
        Map identity=settings.subMap(["package_sha256","marker_names","divisors","pixel_size_um","patch_size","nuclear_channel","additional_markers","marker_text_model"])
        Path deploymentRoot=ai.djl.util.Utils.getCacheDir().resolve("mif-kronos")
        Files.createDirectories(deploymentRoot)
        String deploymentKey=sha(json(identity).getBytes(StandardCharsets.UTF_8))
        Path deployment=deploymentRoot.resolve(deploymentKey)
        Path descriptor=deployment.resolve("deployment.json")
        // A shared completed export avoids another 327 MiB model for every image or fit.
        java.nio.channels.FileChannel.open(deploymentRoot.resolve(deploymentKey+".lock"),
                java.nio.file.StandardOpenOption.CREATE,java.nio.file.StandardOpenOption.WRITE).withCloseable { channel ->
            java.nio.channels.FileLock lock=null
            while(lock==null) {
                if(Thread.currentThread().isInterrupted())throw new InterruptedException("Model preparation cancelled")
                try {lock=channel.tryLock()} catch(java.nio.channels.OverlappingFileLockException busy) {}
                if(lock==null)Thread.sleep(100)
            }
            try {
                if(!Files.isRegularFile(descriptor)) {
                    def preparation=new MifKronosPreparationClient(settings,deploymentRoot)
                    try {
                        Map result=preparation.exchange([op:"export_native",destination:deployment.toString()],new byte[0],600)
                        if(result.header.kind!="native_export")throw new IOException("Model preparation did not publish a native encoder")
                    } finally {preparation.close()}
                }
            } finally {lock.release()}
        }
        Map record=new com.google.gson.Gson().fromJson(Files.readString(descriptor),Map.class)
        if(record.schema!="mif-native-kronos-encoder/v1" || record.package_sha256!=settings.package_sha256 ||
                record.channels!=settings.marker_names.size() || record.patch_size!=settings.patch_size || record.feature_width!=768 ||
                record.panel.marker_names!=settings.marker_names || record.panel.intensity_divisors!=settings.divisors ||
                record.panel.additional_markers!=settings.additional_markers || record.panel.marker_text_model!=settings.marker_text_model ||
                record.panel.patch_size!=settings.patch_size || record.panel.pixel_size_um!=settings.pixel_size_um || record.panel.nuclear_channel!=settings.nuclear_channel ||
                record.model_file!="encoder.pt")
            throw new IOException("The native deployment does not match the selected model and panel")
        model=deployment.resolve("encoder.pt")
        ready=Collections.unmodifiableMap(record)
        cache=directory.resolve("features-"+deploymentKey);Files.createDirectories(cache)
        Files.list(cache).withCloseable { stream ->
            stream.filter { it.fileName.toString().endsWith(".f32") }
                .sorted {a,b -> Files.getLastModifiedTime(a).compareTo(Files.getLastModifiedTime(b))}
                .forEach {path -> entries[path.fileName.toString().replace(".f32","")]=path}
        }
        trimCache()
        String preference=System.getenv("MIF_COMPUTE_DEVICE") ?: "auto"
        if(!(preference in ["auto","cpu","cuda"]))throw new IOException("Native KRONOS supports CPU or CUDA")
        devicePreference=preference
    }
    /** Load the completed model only when a missing frozen vector needs inference. */
    private org.mif.inference.FrozenEncoder activeEncoder() {
        checkActive()
        if(encoder==null) {
            if(Files.size(model)!=(ready.model_bytes as Number).longValue())
                throw new IOException("The completed native encoder has an unexpected size")
            encoder=new org.mif.inference.FrozenEncoder(model,settings.marker_names.size(),(settings.patch_size as Number).intValue(),devicePreference)
        }
        encoder
    }
    /** Bound open table handles and finish each row access before another table can evict them. */
    synchronized Object withPatchTable(Map identity,int rows,int columns,Closure action) {
        checkActive()
        String encoded=json(identity)
        String key=sha((encoded+"/"+rows+"/"+columns).getBytes(StandardCharsets.UTF_8))
        def table=patchTables[key]
        if(table==null) {
            if(patchTables.size()>=16) {
                String oldest=patchTables.keySet().iterator().next()
                patchTables.remove(oldest).close()
            }
            table=new org.mif.inference.FrozenPatchTable(
                ai.djl.util.Utils.getCacheDir().resolve("mif-kronos-patches"),encoded,rows,columns)
            patchTables[key]=table
        }
        action.call(table)
    }
    private void trimCache() {
        while(entries.size()>4096) {
            String key=entries.keySet().iterator().next()
            Files.deleteIfExists(entries.remove(key))
        }
    }
    boolean isAlive() { !closed }
    private void checkActive() {
        if(closed)throw new IOException("Native KRONOS session is closed")
        if(cancelled || Thread.currentThread().isInterrupted())throw new InterruptedException("KRONOS operation cancelled")
    }
    private float[] features(float[] pixels,String identity) {
        checkActive();lastCacheHit=0
        Path path=entries.get(identity)
        if(path!=null) {
            byte[] bytes=Files.readAllBytes(path)
            if(bytes.length!=32+768*4 || !java.util.Arrays.equals(java.util.Arrays.copyOfRange(bytes,0,32),
                    java.security.MessageDigest.getInstance("SHA-256").digest(java.util.Arrays.copyOfRange(bytes,32,bytes.length))))
                throw new IOException("A stored frozen-feature record changed")
            float[] values=new float[768]
            ByteBuffer.wrap(bytes,32,768*4).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(values)
            lastCacheHit=1;return values
        }
        float[] values=activeEncoder().encode(pixels)
        checkActive()
        ByteBuffer bytes=ByteBuffer.allocate(768*4).order(ByteOrder.LITTLE_ENDIAN)
        bytes.asFloatBuffer().put(values)
        Path temporary=Files.createTempFile(cache,".feature-",".tmp")
        try {
            Files.newOutputStream(temporary).withCloseable { output ->
                output.write(java.security.MessageDigest.getInstance("SHA-256").digest(bytes.array()));output.write(bytes.array())
            }
            Path target=cache.resolve(identity+".f32")
            Files.move(temporary,target,java.nio.file.StandardCopyOption.ATOMIC_MOVE,java.nio.file.StandardCopyOption.REPLACE_EXISTING)
            entries[identity]=target;trimCache()
        } finally {Files.deleteIfExists(temporary)}
        values
    }
    private void updatePixels(java.security.MessageDigest digest,float[] pixels) {
        int capacity=digestBuffer.capacity()/4
        for(int offset=0;offset<pixels.length;offset+=capacity) {
            int count=Math.min(capacity,pixels.length-offset)
            digestBuffer.clear();digestBuffer.asFloatBuffer().put(pixels,offset,count)
            digest.update(digestBuffer.array(),0,count*4)
        }
    }
    synchronized float[][] encode(float[] pixels,int channels) {
        int side=(settings.patch_size as Number).intValue()
        if(channels!=settings.marker_names.size() || pixels.length!=channels*side*side)
            throw new IllegalArgumentException("Frozen panel and image channels disagree")
        checkActive()
        // The spatial table owns patch reuse; avoid a second per-patch cache file.
        float[] values=activeEncoder().encode(pixels)
        checkActive()
        [values] as float[][]
    }
    synchronized Map encodeCells(Map fields,List<Map> batch) {
        int channels=settings.marker_names.size(),side=(settings.patch_size as Number).intValue()
        Map schema=[schema:"kronos-cell-feature-schema/v1",package_sha256:ready.package_sha256,
            panel:ready.panel,panel_sha256:ready.panel_sha256,channel_ids:fields.channel_ids,
            channel_names:fields.channel_names,extraction:fields.extraction,feature_width:768]
        String spec=sha(json(schema).getBytes(StandardCharsets.UTF_8))
        List identities=[];List vectors=[];int hits=0
        batch.each { row ->
            float[] raw=row.pixels as float[];byte[] mask=row.mask as byte[]
            if(raw.length!=channels*side*side || mask.length!=side*side)
                throw new IllegalArgumentException("Cell pixels and mask disagree with the frozen panel")
            def digest=java.security.MessageDigest.getInstance("SHA-256")
            digest.update(json([feature_spec_sha256:spec,scope:fields.scope,cell_id:row.cell_id,
                geometry:row.geometry,channel_indices:fields.channel_indices,
                source_pixel_size_um:fields.source_pixel_size_um]).getBytes(StandardCharsets.UTF_8))
            updatePixels(digest,raw);digest.update(mask)
            String identity=digest.digest().encodeHex().toString();identities.add(identity)
            // The owned sampled crop is consumed here; no second full crop or pipe payload.
            org.mif.image.CellImageSamples.applyKronosMask(raw,mask,channels)
            vectors.add(features(raw,identity));hits+=lastCacheHit
        }
        [header:[cell_ids:batch.collect {it.cell_id},panel_sha256:ready.panel_sha256,
            package_sha256:ready.package_sha256,feature_spec_sha256:spec,feature_sha256:identities,cache_hits:hits],
         features:vectors]
    }
    /** One fit task references canonical spatial rows; no embedding copy uses the pipe. */
    Map trainGraph(Map fields,Closure<Map> sourceReader,Closure progress=null) {
        checkActive()
        int count=fields.nodes.size(),width=(fields.feature_width as Number).intValue()
        if(count<1 || count>50000 || width!=768)
            throw new IllegalArgumentException("The graph dataset exceeds the admitted node or feature dimensions")
        Map request=new LinkedHashMap(fields);request.remove("op")
        List tables=[];List sources=new ArrayList(count);Map tableIndices=[:]
        // Backends acquire their own monitor before borrowing this client.
        // Resolve sources first so fitting never reverses that lock order.
        for(int i=0;i<count;i++) {
            checkActive()
            Map source=sourceReader.call(i)
            String key=source.path+"/"+source.sha256
            Integer index=tableIndices[key]
            if(index==null) {
                index=tables.size();tableIndices[key]=index
                tables.add([path:source.path,sha256:source.sha256])
            }
            sources.add([index,source.row_index])
        }
        request.feature_sources=[tables:tables,rows:sources]
        synchronized(this) {
            checkActive()
            Path task=Files.createTempDirectory(directory,"graph-fit-")
            try {
                Files.writeString(task.resolve("task.json"),json(request),StandardCharsets.UTF_8)
                if(fitter==null || !fitter.isAlive())fitter=new MifKronosPreparationClient(settings,directory,true)
                return fitter.exchange([op:"train_graph_dataset",dataset:task.toString()],new byte[0],600,progress)
            } finally {
                // Close cannot remove the task while fitting owns its files.
                Files.deleteIfExists(task.resolve("task.json"));Files.deleteIfExists(task)
            }
        }
    }
    void cancelActive() {cancelled=true;fitter?.cancelActive()}
    @Override synchronized void close() {
        if(closed)return
        closed=true
        try {fitter?.close()} finally {
            try {encoder?.close()} finally {
                entries.clear()
                IOException failure=null
                patchTables.values().each {table ->
                    try {table.close()} catch(IOException error) {
                        if(failure==null)failure=error else failure.addSuppressed(error)
                    }
                }
                patchTables.clear()
                try {if(ownsDirectory)deleteDirectory(directory)} finally {if(failure!=null)throw failure}
            }
        }
    }
}

return [create:{ Map settings,Path directory -> new MifNativeKronosClient(settings,directory) },
        createTemporary:{ Map settings,String prefix -> MifNativeKronosClient.temporary(settings,prefix) }]
