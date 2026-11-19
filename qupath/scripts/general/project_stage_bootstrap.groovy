// MIF_PROJECT_STAGE_BOOTSTRAP_V1_BEGIN
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.ToNumberPolicy
import java.net.URI
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.security.MessageDigest
import java.util.Locale
import java.util.Optional

/**
 * Reads the prepared launch inputs and verifies the saved identity of the current QuPath project.
 *
 * We use these small parsing and checksum helpers before a native processing
 * step opens its request or updates project objects. The calling script then
 * compares the current image entry, original image location and running process
 * with the values supplied for that operation.
 *
 * This same helper implementation is included in each project-processing
 * script so the initial checks are available before another prepared script
 * is loaded.
 */
final class MifProjectStageBootstrapV1 {
    private static final Gson GSON = new GsonBuilder()
        .disableHtmlEscaping().serializeNulls()
        .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
    private static final Set<String> IDENTITY_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256",
        "project_identity_ref", "project_uuid", "origin_kind",
        "origin_qpproj_sha256", "predecessor_project_identity_sha256",
        "project_identity_sha256"
    ] as Set
    private static final Set<String> IDENTITY_REF_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256", "family",
        "key_sha256", "suffix", "media_type"
    ] as Set

    /** Read the issuer's new-publication directory without changing scientific settings. */
    static Path outputRoot(Map request, Map config) {
        canonicalOutputRoot(request.containsKey("operational_output_dir") ?
            request.operational_output_dir : config.output_dir)
    }

    /**
     * Read a relative ROI table beneath its original producing request directory.
     * New envelopes require every selected producer; only historical envelopes
     * without the map use the original saved configuration location.
     */
    static Path producerOutputRoot(Map request, Map config, String producerAttemptId) {
        if (!request.containsKey("producer_output_dirs"))
            return canonicalOutputRoot(config.output_dir)
        if (!(request.producer_output_dirs instanceof Map) ||
                !request.producer_output_dirs.containsKey(producerAttemptId))
            throw new IllegalStateException("Selected ROI table lacks its recorded producer directory")
        canonicalOutputRoot(request.producer_output_dirs[producerAttemptId])
    }

    private static Path canonicalOutputRoot(Object value) {
        if (!(value instanceof String) || !value || value != value.trim())
            throw new IllegalStateException("Output directory requires canonical absolute text")
        Path root = Paths.get(value as String)
        if (!root.isAbsolute() || root != root.normalize() || root.toString() != value)
            throw new IllegalStateException("Output directory requires a normalized absolute path")
        root
    }

    /**
     * Calculates the SHA-256 used to compare saved project and process-input contents.
     *
     * Args:
     *     payload: Complete bytes of the record or prepared script being checked.
     *
     * Returns:
     *     Lowercase hexadecimal SHA-256 of those bytes.
     */
    static String mifSha256(byte[] payload) {
        MessageDigest.getInstance("SHA-256").digest(payload).encodeHex().toString()
    }

    /**
     * Records the saved image state together with its completed stage result.
     *
     * We read the saved image bytes after the stage's existing save operation,
     * then publish the result and its original launch identity as one file.
     * The owning runtime compares this checksum with its current project
     * inspection before accepting the result or recovering an interrupted run.
     *
     * Args:
     *     destination: Result path selected by the authenticated stage guard.
     *     result: The stage's final result envelope, including its manifest reference.
     *     savedImage: Saved data.qpdata belonging to the bound project entry.
     *     identity: Original entry, request, prepared descriptor and process identities.
     */
    static void publishResult(Path destination, Map result, Path savedImage, Map identity) {
        MessageDigest digest = MessageDigest.getInstance("SHA-256")
        Files.newInputStream(savedImage).withCloseable { stream ->
            byte[] buffer = new byte[1024 * 1024]
            int count
            while ((count = stream.read(buffer)) != -1) {
                if (count > 0) digest.update(buffer, 0, count)
            }
        }
        Map receipt = new LinkedHashMap(identity)
        receipt.saved_state_sha256 = digest.digest().encodeHex().toString()
        Map completed = new LinkedHashMap(result)
        completed.saved_project_state = receipt
        byte[] bytes = (GSON.toJson(completed) + "\n").getBytes(StandardCharsets.UTF_8)
        Files.createDirectories(destination.parent)
        Path temporary = Files.createTempFile(destination.parent, ".${destination.fileName}.", ".tmp")
        try {
            java.nio.channels.FileChannel.open(temporary,
                    java.nio.file.StandardOpenOption.WRITE).withCloseable { channel ->
                ByteBuffer buffer = ByteBuffer.wrap(bytes)
                while (buffer.hasRemaining()) channel.write(buffer)
                channel.force(true)
            }
            Files.move(temporary, destination,
                java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        } finally {
            Files.deleteIfExists(temporary)
        }
    }

    /**
     * Converts an image URI to the consistent representation used by the saved project request.
     *
     * We normalize the URI. For local files, we also convert through an absolute
     * normalized filesystem path before returning its URI. Other schemes retain
     * their normalized ASCII URI. This gives the request and current image entry
     * the same path representation for the source-location comparison.
     *
     * Args:
     *     raw: Source URI reported by the current image entry.
     *
     * Returns:
     *     Normalized ASCII URI text.
     */
    static String mifCanonicalUri(URI raw) {
        URI normalized = raw.normalize()
        if (normalized.getScheme()?.equalsIgnoreCase("file")) {
            return Paths.get(normalized).toAbsolutePath().normalize().toUri()
                .normalize().toASCIIString()
        }
        return normalized.toASCIIString()
    }

    /**
     * Decodes a saved record as UTF-8 and reports invalid byte sequences.
     *
     * The decoder is configured to report malformed or unmappable input. A damaged
     * record therefore stops before altered replacement characters can enter its
     * field or checksum comparisons.
     *
     * Args:
     *     payload: Complete bytes read from the selected record.
     *     label: Record description included in a decoding error.
     *
     * Returns:
     *     Decoded text for JSON parsing or script loading.
     *
     * Raises:
     *     IllegalStateException: The bytes cannot be decoded as UTF-8.
     */
    static String mifStrictUtf8(byte[] payload, String label) {
        try {
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(payload)).toString()
        } catch (Exception error) {
            throw new IllegalStateException("${label} is not strict UTF-8", error)
        }
    }

    /**
     * Orders nested dictionary keys before calculating a saved-record checksum.
     *
     * We recursively place map entries into TreeMap order and process list items
     * in their existing sequence. Scalar values are retained. The resulting
     * representation lets the same project fields produce the same encoded bytes
     * regardless of their original dictionary insertion order.
     *
     * Args:
     *     value: Decoded JSON value or collection of checked project fields.
     *
     * Returns:
     *     Nested sorted maps and ordered lists ready for consistent JSON encoding.
     *
     * Raises:
     *     IllegalStateException: A map contains a non-text key.
     */
    private static Object canonicalValue(Object value) {
        if (value instanceof Map) {
            Map sorted = new TreeMap()
            (value as Map).each { key, item ->
                if (!(key instanceof String)) {
                    throw new IllegalStateException("canonical JSON key is invalid")
                }
                sorted[key as String] = canonicalValue(item)
            }
            return sorted
        }
        if (value instanceof List) {
            return (value as List).collect { canonicalValue(it) }
        }
        return value
    }

    /**
     * Encodes ordered project fields as the UTF-8 JSON used by their checksum calculation.
     *
     * Args:
     *     value: Checked nested project-identity or saved-reference values.
     *
     * Returns:
     *     JSON bytes after canonicalValue() sorts dictionary keys. The shared
     *     Gson settings preserve explicit nulls and the supported integer/float values.
     */
    private static byte[] canonicalBytes(Object value) {
        GSON.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8)
    }

    /**
     * Reads a saved JSON object from its UTF-8 bytes.
     *
     * Args:
     *     payload: Complete bytes of a project, launch or prepared-operation record.
     *     label: Record name used when decoding or parsing fails.
     *
     * Returns:
     *     Parsed map for the operation-specific field checks.
     *
     * Raises:
     *     IllegalStateException: UTF-8/JSON decoding fails or the outer value is not an object.
     */
    static Map parseMap(byte[] payload, String label) {
        Object parsed
        try {
            parsed = GSON.fromJson(mifStrictUtf8(payload, label), Object)
        } catch (RuntimeException error) {
            throw new IllegalStateException("${label} JSON is invalid", error)
        }
        if (!(parsed instanceof Map)) {
            throw new IllegalStateException("${label} is not a JSON object")
        }
        return parsed as Map
    }

    /**
     * Reads one prepared-operation path supplied to the QuPath process at launch.
     *
     * The path must already be absolute and normalized. These fields are set by
     * the pipeline when it starts the operation and identify the saved request,
     * script or launch information that QuPath is about to inspect.
     *
     * Args:
     *     environment: Environment values supplied to this QuPath process.
     *     name: Required path-variable name.
     *
     * Returns:
     *     Checked native filesystem Path.
     *
     * Raises:
     *     IllegalStateException: The value cannot be read as a path or is not
     *         already absolute and normalized.
     */
    static Path environmentPath(Map<String, String> environment, String name) {
        Path path
        try {
            path = Path.of(environment[name])
        } catch (RuntimeException error) {
            throw new IllegalStateException("${name} is not a native path", error)
        }
        if (!path.isAbsolute() || path != path.toAbsolutePath().normalize()) {
            throw new IllegalStateException("${name} is not absolute and normalized")
        }
        return path
    }

    /**
     * Requires exactly the saved field set for one project-identity record.
     *
     * Args:
     *     value: Decoded record being checked.
     *     fields: Complete required field-name set.
     *     label: Record description included in an error.
     *
     * Raises:
     *     IllegalStateException: The record is absent or has missing/additional fields.
     */
    private static void requireClosed(Map value, Set<String> fields, String label) {
        if (value == null || value.keySet() != fields) {
            throw new IllegalStateException("${label} is not closed")
        }
    }

    /**
     * Checks the format of a required or optional saved SHA-256 value.
     *
     * Args:
     *     value: Candidate lowercase hexadecimal checksum.
     *     label: Quantity description included in an error.
     *     optional: Whether null is permitted for an inapplicable origin field.
     *
     * Raises:
     *     IllegalStateException: A required value is absent or supplied text
     *         does not contain exactly 64 lowercase hexadecimal characters.
     */
    private static void requireDigest(Object value, String label, boolean optional) {
        if (optional && value == null) return
        if (!(value instanceof String) || !(value ==~ /[0-9a-f]{64}/)) {
            throw new IllegalStateException("${label} is not lowercase SHA-256")
        }
    }

    /**
     * Checks the saved project ID and the information connecting it to the prepared analysis.
     *
     * We parse the current identity record and require its version, UUID and
     * complete field set. The project origin determines which earlier-file
     * values must be present: a newly created project has neither, an adopted
     * project identifies its original file, and a copied-as-new project also
     * identifies the preceding project.
     *
     * We recompute the project checksum from those fields, then recompute the
     * saved-reference key that identifies this project record within the analysis
     * folder. The associated format, file suffix and media type must agree.
     * Finally, its stored JSON must equal the consistently encoded checked fields.
     *
     * Args:
     *     identityJson: Project-identity text retained in the current QuPath
     *         project's metadata by the pipeline's project-preparation step.
     *
     * Returns:
     *     Checked project-identity checksum used to compare the current project
     *     with the prepared operation's expected project.
     *
     * Raises:
     *     IllegalStateException: Project fields, origin relationships, recorded
     *         checksums, reference fields or encoded JSON are inconsistent.
     */
    static String validateIdentity(String identityJson) {
        byte[] identityBytes = identityJson.getBytes(StandardCharsets.UTF_8)
        Map identity = parseMap(identityBytes, "project identity metadata")
        requireClosed(identity, IDENTITY_FIELDS, "project identity metadata")
        requireDigest(identity.authority_layout_sha256, "authority layout digest", false)
        requireDigest(identity.project_identity_sha256, "project identity digest", false)
        requireDigest(identity.origin_qpproj_sha256, "origin qpproj digest", true)
        requireDigest(
            identity.predecessor_project_identity_sha256,
            "predecessor project identity digest", true
        )
        if (identity.schema_name != "mif-qupath-project-identity" ||
            identity.schema_version != 2L ||
            !(identity.project_uuid instanceof String) ||
            !(identity.project_uuid ==~
                /[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/)) {
            throw new IllegalStateException("project identity schema or UUID is invalid")
        }
        boolean originValid =
            (identity.origin_kind == "created" &&
                identity.origin_qpproj_sha256 == null &&
                identity.predecessor_project_identity_sha256 == null) ||
            (identity.origin_kind == "adopted_existing" &&
                identity.origin_qpproj_sha256 != null &&
                identity.predecessor_project_identity_sha256 == null) ||
            (identity.origin_kind == "cloned_as_new" &&
                identity.origin_qpproj_sha256 != null &&
                identity.predecessor_project_identity_sha256 != null)
        if (!originValid) {
            throw new IllegalStateException("project identity origin is invalid")
        }
        Map identityPreimage = [
            schema_name: identity.schema_name,
            schema_version: identity.schema_version,
            authority_layout_sha256: identity.authority_layout_sha256,
            project_uuid: identity.project_uuid,
            origin_kind: identity.origin_kind,
            origin_qpproj_sha256: identity.origin_qpproj_sha256,
            predecessor_project_identity_sha256:
                identity.predecessor_project_identity_sha256
        ]
        if (identity.project_identity_sha256 !=
            mifSha256(canonicalBytes(identityPreimage))) {
            throw new IllegalStateException("project identity digest is invalid")
        }
        if (!(identity.project_identity_ref instanceof Map)) {
            throw new IllegalStateException("project identity ref is invalid")
        }
        Map identityRef = identity.project_identity_ref as Map
        requireClosed(identityRef, IDENTITY_REF_FIELDS, "project identity ref")
        Map refKeyPreimage = [
            authority_layout_sha256: identity.authority_layout_sha256,
            family: "project_identity",
            scope: [project_identity_sha256: identity.project_identity_sha256],
            natural_key: [:]
        ]
        if (identityRef.schema_name != "mif-authority-member-ref" ||
            identityRef.schema_version != 2L ||
            identityRef.authority_layout_sha256 !=
                identity.authority_layout_sha256 ||
            identityRef.family != "project_identity" ||
            identityRef.key_sha256 != mifSha256(canonicalBytes(refKeyPreimage)) ||
            identityRef.suffix != "json" ||
            identityRef.media_type != "application/json") {
            throw new IllegalStateException("project identity ref is invalid")
        }
        if (!java.util.Arrays.equals(canonicalBytes(identity), identityBytes)) {
            throw new IllegalStateException("project identity JSON is not canonical")
        }
        return identity.project_identity_sha256 as String
    }
}

