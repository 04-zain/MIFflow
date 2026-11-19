/** Optional pinned detectors; the stage owner retains domain assignment and publication. */
import java.awt.image.BufferedImage
import java.io.UncheckedIOException
import java.lang.reflect.InvocationTargetException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.*
import java.util.function.Consumer
import java.util.function.BiFunction
import org.locationtech.jts.geom.Envelope
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.geom.Polygon
import org.mif.segmentation.ExactTileFinalizer
import org.bytedeco.opencv.global.opencv_core
import qupath.lib.analysis.features.ObjectMeasurements
import qupath.lib.analysis.images.ContourTracing
import qupath.lib.analysis.images.SimpleImages
import qupath.lib.images.servers.PixelType
import qupath.lib.objects.CellTools
import qupath.lib.objects.PathCellObject
import qupath.lib.objects.PathObjects
import qupath.lib.objects.utils.ObjectProcessor
import qupath.lib.regions.RegionRequest
import qupath.lib.roi.GeometryTools
import qupath.opencv.ops.ImageDataOp
import qupath.opencv.ops.ImageOp
import qupath.opencv.ops.ImageOps

final class MifAlternativeResults {
    long pixels, objects
    int tiles
    long nPixelsProcessed() { pixels }
    int nTilesProcessed() { tiles }
    int nTilesFailed() { 0 }
    long nObjectsDetected() { objects }
    boolean wasInterrupted() { false }
}

