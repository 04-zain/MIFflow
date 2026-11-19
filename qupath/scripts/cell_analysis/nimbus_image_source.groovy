/**
 * Supplies read-only, bounded canonical pixels and finalized cell masks to Nimbus.
 *
 * We open one saved image at a time and serve its requests until Python closes
 * that source. Dense mask indices refer to the stable identities in ready.json;
 * they never replace native cell IDs. Pixel-centre coverage rasterizes the
 * existing cell ROI, including holes. Overlapping cell interiors fail because
 * Nimbus requires a single instance label per pixel. No hierarchy is saved.
 */
import groovy.json.JsonSlurper
import qupath.lib.io.GsonTools
import java.awt.image.BufferedImage
import java.nio.*
import java.nio.file.*
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import qupath.lib.projects.ProjectIO
import qupath.lib.regions.RegionRequest
import org.locationtech.jts.geom.Envelope
import org.locationtech.jts.geom.GeometryFactory
import org.mif.cells.NativeCellScope
import org.mif.image.CellImageSamples
import java.util.function.Consumer

if (args.size() != 2) throw new IllegalArgumentException('Expected source request and evidence paths')
Path requestPath = Paths.get(args[0]).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1]).toAbsolutePath().normalize()
def json = new JsonSlurper()
def gson = GsonTools.getInstance().newBuilder().disableHtmlEscaping().serializeNulls().create()
Map request = json.parse(requestPath.toFile()) as Map
if (request.schema_version != 'mif-nimbus-native-source/v1' || !(request.sources instanceof List) || request.sources.isEmpty())
    throw new IllegalArgumentException('Invalid Nimbus native source request')
