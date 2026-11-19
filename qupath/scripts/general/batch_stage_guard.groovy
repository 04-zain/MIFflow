import java.net.URI
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.ToNumberPolicy

/**
 * Standalone, fail-closed validation for one already-selected project entry.
 * Task 6 supplies the private fixture and invokes this before scientific code.
 */
final class BatchStageGuard {
    private static final Gson CANONICAL_GSON = new GsonBuilder()
        .disableHtmlEscaping().serializeNulls()
        .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
    private static final Set<String> RUN_PLAN_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256",
        "request_sha256", "run_plan_id", "batch_id", "stage_id",
        "stage_version", "launch_mode", "project_uri",
        "project_identity_sha256", "project_authority_head_sha256",
        "project_catalogue_sha256", "script_bundle_sha256",
        "script_bundle_manifest_ref", "script_bundle_manifest_sha256",
        "script_bundle_manifest_byte_count", "target_script_asset_id",
        "target_script_ref", "target_script_sha256",
        "target_script_byte_count", "guard_script_asset_id",
        "guard_script_ref", "guard_script_sha256", "guard_script_byte_count",
        "targets", "target_authorization_set_sha256",
        "request_member_set_sha256", "eligible_entry_set_sha256",
        "excluded_project_entry_ids", "excluded_entry_set_sha256",
        "shared_artifact_ref_sha256s", "shared_artifact_set_sha256",
        "plan_sha256"
    ] as Set

    private static final Set<String> PREPARED_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256",
        "runtime_composition_binding_sha256",
        "workspace_storage_scope_sha256", "project_uri",
        "project_identity_sha256", "project_authority_head_sha256",
        "active_mutation_run_sha256", "acquisition_record_sha256",
        "acquisition_session_challenge_sha256", "artifact_inventory_sha256",
        "mutation_request_sha256", "mutation_authorization_sha256",
        "run_plan_ref", "run_plan_sha256", "run_plan_byte_count",
        "target_authorization_set_sha256", "request_member_set_sha256",
        "script_bundle_sha256", "script_bundle_manifest_ref",
        "script_bundle_manifest_sha256", "script_bundle_manifest_byte_count",
        "target_script_asset_id", "target_script_ref",
        "target_script_sha256", "target_script_byte_count",
        "guard_script_asset_id", "guard_script_ref", "guard_script_sha256",
        "guard_script_byte_count", "fencing_token_sha256",
        "launch_nonce_sha256", "launch_request_identity_sha256",
        "process_launch_ref", "stdout_log_ref", "stderr_log_ref",
        "prepared_descriptor_sha256"
    ] as Set

    private static final Set<String> LAUNCH_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "project_identity_sha256", "active_mutation_run_sha256",
        "acquisition_record_sha256",
        "acquisition_session_challenge_sha256",
        "artifact_inventory_sha256",
        "mutation_request_sha256", "mutation_authorization_sha256",
        "fencing_token_sha256", "run_plan_sha256",
        "prepared_descriptor_sha256", "application_request_sha256",
        "launch_nonce_sha256",
        "process_launch_ref", "launch_request_identity_sha256",
        "attempt_sha256", "process_start_token", "launch_evidence",
        "command_sha256", "environment_sha256", "executable_version",
        "process_launch_sha256"
    ] as Set

    private static final Set<String> MANIFEST_FIELDS = [
        "schema_version", "assets"
    ] as Set
    private static final Set<String> MANIFEST_ASSET_FIELDS = [
        "path", "sha256"
    ] as Set
    private static final Set<String> REF_FIELDS = [
        "schema_name", "schema_version",
        "authority_layout_sha256", "family", "key_sha256", "suffix",
        "media_type"
    ] as Set
    private static final Set<String> LINUX_START_TOKEN_FIELDS = [
        "provider", "process_id", "start_ticks", "canonical_token"
    ] as Set
    private static final Set<String> WINDOWS_START_TOKEN_FIELDS = [
        "provider", "process_id", "start_epoch_millis", "canonical_token"
    ] as Set
    private static final Set<String> REQUEST_ENVELOPE_FIELDS = [
        "schema_version", "invocation_id", "run_id", "stage", "slide_id", "request"
    ] as Set
    private static final Set<String> RUNTIME_REQUEST_FIELDS = [
        "project_file", "project_entry_id", "canonical_image_uri",
        "canonical_image_sha256", "script_bundle_sha256", "script_asset_path",
        "script_asset_sha256", "exchange_version"
    ] as Set

    private static void requireClosed(Map value, Set<String> fields, String label) {
        if (value == null || value.keySet() != fields) {
            throw new IllegalStateException("${label} is not the closed current shape")
        }
    }

    private static void requireSchema(
        Map value, Set<String> fields, String name, int version, String label
    ) {
        requireClosed(value, fields, label)
        if (value.schema_name != name || value.schema_version != version) {
            throw new IllegalStateException("${label} schema is unsupported")
        }
    }

    private static String sha256(byte[] payload) {
        return MessageDigest.getInstance("SHA-256").digest(payload).encodeHex().toString()
    }

    private static String strictUtf8(byte[] payload, String label) {
        try {
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(payload)).toString()
        } catch (Exception error) {
            throw new IllegalStateException("${label} is not strict UTF-8", error)
        }
    }

    private static Object canonicalValue(Object value) {
        if (value instanceof Map) {
            Map sorted = new TreeMap()
            (value as Map).each { key, item ->
                sorted[key as String] = canonicalValue(item)
            }
            return sorted
        }
        if (value instanceof List) {
            return (value as List).collect { canonicalValue(it) }
        }
        return value
    }

    private static void requireRecordDigest(
        Map value, String digestField, String label
    ) {
        Map unsigned = new TreeMap(value)
        Object claimed = unsigned.remove(digestField)
        byte[] payload = CANONICAL_GSON.toJson(canonicalValue(unsigned))
            .getBytes(StandardCharsets.UTF_8)
        if (!(claimed instanceof String) || claimed != sha256(payload)) {
            throw new IllegalStateException("${label} digest is invalid")
        }
    }

    private static Map parseJsonMap(byte[] payload, String label) {
        Object value
        try {
            value = CANONICAL_GSON.fromJson(strictUtf8(payload, label), Object)
        } catch (RuntimeException error) {
            throw new IllegalStateException("${label} JSON is invalid", error)
        }
        // The request bytes are checked against the submitted member below.
        // Python and Gson can write the same finite number differently.
        if (!(value instanceof Map)) {
            throw new IllegalStateException("${label} must contain a JSON object")
        }
        return value as Map
    }

    private static void requireRequestEnvelope(
        byte[] payload, Map target, Map plan, Map input
    ) {
        Map envelope = parseJsonMap(payload, "request member")
        requireClosed(envelope, REQUEST_ENVELOPE_FIELDS, "request envelope")
        if (envelope.schema_version != "roi-stage-request/v2" ||
            envelope.invocation_id != target.attempt_id ||
            envelope.run_id != target.pipeline_run_id ||
            envelope.stage != target.wire_stage_id ||
            envelope.slide_id != target.slide_id ||
            !(envelope.request instanceof Map)) {
            throw new IllegalStateException("request envelope identity differs")
        }
        Map request = envelope.request as Map
        if (!request.keySet().containsAll(RUNTIME_REQUEST_FIELDS) ||
            request.exchange_version != "roi-stage-request/v2" ||
            request.project_entry_id != target.project_entry_id ||
            request.project_entry_id != input.current_project_entry_id ||
            request.script_bundle_sha256 != plan.script_bundle_sha256 ||
            request.script_asset_path != plan.target_script_asset_id.substring(
                "qupath/scripts/".length()
            ) ||
            request.script_asset_sha256 != plan.target_script_sha256 ||
            !(request.canonical_image_sha256 instanceof String) ||
            !(request.canonical_image_sha256 ==~ /[0-9a-f]{64}/) ||
            !(request.project_file instanceof String) ||
            !(request.canonical_image_uri instanceof String)) {
            throw new IllegalStateException("request runtime binding differs")
        }
        Path projectPath
        try {
            projectPath = Path.of(request.project_file as String)
        } catch (Exception error) {
            throw new IllegalStateException("request project path is invalid", error)
        }
        if (!projectPath.isAbsolute() ||
            projectPath != projectPath.toAbsolutePath().normalize() ||
            projectPath.toUri().normalize().toASCIIString() != plan.project_uri) {
            throw new IllegalStateException("request project path is invalid")
        }
        URI canonicalUri
        try {
            canonicalUri = new URI(request.canonical_image_uri as String).normalize()
        } catch (Exception error) {
            throw new IllegalStateException("request canonical image URI is invalid", error)
        }
        String requestedCanonicalUri = request.canonical_image_uri as String
        if (canonicalUri.scheme != "file" || canonicalUri.query != null ||
            canonicalUri.fragment != null ||
            canonicalUri.toASCIIString() != requestedCanonicalUri ||
            input.current_image_role != "primary_mif" ||
            input.current_entry_source_image_sha256 != request.canonical_image_sha256 ||
            input.current_entry_source_uris != [requestedCanonicalUri]) {
            throw new IllegalStateException("current entry image authority differs")
        }
    }

    private static void requireStartToken(Map value, String label) {
        if (!(value.process_id instanceof Number)) {
            throw new IllegalStateException("${label} is invalid")
        }
        long processId = (value.process_id as Number).longValue()
        if (value.provider == "linux-proc-start-ticks/v1") {
            requireClosed(value, LINUX_START_TOKEN_FIELDS, label)
            if (!(value.start_ticks instanceof Number))
                throw new IllegalStateException("${label} is invalid")
            long startTicks = (value.start_ticks as Number).longValue()
            if (processId <= 0L || startTicks <= 0L ||
                value.process_id != processId || value.start_ticks != startTicks ||
                value.canonical_token !=
                    "linux-proc-start-ticks/v1:${processId}:${startTicks}") {
                throw new IllegalStateException("${label} is invalid")
            }
            return
        }
        if (value.provider == "windows-process-creation-epoch-millis/v1") {
            requireClosed(value, WINDOWS_START_TOKEN_FIELDS, label)
            if (!(value.start_epoch_millis instanceof Number))
                throw new IllegalStateException("${label} is invalid")
            long startEpochMillis = (value.start_epoch_millis as Number).longValue()
            if (processId <= 0L || startEpochMillis <= 0L ||
                value.process_id != processId ||
                value.start_epoch_millis != startEpochMillis ||
                value.canonical_token !=
                    "windows-process-creation-epoch-millis/v1:${processId}:${startEpochMillis}") {
                throw new IllegalStateException("${label} is invalid")
            }
            return
        }
        if (processId <= 0L) {
            throw new IllegalStateException("${label} is invalid")
        }
        throw new IllegalStateException("${label} is invalid")
    }

    private static Path memberPath(Path exchangeRoot, Map ref, String layout) {
        requireClosed(ref, REF_FIELDS, "exchange ref")
        if (ref.schema_name != "mif-authority-member-ref" ||
            ref.schema_version != 2 ||
            ref.authority_layout_sha256 != layout ||
            ref.family != "runtime_exchange" ||
            !(ref.key_sha256 ==~ /[0-9a-f]{64}/) ||
            ref.suffix != "json" ||
            ref.media_type != "application/json") {
            throw new IllegalStateException("exchange ref is outside private authority")
        }
        String physicalName = "${ref.family}-${ref.key_sha256}.${ref.suffix}"
        Path resolved = exchangeRoot.resolve(physicalName).normalize()
        if (resolved.parent != exchangeRoot.normalize()) {
            throw new IllegalStateException("exchange member escaped private authority")
        }
        return resolved
    }

    static List<Object> validate(Map input) {
        requireClosed(input, [
            "run_plan", "prepared_descriptor", "process_launch",
            "manifest_bytes", "guard_bytes", "target_bytes", "project_uri",
            "project_identity_sha256",
            "current_project_entry_id", "current_image_role",
            "current_entry_source_uris", "current_entry_source_image_sha256",
            "current_target_script_asset_id", "exchange_root",
            "current_process_start_token", "current_launch_nonce_sha256"
        ] as Set, "guard fixture")
        if (!(input.exchange_root instanceof Path)) {
            throw new IllegalStateException("private exchange root is invalid")
        }
        Path exchangeRoot = input.exchange_root as Path
        Map plan = input.run_plan as Map
        Map prepared = input.prepared_descriptor as Map
        Map launch = input.process_launch as Map
        requireSchema(plan, RUN_PLAN_FIELDS, "mif-project-stage-run-plan", 2, "run plan")
        requireSchema(
            prepared, PREPARED_FIELDS, "mif-project-stage-prepared-run", 1,
            "prepared descriptor"
        )
        requireSchema(
            launch, LAUNCH_FIELDS, "mif-project-stage-process-launch", 1,
            "process launch"
        )
        requireRecordDigest(plan, "plan_sha256", "run plan")
        requireRecordDigest(
            prepared, "prepared_descriptor_sha256", "prepared descriptor"
        )
        requireRecordDigest(launch, "process_launch_sha256", "process launch")
        if (!(input.manifest_bytes instanceof byte[]) ||
            !(input.guard_bytes instanceof byte[]) ||
            !(input.target_bytes instanceof byte[])) {
            throw new IllegalStateException("script bundle bytes are invalid")
        }
        byte[] manifestBytes = input.manifest_bytes as byte[]
        byte[] guardBytes = input.guard_bytes as byte[]
        byte[] targetBytes = input.target_bytes as byte[]
        Map manifest
        try {
            manifest = CANONICAL_GSON.fromJson(
                new String(manifestBytes, StandardCharsets.UTF_8), Map
            ) as Map
        } catch (RuntimeException error) {
            throw new IllegalStateException("bundle manifest JSON is invalid", error)
        }
        requireClosed(manifest, MANIFEST_FIELDS, "bundle manifest")
        if (manifest.schema_version != "qupath-script-bundle/v1" ||
            !(manifest.assets instanceof List) ||
            manifest.assets.any {
                !(it instanceof Map) || it.keySet() != MANIFEST_ASSET_FIELDS
            } ||
            !java.util.Arrays.equals(
                CANONICAL_GSON.toJson(canonicalValue(manifest))
                    .getBytes(StandardCharsets.UTF_8),
                manifestBytes
            )) {
            throw new IllegalStateException("bundle manifest is invalid")
        }
        requireStartToken(launch.process_start_token as Map, "process_start_token")
        requireStartToken(
            input.current_process_start_token as Map,
            "current_process_start_token"
        )
        if (!(input.current_launch_nonce_sha256 instanceof String) ||
            !(input.current_launch_nonce_sha256 ==~ /[0-9a-f]{64}/)) {
            throw new IllegalStateException("current launch nonce digest is invalid")
        }

        String layout = plan.authority_layout_sha256 as String
        memberPath(exchangeRoot, prepared.process_launch_ref as Map, layout)
        String manifestSha256 = sha256(manifestBytes)
        String targetSha256 = sha256(targetBytes)
        String guardSha256 = sha256(guardBytes)
        String assetPrefix = "qupath/scripts/"
        if (!(plan.target_script_asset_id instanceof String) ||
            !(plan.guard_script_asset_id instanceof String) ||
            !(plan.target_script_asset_id as String).startsWith(assetPrefix) ||
            plan.guard_script_asset_id !=
                "qupath/scripts/general/batch_stage_guard.groovy" ||
            plan.target_script_asset_id == plan.guard_script_asset_id) {
            throw new IllegalStateException("registered script identity is invalid")
        }
        String targetPath = (plan.target_script_asset_id as String)
            .substring(assetPrefix.length())
        String guardPath = (plan.guard_script_asset_id as String)
            .substring(assetPrefix.length())
        List targetRows = (manifest.assets as List).findAll {
            (it as Map).path == targetPath
        }
        List guardRows = (manifest.assets as List).findAll {
            (it as Map).path == guardPath
        }
        if (targetRows.size() != 1 || guardRows.size() != 1 ||
            targetRows[0].is(guardRows[0]) ||
            (targetRows[0] as Map).sha256 != targetSha256 ||
            (guardRows[0] as Map).sha256 != guardSha256) {
            throw new IllegalStateException("script bundle member differs")
        }
        if (prepared.authority_layout_sha256 != layout ||
            launch.authority_layout_sha256 != layout ||
            prepared.workspace_storage_scope_sha256 !=
                launch.workspace_storage_scope_sha256 ||
            prepared.run_plan_sha256 != plan.plan_sha256 ||
            launch.run_plan_sha256 != plan.plan_sha256 ||
            launch.prepared_descriptor_sha256 != prepared.prepared_descriptor_sha256 ||
            launch.application_request_sha256 != prepared.prepared_descriptor_sha256 ||
            prepared.active_mutation_run_sha256 != launch.active_mutation_run_sha256 ||
            prepared.acquisition_record_sha256 != launch.acquisition_record_sha256 ||
            prepared.acquisition_session_challenge_sha256 !=
                launch.acquisition_session_challenge_sha256 ||
            prepared.artifact_inventory_sha256 != launch.artifact_inventory_sha256 ||
            prepared.mutation_request_sha256 != launch.mutation_request_sha256 ||
            prepared.mutation_authorization_sha256 !=
                launch.mutation_authorization_sha256 ||
            prepared.fencing_token_sha256 != launch.fencing_token_sha256 ||
            prepared.launch_nonce_sha256 != launch.launch_nonce_sha256 ||
            input.current_process_start_token != launch.process_start_token ||
            input.current_launch_nonce_sha256 != prepared.launch_nonce_sha256 ||
            input.current_launch_nonce_sha256 != launch.launch_nonce_sha256 ||
            prepared.launch_request_identity_sha256 !=
                launch.launch_request_identity_sha256 ||
            prepared.process_launch_ref != launch.process_launch_ref ||
            prepared.project_uri != plan.project_uri ||
            input.project_uri != plan.project_uri ||
            prepared.project_identity_sha256 != plan.project_identity_sha256 ||
            launch.project_identity_sha256 != plan.project_identity_sha256 ||
            input.project_identity_sha256 != plan.project_identity_sha256 ||
            prepared.target_script_asset_id != plan.target_script_asset_id ||
            input.current_target_script_asset_id != plan.target_script_asset_id ||
            prepared.guard_script_asset_id != plan.guard_script_asset_id ||
            prepared.script_bundle_sha256 != plan.script_bundle_sha256 ||
            prepared.script_bundle_manifest_ref != plan.script_bundle_manifest_ref ||
            prepared.script_bundle_manifest_sha256 !=
                plan.script_bundle_manifest_sha256 ||
            prepared.script_bundle_manifest_byte_count !=
                plan.script_bundle_manifest_byte_count ||
            prepared.target_script_ref != plan.target_script_ref ||
            prepared.target_script_sha256 != plan.target_script_sha256 ||
            prepared.target_script_byte_count != plan.target_script_byte_count ||
            prepared.guard_script_ref != plan.guard_script_ref ||
            prepared.guard_script_sha256 != plan.guard_script_sha256 ||
            prepared.guard_script_byte_count != plan.guard_script_byte_count ||
            plan.script_bundle_sha256 != manifestSha256 ||
            plan.script_bundle_manifest_sha256 != manifestSha256 ||
            plan.script_bundle_manifest_byte_count != manifestBytes.length ||
            plan.target_script_sha256 != targetSha256 ||
            plan.target_script_byte_count != targetBytes.length ||
            plan.guard_script_sha256 != guardSha256 ||
            plan.guard_script_byte_count != guardBytes.length) {
            throw new IllegalStateException("project-stage authority identity differs")
        }

        List matches = (plan.targets as List).findAll {
            it.project_entry_id == input.current_project_entry_id
        }
        if (matches.isEmpty()) {
            return ["skip"]
        }
        if (matches.size() != 1 ||
            matches[0].image_role != "primary_mif" ||
            !(input.current_image_role instanceof String) ||
            input.current_image_role != "primary_mif") {
            throw new IllegalStateException("current project entry is ambiguous")
        }
        Map exchange = matches[0].exchange as Map
        Path requestPath = memberPath(exchangeRoot, exchange.request_ref as Map, layout)
        Path resultPath = memberPath(exchangeRoot, exchange.result_ref as Map, layout)
        Path generationPath = memberPath(
            exchangeRoot, exchange.generation_manifest_ref as Map, layout
        )
        Object plannedByteCount = exchange.request_envelope_byte_count
        if (!(plannedByteCount instanceof Number) ||
            plannedByteCount != (plannedByteCount as Number).longValue() ||
            (plannedByteCount as Number).longValue() <= 0L ||
            (plannedByteCount as Number).longValue() > 16L * 1024L * 1024L ||
            !(exchange.request_envelope_sha256 instanceof String) ||
            !(exchange.request_envelope_sha256 ==~ /[0-9a-f]{64}/) ||
            Files.isSymbolicLink(requestPath) ||
            !Files.isRegularFile(requestPath, LinkOption.NOFOLLOW_LINKS)) {
            throw new IllegalStateException("request member is invalid")
        }
        long expectedByteCount = (plannedByteCount as Number).longValue()
        long observedByteCount = Files.size(requestPath)
        byte[] requestBytes = Files.readAllBytes(requestPath)
        if (observedByteCount != expectedByteCount ||
            requestBytes.length != expectedByteCount ||
            sha256(requestBytes) != exchange.request_envelope_sha256) {
            throw new IllegalStateException("request member bytes differ from plan")
        }
        requireRequestEnvelope(requestBytes, matches[0] as Map, plan, input)
        return ["run", requestPath, resultPath, generationPath]
    }
}

return { Map input -> BatchStageGuard.validate(input) }
