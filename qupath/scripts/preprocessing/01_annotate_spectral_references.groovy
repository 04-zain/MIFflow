/** Prepare a dedicated reference project or snapshot explicitly tagged reference ROIs. */

import java.awt.image.BufferedImage
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.StreamReadFeature
import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.databind.SerializationFeature
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.geom.MultiPolygon
import org.locationtech.jts.geom.Polygon
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageChannel
import qupath.lib.images.servers.ImageServerMetadata
import qupath.lib.images.servers.ImageServers
import qupath.lib.projects.ProjectIO
import qupath.lib.projects.Projects
import qupath.lib.objects.classes.PathClass

import static qupath.lib.scripting.QP.*

if (args.size() != 2)
    throw new IllegalArgumentException("Expected reference request and evidence paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1] as String).toAbsolutePath().normalize()
if (Files.exists(evidencePath))
    throw new IllegalStateException("Reference evidence destination already exists")
def jsonFactory = JsonFactory.builder().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build()
def mapper = new ObjectMapper(jsonFactory)
mapper.setSerializationInclusion(JsonInclude.Include.ALWAYS)
def prettyMapper = mapper.copy().enable(SerializationFeature.INDENT_OUTPUT)
def canonicalMapper = mapper.copy().enable(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS)
Map request = mapper.readValue(Files.readString(requestPath), Map.class) as Map

def exactFields = { Object value, Set expected, String label ->
    if (!(value instanceof Map) || ((value as Map).keySet() as Set) != expected)
        throw new IllegalArgumentException("${label} fields are invalid")
    value as Map
}
def requireText = { Object value, String label ->
    if (!(value instanceof String) || value.trim().isEmpty() || value != value.trim())
        throw new IllegalArgumentException("${label} must be non-empty exact text")
    value as String
}
def requireNullableText = { Object value, String label -> value == null ? null : requireText(value, label) }
def requireInteger = { Object value, String label ->
    if (!(value instanceof Integer) || (value as int) <= 0)
        throw new IllegalArgumentException("${label} must be a positive JSON integer")
    value as int
}
def requirePositiveDouble = { Object value, String label ->
    if (!(value instanceof Number))
        throw new IllegalArgumentException("${label} must be numeric")
    double result = (value as Number).doubleValue()
    if (!Double.isFinite(result) || result <= 0d)
        throw new IllegalArgumentException("${label} must be finite and positive")
    result
}
def canonicalValue
canonicalValue = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap<String, Object>()
        (value as Map).each { key, nested -> sorted.put(key as String, canonicalValue(nested)) }
        return sorted
    }
    if (value instanceof List)
        return (value as List).collect { canonicalValue(it) }
    value
}
def canonicalJson = { Object value -> canonicalMapper.writeValueAsString(canonicalValue(value)) }
def sha256Text = { String value ->
    MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
}
def sha256Bytes = { byte[] value ->
    MessageDigest.getInstance("SHA-256").digest(value).encodeHex().toString()
}
def typedChecksumValue
typedChecksumValue = { Object value ->
    if (value == null)
        return ["null"]
    if (value instanceof Boolean)
        return ["bool", value]
    if (value instanceof String)
        return ["str", value]
    if (value instanceof Byte || value instanceof Short || value instanceof Integer ||
            value instanceof Long || value instanceof BigInteger)
        return ["int", value.toString()]
    if (value instanceof Number) {
        double number = (value as Number).doubleValue()
        if (!Double.isFinite(number))
            throw new IllegalArgumentException("Non-finite checksum number is forbidden")
        long bits = Double.doubleToRawLongBits(number)
        return ["f64", String.format(Locale.ROOT, "%016x", Long.valueOf(bits))]
    }
    if (value instanceof List)
        return ["list", (value as List).collect { typedChecksumValue(it) }]
    if (value instanceof Map) {
        List pairs = (value as Map).keySet().collect { key ->
            if (!(key instanceof String))
                throw new IllegalArgumentException("Checksum map keys must be strings")
            key as String
        }.sort().collect { key -> [key, typedChecksumValue((value as Map).get(key))] }
        return ["map", pairs]
    }
    throw new IllegalArgumentException("Unsupported checksum value type ${value.getClass().name}")
}
def typedChecksum = { Object value -> sha256Text(canonicalJson(typedChecksumValue(value))) }
def atomicNewJson = { Path path, Map value ->
    Files.createDirectories(path.parent)
    Path temporary = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    try {
        Files.writeString(temporary, prettyMapper.writeValueAsString(value) + "\n", StandardCharsets.UTF_8)
        try {
            Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE)
        } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
            Files.move(temporary, path)
        }
    } finally {
        Files.deleteIfExists(temporary)
    }
}
def checksumMapping = { Map value ->
    Map result = new LinkedHashMap(value)
    result.content_sha256 = typedChecksum(value)
    result
}
def ring = { sequence -> sequence.toCoordinateArray().collect { c -> [c.x as double, c.y as double] } }
def polygonCoordinates = { Polygon polygon ->
    List rings = [ring(polygon.exteriorRing.coordinateSequence)]
    (0..<polygon.numInteriorRing).each { i -> rings << ring(polygon.getInteriorRingN(i).coordinateSequence) }
    rings
}
def geometryMapping = { Geometry geometry ->
    if (geometry instanceof Polygon)
        return [type: "Polygon", coordinates: polygonCoordinates(geometry)]
    if (geometry instanceof MultiPolygon)
        return [type: "MultiPolygon", coordinates: (0..<geometry.numGeometries).collect {
            polygonCoordinates(geometry.getGeometryN(it) as Polygon)
        }]
    throw new IllegalStateException("Reference geometry must be Polygon/MultiPolygon, got ${geometry.geometryType}")
}

