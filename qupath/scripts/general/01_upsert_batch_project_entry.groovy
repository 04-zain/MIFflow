/**
 * Inspect a project, install its stable pipeline identity, or upsert one entry.
 *
 * This is transport/inventory code.  It deliberately processes one request and
 * never loops a scientific stage over the project image list.
 */

import java.awt.image.BufferedImage
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import java.time.Instant
import com.google.gson.GsonBuilder
import com.google.gson.ToNumberPolicy
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServers
import qupath.lib.projects.ProjectIO
import qupath.lib.projects.Projects
import qupath.lib.common.GeneralTools
import org.mif.image.ManagedImageTypes

if (args.size() != 3)
    throw new IllegalArgumentException("Expected request, project, and evidence paths")

Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path projectPath = Paths.get(args[1] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[2] as String).toAbsolutePath().normalize()
def jsonParser = new GsonBuilder()
    .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE)
    .create()
Map request = jsonParser.fromJson(Files.readString(requestPath), Map.class) as Map
Set envelopeFields = [
    "schema_name", "schema_version", "operation", "project_identity_sha256",
    "mutation_request_sha256", "mutation_authorization_sha256",
    "fencing_token_sha256", "launch_nonce_sha256", "payload",
    "request_envelope_sha256"
] as Set
if (request == null || request.keySet() != envelopeFields ||
        request.schema_name != "mif-qupath-project-catalogue-request" ||
        request.schema_version != 1)
    throw new IllegalArgumentException("Catalogue request is not the current closed schema")
String operation = request.operation as String
if (!(operation in ["inspect_project", "ensure_project_identity", "upsert_entry"]))
    throw new IllegalArgumentException("Unknown catalogue operation")

def sha256File = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    def stream = Files.newInputStream(path)
    try {
        byte[] buffer = new byte[1024 * 1024]
        int count
        while ((count = stream.read(buffer)) >= 0)
            if (count > 0) digest.update(buffer, 0, count)
    } finally {
        stream.close()
    }
    digest.digest().encodeHex().toString()
}
def sha256Text = { String text ->
    MessageDigest.getInstance("SHA-256")
        .digest(text.getBytes(StandardCharsets.UTF_8)).encodeHex().toString()
}
def compareUnicode = { String left, String right ->
    int leftIndex = 0
    int rightIndex = 0
    while (leftIndex < left.length() && rightIndex < right.length()) {
        int leftCodePoint = left.codePointAt(leftIndex)
        int rightCodePoint = right.codePointAt(rightIndex)
        if (leftCodePoint != rightCodePoint)
            return Integer.compare(leftCodePoint, rightCodePoint)
        leftIndex += Character.charCount(leftCodePoint)
        rightIndex += Character.charCount(rightCodePoint)
    }
    Integer.compare(left.length() - leftIndex, right.length() - rightIndex)
}
def appendCanonicalString = { StringBuilder output, CharSequence raw ->
    String value = raw.toString()
    output.append("\"")
    int index = 0
    while (index < value.length()) {
        char unit = value.charAt(index)
        int codePoint = value.codePointAt(index)
        if (Character.isSurrogate(unit) && Character.charCount(codePoint) == 1)
            throw new IllegalArgumentException("Canonical JSON string contains an unpaired surrogate")
        switch (codePoint) {
            case 0x22: output.append("\\\"");
            break
            case 0x5c: output.append("\\\\");
            break
            case 0x08: output.append("\\b");
            break
            case 0x0c: output.append("\\f");
            break
            case 0x0a: output.append("\\n");
            break
            case 0x0d: output.append("\\r");
            break
            case 0x09: output.append("\\t");
            break
            default:
                if (codePoint < 0x20) {
                    String hexadecimal = Integer.toHexString(codePoint)
                    output.append("\\u")
                    output.append("0" * (4 - hexadecimal.length()))
                    output.append(hexadecimal)
                } else {
                    output.appendCodePoint(codePoint)
                }
        }
        index += Character.charCount(codePoint)
    }
    output.append("\"")
}
def appendCanonicalJson
appendCanonicalJson = { StringBuilder output, value ->
    if (value == null) {
        output.append("null")
    } else if (value instanceof Map) {
        List<String> keys = []
        value.keySet().each { key ->
            if (!(key instanceof String))
                throw new IllegalArgumentException("Canonical JSON object keys must be strings")
            keys.add(key as String)
        }
        keys.sort(compareUnicode as Comparator<String>)
        output.append("{")
        keys.eachWithIndex { key, keyIndex ->
            if (keyIndex > 0) output.append(",")
            appendCanonicalString(output, key)
            output.append(":")
            appendCanonicalJson(output, value.get(key))
        }
        output.append("}")
    } else if (value instanceof Collection) {
        output.append("[")
        value.eachWithIndex { item, itemIndex ->
            if (itemIndex > 0) output.append(",")
            appendCanonicalJson(output, item)
        }
        output.append("]")
    } else if (value instanceof CharSequence) {
        appendCanonicalString(output, value)
    } else if (value instanceof Boolean) {
        output.append(value ? "true" : "false")
    } else if (value instanceof Byte || value instanceof Short ||
            value instanceof Integer || value instanceof Long ||
            value instanceof BigInteger) {
        output.append(value.toString())
    } else {
        throw new IllegalArgumentException(
            "Canonical JSON value has unsupported type ${value.getClass().name}"
        )
    }
}
def canonicalJson = { value ->
    StringBuilder output = new StringBuilder()
    appendCanonicalJson(output, value)
    output.toString()
}
def canonicalUri = { URI raw ->
    URI normalized = raw.normalize()
    if (normalized.getScheme()?.equalsIgnoreCase("file"))
        return Paths.get(normalized).toAbsolutePath().normalize().toUri()
    normalized
}

