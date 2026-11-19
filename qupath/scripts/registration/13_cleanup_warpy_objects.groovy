/** Remove or attest absence of exact targets from one indeterminate Warpy attempt. */

import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

if (args.size() != 2)
    throw new IllegalArgumentException("Expected transfer-cleanup request and evidence paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1] as String).toAbsolutePath().normalize()
def gson = GsonTools.getInstance()
def prettyGson = gson.newBuilder().setPrettyPrinting().create()
Map request = gson.fromJson(Files.readString(requestPath), Map.class) as Map
Set requestFields = ["schema_version", "project_file", "attempt_id", "registration",
                     "records", "mutation_lease"] as Set
if (request.keySet() != requestFields ||
        request.schema_version != "qupath-warpy-transfer-cleanup-request/v1" ||
        !(request.attempt_id instanceof String) ||
        !(request.attempt_id ==~ /transfer-attempt-[0-9a-f]{64}/) ||
        !(request.registration instanceof Map) || !(request.records instanceof List) ||
        request.records.isEmpty() || !(request.mutation_lease instanceof Map))
    throw new IllegalArgumentException("Invalid Warpy transfer-cleanup request")
Map mutationLease = request.mutation_lease as Map
Map registration = request.registration as Map
validateMutationLease(
    mutationLease, request.attempt_id as String, request.project_file as String,
    registration.fixed_project_entry_id as String, ["commit", "cleanup"] as Set
)
if (registration.provider != "warpy" || registration.provider_version != "0.5.0" ||
        registration.direction != "moving_to_fixed" || registration.acceptance_state != "accepted")
    throw new IllegalArgumentException("Cleanup requires an accepted Warpy registration")
request.records.each { record ->
    if (!(record instanceof Map) || record.schema_version != "warpy-transfer-qc/v1" ||
            record.registration_id != registration.registration_id ||
            record.registration_revision != registration.revision ||
            record.computed_status != "passed" || record.review_state != "accepted" ||
            record.disposition != "accepted" || !(record.target_object_id instanceof String) ||
            !(record.transfer_qc_id instanceof String))
        throw new IllegalArgumentException("Cleanup received non-authoritative transfer evidence")
}

def project = getProject()
if (project == null || project.getPath().toAbsolutePath().normalize() !=
        Paths.get(request.project_file as String).toAbsolutePath().normalize())
    throw new IllegalArgumentException("Cleanup targets another project")
def fixedMatches = project.getImageList().findAll {
    it.getID().toString() == registration.fixed_project_entry_id
}
if (fixedMatches.size() != 1)
    throw new IllegalStateException("Cleanup fixed project-entry ID is stale")
def fixedEntry = fixedMatches[0]
validateEntryImage(fixedEntry, registration.fixed_image_path as String,
    registration.fixed_image_sha256 as String)
def fixedData = fixedEntry.readImageData()
def hierarchy = fixedData.getHierarchy()
Map sectionRows = (registration.registration_qc.section_records as List).collectEntries {
    [(it.section_id as String): it]
}
Map targetSections = sectionRows.collectEntries { sectionID, row ->
    def matches = hierarchy.getFlattenedObjectList(null).findAll {
        metadataOf(it).get("roi_id") == row.tissue_roi_id
    }
    Map metadata = matches.size() == 1 ? metadataOf(matches[0]) : [:]
    if (matches.size() != 1 || matches[0].getID().toString() != row.tissue_path_object_id ||
            metadata.get("pipeline_stage") != "tissue_detection" ||
            metadata.get("generation_id") != registration.tissue_generation_id ||
            metadata.get("generation_status") != "accepted" ||
            metadata.get("review_status") != "accepted" ||
            metadata.get("section_id") != row.section_id ||
            metadata.get("roi_id") != row.tissue_roi_id)
        throw new IllegalStateException("Cleanup TissueSection scope is stale")
    [(sectionID): matches[0]]
}

List removed = []
request.records.each { record ->
    def matches = hierarchy.getFlattenedObjectList(null).findAll {
        it.getID().toString() == record.target_object_id
    }
    if (matches.size() > 1)
        throw new IllegalStateException("Cleanup target ID is ambiguous")
    if (matches.size() == 1) {
        def object = matches[0]
        Map metadata = metadataOf(object)
        if (object.getParent() != targetSections[record.target_section_id] ||
                object.getROI() == null ||
                geometrySha256(object.getROI().getGeometry()) != record.transformed_geometry_sha256 ||
                metadata.get("transfer_qc_id") != record.transfer_qc_id ||
                metadata.get("registration_id") != registration.registration_id ||
                metadata.get("transfer_attempt_id") != request.attempt_id)
            throw new IllegalStateException("Cleanup target conflicts with exact attempt evidence")
        removed << [object: object, parent: object.getParent()]
    }
}

try {
    removed.each { item -> item.parent.removeChildObject(item.object) }
    fixedEntry.saveImageData(fixedData)
    project.syncChanges()
    List remainingIDs = request.records.collect { it.target_object_id }.findAll { targetID ->
        !hierarchy.getFlattenedObjectList(null).findAll {
            it.getID().toString() == targetID
        }.isEmpty()
    }
    if (!remainingIDs.isEmpty())
        throw new IllegalStateException("Cleanup could not attest every target absent")
    Map evidence = [
        schema_version: "qupath-warpy-transfer-cleanup/v1",
        attempt_id: request.attempt_id,
        registration_id: registration.registration_id,
        registration_revision: registration.revision,
        absent_target_object_ids: request.records.collect { it.target_object_id },
        objects: request.records.collect { record -> [
            source_object_id: record.source_object_id,
            target_object_id: record.target_object_id,
            transfer_qc_id: record.transfer_qc_id,
            absent: true,
        ]},
    ]
    atomicWrite(evidencePath, prettyGson.toJson(evidence) + "\n")
} catch (Exception failure) {
    removed.each { item -> item.parent.addChildObject(item.object) }
    fixedEntry.saveImageData(fixedData)
    project.syncChanges()
    throw failure
}

void validateMutationLease(
        Map lease, String attemptID, String projectFile, String fixedEntryID,
        Set expectedOperations) {
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
            !expectedOperations.contains(lease.operation) ||
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

void validateEntryImage(entry, String expectedPath, String expectedSha256) {
    Path path = Paths.get(expectedPath).toAbsolutePath().normalize()
    def fileURIs = entry.getURIs().findAll { it.getScheme() == "file" }.collect {
        Paths.get(it).toAbsolutePath().normalize()
    }
    // Prepared-image identity is its completed root metadata, as in the image reader.
    Path member = Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) ? path.resolve("zarr.json") : path
    if (fileURIs != [path] || Files.isSymbolicLink(path) || Files.isSymbolicLink(member) ||
            !Files.isRegularFile(member, LinkOption.NOFOLLOW_LINKS) || sha256(member) != expectedSha256)
        throw new IllegalStateException("Cleanup fixed project-entry URI/content scope changed")
}

String sha256Bytes(byte[] bytes) {
    MessageDigest.getInstance("SHA-256").digest(bytes).collect {
        String.format("%02x", it)
    }.join()
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
