// Read a current annotation snapshot, prepare a local review, or build a candidate.
// The live cluster .qpdata is never overwritten by this script.
import java.nio.file.Path
import java.nio.file.Files
import java.nio.charset.StandardCharsets
import java.awt.image.BufferedImage
import java.security.MessageDigest
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.StreamReadFeature
import com.fasterxml.jackson.databind.ObjectMapper
import qupath.lib.io.PathIO
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServerProvider
import qupath.lib.projects.ProjectIO
import qupath.lib.projects.Projects
import qupath.lib.objects.classes.PathClass
import qupath.lib.objects.PathObjects
import qupath.lib.roi.ROIs
import org.mif.cells.NativeCellScope

if(args.size()!=2)throw new IllegalArgumentException("Expected request and result paths")
Path requestPath=Path.of(args[0]),resultPath=Path.of(args[1])
if(Files.size(requestPath)>40L*1024*1024 || Files.exists(resultPath))
    throw new IllegalArgumentException("Request is too large or result already exists")
def mapper=new ObjectMapper(JsonFactory.builder().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
Map request=mapper.readValue(Files.readString(requestPath),Map.class)
def sha={byte[] bytes -> MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()}
byte[] helperBytes=Files.readAllBytes(Path.of(request.helper_file))
if(sha(helperBytes)!=request.helper_sha256)throw new IllegalArgumentException("Annotation helper changed")
def helper=new GroovyShell(this.class.classLoader).evaluate(new String(helperBytes,StandardCharsets.UTF_8))
Map reference = request.supervised_reference as Map
// These four small selections open the same immutable bases, scalar results
// and sparse edits in a disposable draft. Only explicit cell-removal drafts
// return a checkpoint, replayed against the source owner before publication.
List<String> cellSelectionFields = ["mif.cell_datasets", "mif.segmentation_populations", "mif.cell_components", "mif.cell_edits"]
def signature={server ->
    def finite={value -> Double.isFinite(value as double) ? value : null}
    return [pixel_type:server.pixelType.toString(),channels:server.nChannels(),
        pixel_width_um:finite(server.pixelCalibration.pixelWidthMicrons),
        pixel_height_um:finite(server.pixelCalibration.pixelHeightMicrons)]
}
Map result
if(request.mode in ["local","local-corrections"]) {
    Path folder=Path.of(request.local_project_folder)
    if(Files.exists(folder.resolve("project.qpproj")))throw new IllegalArgumentException("Choose a new local review folder")
    Files.createDirectories(folder)
    def project=Projects.createProject(folder.toFile(),BufferedImage.class)
    List images=request.mode=="local-corrections"?request.images:[request]
    List entries=[]
    project.setPathClasses(images.collectMany {it.class_names ?: []}.unique().collect {PathClass.fromString(it)})
    images.each {imageRequest->
    def server=ImageServerProvider.buildServer(imageRequest.local_image as String,BufferedImage.class)
    try {
        if(server.width!=imageRequest.snapshot.width || server.height!=imageRequest.snapshot.height)
            throw new IllegalArgumentException("The mounted image dimensions differ from this review")
        if(signature(server)!=imageRequest.image_signature)
            throw new IllegalArgumentException("The mounted image channel type or spatial calibration differs from this review")
        def entry=project.addImage(server.builder);
        entry.setImageName(imageRequest.image_name)
        def data=new ImageData(server, ImageData.ImageType.valueOf(imageRequest.image_type as String))
        Map annotations = helper.draftObjects.call(imageRequest.snapshot as Map, imageRequest.supervised_reference as Map,
            imageRequest.tissue_candidate as Map) as Map
        if(imageRequest.cell_correction!=null)
            annotations.features.addAll(imageRequest.correction_outline.features)
        byte[] objects=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().toJson(annotations).getBytes(StandardCharsets.UTF_8)
        data.hierarchy.insertPathObjects(PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(objects)))
        if (imageRequest.supervised_reference != null || imageRequest.cell_removal != null || imageRequest.cell_correction != null) {
            if (!(imageRequest.cell_selection instanceof Map) ||
                    !imageRequest.cell_selection.keySet().every { it in cellSelectionFields })
                    throw new IllegalArgumentException("The draft has no accepted cell selection")
            imageRequest.cell_selection.each { key, value -> data.setProperty(key as String, value) }
        }
        entry.saveImageData(data);
        project.syncChanges()
        entries << [entry_id:entry.ID.toString(),width:server.width,height:server.height]
    } finally {server.close()}
    }
    result=[project_file:folder.resolve("project.qpproj").toString()]
    if(request.mode=="local")result.putAll(entries.first())
    else result.entries=entries
} else {
    Path projectPath=Path.of(request.project_file)
    byte[] projectBytes=Files.readAllBytes(projectPath)
    if(sha(projectBytes)!=request.project_sha256)throw new IllegalArgumentException("The project changed before review preparation")
    def project=ProjectIO.loadProject(projectPath.toFile(),BufferedImage.class)
    List<String> entryIds = request.mode == "reference-coverage" ?
        request.references_by_entry.keySet().toList().sort() : request.mode=="collect-local-corrections"?request.entry_ids:[request.entry_id]
    List<Map> collectedCorrections=[]
    List<Map> referenceCoverage = []
    Map referenceSupport = null
    if (request.mode == "reference-coverage") {
        byte[] referenceBytes = Files.readAllBytes(Path.of(request.reference_helper_file))
        if (sha(referenceBytes) != request.reference_helper_sha256)
            throw new IllegalArgumentException("The cell-reference helper changed")
        referenceSupport = new GroovyShell(this.class.classLoader).evaluate(
            new String(referenceBytes, StandardCharsets.UTF_8)) as Map
    }
    entryIds.each { selectedEntryId ->
        def entry=project.imageList.find {it.ID.toString()==selectedEntryId}
        if(entry==null)throw new IllegalArgumentException("The selected image is absent from this project")
        Path source=entry.entryPath.resolve("data.qpdata")
        if(!Files.isRegularFile(source) || Files.size(source)>256L*1024*1024)
            throw new IllegalArgumentException("The saved image objects are unavailable or too large for this annotation handoff")
        byte[] before=Files.readAllBytes(source);
        String beforeSha=sha(before)
        def data=entry.readImageData()
        try {
            if (request.mode == "reference-coverage") {
                List currentReferences = request.references_by_entry[selectedEntryId]
                currentReferences.each { scope ->
                    if (scope.project_entry_id != selectedEntryId)
                        throw new IllegalArgumentException("The reference review refers to another image")
                    Map points = referenceSupport.read.call(data, scope)
                    referenceCoverage << [training_plan_id: scope.training_plan_id,
                        project_entry_id: selectedEntryId, split: scope.split,
                        points: points.values().flatten()]
                }
            } else if(request.mode=="export") {
                def uris=entry.serverBuilder.URIs.collect {it.toString()}
                if(uris.size()!=1)throw new IllegalArgumentException("This review requires a single canonical image")
                result=[image_signature:signature(data.server),snapshot:helper.snapshot(data, reference),source_file:source.toString(),source_sha256:beforeSha,
                    project_sha256:request.project_sha256,entry_id:request.entry_id,annotation_feedback:data.getProperty("mif.annotation_feedback"),
                    image_uri:uris[0],image_name:entry.imageName,image_type:data.getImageType().name(),
                    class_names:project.pathClasses.collect {it.toString()},image_channel_names:data.server.metadata.channels.collect {it.name},
                    pixel_width_um:data.server.pixelCalibration.pixelWidthMicrons,
                    pixel_height_um:data.server.pixelCalibration.pixelHeightMicrons]
                if(request.cell_correction!=null) {
                    Map scope=request.cell_correction as Map
                    def provider=NativeCellScope.open(data,scope.section_id as String).join()
                    if(provider.geometryRevision!=(scope.geometry_revision as Number).longValue())
                        throw new IllegalArgumentException("The saved cell edits changed. Reopen the current cell review.")
                    if(scope.cell_edits!=null)
                        provider.selectEdits(data,Path.of(scope.cell_edits.path as String),scope.cell_edits.sha256 as String)
                    result.correction_outline=helper.knownCellDraft.call(data,scope)
                }
                if (reference != null || request.cell_removal != null || request.cell_correction != null) {
                    result.cell_selection = cellSelectionFields.findAll { data.getProperty(it) != null }
                        .collectEntries { [(it): data.getProperty(it)] }
                }
            } else if(request.mode=="cell-correction-preview") {
                if(beforeSha!=request.source_sha256)
                    throw new IllegalArgumentException("The saved cells changed while this draft was open.")
                Map scope=request.cell_correction as Map
                def provider=NativeCellScope.open(data,scope.section_id as String).join()
                if(provider.geometryRevision!=(scope.geometry_revision as Number).longValue())
                    throw new IllegalArgumentException("The saved cell edits changed. Reopen the current cell review.")
                if(scope.cell_edits!=null)
                    provider.selectEdits(data,Path.of(scope.cell_edits.path as String),scope.cell_edits.sha256 as String)
                Map originalSelection=cellSelectionFields.findAll {data.getProperty(it)!=null}
                    .collectEntries {[(it):data.getProperty(it)]}
                if(request.objects.mif_cell_selection!=request.cell_selection || originalSelection!=request.cell_selection)
                    throw new IllegalArgumentException("Reshape the annotation outline; keep the draft's cell records unchanged.")
                List ids=request.correction_outline.features.collect {it.id}
                List operations=helper.correctionOperations.call(scope)
                Set tasks=operations.findAll {it.operation in ["split","merge","add_cell"]}.collect {helper.correctionTaskID.call(it).toString()} as Set
                ids.addAll(operations.findAll {it.operation=="add_nucleus"}.collect {helper.correctionTaskID.call(it).toString()})
                def isOutline={feature -> feature.id in ids || feature.get("properties")?.get("metadata")?.get("mif.correction_task") in tasks}
                Map outlines=[type:"FeatureCollection",features:request.objects.features.findAll {isOutline(it)}]
                Map annotations=[type:"FeatureCollection",features:request.objects.features.findAll {!isOutline(it)}]
                Map changes=helper.apply.call(data,request.snapshot as Map,annotations)
                if(changes.added!=0 || changes.changed!=0 || changes.removed!=0)
                    throw new IllegalArgumentException("Keep the other annotations unchanged in a cell correction draft.")
                Map preview=scope.containsKey("operations") ? helper.correctKnownCells.call(data,scope,outlines,false) :
                    helper.correctKnownCell.call(data,scope,outlines,false)
                result=[preview:preview,outline:outlines,source_sha256:beforeSha]
            } else if(request.mode=="candidate") {
                if(beforeSha!=request.source_sha256)throw new IllegalArgumentException("The saved image objects changed while the annotations were being edited")
                Map originalReferences = reference == null ? [:] : data.hierarchy.annotationObjects
                    .findAll { helper.ownedReference.call(it, reference) }
                    .collectEntries { [(it.ID.toString()): it] }
                Map changes=helper.apply(data,request.snapshot,request.objects,reference,
                    request.primary_reference_parent_ids as List,request.annotation_reference_labels as List,
                    request.tissue_candidate as Map)
                if (request.cell_removal != null) {
                    if (changes.added!=0 || changes.changed!=0 || changes.removed!=0 ||
                            request.objects.containsKey("mif_analysis_region_selection") ||
                            request.objects.containsKey("mif_annotation_feedback"))
                        throw new IllegalArgumentException("Keep tissue and analysis annotations unchanged in a cell-removal draft")
                    changes.putAll(helper.removeCells.call(data,request.cell_removal as Map,
                        request.cell_selection as Map,request.objects.mif_cell_selection as Map))
                } else if(request.objects.containsKey("mif_cell_selection")) {
                    throw new IllegalArgumentException("Cell removals require the stopped measurement task's draft")
                }
                if (reference != null) {
                    byte[] referenceBytes = Files.readAllBytes(Path.of(request.reference_helper_file))
                    if (sha(referenceBytes) != request.reference_helper_sha256)
                        throw new IllegalArgumentException("The cell-reference helper changed")
                    Map resolver = new GroovyShell(this.class.classLoader).evaluate(
                        new String(referenceBytes, StandardCharsets.UTF_8)) as Map
                    Map owners = [:]
                    reference.sections.each { section ->
                        def provider = NativeCellScope.open(data, section.section_id as String).join()
                        if (!provider.ownsPopulation(section.segmentation_population_id as String))
                            throw new IllegalStateException("The selected reference cells changed; open a new draft")
                        Closure eligible = { cell ->
                            Map values = cell.metadata
                            values.get("generation_id") == section.segmentation_generation_id &&
                                values.get("measurement_generation_id") == section.measurement_generation_id &&
                                values.get("analysis_domain_id") in section.analysis_domain_ids
                        }
                        owners[section.section_id] = [section: section, provider: provider, eligible: eligible,
                            resolver: resolver.create.call(reference.project_entry_id as String,
                                section.segmentation_population_id as String, provider,
                                reference.point_selection_radius_level0 as double, eligible)]
                    }
                    Map counts = reference.sections.collectEntries { section ->
                        [(section.section_id): reference.class_ids.collectEntries { [(it): 0] }]
                    }
                    List preparedPoints = []
                    List unresolved = []
                    data.hierarchy.annotationObjects.findAll {
                        it.ROI?.isPoint() && !(it.ID.toString() in request.snapshot.protected_ids)
                    }.each { point ->
                        String classId = point.pathClass?.toString()
                        if (!(classId in reference.class_ids)) {
                            if (point.ID.toString() in request.snapshot.editable_ids &&
                                    !originalReferences.containsKey(point.ID.toString())) return
                            throw new IllegalArgumentException("Use Set class to assign each new reference point to a declared cell type")
                        }
                        def previous = originalReferences[point.ID.toString()]
                        List savedBindings = previous == null ? [] : mapper.readValue(
                            previous.metadata.get("supervised_point_bindings"), List.class)
                        Map preservedBindings = [:]
                        if (previous != null) {
                            List originalCoordinates = previous.ROI.allPoints
                            if (savedBindings.size() != originalCoordinates.size())
                                throw new IllegalStateException("The saved reference points differ from their cell bindings")
                            // QuPath normalizes and sorts MultiPoint coordinates during
                            // GeoJSON import. Match that same representation, never the
                            // incoming point index. A moved point selects its new cell.
                            originalCoordinates.eachWithIndex { original, index ->
                                Map saved = savedBindings[index] as Map
                                if (saved.x_level0 != original.x || saved.y_level0 != original.y)
                                    throw new IllegalStateException("The saved reference coordinates differ from their cell bindings")
                                // Normalize each original point with its own binding;
                                // normalizing a whole group can reorder its points.
                                def single = PathObjects.createAnnotationObject(
                                    ROIs.createPointsROI(original.x, original.y, previous.ROI.imagePlane))
                                def normalized = PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(
                                    mapper.writeValueAsBytes(helper.exportObjects.call([single])))).first()
                                def coordinate = normalized.ROI.allPoints.first()
                                preservedBindings[[coordinate.x, coordinate.y]] = saved
                            }
                        }
                        List prepared = []
                        point.ROI.allPoints.each { coordinate ->
                            Map saved = preservedBindings[[coordinate.x, coordinate.y]] as Map
                            Map item = [coordinate: coordinate, saved: saved, selected: null]
                            if (saved != null) {
                                Map owner = owners[saved.section_id]
                                def cell = owner?.provider?.lookup(saved.cell_id as String)
                                if (cell == null || !owner.eligible.call(cell))
                                    throw new IllegalStateException("A saved reference cell is no longer eligible; open a new reference draft")
                                item.selected = [owner: owner, cell_id: saved.cell_id]
                            } else {
                                unresolved << item
                            }
                            prepared << item
                        }
                        preparedPoints << [point: point, class_id: classId, previous: previous, coordinates: prepared]
                    }
                    List resolved = resolver.resolve.call(owners, unresolved.collect { it.coordinate })
                    unresolved.eachWithIndex { item, index -> item.selected = resolved[index] }
                    Set<String> selectedCells = [] as Set
                    preparedPoints.each { prepared ->
                        def point = prepared.point
                        def previous = prepared.previous
                        String classId = prepared.class_id
                        List bindings = []
                        prepared.coordinates.each { item ->
                            def coordinate = item.coordinate
                            Map saved = item.saved
                            Map selected = item.selected
                            if (selected == null)
                                throw new IllegalArgumentException("A reference point has no eligible cell within the selected radius; move it onto a cell in the retained draft")
                            if (!selectedCells.add(selected.cell_id as String))
                                throw new IllegalArgumentException("Each cell can have only one reference label in this image and role")
                            Map section = selected.owner.section
                            bindings << [point_id: saved?.point_id ?: UUID.randomUUID().toString(),
                                cell_id: selected.cell_id, x_level0: coordinate.x, y_level0: coordinate.y,
                                section_id: section.section_id,
                                segmentation_generation_id: section.segmentation_generation_id,
                                segmentation_population_id: section.segmentation_population_id,
                                measurement_generation_id: section.measurement_generation_id]
                            counts[section.section_id][classId]++
                        }
                        point.metadata.putAll([
                            pipeline_stage: "supervised_training", "mif.training_plan_id": reference.training_plan_id,
                            "mif.reference_selection_sha256": reference.reference_selection_sha256,
                            "mif.split": reference.split, slide_id: reference.slide_id,
                            supervised_image_entry_id: reference.project_entry_id,
                            supervised_evidence_set: reference.reference_role,
                            spatial_region_id: previous?.metadata?.get("spatial_region_id") ?: point.ID.toString(),
                            supervised_point_bindings: mapper.writeValueAsString(bindings),
                        ].collectEntries { key, value -> [(key.toString()): value.toString()] })
                    }
                    changes.supervised_reference_counts = counts
                }
                Path candidate=Path.of(request.candidate_file)
                if(Files.exists(candidate))throw new IllegalArgumentException("The annotation candidate already exists")
                PathIO.writeImageData(candidate,data)
                result=[candidate_file:candidate.toString(),candidate_sha256:sha(Files.readAllBytes(candidate)),
                    source_file:source.toString(),source_sha256:beforeSha,changes:changes]
            } else if(request.mode in ["collect-local","collect-local-corrections"]) {
                def uris=entry.serverBuilder.URIs.collect {it.toString()}
                if(uris.size()!=1)throw new IllegalArgumentException("The local review must retain its original image")
                result=[objects:helper.snapshot(data).objects,image_uri:uris[0],image_signature:signature(data.server)]
                def selection=data.getProperty("mif.analysis_region_selection")
                if(selection!=null)result.objects.mif_analysis_region_selection=mapper.readValue(selection.toString(),List.class)
                if(request.cell_removal != null || request.cell_correction != null || request.mode=="collect-local-corrections") {
                    if(data.hierarchy.detectionObjects.any { !it.isCell() ||
                            !it.metadata.get("mif.cell_id") || !it.metadata.get("mif.population_id") })
                        throw new IllegalArgumentException("This draft accepts existing cell removals only; do not add cells")
                    result.objects.mif_cell_selection = cellSelectionFields.findAll { data.getProperty(it) != null }
                        .collectEntries { [(it): data.getProperty(it)] }
                }
                if(request.mode=="collect-local-corrections")collectedCorrections << (result+[entry_id:selectedEntryId])
            } else throw new IllegalArgumentException("Unknown annotation operation")
            if(sha(Files.readAllBytes(projectPath))!=request.project_sha256 || sha(Files.readAllBytes(source))!=beforeSha)
                throw new IllegalArgumentException("The project or image objects changed during annotation preparation")
        } finally {NativeCellScope.close(data); data.server.close()}
    }
    if (request.mode == "reference-coverage") result = [references: referenceCoverage]
    if (request.mode == "collect-local-corrections") result = [entries: collectedCorrections]
}
Files.writeString(resultPath,mapper.writeValueAsString(result))
