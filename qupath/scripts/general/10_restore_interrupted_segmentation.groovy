import qupath.lib.io.PathIO
import qupath.lib.io.GsonTools
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.io.ByteOrderValues
import java.security.MessageDigest
import java.nio.file.Files
import java.util.function.Consumer
import groovy.json.JsonOutput
import groovy.json.JsonSlurper

// Validate one captured whole-tissue section and save a separate candidate only.
// The Python owner authenticates accepted artifacts and publishes the restoration.
def request = new JsonSlurper().parse(new File(args[0]))
def root = new File(request.capture_directory as String).canonicalFile
def fixture = request
assert fixture.images.size() == 1
File candidate = new File(request.candidate as String)
assert !candidate.exists()
def sha = { byte[] bytes -> MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString() }
def nativeJar = new File(request.native_reader.path as String).canonicalFile
assert sha(nativeJar.bytes) == request.native_reader.sha256
def loader = this.class.classLoader
loader.addURL(nativeJar.toURI().toURL())
def roiInventory = loader.loadClass('org.mif.roi.RoiInventory')
assert sha(new File(roiInventory.protectionDomain.codeSource.location.toURI()).bytes) == request.native_reader.sha256
def gson = GsonTools.getInstance()
def canonicalGson = new com.google.gson.GsonBuilder().serializeNulls().disableHtmlEscaping().create()
Closure canonical
canonical = { value -> value instanceof Map ? new TreeMap(value.collectEntries {k,v -> [(k.toString()):canonical(v)]}) : value instanceof List ? value.collect {canonical(it)} : value }
def digest = { value -> sha(canonicalGson.toJson(canonical(value)).getBytes('UTF-8')) }
def geometryDigest = { geometry ->
    assert geometry != null && !geometry.isEmpty() && geometry.isValid()
    def normalized = geometry.copy()
    normalized.normalize()
    sha(new WKBWriter(2,ByteOrderValues.LITTLE_ENDIAN,false).write(normalized))
}
def outcomes = []
fixture.images.each { input ->
    def dataPath = new File(root,"captured/qupath/data/${input.entry_id}/data.qpdata")
    def data = PathIO.readImageData(dataPath)
    def hierarchy = data.hierarchy
    def all = []
    def queue = new ArrayDeque([hierarchy.rootObject])
    while (!queue.isEmpty()) {
        def object = queue.removeFirst()
        all << object
        object.childObjects.each { queue.add(it) }
    }
    assert all.collect {it.ID}.toSet().size() == all.size()
    assert hierarchy.TMAGrid == null
    assert hierarchy.rootObject.metadata.isEmpty() && hierarchy.rootObject.measurementList.isEmpty()
    assert data.imageType.name() == 'FLUORESCENCE'
    assert data.historyWorkflow.steps.every { it.name == 'Set image type' && gson.fromJson(gson.toJson(it.parameterMap),Map) == ['Image type':'FLUORESCENCE'] }
    def artifacts = input.accepted_artifacts
    def acceptedTissue = artifacts.tissue_acceptance_result.owning_identity
    def acceptedDomain = artifacts.accepted_analysis_domain.owning_identity
    def acceptedPrimary = artifacts.primary_inference_result.owning_identity
    def domain = acceptedDomain.domain_authorities[0]
    assert domain.analysis_domain_kind == 'whole_tissue_section'
    assert acceptedDomain.selection_mode == 'whole_tissue_direct'
    def tissues = all.findAll { it.metadata.get('pipeline_stage') == 'tissue_detection' }
    assert tissues.size() == 1
    def tissue = tissues[0]
    Set tissueKeys = 'canonical_image_id canonical_image_sha256 derived_from detector_id detector_settings_hash detector_version display_number generation_id generation_status native_level_read_attestation pipeline_stage pipeline_stage_version provenance_record_id reference_channel_id reference_channel_selection_source review_status revision roi_id run_id section_id slide_id source supersedes tissue_resolution_selection'.split(' ') as Set
    assert tissue.metadata.keySet() == tissueKeys
    def section = input.tissue_generation.sections[0]
    assert tissue.ID.toString() == section.path_object_id
    assert tissue.parent == hierarchy.rootObject && tissue.isLocked()
    assert tissue.classification == 'Tissue'
    assert tissue.name == 'TissueSection_001' && tissue.color == null
    assert tissue.measurementList.isEmpty()
    def acceptedGeometry = gson.fromJson(gson.toJson(section.geometry),Geometry)
    def tissueGeometrySha = geometryDigest(tissue.ROI.geometry)
    assert tissueGeometrySha == geometryDigest(acceptedGeometry)
    assert tissue.ROI.c == -1 && tissue.ROI.z == 0 && tissue.ROI.t == 0
    ['slide_id','section_id','roi_id','generation_id','review_status'].each { key -> assert tissue.metadata.get(key) == section[key] }
    assert tissue.metadata.get('generation_status') == 'accepted'
    assert tissue.metadata.get('run_id') == acceptedTissue.pipeline_run_id
    assert tissue.metadata.get('canonical_image_sha256') == acceptedTissue.canonical_image_sha256
    assert tissue.metadata.get('detector_settings_hash') == acceptedTissue.detector_settings_hash
    assert tissue.metadata.get('provenance_record_id') == section.provenance_pointer
    assert tissue.metadata.get('revision') == section.revision.toInteger().toString()
    assert tissue.metadata.get('display_number') == section.display_number.toInteger().toString()
    ['canonical_image_id','canonical_image_sha256','detector_id','detector_version',
     'reference_channel_id','reference_channel_selection_source'].each { key ->
        assert tissue.metadata.get(key) == input.tissue_generation[key]
    }
    assert tissue.metadata.get('pipeline_stage_version') == input.tissue_generation.stage_version
    assert tissue.metadata.get('source') == 'classifier'
    ['derived_from','supersedes'].each { key ->
        assert new JsonSlurper().parseText(tissue.metadata.get(key)) == section[key]
    }
    ['native_level_read_attestation','tissue_resolution_selection'].each { key ->
        assert new JsonSlurper().parseText(tissue.metadata.get(key)) == input.tissue_generation[key]
    }
    assert domain.tissue_path_object_id == tissue.ID.toString()
    assert domain.source_path_object_id == tissue.ID.toString()
    assert domain.tissue_generation_id == section.generation_id && domain.tissue_roi_id == section.roi_id
    assert domain.canonical_image_sha256 == acceptedTissue.canonical_image_sha256
    def primaries = all.findAll { it.metadata.get('pipeline_stage') == 'primary_classifier' }
    assert all.size() == 2 + primaries.size()
    assert primaries.every { it.isAnnotation() && it.parent == tissue && it.childObjects.isEmpty() }
    def primaryByRoi = [:]
    def revisions = [:]
    primaries.each { object ->
        def md = object.metadata
        Set primaryKeys = 'analysis_domain_id analysis_domain_kind analysis_domain_path_object_id analysis_domain_revision analysis_domain_revision_sha256 classifier_id classifier_status classifier_version coordinate_system domain_set_sha256 generation_id geometry_sha256 material_parent_revision_sha256 material_parent_type material_parent_type_id object_revision_sha256 parent_path_object_id parent_roi_id pipeline_active pipeline_owner pipeline_run_id pipeline_stage primary_class_id review_status roi_id roi_level section_id slide_id source'.split(' ') as Set
        assert md.keySet() == primaryKeys
        assert md.get('pipeline_owner') == ['primary_classifier',acceptedPrimary.pipeline_run_id,section.generation_id,domain.analysis_domain_id].join('|')
        assert md.get('coordinate_system') == 'global_level0' && md.get('roi_level') == 'primary' && md.get('source') == 'classifier'
        assert md.get('material_parent_type') == 'analysis_domain' && md.get('material_parent_type_id') == 'AnalysisDomain'
        assert md.get('material_parent_revision_sha256') == domain.analysis_domain_revision_sha256
        assert md.get('analysis_domain_kind') == domain.analysis_domain_kind
        assert md.get('analysis_domain_path_object_id') == domain.source_path_object_id
        assert md.get('analysis_domain_revision') == domain.analysis_domain_revision.toString()
        assert md.get('parent_path_object_id') == tissue.ID.toString() && md.get('parent_roi_id') == domain.analysis_domain_id
        assert object.isLocked() && object.name == null && object.color == null
        assert object.measurementList.isEmpty()
        assert object.ROI.imagePlane == tissue.ROI.imagePlane
        assert object.ROI.geometry.coveredBy(tissue.ROI.geometry)
        assert md.get('pipeline_active') == 'true'
        assert md.get('pipeline_run_id') == acceptedPrimary.pipeline_run_id
        assert md.get('generation_id') == section.generation_id
        assert md.get('slide_id') == artifacts.analysis_frame.scope.slide_id && md.get('section_id') == section.section_id
        assert md.get('analysis_domain_id') == domain.analysis_domain_id
        assert md.get('analysis_domain_revision_sha256') == domain.analysis_domain_revision_sha256
        assert md.get('domain_set_sha256') == acceptedDomain.domain_set.domain_set_sha256
        assert md.get('classifier_version') == acceptedPrimary.classifier_version
        assert md.get('classifier_id') == acceptedPrimary.classifier_id
        assert md.get('classifier_status') == 'accepted' && md.get('review_status') == 'accepted'
        def geometrySha = geometryDigest(object.ROI.geometry)
        assert geometrySha == md.get('geometry_sha256')
        def revision = digest([analysis_domain_id:domain.analysis_domain_id,
            analysis_domain_revision_sha256:domain.analysis_domain_revision_sha256,
            analysis_domain_set_sha256:acceptedDomain.domain_set.domain_set_sha256,
            classification:object.classification, geometry_sha256:geometrySha,
            parent_path_object_id:object.parent.ID.toString(), path_object_id:object.ID.toString(),
            pipeline_active:md.get('pipeline_active'), type:'primary',type_id:md.get('primary_class_id')])
        assert revision == md.get('object_revision_sha256')
        assert primaryByRoi.put(md.get('roi_id'),object) == null
        revisions[md.get('roi_id')] = revision
    }
    def observed = [] as Set
    def counts = [:].withDefault {0}
    String producerAttempt = artifacts.primary_inference_result.producer_attempt_id
    roiInventory.read(new File(root,'captured').toPath(),acceptedPrimary.roi_inventory as Map,
        roiInventory.member(producerAttempt), { row ->
            def object = primaryByRoi[row.roiId()]
            assert object != null
            assert object.ID.toString() == row.pathObjectId()
            assert revisions[row.roiId()] == row.objectRevisionSha256()
            assert object.metadata.get('primary_class_id') == row.classId()
            observed.add(row.roiId())
            counts[row.classId()] += 1
        } as Consumer)
    assert observed == primaryByRoi.keySet()
    assert counts == input.roi_class_counts
    def frame = artifacts.analysis_frame.owning_identity
    def server = data.serverMetadata
    assert server.width == frame.spatial_frame.level0_size_x_px && server.height == frame.spatial_frame.level0_size_y_px
    assert server.pixelCalibration.pixelWidthMicrons == frame.spatial_frame.x_um_per_px
    assert server.pixelCalibration.pixelHeightMicrons == frame.spatial_frame.y_um_per_px
    assert server.channels.collect {it.name} == frame.channels.collect {it.name}
    assert data.properties.keySet() == ['PROJECT_ENTRY_ID','mif.segmentation_populations','mif.cell_datasets'] as Set
    def populations = new JsonSlurper().parseText(data.properties.get('mif.segmentation_populations').toString())
    def datasets = new JsonSlurper().parseText(data.properties.get('mif.cell_datasets').toString())
    assert populations == [(section.section_id):request.interrupted_population]
    assert datasets == [(section.section_id):request.interrupted_population.cell_dataset.path]
    def dataset = request.interrupted_population.cell_dataset
    def datasetFile = new File(dataset.path as String)
    assert datasetFile.isFile() && datasetFile.length() <= 32L * 1024L * 1024L
    assert sha(datasetFile.bytes) == dataset.sha256
    def fingerprint = { image ->
        def objects = []; def pending = new ArrayDeque([image.hierarchy.rootObject])
        while (!pending.isEmpty()) {
            def object = pending.removeFirst(); object.childObjects.each { pending.add(it) }
            objects << [id:object.ID.toString(),type:object.class.name,parent:object.parent?.ID?.toString(),
                children:object.childObjects.collect {it.ID.toString()}.sort(), metadata:new TreeMap(object.metadata),
                classification:object.classification,name:object.name,color:object.color,locked:object.isLocked(),
                geometry:object.ROI == null ? null : geometryDigest(object.ROI.geometry),
                plane:object.ROI == null ? null : [object.ROI.c,object.ROI.z,object.ROI.t]]
        }
        [objects:objects.sort {it.id},server:gson.fromJson(gson.toJson(image.serverMetadata),Map),
            builder:gson.fromJson(gson.toJson(image.serverBuilder),Map),image_type:image.imageType.name(),
            history:image.historyWorkflow.steps.collect { [name:it.name,parameters:gson.fromJson(gson.toJson(it.parameterMap),Map)] },
            project_entry:image.properties.get('PROJECT_ENTRY_ID')]
    }
    def before = fingerprint(data)
    assert data.removeProperty('mif.segmentation_populations') != null
    assert data.removeProperty('mif.cell_datasets') != null
    PathIO.writeImageData(candidate,data)
    def reopened = PathIO.readImageData(candidate)
    assert reopened.properties.keySet() == ['PROJECT_ENTRY_ID'] as Set
    def after = fingerprint(reopened)
    // PathIO.readImageData calls setImageType, which adds this documented,
    // idempotent workflow step on each reopen. Preserve the preceding history.
    assert after.history == before.history + [[name:'Set image type',parameters:['Image type':'FLUORESCENCE']]]
    after.history = before.history
    def differences = before.keySet().findAll { before[it] != after[it] }
    assert differences.isEmpty(): "Saved restoration changed: ${differences}"
    reopened.close()
    outcomes << [entry_id:input.entry_id,sample_id:input.sample_id,total_objects:all.size(),
        tissue_count:1,primary_count:primaries.size(),primary_class_counts:counts,
        tissue_normalized_wkb_sha256:tissueGeometrySha,exact_complete_primary_inventory:true,
        normalized_geometry_class_parent_domain_revisions_match:true,
        hierarchy_manual_objects:0,unexpected_properties:[],native_cells:0,
        image_plane:[c:-1,z:0,t:0],prepared_channel_names:server.channels.collect {it.name},
        all_measurement_lists_empty:true,tma_grid_absent:true,
        native_reopen_appended_image_type_step:true]
    data.close()
}
new File(args[1]).text = JsonOutput.prettyPrint(JsonOutput.toJson([
    schema_version:'interrupted-segmentation-restoration/v1',
    source_head_sha256:fixture.source_head_sha256,
    source_authorization_sha256:fixture.source_authorization_sha256,
    original_exit_status:null,old_result_accepted:false,
    candidate_sha256:sha(candidate.bytes),candidate_byte_count:candidate.length(),
    preserved_complete_hierarchy:true,removed_properties:['mif.segmentation_populations','mif.cell_datasets'],
    images:outcomes]))
println "Validated and saved a separate segmentation restoration candidate"