def project
if (Files.isRegularFile(projectPath))
    project = ProjectIO.loadProject(projectPath.toFile(), BufferedImage.class)
else if (operation == "ensure_project_identity" && request.payload.create_if_missing == true) {
    Files.createDirectories(projectPath.parent)
    project = Projects.createProject(projectPath.toFile(), BufferedImage.class)
} else
    throw new IllegalStateException("QuPath project is missing")

String identityKey = "mif_pipeline.project_identity_record_v1"
String expectedIdentityJson = operation == "ensure_project_identity"
    ? canonicalJson(request.payload.identity_record)
    : null
String embeddedIdentityJson = project.getMetadata().get(identityKey) as String
if (operation == "ensure_project_identity") {
    if (embeddedIdentityJson != null && embeddedIdentityJson != expectedIdentityJson)
        throw new IllegalStateException("Project identity metadata conflicts")
    project.getMetadata().put(identityKey, expectedIdentityJson)
} else if (request.project_identity_sha256 != null && embeddedIdentityJson == null)
    throw new IllegalStateException("Managed project identity metadata is missing")

boolean replacement = operation == "upsert_entry" && request.payload.entry_mode == "replace"
byte[] predecessorProject = replacement ? Files.readAllBytes(projectPath) : null
// QuPath persists project metadata separately from the active project file.
// Retain its literal predecessor so failed publication cannot leave history active.
Path projectMetadataPath = projectPath.parent.resolve("data/metadata.json")
byte[] predecessorMetadata = replacement && Files.exists(projectMetadataPath)
    ? Files.readAllBytes(projectMetadataPath) : null