final String mifCurrentTargetScriptAssetId = "@@MIF_TARGET_SCRIPT_ASSET_ID@@"
final Set<String> mifRequiredProjectStageEnvironmentNames = [
    "MIF_QUPATH_PROJECT_STAGE_CONTEXT_VERSION",
    "MIF_QUPATH_PROJECT_STAGE_RUN_PLAN_PATH",
    "MIF_QUPATH_PROJECT_STAGE_PREPARED_DESCRIPTOR_PATH",
    "MIF_QUPATH_PROJECT_STAGE_PROCESS_LAUNCH_PATH",
    "MIF_QUPATH_PROJECT_STAGE_BUNDLE_MANIFEST_PATH",
    "MIF_QUPATH_PROJECT_STAGE_GUARD_SCRIPT_PATH",
    "MIF_QUPATH_PROJECT_STAGE_TARGET_SCRIPT_PATH"
] as Set
final Map<String, String> mifProjectStageEnvironment = System.getenv()
final Set<String> mifProjectStageEnvironmentNames = mifProjectStageEnvironment
    .keySet()
    .findAll { String name ->
        name.toUpperCase(Locale.ROOT).startsWith("MIF_QUPATH_PROJECT_STAGE_")
    } as Set
final String mifProjectStageContextVersion = mifProjectStageEnvironment[
    "MIF_QUPATH_PROJECT_STAGE_CONTEXT_VERSION"
]
boolean mifAuthenticatedHeadlessProjectStage = false
Closure mifPublishProjectStageResult = { Path destination, Map result ->
    throw new IllegalStateException("Stage result publication requires an authenticated project launch")
}
if (mifProjectStageContextVersion != null || !mifProjectStageEnvironmentNames.isEmpty()) {
    if (mifProjectStageEnvironmentNames !=
        mifRequiredProjectStageEnvironmentNames) {
        throw new IllegalStateException(
            "project-stage environment names are incomplete or unknown"
        )
    }
    final String mifProjectStageLaunchNonce = mifProjectStageEnvironment[
        "MIF_QUPATH_PROJECT_LAUNCH_NONCE"
    ]
    if (mifProjectStageContextVersion != "1" ||
        mifRequiredProjectStageEnvironmentNames.any { String name ->
            String value = mifProjectStageEnvironment[name]
            value == null || value.trim().isEmpty() || value.indexOf("\u0000") >= 0
        } ||
        mifProjectStageLaunchNonce == null ||
        !(mifProjectStageLaunchNonce ==~ /[0-9a-f]{64}/) ||
        mifProjectStageLaunchNonce.indexOf("\u0000") >= 0) {
        throw new IllegalStateException("project-stage environment is invalid")
    }

    final Path mifRunPlanPath = MifProjectStageBootstrapV1.environmentPath(
        mifProjectStageEnvironment, "MIF_QUPATH_PROJECT_STAGE_RUN_PLAN_PATH"
    )
    final Path mifPreparedDescriptorPath =
        MifProjectStageBootstrapV1.environmentPath(
            mifProjectStageEnvironment,
            "MIF_QUPATH_PROJECT_STAGE_PREPARED_DESCRIPTOR_PATH"
        )
    final Path mifProcessLaunchPath = MifProjectStageBootstrapV1.environmentPath(
        mifProjectStageEnvironment,
        "MIF_QUPATH_PROJECT_STAGE_PROCESS_LAUNCH_PATH"
    )
    final Path mifBundleManifestPath =
        MifProjectStageBootstrapV1.environmentPath(
            mifProjectStageEnvironment,
            "MIF_QUPATH_PROJECT_STAGE_BUNDLE_MANIFEST_PATH"
        )
    final Path mifGuardPath = MifProjectStageBootstrapV1.environmentPath(
        mifProjectStageEnvironment,
        "MIF_QUPATH_PROJECT_STAGE_GUARD_SCRIPT_PATH"
    )
    final Path mifTargetPath = MifProjectStageBootstrapV1.environmentPath(
        mifProjectStageEnvironment,
        "MIF_QUPATH_PROJECT_STAGE_TARGET_SCRIPT_PATH"
    )
    for (Path mifInputPath : [
        mifRunPlanPath, mifPreparedDescriptorPath, mifProcessLaunchPath,
        mifBundleManifestPath, mifGuardPath, mifTargetPath
    ]) {
        if (!Files.isRegularFile(mifInputPath)) {
            throw new IllegalStateException("project-stage input member is missing")
        }
    }

    final byte[] mifRunPlanBytes = Files.readAllBytes(mifRunPlanPath)
    final byte[] mifPreparedDescriptorBytes =
        Files.readAllBytes(mifPreparedDescriptorPath)
    final byte[] mifProcessLaunchBytes = Files.readAllBytes(mifProcessLaunchPath)
    final byte[] mifManifestBytes = Files.readAllBytes(mifBundleManifestPath)
    final byte[] mifGuardBytes = Files.readAllBytes(mifGuardPath)
    final Map mifRunPlan = MifProjectStageBootstrapV1.parseMap(
        mifRunPlanBytes, "run plan"
    )
    // We match the helper to the installed script version selected in this run.
    if (MifProjectStageBootstrapV1.mifSha256(mifGuardBytes) !=
        mifRunPlan.guard_script_sha256) {
        throw new IllegalStateException("project-stage guard bytes differ")
    }
    final String mifGuardSource = MifProjectStageBootstrapV1.mifStrictUtf8(mifGuardBytes, "project-stage guard")
    final byte[] mifTargetBytes = Files.readAllBytes(mifTargetPath)
    final Map mifPreparedDescriptor = MifProjectStageBootstrapV1.parseMap(
        mifPreparedDescriptorBytes, "prepared descriptor"
    )
    final Map mifProcessLaunch = MifProjectStageBootstrapV1.parseMap(
        mifProcessLaunchBytes, "process launch"
    )

    final Path mifBundleRoot = mifBundleManifestPath.parent
    if (mifBundleRoot == null ||
        mifBundleRoot.fileName.toString() !=
            "bundle-${mifRunPlan.script_bundle_sha256}") {
        throw new IllegalStateException("project-stage bundle root is invalid")
    }
    final Path mifExpectedGuardPath = mifBundleRoot
        .resolve("general").resolve("batch_stage_guard.groovy").normalize()
    final String mifAssetPrefix = "qupath/scripts/"
    if (!mifCurrentTargetScriptAssetId.startsWith(mifAssetPrefix)) {
        throw new IllegalStateException("current target asset ID is invalid")
    }
    final Path mifExpectedTargetPath = mifBundleRoot.resolve(
        Path.of(mifCurrentTargetScriptAssetId.substring(mifAssetPrefix.length()))
    ).normalize()
    if (mifGuardPath != mifExpectedGuardPath ||
        mifTargetPath != mifExpectedTargetPath) {
        throw new IllegalStateException("project-stage script path differs")
    }
    if (!(mifPreparedDescriptor.process_launch_ref instanceof Map)) {
        throw new IllegalStateException("process launch ref is invalid")
    }
    final Map mifProcessLaunchRef = mifPreparedDescriptor.process_launch_ref as Map
    final Set<String> mifProcessLaunchRefFields = [
        "schema_name", "schema_version", "authority_layout_sha256", "family",
        "key_sha256", "suffix", "media_type"
    ] as Set
    if (mifProcessLaunchRef.keySet() != mifProcessLaunchRefFields ||
        mifProcessLaunchRef.schema_name != "mif-authority-member-ref" ||
        mifProcessLaunchRef.schema_version != 2L ||
        mifProcessLaunchRef.authority_layout_sha256 !=
            mifRunPlan.authority_layout_sha256 ||
        mifProcessLaunchRef.authority_layout_sha256 !=
            mifPreparedDescriptor.authority_layout_sha256 ||
        mifProcessLaunchRef.family != "runtime_exchange" ||
        !(mifProcessLaunchRef.key_sha256 ==~ /[0-9a-f]{64}/) ||
        mifProcessLaunchRef.suffix != "json" ||
        mifProcessLaunchRef.media_type != "application/json") {
        throw new IllegalStateException("process launch ref is invalid")
    }
    final Path mifExchangeRoot = mifProcessLaunchPath.parent
    if (mifExchangeRoot == null ||
        mifExchangeRoot.resolve(
            "${mifProcessLaunchRef.family}-${mifProcessLaunchRef.key_sha256}.${mifProcessLaunchRef.suffix}"
        ).normalize() != mifProcessLaunchPath) {
        throw new IllegalStateException("process launch path differs")
    }

    final Object mifProject = getProject()
    final Object mifProjectEntry = getProjectEntry()
    if (mifProject == null || mifProjectEntry == null ||
        mifProject.getPath() == null) {
        throw new IllegalStateException("live QuPath project context is missing")
    }
    final String mifProjectUri = mifProject.getPath().toAbsolutePath().normalize().toUri().normalize().toASCIIString()
    final Object mifIdentityMetadata = mifProject.getMetadata().get(
        "mif_pipeline.project_identity_record_v1"
    )
    if (!(mifIdentityMetadata instanceof String)) {
        throw new IllegalStateException("project identity metadata is missing")
    }
    final String mifProjectIdentitySha256 =
        MifProjectStageBootstrapV1.validateIdentity(mifIdentityMetadata as String)
    final String mifCurrentProjectEntryId = mifProjectEntry.getID().toString()
    final Object mifCurrentImageRole = mifProjectEntry.getMetadataMap().get(
        "mif_pipeline.image_role"
    )
    final List<String> mifCurrentEntrySourceUris = mifProjectEntry.getURIs()
        .collect { URI uri -> MifProjectStageBootstrapV1.mifCanonicalUri(uri) }
        .toSet().toList().sort()
    final Object mifCurrentEntrySourceImageSha256 =
        mifProjectEntry.getMetadataMap().get("mif_pipeline.source_image_sha256")

    final ProcessHandle mifCurrentProcess = ProcessHandle.current()
    final long mifProcessId = mifCurrentProcess.pid()
    final String mifOsName =
        System.getProperty("os.name", "").toLowerCase(Locale.ROOT)
    final Map mifCurrentProcessStartToken
    if (mifOsName.contains("linux")) {
        final String mifProcStat = Files.readString(Paths.get("/proc/self/stat"))
        final int mifCommEnd = mifProcStat.lastIndexOf(")")
        if (mifCommEnd < 0) {
            throw new IllegalStateException("Linux process identity is unavailable")
        }
        final String[] mifProcFields =
            mifProcStat.substring(mifCommEnd + 2).trim().split(/\s+/)
        if (mifProcFields.length <= 19) {
            throw new IllegalStateException("Linux process identity is incomplete")
        }
        final long mifStartTicks = Long.parseLong(mifProcFields[19])
        if (mifStartTicks <= 0L) {
            throw new IllegalStateException("Linux process start ticks are invalid")
        }
        mifCurrentProcessStartToken = [
            provider: "linux-proc-start-ticks/v1",
            process_id: mifProcessId,
            start_ticks: mifStartTicks,
            canonical_token:
                "linux-proc-start-ticks/v1:${mifProcessId}:${mifStartTicks}"
        ]
    } else if (mifOsName.contains("windows")) {
        final Optional<java.time.Instant> mifStartInstant =
            mifCurrentProcess.info().startInstant()
        if (!mifStartInstant.isPresent()) {
            throw new IllegalStateException("process creation time is unavailable")
        }
        final long mifStartEpochMillis = mifStartInstant.get().toEpochMilli()
        mifCurrentProcessStartToken = [
            provider: "windows-process-creation-epoch-millis/v1",
            process_id: mifProcessId,
            start_epoch_millis: mifStartEpochMillis,
            canonical_token:
                "windows-process-creation-epoch-millis/v1:${mifProcessId}:${mifStartEpochMillis}"
        ]
    } else {
        throw new IllegalStateException("process identity platform is unsupported")
    }
    final byte[] mifLaunchNonceBytes = mifProjectStageLaunchNonce.decodeHex()
    if (mifLaunchNonceBytes.length != 32) {
        throw new IllegalStateException("project-stage launch nonce is invalid")
    }
    final String mifCurrentLaunchNonceSha256 =
        MifProjectStageBootstrapV1.mifSha256(mifLaunchNonceBytes)

    final Object mifGuardValue = new GroovyShell(this.class.classLoader)
        .evaluate(mifGuardSource)
    if (!(mifGuardValue instanceof Closure)) {
        throw new IllegalStateException("project-stage guard is not callable")
    }
    final Closure mifGuard = mifGuardValue as Closure
    final Map mifGuardInput = [
        run_plan: mifRunPlan,
        prepared_descriptor: mifPreparedDescriptor,
        process_launch: mifProcessLaunch,
        manifest_bytes: mifManifestBytes,
        guard_bytes: mifGuardBytes,
        target_bytes: mifTargetBytes,
        project_uri: mifProjectUri,
        project_identity_sha256: mifProjectIdentitySha256,
        current_project_entry_id: mifCurrentProjectEntryId,
        current_image_role: mifCurrentImageRole,
        current_entry_source_uris: mifCurrentEntrySourceUris,
        current_entry_source_image_sha256: mifCurrentEntrySourceImageSha256,
        current_target_script_asset_id: mifCurrentTargetScriptAssetId,
        exchange_root: mifExchangeRoot,
        current_process_start_token: mifCurrentProcessStartToken,
        current_launch_nonce_sha256: mifCurrentLaunchNonceSha256
    ]
    final Object mifGuardResult = mifGuard.call(mifGuardInput)
    if (!mifGuardInput.guard_bytes.is(mifGuardBytes)) {
        throw new IllegalStateException("guard input bytes were replaced")
    }
    if (mifGuardResult == ["skip"]) return
    if (!(mifGuardResult instanceof List) || mifGuardResult.size() != 4 ||
        mifGuardResult[0] != "run" ||
        !(mifGuardResult[1] instanceof Path) ||
        !(mifGuardResult[2] instanceof Path) ||
        !(mifGuardResult[3] instanceof Path)) {
        throw new IllegalStateException("project-stage guard result is invalid")
    }
    final List<Path> mifGuardPaths = mifGuardResult.subList(1, 4) as List<Path>
    if (mifGuardPaths.any { Path path ->
        !path.isAbsolute() || path != path.toAbsolutePath().normalize()
    }) {
        throw new IllegalStateException("project-stage guard path is invalid")
    }
    mifAuthenticatedHeadlessProjectStage =
        mifRunPlan.launch_mode == "headless_project_script"
    final Map mifSelectedTarget = (mifRunPlan.targets as List).find {
        it.project_entry_id == mifCurrentProjectEntryId
    } as Map
    final Map mifCompletionIdentity = [
        project_entry_id: mifCurrentProjectEntryId,
        request_envelope_sha256: mifSelectedTarget.exchange.request_envelope_sha256,
        prepared_descriptor_sha256: mifPreparedDescriptor.prepared_descriptor_sha256,
        process_launch_sha256: mifProcessLaunch.process_launch_sha256
    ]
    final Path mifSavedImagePath = mifProjectEntry.getEntryPath().resolve("data.qpdata")
    mifPublishProjectStageResult = { Path destination, Map result ->
        if (destination.toAbsolutePath().normalize() != mifGuardPaths[1])
            throw new IllegalStateException("Stage result path differs from its authenticated target")
        MifProjectStageBootstrapV1.publishResult(
            mifGuardPaths[1], result, mifSavedImagePath, mifCompletionIdentity)
    }
    if (mifAuthenticatedHeadlessProjectStage &&
        System.getenv("MIF_IMAGE_CACHE_BYTES") != null) {
        // The native provider owns the admitted shared decoded-image cache.
        // Suppress QuPath's additional default 25%-of-heap tile cache here.
        qupath.lib.images.servers.ImageServerProvider.setCache(
            com.github.benmanes.caffeine.cache.Caffeine.newBuilder()
                .maximumSize(0L).build().asMap(), java.awt.image.BufferedImage)
    }
    args = [
        mifGuardPaths[0].toString(),
        mifGuardPaths[1].toString(),
        mifGuardPaths[2].toString()
    ] as String[]
}
// MIF_PROJECT_STAGE_BOOTSTRAP_V1_END
