/** Commit only object transfers whose immutable QC records Python accepted. */

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
    throw new IllegalArgumentException("Expected validated-transfer request and evidence paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1] as String).toAbsolutePath().normalize()
def gson = GsonTools.getInstance()
def prettyGson = gson.newBuilder().setPrettyPrinting().create()
Map request = gson.fromJson(Files.readString(requestPath), Map.class) as Map
if (request.keySet() != (["schema_version", "transform_reader", "project_file", "attempt_id", "registration", "records", "mutation_lease"] as Set) ||
        request.schema_version != "qupath-warpy-transfer-commit-request/v2" ||
        !(request.attempt_id instanceof String) ||
        !(request.attempt_id ==~ /transfer-attempt-[0-9a-f]{64}/) ||
        !(request.registration instanceof Map) || !(request.records instanceof List) ||
        request.records.isEmpty() || !(request.mutation_lease instanceof Map))
    throw new IllegalArgumentException("Invalid validated-transfer commit request")
Map mutationLease = request.mutation_lease as Map
Map registration = request.registration as Map
validateMutationLease(
    mutationLease, request.attempt_id as String, request.project_file as String,
    registration.fixed_project_entry_id as String, "commit"
)
if (registration.provider != "warpy" || registration.provider_version != "0.5.0" ||
        registration.direction != "moving_to_fixed" ||
        registration.acceptance_state != "accepted")
    throw new IllegalArgumentException("Transfer commit requires accepted Warpy registration")
if (!(registration.runtime_versions instanceof Map) ||
        registration.runtime_versions.warpy != WarpyExtension.getWarpyVersion() ||
        registration.runtime_versions.qupath != GeneralTools.getVersion().toString())
    throw new IllegalStateException("Transfer commit runtime versions changed after registration")
request.records.each { record ->
    if (!(record instanceof Map) || record.schema_version != "warpy-transfer-qc/v1" ||
            record.registration_id != registration.registration_id ||
            record.registration_revision != registration.revision ||
            record.computed_status != "passed" || record.review_state != "accepted" ||
            record.disposition != "accepted" || !(record.target_object_id instanceof String) ||
            !(record.target_section_id instanceof String) ||
            record.source_object_type != "PathAnnotationObject" ||
            record.source_has_descendants != false || record.source_has_nucleus_roi != false)
        throw new IllegalArgumentException("Transfer commit received a non-authoritative QC record")
}

def project = getProject()
if (project == null || project.getPath().toAbsolutePath().normalize() !=
        Paths.get(request.project_file as String).toAbsolutePath().normalize())
    throw new IllegalArgumentException("Transfer commit targets another project")
def movingMatches = project.getImageList().findAll {
    it.getID().toString() == registration.moving_project_entry_id
}
def fixedMatches = project.getImageList().findAll {
    it.getID().toString() == registration.fixed_project_entry_id
}
if (movingMatches.size() != 1 || fixedMatches.size() != 1)
    throw new IllegalStateException("Transfer commit project-entry IDs are stale")
def movingEntry = movingMatches[0]
def fixedEntry = fixedMatches[0]
validateEntryImage(movingEntry, registration.moving_image_path as String,
    registration.moving_image_sha256 as String)
validateEntryImage(fixedEntry, registration.fixed_image_path as String,
    registration.fixed_image_sha256 as String)
Path transformFile = Paths.get(registration.transform_file as String).toAbsolutePath().normalize()
Path expectedTransform = fixedEntry.getEntryPath().resolve(
    "transform_${fixedEntry.getID()}_${movingEntry.getID()}.json"
).toAbsolutePath().normalize()
if (transformFile != expectedTransform || !Files.isRegularFile(transformFile) ||
        sha256(transformFile) != registration.transform_file_sha256)
    throw new IllegalStateException("Transfer commit transform scope changed after QC")
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
    throw new IllegalStateException("Warpy could not reload the accepted transform")

def movingData = movingEntry.readImageData()
def fixedData = fixedEntry.readImageData()
if (!sameCalibration(calibrationOf(movingData.getServer()),
        registration.moving_physical_calibration) ||
        !sameCalibration(calibrationOf(fixedData.getServer()),
        registration.fixed_physical_calibration))
    throw new IllegalStateException("Transfer commit physical calibration changed after QC")
def movingHierarchy = movingData.getHierarchy()
def fixedHierarchy = fixedData.getHierarchy()
Map sectionRows = (registration.registration_qc.section_records as List).collectEntries {
    [(it.section_id as String): it]
}
Map targetSections = sectionRows.collectEntries { sectionID, row ->
    def matches = fixedHierarchy.getFlattenedObjectList(null).findAll {
        metadataOf(it).get("roi_id") == row.tissue_roi_id
    }
    Map tissueMetadata = matches.size() == 1 ? metadataOf(matches[0]) : [:]
    if (matches.size() != 1 ||
            matches[0].getID().toString() != row.tissue_path_object_id ||
            tissueMetadata.get("pipeline_stage") != "tissue_detection" ||
            tissueMetadata.get("generation_id") != registration.tissue_generation_id ||
            tissueMetadata.get("generation_status") != "accepted" ||
            tissueMetadata.get("review_status") != "accepted" ||
            tissueMetadata.get("section_id") != row.section_id ||
            tissueMetadata.get("roi_id") != row.tissue_roi_id)
        throw new IllegalStateException("Transfer commit TissueSection scope is stale")
    [(sectionID): matches[0]]
}
// Commit binds and remeasures against its independently reopened current tissue.
List transferSections = targetSections.collect { sectionID, section ->
    new TransferGeometryMetrics.Section(sectionID as String,section.getROI().getGeometry())
}
List prepared = []
List reconciled = []
double movingAreaScale = calibrationOf(movingData.getServer()).x_um_per_px *
    calibrationOf(movingData.getServer()).y_um_per_px
double fixedAreaScale = calibrationOf(fixedData.getServer()).x_um_per_px *
    calibrationOf(fixedData.getServer()).y_um_per_px
def imageBounds = ROIs.createRectangleROI(
    0d, 0d, fixedData.getServer().getWidth() as double,
    fixedData.getServer().getHeight() as double, ImagePlane.getDefaultPlane()
).getGeometry()
request.records.each { record ->
    def sourceMatches = movingHierarchy.getFlattenedObjectList(null).findAll {
        it.getID().toString() == record.source_object_id
    }
    if (sourceMatches.size() != 1 || sourceMatches[0].getROI() == null)
        throw new IllegalStateException("Validated transfer source scope changed after QC")
    def source = sourceMatches[0]
    if (source.getClass().getSimpleName() != "PathAnnotationObject" ||
            source instanceof PathCellObject ||
            !source.getChildObjects().isEmpty())
        throw new IllegalStateException(
            "Validated transfer source is no longer a single-ROI leaf annotation"
        )
    def sourceGeometry = source.getROI().getGeometry()
    String sourceDigest = geometrySha256(sourceGeometry)
    if (sourceDigest != record.source_geometry_sha256 ||
            objectRevisionSha256(source, sourceDigest) != record.source_object_revision_sha256)
        throw new IllegalStateException("Validated transfer source geometry/revision changed after QC")
    def transformed = Warpy.transformPathObjects([source], transform) as List
    if (transformed.size() != 1 || transformed[0]?.getROI() == null)
        throw new IllegalStateException("Validated Warpy transfer is no longer reproducible")
    def object = transformed[0]
    def geometry = object.getROI().getGeometry()
    String transformedDigest = geometrySha256(geometry)
    Map measured = TransferGeometryMetrics.measure(
        sourceGeometry, geometry, transferSections, imageBounds,
        movingAreaScale, fixedAreaScale
    )
    Map expected = [
        geometry_valid: record.geometry_valid,
        geometry_nonempty: record.geometry_nonempty,
        coordinates_finite: record.coordinates_finite,
        target_tissue_intersection_fraction: record.target_tissue_intersection_fraction,
        original_area_um2: record.original_area_um2,
        transformed_area_um2: record.transformed_area_um2,
        area_ratio: record.area_ratio,
        self_intersection: record.self_intersection,
        original_fragment_count: record.original_fragment_count,
        transformed_fragment_count: record.transformed_fragment_count,
        candidate_target_section_ids: record.candidate_target_section_ids,
        out_of_bounds_fraction: record.out_of_bounds_fraction,
    ]
    if (transformedDigest != record.transformed_geometry_sha256 ||
            !sameMeasurements(measured, expected) ||
            measured.candidate_target_section_ids != [record.target_section_id])
        throw new IllegalStateException("Validated transfer policy evidence changed after QC")
    def existing = fixedHierarchy.getFlattenedObjectList(null).findAll {
        it.getID().toString() == record.target_object_id
    }
    if (existing.size() > 1)
        throw new IllegalStateException("Validated transfer target ID is ambiguous")
    if (existing.size() == 1) {
        def current = existing[0]
        if (current.getParent() != targetSections[record.target_section_id] ||
                current.getROI() == null ||
                geometrySha256(current.getROI().getGeometry()) != transformedDigest ||
                metadataOf(current).get("transfer_qc_id") != record.transfer_qc_id ||
                metadataOf(current).get("registration_id") != registration.registration_id ||
                metadataOf(current).get("transfer_attempt_id") != request.attempt_id)
            throw new IllegalStateException("Existing transfer target conflicts with accepted evidence")
        reconciled << [object: current, record: record]
        return
    }
    object.setID(UUID.fromString(record.target_object_id as String))
    object.getMetadata().put("transfer_qc_id", record.transfer_qc_id as String)
    object.getMetadata().put("registration_id", registration.registration_id as String)
    object.getMetadata().put("transfer_attempt_id", request.attempt_id as String)
    prepared << [object: object, section: targetSections[record.target_section_id], record: record]
}

try {
    prepared.each { item -> item.section.addChildObject(item.object) }
    fixedEntry.saveImageData(fixedData)
    project.syncChanges()
    List liveRows = request.records.collect { record ->
        def live = fixedHierarchy.getFlattenedObjectList(null).findAll {
            it.getID().toString() == record.target_object_id
        }
        if (live.size() != 1 || live[0].getParent() != targetSections[record.target_section_id] ||
                live[0].getROI() == null ||
                geometrySha256(live[0].getROI().getGeometry()) != record.transformed_geometry_sha256 ||
                metadataOf(live[0]).get("transfer_qc_id") != record.transfer_qc_id ||
                metadataOf(live[0]).get("registration_id") != registration.registration_id ||
                metadataOf(live[0]).get("transfer_attempt_id") != request.attempt_id)
            throw new IllegalStateException("Persisted transfer target does not match accepted evidence")
        [
            source_object_id: record.source_object_id,
            target_object_id: record.target_object_id,
            target_section_id: record.target_section_id,
            target_geometry_sha256: record.transformed_geometry_sha256,
            reconciled: reconciled.any { it.record.target_object_id == record.target_object_id },
        ]
    }
    Map evidence = [
        schema_version: "qupath-warpy-transfer-commit/v1",
        attempt_id: request.attempt_id,
        registration_id: registration.registration_id,
        registration_revision: registration.revision,
        authoritative_object_ids: request.records.collect { it.target_object_id },
        objects: liveRows,
    ]
    atomicWrite(evidencePath, prettyGson.toJson(evidence) + "\n")
} catch (Exception failure) {
    prepared.reverseEach { item -> item.section.removeChildObject(item.object) }
    fixedEntry.saveImageData(fixedData)
    project.syncChanges()
    throw failure
}
println "Committed ${prepared.size()} independently accepted Warpy transfers"

void validateMutationLease(
        Map lease, String attemptID, String projectFile, String fixedEntryID,
        String expectedOperation) {
    Set fields = ["schema_version", "attempt_id", "epoch", "token", "owner_id",
                  "operation", "lease_head_path", "hierarchy_lock_path",
                  "hierarchy_lock_scope", "hierarchy_lock_scope_sha256",
                  "attempt_store_identity", "hierarchy_lock_identity",
                  "lease_sha256"] as Set
    if (lease.keySet() != fields ||
            lease.schema_version != "warpy-transfer-mutation-lease/v5" ||
            !(attemptID ==~ /transfer-attempt-[0-9a-f]{64}/) ||
            lease.attempt_id != attemptID || !(lease.epoch instanceof Number) ||
            (lease.epoch as long) < 1L || !(lease.token instanceof String) ||
            !(lease.token ==~ /[0-9a-f]{64}/) ||
            !(lease.owner_id instanceof String) || lease.owner_id.isBlank() ||
            lease.operation != expectedOperation ||
            !(lease.lease_head_path instanceof String) ||
            !(lease.hierarchy_lock_path instanceof String) ||
            !(lease.hierarchy_lock_scope instanceof Map) ||
            !(lease.hierarchy_lock_scope_sha256 instanceof String) ||
            !(lease.hierarchy_lock_scope_sha256 ==~ /[0-9a-f]{64}/) ||
            !validTaggedFileIdentity(lease.attempt_store_identity) ||
            !validTaggedFileIdentity(lease.hierarchy_lock_identity) ||
            !(lease.lease_sha256 instanceof String) ||
            !(lease.lease_sha256 ==~ /[0-9a-f]{64}/))
        throw new IllegalArgumentException("Invalid transfer mutation lease")
    Path canonicalProject = Paths.get(projectFile).toAbsolutePath().normalize()
    if (fixedEntryID == null || fixedEntryID.isBlank() ||
            canonicalProject.toString().contains("\u0000") ||
            fixedEntryID.contains("\u0000"))
        throw new IllegalArgumentException("Invalid hierarchy mutation scope")
    Map expectedScope = [
        schema_version: "qupath-hierarchy-mutation-scope/v1",
        project_file: canonicalProject.toString(),
        fixed_project_entry_id: fixedEntryID,
    ]
    String expectedDigest = hierarchyScopeSha256(canonicalProject, fixedEntryID)
    Path expectedHeadPath = canonicalProject.parent.resolve("data").resolve(
        "registration-transfer-attempts"
    ).resolve("${attemptID}.lease.json").toAbsolutePath().normalize()
    Path expectedLockPath = canonicalProject.parent.resolve(
        ".mif-warpy-hierarchy-locks"
    ).resolve("${expectedDigest}.lock").toAbsolutePath().normalize()
    Path leaseHeadPath = Paths.get(
        lease.lease_head_path as String
    ).toAbsolutePath().normalize()
    Path hierarchyLockPath = Paths.get(
        lease.hierarchy_lock_path as String
    ).toAbsolutePath().normalize()
    if ((lease.hierarchy_lock_scope as Map) != expectedScope ||
            lease.hierarchy_lock_scope_sha256 != expectedDigest ||
            leaseHeadPath != expectedHeadPath ||
            hierarchyLockPath != expectedLockPath ||
            lease.lease_head_path != expectedHeadPath.toString() ||
            lease.hierarchy_lock_path != expectedLockPath.toString() ||
            lease.lease_sha256 != mutationLeaseSha256(lease))
        throw new IllegalArgumentException("Transfer hierarchy mutation scope changed")
}

boolean validTaggedFileIdentity(Object value) {
    if (!(value instanceof Map)) return false
    if (value.schema_version == "posix-file-identity/v1")
        return value.keySet() ==
                (["schema_version", "device", "inode"] as Set) &&
            value.device instanceof Number && (value.device as BigDecimal) >= 0 &&
            (value.device as BigDecimal) <= 9007199254740991 &&
            value.inode instanceof Number && (value.inode as BigDecimal) >= 0 &&
            (value.inode as BigDecimal) <= 9007199254740991
    if (value.schema_version == "windows-file-identity/v1")
        return value.keySet() ==
                (["schema_version", "volume_serial_number", "file_id"] as Set) &&
            value.volume_serial_number instanceof String &&
            (value.volume_serial_number ==~ /[0-9a-f]{16}/) &&
            value.file_id instanceof String &&
            (value.file_id ==~ /[0-9a-f]{32}/)
    false
}

String hierarchyScopeSha256(Path projectFile, String fixedEntryID) {
    sha256Bytes([
        "qupath-hierarchy-mutation-scope/v1",
        projectFile.toString(),
        fixedEntryID,
    ].join("\u0000").getBytes(StandardCharsets.UTF_8))
}

String mutationLeaseSha256(Map lease) {
    Map scope = lease.hierarchy_lock_scope as Map
    List values = [
        lease.schema_version, lease.attempt_id, (lease.epoch as long).toString(),
        lease.token, lease.owner_id, lease.operation, lease.lease_head_path,
        lease.hierarchy_lock_path, scope.schema_version, scope.project_file,
        scope.fixed_project_entry_id, lease.hierarchy_lock_scope_sha256,
    ]
    [lease.attempt_store_identity, lease.hierarchy_lock_identity].each { identity ->
        values << identity.schema_version
        if (identity.schema_version == "posix-file-identity/v1") {
            values << (identity.device as long).toString()
            values << (identity.inode as long).toString()
        } else {
            values << identity.volume_serial_number
            values << identity.file_id
        }
    }
    if (values.any { !(it instanceof String) || (it as String).contains("\u0000") })
        throw new IllegalStateException("Transfer mutation lease digest scope changed")
    sha256Bytes(values.join("\u0000").getBytes(StandardCharsets.UTF_8))
}
boolean sameMeasurements(Map actual, Map expected) {
    actual.every { key, value ->
        value instanceof Number ? sameDouble(value as double, expected[key] as double) :
            value == expected[key]
    }
}

boolean sameDouble(double left, double right) {
    Double.isFinite(left) && Double.isFinite(right) &&
        Math.abs(left - right) <= Math.max(1e-12d, Math.abs(right) * 1e-12d)
}

Map metadataOf(object) {
    Map metadata = object.getMetadata()
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
        throw new IllegalStateException("Transfer commit project-entry URI/content scope changed")
}

String sha256Bytes(byte[] bytes) {
    MessageDigest.getInstance("SHA-256").digest(bytes).collect {
        String.format("%02x", it)
    }.join()
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