// Researchers assign these roles with QuPath's existing annotation class controls.
Map referenceClasses = ["Positive reference": "positive", "Autofluorescence control": "af",
    "Negative reference": "negative", "Image background": "background"]
Map referenceColors = [positive: 0xffcc33, af: 0xff66cc, negative: 0xff9900, background: 0x00bfff]

Set prepareImageFields = ["reference_input_id", "canonical_image_id", "canonical_pixels_id",
    "source_file_sha256", "source_path", "width_px", "height_px", "x_um_per_px",
    "y_um_per_px", "source_role", "source_link_id"] as Set
Set snapshotEntryFields = ["reference_input_id", "project_entry_id", "project_entry_path",
    "source_uri", "canonical_image_id", "canonical_pixels_id", "width_px", "height_px",
    "x_um_per_px", "y_um_per_px", "source_role", "source_link_id", "section_candidates"] as Set
Set sectionFields = ["section_id", "scope_kind", "tissue_path_object_id",
    "section_geometry_json", "section_geometry_sha256", "review_state"] as Set
Set requirementFields = ["roi_role", "role_link_id", "reference_input_id", "minimum_count"] as Set

// Only Python-validated retained imports supply calibration and ordered band labels.
// Subsequent reads compare persisted channel labels and calibration directly.
def importedMetadata = { int index ->
    if (!(request.imported_metadata instanceof List) || index >= request.imported_metadata.size())
        throw new IllegalArgumentException("Imported reference metadata is missing")
    Map metadata = exactFields(request.imported_metadata[index], ["channel_names", "source_observations"] as Set,
        "imported reference metadata")
    if (!(metadata.source_observations instanceof Map) || !(metadata.channel_names instanceof List) ||
            metadata.channel_names.isEmpty())
        throw new IllegalArgumentException("Imported reference metadata is invalid")
    metadata.channel_names.each { requireText(it, "imported channel name") }
    metadata
}