Closure canonical
canonical = { value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap()
        value.each { key, child -> sorted[key as String] = canonical(child) }
        return sorted
    }
    if (value instanceof List) return value.collect { canonical(it) }
    value
}
def digest = { value ->
    String jsonText = gson.toJson(canonical(value))
    StringBuilder ascii = new StringBuilder()
    jsonText.chars().forEach { code ->
        if (code >= 128) ascii.append(String.format('\\u%04x', code))
        else ascii.append((char)code)
    }
    MessageDigest.getInstance('SHA-256').digest(ascii.toString().getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
}
def publish = { Path target, Object value ->
    Path temporary = target.resolveSibling(target.fileName.toString() + '.tmp')
    Files.writeString(temporary, gson.toJson(value), StandardOpenOption.CREATE_NEW)
    Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE)
}
Path projectPath = Paths.get(request.project_file as String).toAbsolutePath().normalize()
def project = ProjectIO.loadProject(projectPath.toFile(), BufferedImage.class)
List completedSources = []
for (Map source : request.sources) {
    Path directory = Paths.get(source.bridge_directory as String).toAbsolutePath().normalize()
    if (!Files.isDirectory(directory) || Files.isSymbolicLink(directory))
        throw new IllegalArgumentException('Nimbus bridge requires an existing private directory')
    def entries = project.getImageList().findAll { it.getID().toString() == source.project_entry_id }
    if (entries.size() != 1) throw new IllegalStateException('Nimbus requires one exact saved project image')
    def entry = entries.first()
    if (entry.getMetadataMap().get('mif_pipeline.image_role') != 'primary_mif' ||
            entry.getMetadataMap().get('mif_pipeline.source_image_sha256') != source.canonical_image_sha256)
        throw new IllegalStateException('Nimbus canonical image binding is stale')
    def imageData = entry.readImageData()
    def server = imageData.getServer()
    try {
        double magnification = server.getMetadata().getMagnification()
        String magnificationSource = 'image_metadata'
        if (!Double.isFinite(magnification) || magnification <= 0) {
            // Saved display metadata can omit an acquisition value that is
            // available from the image server's retained OME metadata.
            magnification = server.getOriginalMetadata().getMagnification()
        }
        if (!Double.isFinite(magnification) || magnification <= 0) {
            if (!(source.magnification_fallback instanceof Number) ||
                    !Double.isFinite(source.magnification_fallback.doubleValue()) || source.magnification_fallback.doubleValue() <= 0) {
                String message = 'Nimbus needs the objective magnification. It is missing from this image’s metadata; enter it in the Nimbus settings.'
                publish(directory.resolve('prerequisite.json'), [code:'magnification_missing', message:message])
                throw new IllegalStateException(message)
            }
            magnification = source.magnification_fallback.doubleValue()
            magnificationSource = 'configured_acquisition'
        }
        if (server.nZSlices() != 1 || server.nTimepoints() != 1)
            throw new IllegalStateException('Nimbus currently requires a single-plane canonical image')
        List<String> channels = server.getMetadata().getChannels().collect { it.getName() }
        if (channels.toSet().size() != channels.size())
            throw new IllegalStateException('Nimbus canonical channel names must be unique')
        Map providers = [:]
        List identities = []
        source.scopes.each { scope ->
            String sectionId = scope.identity[3] as String
            if (providers.containsKey(sectionId)) throw new IllegalStateException('Nimbus repeats an accepted section')
            def provider = NativeCellScope.open(imageData, sectionId).join()
            if (provider.getPopulation() != scope.segmentation_population_id)
                throw new IllegalStateException('Nimbus segmentation population is stale')
            providers[sectionId] = provider
            List<String> ids = []
            provider.forEach({ cell ->
                Map metadata = cell.metadata
                if (metadata.pipeline_stage != 'segmentation' || metadata.slide_id != scope.identity[2] ||
                        metadata.section_id != sectionId || metadata.generation_id != scope.tissue_generation_id ||
                        metadata.segmentation_population_id != scope.segmentation_population_id ||
                        metadata.segmentation_section_population_sha256 != scope.segmentation_population_sha256 ||
                        metadata.scientific_measurements_stale == 'true' || metadata.'mif.measurements_stale' == 'true')
                    throw new IllegalStateException('Nimbus finalized cell scope or accepted measurements are stale')
                if (!cell.ROI || !cell.ROI.geometry.isValid() || cell.ROI.z != 0 || cell.ROI.t != 0)
                    throw new IllegalStateException('Nimbus requires finalized geometry in the canonical image plane')
                String id = metadata.cell_id
                if (!id) throw new IllegalStateException('Nimbus requires stable cell identities')
                ids << id
                List identity = new ArrayList(scope.identity);identity.add(id);identities.add(identity)
            } as Consumer)
            ids.sort()
            if (ids.toSet().size() != ids.size() || digest(ids) != scope.cell_ids_sha256)
                throw new IllegalStateException('Nimbus finalized cell identity inventory changed')
        }
        identities.sort { a,b -> gson.toJson(a) <=> gson.toJson(b) }
        if (identities.size() >= Integer.MAX_VALUE) throw new IllegalStateException('Nimbus instance labels exceed int32 capacity')
        List<String> identityKeys = identities.collect { gson.toJson(it) }
        if (identityKeys.toSet().size() != identityKeys.size())
            throw new IllegalStateException('Nimbus repeats a full cell identity')
        publish(directory.resolve('ready.json'), [width:server.getWidth(),height:server.getHeight(),magnification:magnification,
            magnification_source:magnificationSource,
            channels:channels,identities:identities,scopes:source.scopes.collect { scope ->
                [identity:scope.identity,canonical_image_id:scope.canonical_image_id,segmentation_population_id:scope.segmentation_population_id]
            }])
        long lastRequest = System.nanoTime()
        long lastProcessedId = 0
        boolean closed = false
        GeometryFactory factory = new GeometryFactory()
        while (!closed) {
            if (Files.exists(requestPath.parent.resolve('cancel'))) throw new InterruptedException('Nimbus source cancelled')
            Path operationPath = directory.resolve('request.json')
            // Python retires each request after consuming its response. Read the
            // request directly: a separate existence check can race that removal.
            String operationJson = null
            if (!Files.exists(directory.resolve('response.json'))) {
                try { operationJson = Files.readString(operationPath, StandardCharsets.UTF_8) }
                catch (NoSuchFileException ignored) { /* No pending request yet. */ }
            }
            if (operationJson == null) {
                if ((System.nanoTime()-lastRequest)/1e9 > 600) throw new IllegalStateException('Nimbus source timed out waiting for its worker')
                Thread.sleep(20); continue
            }
            Map operation = json.parseText(operationJson) as Map
            if (!(operation.request_id instanceof Number) || operation.request_id.longValue() <= lastProcessedId) {
                Thread.sleep(20); continue
            }
            lastProcessedId = operation.request_id.longValue()
            lastRequest = System.nanoTime()
            try {
                if (operation.operation == 'close') {
                    publish(directory.resolve('response.json'), [request_id:operation.request_id,status:'ok'])
                    closed = true; continue
                }
                List numbers = ['x','y','width','height'].collect { operation[it] }
                if (numbers.any { !(it instanceof Number) || it.doubleValue() != it.intValue() })
                    throw new IllegalArgumentException('Nimbus rectangle must contain integers')
                int x=operation.x, y=operation.y, width=operation.width, height=operation.height
                boolean occupancy = operation.operation == 'has_cells'
                if (x<0 || y<0 || width<1 || height<1 || (!occupancy && (width>1024 || height>1024)) ||
                        (long)x+width>server.getWidth() || (long)y+height>server.getHeight())
                    throw new IllegalArgumentException('Nimbus rectangle exceeds the bounded canonical image')
                if (occupancy) {
                    // Output occupancy uses existing group bounds and moved edits;
                    // it allocates neither an image window nor a cell-mask plane.
                    Envelope bounds = new Envelope(x,x+width,y,y+height)
                    boolean possible = providers.values().any { it.mayHaveCellsInBounds(bounds) }
                    publish(directory.resolve('response.json'),
                        [request_id:operation.request_id,status:'ok',has_cells:possible])
                    continue
                }
                ByteBuffer buffer
                List shape
                String dtype
                if (operation.operation == 'pixels') {
                    List selected = operation.channels
                    if (!selected || selected.toSet().size()!=selected.size() || !channels.containsAll(selected))
                        throw new IllegalArgumentException('Nimbus selected channels do not match canonical names')
                    def image = server.readRegion(RegionRequest.createInstance(server.getPath(),1.0,x,y,width,height,0,0))
                    if (image.getWidth()!=width || image.getHeight()!=height)
                        throw new IllegalStateException('Native image reader returned an incomplete rectangle')
                    List<Integer> bands = selected.collect { channels.indexOf(it) }
                    buffer = CellImageSamples.nimbusPixels(image.getRaster(),width,height,bands)
                    shape=[selected.size(),height,width]; dtype='<f4'
                } else if (operation.operation == 'labels') {
                    int[] labels = new int[width*height]
                    Envelope bounds = new Envelope(x,x+width,y,y+height)
                    source.scopes.each { scope ->
                        providers[scope.identity[3]].forEachInBounds(bounds, { cell ->
                            List identity = new ArrayList(scope.identity);identity.add(cell.metadata.cell_id)
                            int ordinal = Collections.binarySearch(identityKeys, gson.toJson(identity))
                            if (ordinal < 0) throw new IllegalStateException('Nimbus mask names an unaccepted cell')
                            CellImageSamples.paintNimbusCell(labels,x,y,width,height,
                                cell.ROI.geometry,ordinal+1,factory)
                        } as Consumer)
                    }
                    buffer=CellImageSamples.nimbusLabels(labels)
                    shape=[height,width]; dtype='<i4'
                } else throw new IllegalArgumentException('Unknown Nimbus source operation')
                Path dataPath=directory.resolve('response.bin')
                Files.write(dataPath,buffer.array(),StandardOpenOption.CREATE_NEW)
                publish(directory.resolve('response.json'),[request_id:operation.request_id,status:'ok',path:'response.bin',dtype:dtype,shape:shape])
            } catch (Exception error) {
                publish(directory.resolve('response.json'),[request_id:operation.request_id,status:'error',error:error.getMessage()])
                throw error
            }
        }
        completedSources.add([project_entry_id:source.project_entry_id,cell_count:identities.size()])
    } finally { NativeCellScope.close(imageData); server.close() }
}
publish(evidencePath,[schema_version:'mif-nimbus-native-evidence/v1',sources:completedSources])