final class MifAlternativeSegmentation {
    static Object field(Object object, String name) {
        def field = object.class.getDeclaredField(name); field.setAccessible(true); field.get(object)
    }
    static Object invokeNative(Object object, String name, Object... args) {
        def method = object.class.declaredMethods.find { it.name == name && it.parameterCount == args.length }
        if (method == null) throw new IllegalStateException("Pinned StarDist API is unavailable: " + name)
        method.setAccessible(true)
        try { return method.invoke(object, args) }
        catch (InvocationTargetException error) { throw error.cause }
    }
    static Object canonical(Object value) {
        if (value instanceof Map) {
            Map sorted = new TreeMap(); value.each { key, item -> sorted[key] = canonical(item) }; return sorted
        }
        if (value instanceof List) return value.collect { canonical(it) }
        return value
    }
    static String digest(Object value) {
        def gson = new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().create()
        java.security.MessageDigest.getInstance("SHA-256").digest(gson.toJson(canonical(value)).getBytes("UTF-8")).encodeHex().toString()
    }
    static Map normalize(Map selected) {
        Map normalized = new LinkedHashMap(selected)
        ["tile_size_px", "padding_px", "workers"].each { name -> normalized[name] = (selected[name] as Number).intValue() }
        Map parameters = new LinkedHashMap(selected.parameters as Map)
        parameters.channels = parameters.channels.collect { (it as Number).intValue() }
        normalized.parameters = parameters
        return normalized
    }
    static Map select(Object raw, Object sha, Object admitted, Object evidence, boolean excluded, Object revision) {
        if (!(raw instanceof Map) || raw.keySet() != ["model_path", "model_version", "device", "tile_size_px", "padding_px", "workers", "scope", "method", "parameters"] as Set)
            throw new IllegalArgumentException("Alternative segmentation settings are incomplete")
        Map selected = normalize(raw as Map)
        if (!(selected.method in ["cellpose", "stardist"]) || digest(selected) != sha || selected.workers != 1)
            throw new IllegalArgumentException("Alternative segmentation selection differs from its request")
        if (admitted != null && revision == null && digest(normalize(admitted as Map)) != sha)
            throw new IllegalArgumentException("Alternative segmentation differs from admitted settings")
        if (revision != null && digest(normalize(revision.settings as Map)) != sha)
            throw new IllegalArgumentException("Alternative segmentation differs from its reviewed revision")
        if (!excluded && (!(evidence instanceof Map) || evidence.model_path != selected.model_path || evidence.model_version != selected.model_version))
            throw new IllegalArgumentException("Alternative detector lacks its selected installed model")
        if (!excluded) for (def item : evidence.model_files) {
            Path path = Paths.get(selected.model_path as String).resolve(item.relative_path as String)
            if (!Files.isRegularFile(path) || Files.size(path) != (item.size_bytes as Number).longValue())
                throw new IllegalStateException("Selected detector asset is missing or changed: " + path.fileName)
        }
        return selected
    }
    static String awaitLine(Process process, BufferedReader reader) {
        while (!reader.ready()) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException("Segmentation cancelled")
            if (!process.isAlive()) throw new IllegalStateException("Cellpose worker exited; see cellpose.log")
            Thread.sleep(25)
        }
        String line = reader.readLine()
        if (line == null) throw new IllegalStateException("Cellpose worker closed its result stream")
        return line
    }
    static List cellposeOverlap(Collection candidates) {
        // Prefer the prediction with the most image context. Resolve duplicates without merging cells.
        List sorted = new ArrayList(candidates)
        sorted.sort { a, b -> Double.compare(b.measurementList.get("Segmentation: tile margin"), a.measurementList.get("Segmentation: tile margin")) }
        List kept = []
        for (def cell : sorted) {
            Geometry original = cell.ROI.geometry
            Geometry geometry = original
            for (def other : kept) {
                if (geometry.envelopeInternal.intersects(other.ROI.geometry.envelopeInternal))
                    geometry = GeometryTools.ensurePolygonal(geometry.difference(other.ROI.geometry))
                if (geometry.isEmpty()) break
            }
            if (geometry instanceof Polygon && geometry.area >= original.area * 0.5d) {
                def result = PathObjects.createCellObject(GeometryTools.geometryToROI(geometry, cell.ROI.imagePlane), null)
                result.measurementList.putAll(cell.measurementList)
                kept << result
            }
        }
        return kept
    }
    static MifAlternativeResults detect(Map settings, image, parent, Path work, Consumer sink) {
        def cal = image.server.pixelCalibration
        double x = cal.pixelWidthMicrons, y = cal.pixelHeightMicrons
        if (!image.serverMetadata.pixelSizeCalibrated() || !Double.isFinite(x) || !Double.isFinite(y) || x <= 0 || y <= 0 || Math.abs(x-y) > 0.01d * x)
            throw new IllegalArgumentException("Alternative segmentation requires calibrated, approximately square X/Y pixels")
        Map params = settings.parameters as Map
        List<Integer> channels = params.channels as List<Integer>
        if (channels.any { it < 0 || it >= image.server.nChannels() } || channels.toSet().size() != channels.size() || channels.isEmpty() || channels.size() > (settings.method == "stardist" ? 1 : 3))
            throw new IllegalArgumentException("Select the actual image channels for this detector")
        double downsample = (params.pixel_size_um as Number).doubleValue() / ((x+y)/2d)
        int size = settings.tile_size_px as int, padding = settings.padding_px as int
        int core = (int)Math.round((size-2d*padding)*downsample)
        int pad = (int)Math.round(padding*downsample)
        if (core <= 0 || padding < 0 || size < 128 || size > 2048) throw new IllegalArgumentException("Invalid detector tile settings")
        long allowance = Long.parseLong(System.getenv("MIF_COMPUTE_MEMORY_BYTES") ?: "1073741824")
        if (settings.method == "cellpose" && allowance < 4L*1024*1024*1024)
            throw new IllegalArgumentException("Cellpose-SAM requires at least 4 GiB of admitted working memory")
        long metadata = Long.parseLong(System.getenv("MIF_METADATA_CACHE_BYTES") ?: "33554432")
        def geometry = parent.ROI.geometry
        def bounds = geometry.envelopeInternal
        List<RegionRequest> requests = []
        List<RegionRequest> padded = []
        for (int yy = Math.max(0, (int)Math.floor(bounds.minY/core)*core); yy < bounds.maxY; yy += core) {
            for (int xx = Math.max(0, (int)Math.floor(bounds.minX/core)*core); xx < bounds.maxX; xx += core) {
                int width = Math.min(core, image.server.width-xx), height = Math.min(core, image.server.height-yy)
                if (width <= 0 || height <= 0 || !geometry.intersects(GeometryTools.createRectangle(xx,yy,width,height))) continue
                if ((requests.size()+1L)*512L > metadata/2) throw new IllegalArgumentException("Detector tile plan exceeds its metadata allowance")
                def request = RegionRequest.createInstance(image.server.path,downsample,xx,yy,width,height,parent.ROI.z,parent.ROI.t)
                requests << request
                padded << request.pad2D(qupath.lib.regions.Padding.symmetric(pad)).intersect2D(0,0,image.server.width,image.server.height)
            }
        }
        double expansion = settings.method == "stardist" ? (params.cell_expansion_um as Number).doubleValue()/((x+y)/2d) : 0d
        List<Envelope> footprints = padded.collect { request -> def e = new Envelope(request.x,request.maxX,request.y,request.maxY);e.expandBy(expansion+downsample+0.02d);e }
        def result = new MifAlternativeResults()
        def gson = new com.google.gson.Gson()
        Process process = null
        BufferedReader reader = null
        BufferedWriter writer = null
        def detector = null
        def loader = null
        def starOp = null
        def potentialConstructor = null
        def fullAreaField = null
        ObjectProcessor reconciliation
        Closure starConvert
        try {
            opencv_core.setNumThreads(1)
            if (settings.method == "stardist") {
                Path jar = Paths.get(settings.model_path as String).resolve("qupath-extension-stardist-0.6.0.jar")
                loader = new java.net.URLClassLoader([jar.toUri().toURL()] as URL[], MifAlternativeSegmentation.class.classLoader)
                def type = loader.loadClass("qupath.ext.stardist.StarDist2D")
                def builder = type.builder(Paths.get(settings.model_path as String).resolve("dsb2018_heavy_augment.pb").toString())
                    .channels(channels.collect { it.intValue() } as int[]).threshold((params.probability_threshold as Number).doubleValue())
                    .normalizePercentiles(1d,99.8d).pixelSize((params.pixel_size_um as Number).doubleValue())
                    .padding(padding).tileSize(size).nThreads(1).simplify(0d).includeProbability(true)
                if (params.containsKey("cell_constrain_scale"))
                    builder.cellConstrainScale((params.cell_constrain_scale as Number).doubleValue())
                detector = builder.build()
                def ops = [ImageOps.Core.ensureType(PixelType.FLOAT32)] + (field(detector,"preprocess") as List) + [ImageOps.Core.ensureType(PixelType.FLOAT32)]
                ImageDataOp originalOp = (field(detector,"op") as ImageDataOp).appendOps(ops as ImageOp[])
                // The upstream tile method catches IOException. Convert that failure to an unchecked
                // exception so a failed image read cannot masquerade as a successful empty tile.
                starOp = java.lang.reflect.Proxy.newProxyInstance(ImageDataOp.class.classLoader,[ImageDataOp] as Class[],
                    { proxy, method, args ->
                        try { method.invoke(originalOp,args) }
                        catch (InvocationTargetException error) {
                            if (error.cause instanceof IOException) throw new UncheckedIOException(error.cause as IOException)
                            throw error.cause
                        }
                    } as java.lang.reflect.InvocationHandler)
                def potential = loader.loadClass('qupath.ext.stardist.StarDist2D$PotentialNucleus')
                potentialConstructor = potential.declaredConstructors.first();potentialConstructor.setAccessible(true)
                fullAreaField = potential.getDeclaredField("fullArea");fullAreaField.setAccessible(true)
                starConvert = { nucleus ->
                    def object = MifAlternativeSegmentation.invokeNative(detector,"convertToObject",nucleus,parent.ROI.imagePlane,expansion,geometry)
                    if (object == null) return null
                    if (!(object instanceof PathCellObject)) object = PathObjects.createCellObject(object.ROI,object.ROI)
                    object.measurementList.put("StarDist: probability",(field(nucleus,"probability") as Number).doubleValue())
                    object.measurementList.put("StarDist: original nucleus area",(field(nucleus,"fullArea") as Number).doubleValue())
                    return object
                }
                reconciliation = { candidates ->
                    List nuclei = candidates.collect { cell ->
                        def n = potentialConstructor.newInstance(cell.nucleusROI.geometry,cell.measurementList.get("StarDist: probability"),0)
                        fullAreaField.setDouble(n,cell.measurementList.get("StarDist: original nucleus area"));n
                    }
                    List kept = (MifAlternativeSegmentation.invokeNative(detector,"filterNuclei",nuclei) as List).collect { starConvert(it) }.findAll { it != null }
                    return expansion > 0 ? CellTools.constrainCellOverlaps(kept) : kept
                } as ObjectProcessor
            } else {
                Path settingsPath = work.resolve("cellpose-settings.json")
                Files.writeString(settingsPath,gson.toJson(settings))
                List command = gson.fromJson(System.getenv("MIF_CELLPOSE_COMMAND"),List.class)
                if (command == null || command.isEmpty()) throw new IllegalStateException("The installed Cellpose worker is unavailable")
                command = new ArrayList(command); command.addAll(["--settings",settingsPath.toString()])
                process = new ProcessBuilder(command as List<String>).redirectError(work.resolve("cellpose.log").toFile()).start()
                reader = new BufferedReader(new InputStreamReader(process.inputStream,"UTF-8"))
                writer = new BufferedWriter(new OutputStreamWriter(process.outputStream,"UTF-8"))
                if (gson.fromJson(awaitLine(process,reader),Map.class).ready != true) throw new IllegalStateException("Cellpose model did not become ready")
                reconciliation = { candidates -> cellposeOverlap(candidates) } as ObjectProcessor
            }
            Consumer finalized = { cell ->
                cell.metadata.put("segmentation_method",settings.method as String)
                cell.metadata.put("cell_boundary_source",settings.method == "cellpose" ? "learned_cell" : expansion > 0 ? "estimated_nuclear_expansion" : "nuclear_outline")
                cell.metadata.put("nucleus_boundary_source",settings.method == "cellpose" ? "not_segmented" : "stardist_nucleus")
                ObjectMeasurements.addShapeMeasurements(cell,cal,ObjectMeasurements.ShapeFeatures.values())
                sink.accept(cell)
            } as Consumer
            long objects = settings.method == "cellpose" ? Math.max(32L*1024*1024,allowance/16) : allowance/4
            def finalizer = new ExactTileFinalizer(footprints,work.resolve("alternative-frontier.bin"),objects,metadata, reconciliation,finalized)
            try {
                for (int tile=0; tile<requests.size(); tile++) {
                    if (Thread.currentThread().isInterrupted()) throw new InterruptedException("Segmentation cancelled")
                    List cells
                    if (settings.method == "stardist") {
                        List nuclei = MifAlternativeSegmentation.invokeNative(detector,"detectObjectsForTile",starOp,field(detector,"dnn"),image,requests[tile],requests.size()>1,geometry) as List
                        cells = nuclei.collect { starConvert(it) }.findAll { it != null }
                    } else {
                        def request = padded[tile]
                        def pixels = image.server.readRegion(request)
                        def raster = pixels.raster
                        int width=pixels.width,height=pixels.height
                        def buffer = ByteBuffer.allocate(Math.multiplyExact(width*height,channels.size()*4)).order(ByteOrder.LITTLE_ENDIAN)
                        for (int yy=0;yy<height;yy++) for (int xx=0;xx<width;xx++) for(int channel : channels) buffer.putFloat(raster.getSampleFloat(xx,yy,channel))
                        Path input=work.resolve("cellpose-input.f32"),output=work.resolve("cellpose-output.i32")
                        Files.write(input,buffer.array())
                        writer.write(gson.toJson([input:input.toString(),output:output.toString(),shape:[height,width,channels.size()]]));writer.newLine();writer.flush()
                        Map reply=gson.fromJson(awaitLine(process,reader),Map.class)
                        if (reply.shape != [height,width] || !Files.isRegularFile(output) || Files.size(output)!=width*height*4L)
                            throw new IllegalStateException("Cellpose tile output dimensions differ")
                        def labels=ByteBuffer.wrap(Files.readAllBytes(output)).order(ByteOrder.LITTLE_ENDIAN)
                        float[] values=new float[width*height]
                        for(int i=0;i<values.length;i++) values[i]=labels.getInt()
                        cells=ContourTracing.createObjects(SimpleImages.createFloatImage(values,width,height),request,1,-1,
                            { roi,label -> PathObjects.createCellObject(roi,null) } as BiFunction)
                        cells=cells.collect { cell ->
                            def raw=cell.ROI.geometry
                            def clipped=GeometryTools.ensurePolygonal(raw.intersection(geometry))
                            if (clipped.isEmpty()) return null
                            def roi=GeometryTools.geometryToROI(clipped,cell.ROI.imagePlane)
                            def kept=PathObjects.createCellObject(roi,null)
                            def c=raw.centroid
                            kept.measurementList.put("Segmentation: tile margin",Math.min(Math.min(c.x-request.x,request.maxX-c.x),Math.min(c.y-request.y,request.maxY-c.y)))
                            return kept
                        }.findAll { it != null }
                        Files.delete(input);Files.delete(output)
                    }
                    if (Thread.currentThread().isInterrupted() ||
                            (detector != null && field(detector,"cancelRuns") == true))
                        throw new InterruptedException("Segmentation cancelled")
                    finalizer.completeTile(tile,cells)
                    result.tiles++;result.pixels+=(long)Math.ceil(padded[tile].width/downsample)*(long)Math.ceil(padded[tile].height/downsample)
                }
                if (Thread.currentThread().isInterrupted()) throw new InterruptedException("Segmentation cancelled")
                finalizer.finish();result.objects=finalizer.finalCount
            } finally { finalizer.close() }
            return result
        } finally {
            if (process != null) { process.destroyForcibly(); process.waitFor() }
            if (detector != null) detector.close()
            if (loader != null) loader.close()
        }
    }
}
return MifAlternativeSegmentation