def liveImageEvidence = { entry, Map expected, String expectedUri, Map imported ->
    def imageData = entry.readImageData()
    def server = imageData.server
    if (server.metadata.channels.collect { it.name } != imported.channel_names)
        throw new IllegalStateException("Saved reference channel metadata differs from its retained import")
    List uris = server.getURIs().collect { it.normalize() }.unique()
    if (uris.size() != 1)
        throw new IllegalStateException("Reference entry must expose exactly one source URI")
    def metadataStore
    try {
        metadataStore = server.getMetadataStore()
    } catch (MissingMethodException exc) {
        throw new IllegalStateException("Reference entry is not backed by a live Bio-Formats metadata store", exc)
    }
    int series
    try {
        series = server.getSeries() as int
    } catch (MissingMethodException exc) {
        throw new IllegalStateException("Reference entry does not expose its Bio-Formats series", exc)
    }
    String imageId = requireText(metadataStore.getImageID(series), "live OME Image ID")
    String pixelsId = requireText(metadataStore.getPixelsID(series), "live OME Pixels ID")
    int width = server.width as int
    int height = server.height as int
    double x = server.pixelCalibration.pixelWidthMicrons
    double y = server.pixelCalibration.pixelHeightMicrons
    String sourceUri
    try {
        sourceUri = Paths.get(uris[0]).toAbsolutePath().normalize().toUri().toString()
    } catch (Exception exc) {
        throw new IllegalStateException("Reference entry source URI must resolve to one local canonical file", exc)
    }
    if (sourceUri != expectedUri || imageId != requireText(expected.canonical_image_id, "canonical_image_id") ||
            pixelsId != requireText(expected.canonical_pixels_id, "canonical_pixels_id") ||
            width != requireInteger(expected.width_px, "width_px") || height != requireInteger(expected.height_px, "height_px") ||
            !Double.isFinite(x) || !Double.isFinite(y) ||
            Math.abs(x - requirePositiveDouble(expected.x_um_per_px, "x_um_per_px")) > 1e-9 ||
            Math.abs(y - requirePositiveDouble(expected.y_um_per_px, "y_um_per_px")) > 1e-9)
        throw new IllegalStateException("Live canonical reference identity/calibration changed: " +
            "uri=${sourceUri}/${expectedUri}, image=${imageId}/${expected.canonical_image_id}, " +
            "pixels=${pixelsId}/${expected.canonical_pixels_id}, size=${width}x${height}/" +
            "${expected.width_px}x${expected.height_px}, calibration=${x},${y}/" +
            "${expected.x_um_per_px},${expected.y_um_per_px}")
    [reference_input_id: requireText(expected.reference_input_id, "reference_input_id"),
     project_entry_id: entry.getID().toString(),
     project_entry_path: entry.entryPath.toAbsolutePath().normalize().toString(),
     source_uri: sourceUri, canonical_image_id: imageId, canonical_pixels_id: pixelsId,
     width_px: width, height_px: height, x_um_per_px: x as double, y_um_per_px: y as double]
}

