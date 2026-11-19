/** Transform requested objects with Warpy and emit non-authoritative per-object QC. */

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import org.mif.registration.TransferGeometryMetrics
import org.locationtech.jts.io.WKBWriter
import qupath.ext.warpy.Warpy
import qupath.ext.warpy.WarpyExtension
import qupath.lib.common.GeneralTools
import qupath.lib.io.GsonTools
import qupath.lib.objects.PathCellObject
import qupath.lib.regions.ImagePlane
import qupath.lib.roi.ROIs

import static qupath.lib.scripting.QP.*

if (args.size() != 2)
    throw new IllegalArgumentException("Expected Warpy transfer request and evidence paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1] as String).toAbsolutePath().normalize()
def gson = GsonTools.getInstance()
def prettyGson = gson.newBuilder().serializeNulls().setPrettyPrinting().create()
Map request = gson.fromJson(Files.readString(requestPath), Map.class) as Map
Set required = [
    "schema_version", "transform_reader", "project_file", "registration", "source_object_ids",
    "accepted_tissue_sections", "transfer_qc_policy"
] as Set
if (request.keySet() != required ||
        request.schema_version != "qupath-warpy-transfer-request/v2" ||
        !(request.registration instanceof Map) ||
        request.registration.provider != "warpy" ||
        request.registration.provider_version != "0.5.0" ||
        request.registration.direction != "moving_to_fixed" ||
        request.registration.acceptance_state != "accepted" ||
        !(request.source_object_ids instanceof List) ||
        request.source_object_ids.isEmpty() ||
        request.source_object_ids.toSet().size() != request.source_object_ids.size() ||
        !(request.accepted_tissue_sections instanceof List))
    throw new IllegalArgumentException("Invalid Warpy transfer request")
Map registration = request.registration as Map
if (!(registration.runtime_versions instanceof Map) ||
        registration.runtime_versions.warpy != WarpyExtension.getWarpyVersion() ||
        registration.runtime_versions.qupath != GeneralTools.getVersion().toString())
    throw new IllegalStateException("Transfer QC runtime versions changed after registration")

def project = getProject()
if (project == null)
    throw new IllegalStateException("No prepared QuPath project is open")
if (project.getPath().toAbsolutePath().normalize() !=
        Paths.get(request.project_file as String).toAbsolutePath().normalize())
    throw new IllegalArgumentException("Transfer request targets another project")
def movingMatches = project.getImageList().findAll {
    it.getID().toString() == registration.moving_project_entry_id
}
def fixedMatches = project.getImageList().findAll {
    it.getID().toString() == registration.fixed_project_entry_id
}
if (movingMatches.size() != 1 || fixedMatches.size() != 1)
    throw new IllegalStateException("Transfer project-entry IDs must each resolve exactly once")
def movingEntry = movingMatches[0]
def fixedEntry = fixedMatches[0]
validateEntryImage(movingEntry, registration.moving_image_path as String,
    registration.moving_image_sha256 as String)
validateEntryImage(fixedEntry, registration.fixed_image_path as String,
    registration.fixed_image_sha256 as String)
Path transformFile = Paths.get(registration.transform_file as String)
    .toAbsolutePath().normalize()
Path expectedTransform = fixedEntry.getEntryPath().resolve(
    "transform_${fixedEntry.getID()}_${movingEntry.getID()}.json"
).toAbsolutePath().normalize()
if (transformFile != expectedTransform || !Files.isRegularFile(transformFile) ||
        sha256(transformFile) != registration.transform_file_sha256)
    throw new IllegalStateException("Transfer QC transform scope changed after registration")
// Load the same coefficient calculation used for the accepted image overlay.
// Its contents were selected and checked by the current Python installation.
Map reader=request.transform_reader
if(!(reader instanceof Map) || reader.keySet()!=["source","sha256"] as Set ||
   !(reader.source instanceof String) || !(reader.sha256==~/[0-9a-f]{64}/) ||
   reader.source.getBytes(StandardCharsets.UTF_8).length>512*1024 ||
   MessageDigest.getInstance("SHA-256").digest(reader.source.getBytes(StandardCharsets.UTF_8)).encodeHex().toString()!=reader.sha256)
    throw new IllegalArgumentException("The registration transform reader is invalid. Reopen the alignment step.")
Map transformApi=new GroovyShell(this.class.classLoader).evaluate(reader.source,"paired_geometry.groovy")
def transform = transformApi.load(transformFile)
if (transform == null)
    throw new IllegalStateException("Warpy could not load the accepted exact-entry transform")

def movingData = movingEntry.readImageData()
def fixedData = fixedEntry.readImageData()
def movingHierarchy = movingData.getHierarchy()
def fixedHierarchy = fixedData.getHierarchy()
def movingServer = movingData.getServer()
def fixedServer = fixedData.getServer()
Map movingCalibration = calibrationOf(movingServer)
Map fixedCalibration = calibrationOf(fixedServer)
if (!sameCalibration(movingCalibration, registration.moving_physical_calibration) ||
        !sameCalibration(fixedCalibration, registration.fixed_physical_calibration))
    throw new IllegalStateException("Transfer QC physical calibration changed after registration")
double movingAreaScale = movingCalibration.x_um_per_px * movingCalibration.y_um_per_px
double fixedAreaScale = fixedCalibration.x_um_per_px * fixedCalibration.y_um_per_px
def imageBounds = ROIs.createRectangleROI(
    0d, 0d, fixedServer.getWidth() as double, fixedServer.getHeight() as double,
    ImagePlane.getDefaultPlane()
).getGeometry()

List sectionScopes = request.accepted_tissue_sections.collect { row ->
    Set tissueFields = [
        "section_id", "tissue_roi_id", "tissue_path_object_id", "generation_id",
        "generation_status", "review_status", "pipeline_stage"
    ] as Set
    if (!(row instanceof Map) || row.keySet() != tissueFields)
        throw new IllegalArgumentException("Accepted tissue-section scope is invalid")
    def objects = fixedHierarchy.getFlattenedObjectList(null)
    def uuidMatches = objects.findAll {
        it.getID().toString() == row.tissue_path_object_id
    }
    def roiMatches = objects.findAll {
        metadataOf(it).get("roi_id") == row.tissue_roi_id
    }
    if (uuidMatches.size() != 1 || roiMatches.size() != 1 ||
            uuidMatches[0] != roiMatches[0])
        throw new IllegalStateException("Accepted TissueSection PathObject scope is stale")
    def tissueObject = uuidMatches[0]
    Map tissueMetadata = metadataOf(tissueObject)
    if (!transformApi.acceptedTissue(tissueObject) ||
            tissueMetadata.get("pipeline_stage") != row.pipeline_stage ||
            tissueMetadata.get("generation_id") != row.generation_id ||
            tissueMetadata.get("generation_status") != row.generation_status ||
            tissueMetadata.get("review_status") != row.review_status ||
            tissueMetadata.get("section_id") != row.section_id)
        throw new IllegalStateException("Accepted TissueSection PathObject scope is stale")
    [section_id: row.section_id, tissue_roi_id: row.tissue_roi_id,
     object: tissueObject, geometry: tissueObject.getROI().getGeometry()]
}
if (sectionScopes.collect { it.section_id } != registration.tissue_section_ids)
    throw new IllegalArgumentException("Transfer tissue sections differ from registration scope")
if (sectionScopes.collect { it.tissue_roi_id } != registration.tissue_roi_ids ||
        sectionScopes.collect { it.object.getID().toString() } != registration.tissue_path_object_ids)
    throw new IllegalArgumentException("Transfer tissue identities differ from registration scope")
if (registration.tissue_generation_id == null)
    throw new IllegalArgumentException("Accepted registration lacks tissue generation scope")
// Borrow admitted section geometry for this preview only; commit reopens its own inventory.
List transferSections = sectionScopes.collect { section ->
    new TransferGeometryMetrics.Section(section.section_id as String,section.geometry)
}

Map policy = request.transfer_qc_policy as Map
Set policyFields = [
    "min_target_tissue_intersection_fraction", "min_area_ratio", "max_area_ratio",
    "max_fragment_count_increase", "max_out_of_bounds_fraction"
] as Set
if (policy.keySet() != policyFields)
    throw new IllegalArgumentException("Transfer QC policy is invalid")

List<Map> records = []
request.source_object_ids.each { requestedID ->
    def matches = movingHierarchy.getFlattenedObjectList(null).findAll {
        it.getID().toString() == requestedID
    }
    Map base = requestedSourceBase(registration, policy, requestedID as String)
    if (matches.isEmpty()) {
        records << uncertainRecord(base, "source_object_missing")
        return
    }
    if (matches.size() != 1) {
        records << uncertainRecord(base, "source_object_ambiguous")
        return
    }
    def source = matches[0]
    boolean hasDescendants = !source.getChildObjects().isEmpty()
    boolean hasNucleusROI = source instanceof PathCellObject && source.getNucleusROI() != null
    String sourceObjectType = source.getClass().getSimpleName()
    base += [
        source_object_type: sourceObjectType,
        source_has_descendants: hasDescendants,
        source_has_nucleus_roi: hasNucleusROI,
    ]
    if (source.getROI() == null) {
        records << uncertainRecord(base, "source_roi_missing")
        return
    }
    def sourceGeometry = source.getROI().getGeometry()
    String sourceGeometrySha256 = geometrySha256(sourceGeometry)
    String sourceRevisionSha256 = objectRevisionSha256(source, sourceGeometrySha256)
    double originalArea = sourceGeometry.getArea() * movingAreaScale
    int originalFragments = sourceGeometry.getNumGeometries()
    String targetID = base.target_object_id
    base += [
        source_geometry_sha256: sourceGeometrySha256,
        source_object_revision_sha256: sourceRevisionSha256,
        original_area_um2: originalArea,
        original_fragment_count: originalFragments,
    ]
    if (hasDescendants) {
        records << uncertainRecord(base, "source_has_descendants")
        return
    }
    if (sourceObjectType != "PathAnnotationObject" || hasNucleusROI) {
        records << uncertainRecord(base, "unsupported_multi_roi_source")
        return
    }
    if (!Double.isFinite(originalArea) || originalArea <= 0d || originalFragments <= 0) {
        records << uncertainRecord(base + [
            original_area_um2: null, original_fragment_count: null
        ], "source_geometry_unusable")
        return
    }
    try {
        def transformedItems = Warpy.transformPathObjects([source], transform) as List
        if (transformedItems.size() != 1 || transformedItems[0]?.getROI() == null) {
            records << uncertainRecord(base, "warpy_transform_unavailable")
            return
        }
        def transformed = transformedItems[0]
        def geometry = transformed.getROI().getGeometry()
        String transformedGeometrySha256 = geometrySha256(geometry)
        def targetFacts = TransferGeometryMetrics.describe(geometry,fixedAreaScale)
        boolean valid = targetFacts.valid(),nonempty = targetFacts.nonempty(),finite = targetFacts.finite()
        boolean selfIntersection = targetFacts.selfIntersection()
        double transformedArea = targetFacts.area()
        int transformedFragments = targetFacts.fragments()
        if (!Double.isFinite(transformedArea) || transformedArea <= 0d || transformedFragments <= 0) {
            records << uncertainRecord(base, "transformed_geometry_measurement_unavailable")
            return
        }
        Map measured = TransferGeometryMetrics.measure(originalArea,originalFragments,geometry,
            targetFacts,transferSections,imageBounds)
        double areaRatio = measured.area_ratio as double
        List<String> candidates = measured.candidate_target_section_ids as List<String>
        double bestIntersection = measured.target_tissue_intersection_fraction as double
        double outOfBounds = measured.out_of_bounds_fraction as double
        List<String> reasons = []
        if (!valid) reasons << "geometry_invalid"
        if (!nonempty) reasons << "geometry_empty"
        if (!finite) reasons << "coordinates_nonfinite"
        if (bestIntersection < (policy.min_target_tissue_intersection_fraction as double))
            reasons << "target_tissue_intersection_insufficient"
        if (areaRatio < (policy.min_area_ratio as double) ||
                areaRatio > (policy.max_area_ratio as double))
            reasons << "area_ratio_implausible"
        if (selfIntersection) reasons << "pathological_self_intersection"
        if (transformedFragments - originalFragments >
                (policy.max_fragment_count_increase as int))
            reasons << "unexpected_fragmentation"
        if (candidates.size() != 1) reasons << "target_section_ambiguous"
        if (outOfBounds > (policy.max_out_of_bounds_fraction as double))
            reasons << "out_of_bounds_exceeded"
        boolean passed = reasons.isEmpty()
        String targetSectionID = passed ? candidates[0] : null
        transformed.setID(UUID.fromString(targetID))
        Map record = base + [
            target_object_id: passed ? targetID : null,
            transformed_geometry_sha256: transformedGeometrySha256,
            target_section_id: targetSectionID,
            geometry_valid: valid,
            geometry_nonempty: nonempty,
            coordinates_finite: finite,
            target_tissue_intersection_fraction: bestIntersection,
            transformed_area_um2: transformedArea,
            area_ratio: areaRatio,
            self_intersection: selfIntersection,
            transformed_fragment_count: transformedFragments,
            candidate_target_section_ids: candidates,
            out_of_bounds_fraction: outOfBounds,
            computed_status: passed ? "passed" : "failed",
            review_state: "pending",
            disposition: passed ? "pending_review" : "rejected",
            reason_codes: reasons,
        ]
        records << record
    } catch (Exception ignored) {
        records << uncertainRecord(base, "qc_geometry_operation_unavailable")
    }
}

Map evidence = [
    schema_version: "qupath-warpy-transfer-batch/v1",
    registration_id: registration.registration_id,
    registration_revision: registration.revision,
    tissue_generation_id: registration.tissue_generation_id,
    moving_project_entry_id: registration.moving_project_entry_id,
    fixed_project_entry_id: registration.fixed_project_entry_id,
    requested_source_object_ids: request.source_object_ids,
    records: records,
    authoritative_object_ids: [],
    rejected_object_ids: records.findAll { it.disposition == "rejected" }.collect { it.source_object_id },
    quarantined_object_ids: records.findAll { it.disposition == "quarantined" }.collect { it.source_object_id },
]
atomicWrite(evidencePath, prettyGson.toJson(evidence) + "\n")
println "Validated ${records.size()} Warpy object transfers"

Map uncertainRecord(Map base, String reason) {
    base + [
        target_object_id: null,
        target_section_id: null,
        geometry_valid: null,
        geometry_nonempty: null,
        coordinates_finite: null,
        target_tissue_intersection_fraction: null,
        transformed_area_um2: null,
        area_ratio: null,
        self_intersection: null,
        transformed_fragment_count: null,
        candidate_target_section_ids: [],
        out_of_bounds_fraction: null,
        computed_status: "uncertain",
        review_state: "pending",
        disposition: "quarantined",
        reason_codes: [reason],
    ]
}

Map requestedSourceBase(Map registration, Map policy, String requestedID) {
    [
        schema_version: "warpy-transfer-qc/v1",
        transfer_qc_id: ("transfer-" + stableUUID("${registration.registration_id}:${requestedID}:qc".toString())).toString(),
        registration_id: registration.registration_id,
        registration_revision: registration.revision,
        tissue_generation_id: registration.tissue_generation_id,
        moving_project_entry_id: registration.moving_project_entry_id,
        fixed_project_entry_id: registration.fixed_project_entry_id,
        moving_image_id: registration.moving_image_id,
        fixed_image_id: registration.fixed_image_id,
        moving_image_sha256: registration.moving_image_sha256,
        fixed_image_sha256: registration.fixed_image_sha256,
        transform_file: registration.transform_file,
        transform_file_sha256: registration.transform_file_sha256,
        moving_physical_calibration: registration.moving_physical_calibration,
        fixed_physical_calibration: registration.fixed_physical_calibration,
        policy: policy,
        source_object_id: requestedID,
        source_object_type: null,
        source_has_descendants: null,
        source_has_nucleus_roi: null,
        source_geometry_sha256: null,
        source_object_revision_sha256: null,
        target_object_id: stableUUID("${registration.registration_id}:${requestedID}:target"),
        transformed_geometry_sha256: null,
        target_section_id: null,
        original_area_um2: null,
        original_fragment_count: null,
    ]
}

Map calibrationOf(server) {
    def calibration = server.getPixelCalibration()
    double x = calibration.getPixelWidthMicrons()
    double y = calibration.getPixelHeightMicrons()
    if (!Double.isFinite(x) || !Double.isFinite(y) || x <= 0d || y <= 0d)
        throw new IllegalStateException("Every Warpy project entry requires physical calibration")
    [x_um_per_px: x, y_um_per_px: y]
}

boolean sameCalibration(Map actual, Object recorded) {
    if (!(recorded instanceof Map)) return false
    double x = recorded.x_um_per_px as double
    double y = recorded.y_um_per_px as double
    Double.isFinite(x) && Double.isFinite(y) &&
        Double.compare(actual.x_um_per_px as double, x) == 0 &&
        Double.compare(actual.y_um_per_px as double, y) == 0
}

Map metadataOf(object) {
    Map metadata = [:]
    object.getMetadata().each { key, value -> metadata[key.toString()] = value }
    metadata
}

String geometrySha256(geometry) {
    def normalized = geometry.copy()
    normalized.normalize()
    sha256Bytes(new WKBWriter().write(normalized))
}

String objectRevisionSha256(object, String geometryDigest) {
    Map metadata = new TreeMap(metadataOf(object))
    String value = [
        object.getID().toString(), object.getName(), object.getPathClass()?.toString(),
        geometryDigest, metadata.toString()
    ].join("\u001f")
    sha256Bytes(value.getBytes(StandardCharsets.UTF_8))
}

void validateEntryImage(entry, String expectedPath, String expectedSha256) {
    Path path = Paths.get(expectedPath).toAbsolutePath().normalize()
    def fileURIs = entry.getURIs().findAll { it.getScheme() == "file" }.collect {
        Paths.get(it).toAbsolutePath().normalize()
    }
    // Prepared-image identity is its completed root metadata, as in the image reader.
    Path member = Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) ? path.resolve("zarr.json") : path
    if (fileURIs != [path] || Files.isSymbolicLink(path) || Files.isSymbolicLink(member) ||
            !Files.isRegularFile(member, LinkOption.NOFOLLOW_LINKS) || sha256(member) != expectedSha256)
        throw new IllegalStateException("Transfer project-entry URI/content scope changed")
}

String sha256Bytes(byte[] bytes) {
    MessageDigest.getInstance("SHA-256").digest(bytes).collect {
        String.format("%02x", it)
    }.join()
}

String stableUUID(String value) {
    UUID.nameUUIDFromBytes(value.getBytes(StandardCharsets.UTF_8)).toString()
}

String sha256(Path path) {
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { stream ->
        byte[] buffer = new byte[1024 * 1024]
        int count
        while ((count = stream.read(buffer)) >= 0) {
            if (count > 0) digest.update(buffer, 0, count)
        }
    }
    digest.digest().collect { String.format("%02x", it) }.join()
}

void atomicWrite(Path path, String content) {
    Files.createDirectories(path.parent)
    Path temporary = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temporary, content, StandardCharsets.UTF_8)
    try {
        Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE,
            StandardCopyOption.REPLACE_EXISTING)
    } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
        Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING)
    }
}