boolean replacementStarted = false
try {
def affectedEntry = null
if (operation == "upsert_entry") {
    if (!(request.payload.entry_mode in ["reuse", "replace"]))
        throw new IllegalArgumentException("Entry mode is not current")
    URI sourceUri = canonicalUri(new URI(request.payload.source_uri as String))
    def matches = project.getImageList().findAll { item ->
        item.getURIs().any { uri -> canonicalUri(uri) == sourceUri }
    }
    if (matches.size() > 1)
        throw new IllegalStateException("Source URI resolves to multiple project entries")
    String expectedId = request.payload.expected_project_entry_id as String
    affectedEntry = matches ? matches[0] : null
    if (replacement) {
        if (expectedId == null || affectedEntry != null)
            throw new IllegalStateException("Source replacement requires a distinct new image and its saved predecessor")
        def oldEntries = project.getImageList().findAll { item -> item.getID().toString() == expectedId }
        if (oldEntries.size() != 1)
            throw new IllegalStateException("Saved source image is missing or ambiguous")
        def oldEntry = oldEntries[0]
        for (String key : ["mif_pipeline.batch_id", "mif_pipeline.sample_id", "mif_pipeline.slide_id", "mif_pipeline.validation_domain_id", "mif_pipeline.image_role"])
            if (oldEntry.getMetadataValue(key) != request.payload.entry_metadata[key])
                throw new IllegalStateException("Saved source image identity changed")
        def support = ImageServers.getImageSupport(sourceUri)
        if (support == null || support.getBuilders().size() != 1)
            throw new IllegalStateException("Replacement image must resolve to exactly one image builder")
        Path historyPath = projectPath.parent.resolve("source-history-${request.mutation_request_sha256}.qpproj")
        if (Files.isSymbolicLink(historyPath))
            throw new IllegalStateException("Source project history is a symbolic link")
        if (Files.exists(historyPath)) {
            if (!Arrays.equals(Files.readAllBytes(historyPath), predecessorProject))
                throw new IllegalStateException("Source project history conflicts with its admitted predecessor")
        } else {
            Files.write(historyPath, predecessorProject, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
        }
        String historyKey = "mif_pipeline.source_history_v1"
        String priorHistory = project.getMetadata().get(historyKey)
        List history = priorHistory == null ? [] : jsonParser.fromJson(priorHistory, List.class)
        Map historyEntry = [mutation_request_sha256: request.mutation_request_sha256, project_entry_id: expectedId]
        if (history.any { it.mutation_request_sha256 == request.mutation_request_sha256 })
            throw new IllegalStateException("Source replacement history already contains this request")
        history.add(historyEntry)
        project.getMetadata().put(historyKey, canonicalJson(history))
        replacementStarted = true
        // Removing an image from the active list must retain its data directory.
        project.removeImage(oldEntry, false)
        affectedEntry = project.addImage(support.getBuilders().get(0))
        if (history.any { it.project_entry_id == affectedEntry.getID().toString() })
            throw new IllegalStateException("Replacement image reused a retained historical entry ID")
    } else {
        if (affectedEntry == null) {
            def support = ImageServers.getImageSupport(sourceUri)
            if (support == null || support.getBuilders().size() != 1)
                throw new IllegalStateException("Source URI must resolve to exactly one image builder")
            affectedEntry = project.addImage(support.getBuilders().get(0))
        }
        if (expectedId != null && affectedEntry.getID().toString() != expectedId)
            throw new IllegalStateException("Existing project entry ID changed")
    }
    affectedEntry.setImageName(request.payload.entry_name as String)
    (request.payload.entry_metadata as Map).each { key, value ->
        if (!(key as String).startsWith("mif_pipeline."))
            throw new IllegalArgumentException("Only namespaced entry metadata is permitted")
        affectedEntry.putMetadataValue(key as String, value as String)
    }
    // We initialize an unset image from its reviewed role before saving it.
    // An explicit existing type stays selected; required saved-state hashes
    // still come from the completed ordinary save below.
    String imageRole = request.payload.entry_metadata["mif_pipeline.image_role"] as String
    if (request.payload.require_saved_state_attestation == true ||
            imageRole in ["primary_mif", "moving_reference"]) {
        // A failed unpublished entry can leave qpdata at a reused numeric ID.
        // Build the replacement from its new server, never from that hierarchy.
        def imageData = replacement
            ? new ImageData<BufferedImage>(affectedEntry.getServerBuilder().build())
            : affectedEntry.readImageData()
        boolean initialized = imageRole in ["primary_mif", "moving_reference"] &&
            ManagedImageTypes.initializeIfUnset(imageData, imageRole,
                request.payload.entry_metadata["mif_pipeline.reference_modality"] as String)
        if (request.payload.require_saved_state_attestation == true || initialized)
            affectedEntry.saveImageData(imageData)
    }
}

if (operation != "inspect_project")
    project.syncChanges()
project = ProjectIO.loadProject(projectPath.toFile(), BufferedImage.class)
embeddedIdentityJson = project.getMetadata().get(identityKey) as String
if (request.project_identity_sha256 != null && embeddedIdentityJson == null)
    throw new IllegalStateException("Managed project identity metadata was not retained")

def entryEvidence = { entry ->
    Path entryPath = entry.getEntryPath().toAbsolutePath().normalize()
    Path dataPath = entryPath.resolve("data.qpdata")
    Map unsigned = [
        project_entry_id: entry.getID().toString(),
        entry_name: entry.getImageName(),
        entry_path: entryPath.toString(),
        source_uris: entry.getURIs().collect {
            canonicalUri(it).toString()
        }.sort(),
        metadata: new TreeMap(entry.getMetadataMap()),
        saved_state_attestation_sha256: Files.isRegularFile(dataPath) ? sha256File(dataPath) : null,
    ]
    unsigned + [entry_projection_sha256: sha256Text(canonicalJson(unsigned))]
}
List entries = project.getImageList().collect(entryEvidence).sort { it.project_entry_id }
Map selected = affectedEntry == null ? null : entries.find {
    it.project_entry_id == affectedEntry.getID().toString()
}
String nonce = System.getenv("MIF_QUPATH_PROJECT_LAUNCH_NONCE")
Map token = null
String nonceDigest = null
if (operation != "inspect_project") {
    if (nonce == null || nonce.isEmpty())
        throw new IllegalStateException("Mutation launch nonce is missing")
    if (!(nonce ==~ /[0-9a-f]{64}/))
        throw new IllegalStateException("Mutation launch nonce has invalid encoding")
    nonceDigest = MessageDigest.getInstance("SHA-256")
        .digest(nonce.decodeHex()).encodeHex().toString()
    def handle = ProcessHandle.current()
    long processId = handle.pid()
    String osName = System.getProperty("os.name", "").toLowerCase(Locale.ROOT)
    if (osName.contains("linux")) {
        String procStat = Files.readString(Paths.get("/proc/self/stat"))
        int commEnd = procStat.lastIndexOf(")")
        if (commEnd < 0)
            throw new IllegalStateException("Linux process identity is unavailable")
        String[] fields = procStat.substring(commEnd + 2).trim().split(/\s+/)
        if (fields.length <= 19)
            throw new IllegalStateException("Linux process identity is incomplete")
        long startTicks = Long.parseLong(fields[19])
        if (startTicks <= 0L)
            throw new IllegalStateException("Linux process start ticks are invalid")
        token = [
            provider: "linux-proc-start-ticks/v1",
            process_id: processId,
            start_ticks: startTicks,
            canonical_token: "linux-proc-start-ticks/v1:${processId}:${startTicks}",
        ]
    } else if (osName.contains("windows")) {
        long startMillis = handle.info().startInstant().orElseThrow {
            new IllegalStateException("Process creation time is unavailable")
        }.toEpochMilli()
        if (startMillis <= 0L)
            throw new IllegalStateException("Process creation time is invalid")
        token = [
            provider: "windows-process-creation-epoch-millis/v1",
            process_id: processId,
            start_epoch_millis: startMillis,
            canonical_token: "windows-process-creation-epoch-millis/v1:${processId}:${startMillis}",
        ]
    } else {
        throw new IllegalStateException("Process identity platform is unsupported")
    }
}
Map snapshot = [
    project_path: projectPath.toString(), project_uri: projectPath.toUri().normalize().toString(),
    identity_record: embeddedIdentityJson == null ? null : jsonParser.fromJson(embeddedIdentityJson, Map.class),
    qpproj_sha256: sha256File(projectPath), entries: entries,
]
Map materialization = null
if (operation == "ensure_project_identity") {
    Map unsigned = [
        schema_name: "mif-qupath-project-identity-materialization", schema_version: 1,
        project_identity_sha256: request.payload.identity_record.project_identity_sha256,
        origin_kind: request.payload.identity_record.origin_kind,
        origin_qpproj_sha256: request.payload.identity_record.origin_qpproj_sha256,
        identity_metadata_sha256: sha256Text(embeddedIdentityJson),
        mutation_request_sha256: request.mutation_request_sha256,
        qupath_runtime_version: GeneralTools.getVersion(),
        post_save_qpproj_snapshot_sha256: snapshot.qpproj_sha256,
    ]
    materialization = unsigned + [
        materialization_attestation_sha256: sha256Text(canonicalJson(unsigned))
    ]
}
Map evidenceUnsigned = [
    schema_name: "mif-qupath-project-catalogue-evidence", schema_version: 1,
    operation: operation,
    request_envelope_sha256: request.request_envelope_sha256,
    launch_nonce_sha256: nonceDigest,
    process_start_token: token,
    entry: selected,
    post_save_project_snapshot: snapshot,
    materialization_attestation: materialization,
]
Map evidence = evidenceUnsigned + [evidence_sha256: sha256Text(canonicalJson(evidenceUnsigned))]
try {
    Files.writeString(evidencePath, canonicalJson(evidence), StandardCharsets.UTF_8, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
} catch (java.nio.file.FileAlreadyExistsException conflict) {
    System.err.println("Catalogue evidence result already exists: ${evidencePath}")
    throw conflict
}

} catch (Throwable failure) {
    if (replacementStarted) {
        // Restore both native project files, attempting each even if one fails.
        // Historical image data was never deleted; unpublished fresh data may remain.
        for (Map saved : [[path: projectPath, bytes: predecessorProject],
                          [path: projectMetadataPath, bytes: predecessorMetadata]]) {
            try {
                if (saved.bytes == null)
                    Files.deleteIfExists(saved.path as Path)
                else
                    Files.write(saved.path as Path, saved.bytes as byte[],
                        StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING,
                        StandardOpenOption.WRITE)
            } catch (Throwable rollbackFailure) {
                failure.addSuppressed(rollbackFailure)
            }
        }
    }
    throw failure
}