if (request.schema_version == "spectral-reference-project-request/v1") {
    exactFields(request, ["schema_version", "project_id", "project_file", "images", "imported_metadata"] as Set, "reference project request")
    requireText(request.project_id, "project_id")
    if (!(request.images instanceof List) || (request.images as List).isEmpty())
        throw new IllegalArgumentException("Reference project requires at least one image")
    Path projectPath = Paths.get(requireText(request.project_file, "project_file")).toAbsolutePath().normalize()
    Files.createDirectories(projectPath.parent)
    def project = Files.isRegularFile(projectPath) ? ProjectIO.loadProject(projectPath.toFile(), BufferedImage.class) :
        Projects.createProject(projectPath.toFile(), BufferedImage.class)
    project.setPathClasses(referenceClasses.collect { name, role -> PathClass.fromString(name, referenceColors[role] as Integer) })
    List<Map> entries = []
    Set requestedUris = [] as Set
    Set seenInputIds = [] as Set
    Set seenEntryIds = [] as Set
    Set seenCanonicalIdentities = [] as Set
    if (!(request.imported_metadata instanceof List) || request.imported_metadata.size() != request.images.size())
        throw new IllegalArgumentException("Imported metadata must cover every reference image")
    (request.images as List).eachWithIndex { rawImage, imageIndex ->
        Map imported = importedMetadata(imageIndex)
        Map image = exactFields(rawImage, prepareImageFields, "reference image")
        String inputId = requireText(image.reference_input_id, "reference_input_id")
        String imageId = requireText(image.canonical_image_id, "canonical_image_id")
        String sourceSha = requireText(image.source_file_sha256, "source_file_sha256")
        requireText(image.canonical_pixels_id, "canonical_pixels_id")
        requireText(image.source_role, "source_role")
        requireText(image.source_link_id, "source_link_id")
        if (!seenInputIds.add(inputId) || !seenCanonicalIdentities.add("${imageId}\u0000${sourceSha}"))
            throw new IllegalArgumentException("Duplicate reference input or canonical identity")
        Path imagePath = Paths.get(requireText(image.source_path, "source_path")).toAbsolutePath().normalize()
        if (!Files.isRegularFile(imagePath))
            throw new IllegalArgumentException("Reference image is not a local file: ${imagePath}")
        def uri = imagePath.toUri().normalize()
        requestedUris.add(uri)
        def matches = project.imageList.findAll { candidate -> candidate.getURIs().any { it.normalize() == uri } }
        if (matches.size() > 1)
            throw new IllegalStateException("Reference image resolves to multiple project entries")
        def entry = matches ? matches[0] : null
        boolean newEntry = entry == null
        if (newEntry) {
            def support = ImageServers.getImageSupport(uri)
            if (support == null || support.builders.size() != 1)
                throw new IllegalStateException("Reference image requires exactly one QuPath server builder")
            entry = project.addImage(support.builders[0])
        }
        if (!seenEntryIds.add(entry.getID().toString()))
            throw new IllegalStateException("Duplicate QuPath project-entry ID")
        entry.setImageName(inputId)
        def imageData = entry.readImageData()
        if (imageData.server.nChannels() != imported.channel_names.size())
            throw new IllegalStateException("Native channel count differs from retained imported metadata")
        if (newEntry) {
            def channels = imported.channel_names.withIndex().collect { name, index ->
                ImageChannel.getInstance(name as String, imageData.server.metadata.channels[index].color)
            }
            imageData.server.setMetadata(new ImageServerMetadata.Builder(imageData.server.metadata)
                .channels(channels)
                .pixelSizeMicrons(requirePositiveDouble(image.x_um_per_px, "x_um_per_px"),
                    requirePositiveDouble(image.y_um_per_px, "y_um_per_px"))
                .build())
        }
        imageData.setImageType(ImageData.ImageType.FLUORESCENCE)
        entry.saveImageData(imageData)
        entries << liveImageEvidence(entry, image, uri.toString(), imported)
    }
    Set actualUris = project.imageList.collectMany { e -> e.getURIs().collect { it.normalize() } } as Set
    if (project.imageList.size() != entries.size() || actualUris != requestedUris)
        throw new IllegalStateException("Dedicated reference project contains unexpected entries")
    project.syncChanges()
    Map evidence = [schema_version: "spectral-reference-project-evidence/v2",
        project_id: requireText(request.project_id, "project_id"),
        project_file: project.path.toAbsolutePath().normalize().toString(),
        request_sha256: sha256Bytes(Files.readAllBytes(requestPath)), entries: entries]
    atomicNewJson(evidencePath, checksumMapping(evidence))
    println "Prepared ${entries.size()} canonical reference project entries"
    return
}

if (request.schema_version != "spectral-reference-roi-snapshot-request/v1")
    throw new IllegalArgumentException("Unsupported spectral-reference QuPath request")
exactFields(request, ["schema_version", "project_id", "project_file", "project_review_state",
    "entries", "role_requirements", "created_at", "predecessor_content_sha256", "imported_metadata"] as Set,
    "reference ROI snapshot request")
if (!(request.entries instanceof List) || (request.entries as List).isEmpty() ||
        !(request.role_requirements instanceof List) || (request.role_requirements as List).isEmpty())
    throw new IllegalArgumentException("Snapshot request requires entries and role requirements")
String projectReviewState = requireText(request.project_review_state, "project_review_state")
if (!(projectReviewState in ["pending", "accepted", "rejected"]))
    throw new IllegalArgumentException("Invalid project review state")
requireNullableText(request.predecessor_content_sha256, "predecessor_content_sha256")
Path requestedProject = Paths.get(requireText(request.project_file, "project_file")).toAbsolutePath().normalize()
if (!Files.isRegularFile(requestedProject))
    throw new IllegalStateException("Dedicated reference project is missing")
// Run this whole-project snapshot once; --project would repeat it for every image.
def project = ProjectIO.loadProject(requestedProject.toFile(), BufferedImage.class)
if (project.path.toAbsolutePath().normalize() != requestedProject)
    throw new IllegalArgumentException("Snapshot request targets another QuPath project")
if (project.imageList.size() != (request.entries as List).size())
    throw new IllegalStateException("Dedicated reference project entry inventory changed")

if (!(request.imported_metadata instanceof List) || request.imported_metadata.size() != request.entries.size())
    throw new IllegalArgumentException("Imported metadata must cover every snapshot entry")
Map importedByEntryId = [:]
Map expectedByEntryId = [:]
(request.entries as List).eachWithIndex { rawEntry, imageIndex ->
    Map expected = exactFields(rawEntry, snapshotEntryFields, "snapshot project entry")
    String id = requireText(expected.project_entry_id, "project_entry_id")
    importedByEntryId[id] = importedMetadata(imageIndex)
    if (expectedByEntryId.put(id, expected) != null)
        throw new IllegalArgumentException("Duplicate requested project-entry ID")
}
if ((project.imageList.collect { it.getID().toString() } as Set) != expectedByEntryId.keySet())
    throw new IllegalStateException("Dedicated reference project entry inventory changed")
Map requirementByKey = [:]
(request.role_requirements as List).each { rawRequirement ->
    Map requirement = exactFields(rawRequirement, requirementFields, "role requirement")
    String role = requireText(requirement.roi_role, "roi_role")
    String link = requireText(requirement.role_link_id, "role_link_id")
    String inputId = requireText(requirement.reference_input_id, "reference_input_id")
    int minimum = requireInteger(requirement.minimum_count, "minimum_count")
    String key = "${role}\u0000${link}\u0000${inputId}"
    if (requirementByKey.put(key, minimum) != null)
        throw new IllegalArgumentException("Duplicate role requirement")
}

List<Map> exportedEntries = []
List<Map> rois = []
Set roiIds = [] as Set
Set pathObjectIds = [] as Set
Set roleValues = ["positive", "af", "negative", "background"] as Set
project.imageList.each { entry ->
    Map expected = expectedByEntryId[entry.getID().toString()] as Map
    if (entry.entryPath.toAbsolutePath().normalize().toString() != requireText(expected.project_entry_path, "project_entry_path"))
        throw new IllegalStateException("Snapshot project-entry path identity changed")
    Map live = liveImageEvidence(entry, expected, requireText(expected.source_uri, "source_uri"), importedByEntryId[entry.getID().toString()] as Map)
    if (!(requireText(expected.source_role, "source_role") in ["single_component_reference", "unstained_control", "negative_control"]))
        throw new IllegalArgumentException("Invalid reference source role")
    String sourceLink = requireText(expected.source_link_id, "source_link_id")
    def imageData = entry.readImageData()
    List<Map> sectionEvidence = []
    Map tissueGeometries = [:]
    Set sectionIds = [] as Set
    List rawSections = expected.section_candidates as List
    if (rawSections.isEmpty())
        throw new IllegalArgumentException("Reference entry requires section candidates")
    rawSections.each { rawSection ->
        Map section = exactFields(rawSection, sectionFields, "reference section")
        String sectionId = requireText(section.section_id, "section_id")
        if (section.review_state != "accepted" || !sectionIds.add(sectionId))
            throw new IllegalArgumentException("Reference section scope is not uniquely accepted")
        if (section.scope_kind == "accepted_tissue_section") {
            String tissueId = requireText(section.tissue_path_object_id, "tissue_path_object_id")
            def matches = imageData.hierarchy.annotationObjects.findAll { it.getID().toString() == tissueId }
            if (matches.size() != 1)
                throw new IllegalStateException("Accepted reference section PathObject is missing or ambiguous")
            def tissue = matches[0]
            Map metadata = tissue.getMetadata()
            if (tissue.pathClass?.toString() != "TissueSection" || metadata.get("pipeline_stage") != "tissue_detection" ||
                    metadata.get("generation_status") != "accepted" || metadata.get("review_status") != "accepted")
                throw new IllegalStateException("Accepted reference section PathObject evidence changed")
            Geometry sectionGeometry = tissue.ROI.geometry
            if (sectionGeometry == null || sectionGeometry.isEmpty() || !sectionGeometry.isValid() || sectionGeometry.area <= 0d)
                throw new IllegalStateException("Accepted reference section geometry is invalid")
            Map liveGeometryMap = geometryMapping(sectionGeometry)
            String expectedGeometryJson = requireText(section.section_geometry_json, "section_geometry_json")
            Map expectedGeometryMap = mapper.readValue(expectedGeometryJson, Map.class) as Map
            if (typedChecksum(liveGeometryMap) != typedChecksum(expectedGeometryMap) ||
                    sha256Text(expectedGeometryJson) != requireText(section.section_geometry_sha256, "section_geometry_sha256"))
                throw new IllegalStateException("Accepted reference section geometry changed")
            tissueGeometries[sectionId] = sectionGeometry
        } else if (section.scope_kind == "declared_whole_image") {
            if (rawSections.size() != 1 || section.tissue_path_object_id != null ||
                    section.section_geometry_json != null || section.section_geometry_sha256 != null)
                throw new IllegalArgumentException("Whole-image reference scope is ambiguous or malformed")
        } else {
            throw new IllegalArgumentException("Invalid reference section scope kind")
        }
        sectionEvidence << section
    }
    // Section outlines and unrelated/unclassified annotations are not reference regions.
    imageData.hierarchy.annotationObjects.findAll {
        referenceClasses.containsKey(it.pathClass?.toString())
    }.each { annotation ->
        String role = referenceClasses[annotation.pathClass?.toString()]
        String objectId = annotation.getID().toString()
        String roiId = "${expected.reference_input_id}:${role}:${objectId}"
        String roleLink = role == "background" ? expected.reference_input_id : sourceLink
        String sectionId = null
        if (!roiIds.add(roiId) || !pathObjectIds.add(objectId))
            throw new IllegalStateException("Duplicate spectral-reference ROI identity")
        if (!roleValues.contains(role))
            throw new IllegalStateException("Reference ROI role is invalid")
        if ((role == "positive" &&
                (expected.source_role != "single_component_reference" || roleLink != sourceLink)) ||
                (role == "negative" && (expected.source_role != "negative_control" || roleLink != sourceLink)) ||
                (role == "af" && (!(expected.source_role in ["unstained_control", "negative_control"]) || roleLink != sourceLink)) ||
                (role == "background" && roleLink != expected.reference_input_id))
            throw new IllegalStateException("Reference ROI role/link/source semantics do not match")
        String requirementKey = "${role}\u0000${roleLink}\u0000${expected.reference_input_id}"
        if (!requirementByKey.containsKey(requirementKey))
            throw new IllegalStateException("Reference ROI has no exact role requirement")
        Geometry geometry = annotation.ROI.geometry
        if (geometry == null || geometry.isEmpty() || !geometry.isValid() || geometry.area <= 0d)
            throw new IllegalStateException("Reference ROI geometry must be valid, nonempty, and positive-area")
        geometry.coordinates.each { c ->
            if (!Double.isFinite(c.x) || !Double.isFinite(c.y) || c.x < 0d || c.y < 0d ||
                    c.x > (live.width_px as int) || c.y > (live.height_px as int))
                throw new IllegalStateException("Reference ROI geometry is nonfinite or out of bounds")
        }
        List<String> spatialMatches = tissueGeometries.isEmpty() ? sectionIds as List :
            tissueGeometries.findAll { id, g -> geometry.intersects(g as Geometry) }.keySet() as List<String>
        if (spatialMatches.size() != 1)
            throw new IllegalStateException("Reference ROI must resolve spatially to exactly one declared section")
        sectionId = spatialMatches[0]
        rois << [roi_id: roiId, path_object_id: objectId, project_entry_id: entry.getID().toString(),
            roi_role: role, role_link_id: roleLink, section_id: sectionId,
            coordinate_system: "global_level0_pixels", geometry: geometryMapping(geometry),
            review_state: "pending",
            reviewer_id: null, reviewed_at: null]
    }
    live.section_candidates = sectionEvidence
    exportedEntries << live
}
if (projectReviewState == "accepted" && rois.isEmpty())
    throw new IllegalStateException("Accepted reference project cannot have an empty ROI snapshot")
requirementByKey.each { key, minimum ->
    List parts = key.split("\\u0000", -1) as List
    int count = rois.count { roi ->
        Map expected = expectedByEntryId[roi.project_entry_id] as Map
        roi.roi_role == parts[0] && roi.role_link_id == parts[1] && expected.reference_input_id == parts[2]
    }
    if (count < (minimum as int))
        throw new IllegalStateException("Reference ROI role requirement cardinality is not satisfied")
}
Map evidence = [schema_version: "spectral-reference-roi-live-evidence/v1",
    project_id: requireText(request.project_id, "project_id"), project_file: requestedProject.toString(),
    request_sha256: sha256Bytes(Files.readAllBytes(requestPath)), entries: exportedEntries, rois: rois]
atomicNewJson(evidencePath, checksumMapping(evidence))
println "Snapshotted ${rois.size()} pending spectral-reference ROIs"
