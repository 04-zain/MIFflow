// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE

/** Native resource inspection/import. No pixel reads or hierarchy edits. */
import java.nio.channels.FileChannel
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import qupath.lib.classifiers.pixel.PixelClassifier
import qupath.lib.images.ImageData
import qupath.lib.projects.Project
import qupath.opencv.ml.pixel.PixelClassifiers
import static qupath.lib.scripting.QP.*

final class MifTissueSetupV1 {
    static final int MAX_CLASSIFIER_BYTES = 64 * 1024 * 1024
    private static final Set SELECTION_FIELDS = [
        "classifier_name", "expected_classifier_sha256", "source", "reference_channel_id",
        "channel_index", "channel_name", "permitted_native_resolution_indices"
    ] as Set
    private static final Set REQUEST_FIELDS = SELECTION_FIELDS + [
        "schema_version", "action", "native_pyramid_inventory", "channels"
    ] as Set
    private static final Set LEVEL_FIELDS = [
        "native_resolution_index", "x_downsample", "y_downsample", "x_um_per_px", "y_um_per_px"
    ] as Set
    private static final Set CHANNEL_FIELDS = [
        "channel_id", "name", "component", "fluorophore", "biomarker"
    ] as Set

    private static void require(boolean condition, String message) {
        if (!condition) throw new IllegalArgumentException(message)
    }

    private static boolean integer(Object value) {
        value instanceof Number && !(value instanceof Float) && !(value instanceof Double) &&
            value.longValue() >= 0L && value.longValue() <= Integer.MAX_VALUE
    }

    private static boolean digest(Object value) {
        value instanceof String && value ==~ /[0-9a-f]{64}/
    }

    private static boolean text(Object value) {
        value instanceof String && !value.isEmpty() && value == value.trim()
    }

    private static boolean opaqueId(Object value) {
        value instanceof String && !value.isBlank()
    }

    private static boolean resourceName(Object name) {
        if (!(name instanceof String) || !name.endsWith(".json") || name.length() <= 5) return false
        int first = name.codePointAt(0)
        if (Character.isWhitespace(first) || Character.isSpaceChar(first) || first == 0x85) return false
        if (name.codePoints().anyMatch { int point ->
            Character.getType(point) == Character.CONTROL || "<>:\"/\\|?*".indexOf(point) >= 0
        }) return false
        String stem = name.substring(0, name.length() - 5).split("\\.", -1)[0].replaceFirst(/ +$/, "")
            .toUpperCase(Locale.ROOT)
        return !(stem in ["CON", "PRN", "AUX", "NUL"] || stem ==~ /(?:COM|LPT)[1-9]/)
    }

    private static void safeExistingPath(Path path) {
        require(path.isAbsolute() && path == path.normalize() && !Files.isSymbolicLink(path) &&
            Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS) && path.toRealPath() == path,
            "Classifier path is not a safe regular local file")
    }

    static byte[] readBounded(Path path) {
        safeExistingPath(path)
        ByteArrayOutputStream output = new ByteArrayOutputStream()
        FileChannel channel = FileChannel.open(path, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)
        try {
            ByteBuffer buffer = ByteBuffer.allocate(8192)
            while (true) {
                buffer.clear()
                buffer.limit(Math.min(buffer.capacity(), MAX_CLASSIFIER_BYTES + 1 - output.size()))
                int count = channel.read(buffer)
                if (count < 0) break
                if (count > 0) output.write(buffer.array(), 0, count)
                require(output.size() <= MAX_CLASSIFIER_BYTES, "Classifier exceeds 64 MiB limit")
            }
        } finally { channel.close() }
        safeExistingPath(path)
        require(output.size() > 0, "Classifier document is empty")
        return output.toByteArray()
    }

    static byte[] verifySource(Path path, Map expected) {
        byte[] payload = readBounded(path)
        require(payload.length == expected.byte_count &&
            MifProjectStageBootstrapV1.mifSha256(payload) == expected.sha256,
            "Selected source classifier changed or has the wrong source identity")
        return payload
    }

    static Map validateRequest(Object raw) {
        require(raw instanceof Map && raw.keySet() == REQUEST_FIELDS, "Tissue setup request fields are not closed")
        Map request = raw as Map
        require(request.schema_version == "tissue-detector-setup-request/v1" &&
            request.action in ["request_selection", "observe_project", "import_local"],
            "Unsupported tissue setup request")
        require(request.channels instanceof List && !request.channels.isEmpty(),
            "Canonical channel inventory is missing")
        Set ids = []
        request.channels.each { row ->
            require(row instanceof Map && row.keySet() == CHANNEL_FIELDS &&
                opaqueId(row.channel_id) && ids.add(row.channel_id) &&
                ["name", "component", "fluorophore", "biomarker"].every { row[it] == null || text(row[it]) },
                "Canonical channel inventory is invalid")
        }
        require(request.native_pyramid_inventory instanceof List &&
            !request.native_pyramid_inventory.isEmpty(), "Native pyramid inventory is missing")
        request.native_pyramid_inventory.eachWithIndex { Object level, int index ->
            require(level instanceof Map && level.keySet() == LEVEL_FIELDS &&
                integer(level.native_resolution_index) && level.native_resolution_index == index,
                "Native pyramid inventory is not complete and ordered")
            LEVEL_FIELDS.findAll { it != "native_resolution_index" }.each { String field ->
                require(level[field] instanceof Number && Double.isFinite(level[field].doubleValue()) &&
                    level[field].doubleValue() > 0, "Native pyramid values must be finite and positive")
            }
        }
        if (request.action == "request_selection") {
            require(SELECTION_FIELDS.every { request[it] == null },
                "Initial setup selection cannot carry a detector choice")
            return request
        }
        require(resourceName(request.classifier_name), "Classifier name must be a safe single JSON basename")
        require(opaqueId(request.reference_channel_id) && text(request.channel_name) && integer(request.channel_index),
            "Invalid canonical channel binding")
        require(request.channel_index < request.channels.size() &&
            request.channels[request.channel_index as int].channel_id == request.reference_channel_id &&
            request.channels[request.channel_index as int].name == request.channel_name,
            "Selected canonical channel does not match the ordered channel inventory")
        require(request.expected_classifier_sha256 == null || digest(request.expected_classifier_sha256),
            "Invalid expected classifier hash")
        if (request.action == "observe_project") {
            require(request.source == null, "Project observation cannot name a source")
        } else {
            require(request.expected_classifier_sha256 == null && request.source instanceof Map &&
                request.source.keySet() == ["path", "sha256", "byte_count"] as Set,
                "Import requires exact source identity and no expected saved hash")
            require(text(request.source.path) && digest(request.source.sha256) &&
                integer(request.source.byte_count) && request.source.byte_count > 0 &&
                request.source.byte_count <= MAX_CLASSIFIER_BYTES, "Invalid source classifier identity or size")
            Path path = Path.of(request.source.path as String)
            require(path.isAbsolute() && path == path.normalize(), "Source path must be absolute and normalized")
        }
        def permitted = request.permitted_native_resolution_indices
        require(permitted instanceof List && !permitted.isEmpty() &&
            permitted.every { integer(it) && it < request.native_pyramid_inventory.size() } &&
            permitted == permitted.toSet().toList().sort(), "Permitted native resolution indices are invalid")
        return request
    }

    private static void validateLiveImage(ImageData imageData, Map request) {
        require(imageData != null, "Tissue setup requires the current image")
        def server = imageData.server
        require(request.channels.size() == server.metadata.channels.size(),
            "Canonical channel count differs from the current native image")
        request.channels.eachWithIndex { row, int index ->
            require(row.name == null || row.name == server.metadata.channels[index].name,
                "Canonical channel differs from current native channel")
        }
        double x = server.pixelCalibration.pixelWidthMicrons
        double y = server.pixelCalibration.pixelHeightMicrons
        require(server.pixelCalibration.hasPixelSizeMicrons() &&
            Double.isFinite(x) && Double.isFinite(y) && x > 0 && y > 0,
            "Current native pyramid calibration is missing")
        double[] levels = server.getPreferredDownsamples()
        require(levels != null && levels.length == request.native_pyramid_inventory.size(),
            "Native pyramid inventory changed")
        double previous = 0d
        levels.eachWithIndex { double downsample, int index ->
            require(Double.isFinite(downsample) && downsample > previous &&
                (index != 0 || Math.abs(downsample - 1d) <= 1e-9), "Native pyramid inventory is invalid")
            Map row = request.native_pyramid_inventory[index]
            require(row.x_downsample.doubleValue() == downsample && row.y_downsample.doubleValue() == downsample &&
                row.x_um_per_px.doubleValue() == x * downsample && row.y_um_per_px.doubleValue() == y * downsample,
                "Native pyramid inventory or calibration changed")
            previous = downsample
        }
    }

    private static Map validateClassifier(PixelClassifier classifier, byte[] payload,
                                           ImageData imageData, Map request, Closure support) {
        Map document = MifProjectStageBootstrapV1.parseMap(payload, "classifier")
        List<Map> permittedRows = request.permitted_native_resolution_indices.collect {
            request.native_pyramid_inventory[it as int] as Map
        }
        Map observed = support.call(classifier, document, imageData,
            request.channel_index as int, permittedRows) as Map
        return [reference_channel_id: request.reference_channel_id,
            channel_index: request.channel_index, channel_name: request.channel_name] + observed
    }

    private static Path resourcePath(Project project, String name) {
        require(resourceName(name), "Classifier resource name is unsafe")
        Path root = project.getPath().toAbsolutePath().normalize().parent
        require(root.toRealPath() == root, "Project resource path ancestry is unsafe")
        Path path = root.resolve("classifiers/pixel_classifiers").resolve(name)
        Path cursor = path.parent
        while (cursor != root) {
            require(!Files.isSymbolicLink(cursor) && (!Files.exists(cursor, LinkOption.NOFOLLOW_LINKS) ||
                (Files.isDirectory(cursor, LinkOption.NOFOLLOW_LINKS) && cursor.toRealPath() == cursor)),
                "Project resource path ancestry is unsafe")
            cursor = cursor.parent
        }
        return path
    }

    static Map selectionContext(Project project, ImageData imageData, Object raw) {
        Map request = validateRequest(raw)
        require(request.action == "request_selection", "Expected initial selection request")
        validateLiveImage(imageData, request)
        require(project != null, "Tissue setup requires the bound project")
        def manager = project.getPixelClassifiers()
        List names = manager.getNames().toList().sort()
        require(names.size() <= 256, "More than 256 saved classifier resources")
        List<Map> available = names.collect { name ->
            String filename = name + ".json"
            Path path = resourcePath(project, filename)
            byte[] bytes = readBounded(path)
            [classifier_name: filename, sha256: MifProjectStageBootstrapV1.mifSha256(bytes), byte_count: bytes.length]
        }
        Map context = [schema_version: "tissue-detector-setup-interaction/v1",
            available_classifiers: available, channels: request.channels,
            native_pyramid_inventory: request.native_pyramid_inventory,
            project_path: project.getPath().toAbsolutePath().normalize().toString()]
        String hash = MifProjectStageBootstrapV1.mifSha256(
            canonicalJson(context).getBytes(StandardCharsets.UTF_8))
        return context + [checkpoint_id: "tissue-setup-" + hash.substring(0, 48)]
    }

    private static Object sortedJson(Object value) {
        if (value instanceof Map) {
            Map sorted = new TreeMap()
            value.each { key, item -> sorted[key] = sortedJson(item) }
            return sorted
        }
        if (value instanceof List) return value.collect { sortedJson(it) }
        return value
    }

    private static String canonicalJson(Object value) {
        new GsonBuilder().disableHtmlEscaping().serializeNulls().create().toJson(sortedJson(value))
    }

    static Map execute(Project project, ImageData imageData, Object raw, Closure support) {
        Map request = validateRequest(raw)
        require(request.action != "request_selection", "Expected concrete detector selection")
        validateLiveImage(imageData, request)
        require(project != null, "Tissue setup requires the bound project")
        Path target = resourcePath(project, request.classifier_name as String)
        String resource = (request.classifier_name as String).replaceFirst(/\.json$/, "")
        def manager = project.getPixelClassifiers()
        Map sourceEvidence = null
        if (request.action == "import_local") {
            require(!manager.contains(resource) && !Files.exists(target, LinkOption.NOFOLLOW_LINKS),
                "Classifier resource already exists; import cannot overwrite it")
            Path source = Path.of(request.source.path as String)
            byte[] sourceBytes = verifySource(source, request.source as Map)
            PixelClassifier classifier = PixelClassifiers.readClassifier(source)
            validateClassifier(classifier, sourceBytes, imageData, request, support)
            verifySource(source, request.source as Map)
            resourcePath(project, request.classifier_name as String)
            require(!manager.contains(resource) && !Files.exists(target, LinkOption.NOFOLLOW_LINKS),
                "Classifier resource already exists; import cannot overwrite it")
            manager.put(resource, classifier)
            project.syncChanges()
            sourceEvidence = [sha256: request.source.sha256, byte_count: request.source.byte_count]
        }
        byte[] savedBytes = readBounded(target)
        String savedHash = MifProjectStageBootstrapV1.mifSha256(savedBytes)
        require(request.expected_classifier_sha256 == null || request.expected_classifier_sha256 == savedHash,
            "Configured classifier hash differs from the saved resource")
        PixelClassifier reopened = manager.get(resource)
        Map observed = validateClassifier(reopened, savedBytes, imageData, request, support)
        require(Arrays.equals(savedBytes, readBounded(target)), "Saved classifier changed during native observation")
        return [schema_version: "tissue-detector-setup-observation/v1",
            classifier_name: request.classifier_name, classifier_sha256: savedHash,
            classifier_byte_count: savedBytes.length, source: sourceEvidence] + observed
    }
}

final String SCRIPT_ASSET_PATH = "tissue_detection/00_setup_tissue_detector.groovy"
final String STAGE = "tissue_setup"
if (args.size() != 3)
    throw new IllegalArgumentException("Expected request, stage-result and generation-manifest paths")
Path requestPath = Paths.get(args[0] as String)
Path resultPath = Paths.get(args[1] as String)
def gson = new GsonBuilder().disableHtmlEscaping().serializeNulls()
    .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
Map envelope = gson.fromJson(Files.readString(requestPath), Map.class) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != STAGE)
    throw new IllegalArgumentException("Invalid tissue-setup request envelope")
Map request = envelope.request as Map
def boundProject = getProject()
def boundImageData = getCurrentImageData()
def boundEntry = getProjectEntry()
if (boundProject == null || boundImageData == null || boundEntry == null)
    throw new IllegalStateException("Stage execution requires an open saved project image")
Path boundProjectPath = boundProject.getPath().toAbsolutePath().normalize()
String requestedProjectFile = request.project_file as String
String requestedEntryId = request.project_entry_id as String
String requestedCanonicalSha256 = request.canonical_image_sha256 as String
if (request.exchange_version != "roi-stage-request/v2" ||
        !requestedProjectFile || Paths.get(requestedProjectFile).toAbsolutePath().normalize() != boundProjectPath ||
        !requestedEntryId || boundEntry.getID().toString() != requestedEntryId ||
        !(requestedCanonicalSha256 ==~ /[0-9a-f]{64}/) ||
        !((request.script_bundle_sha256 as String) ==~ /[0-9a-f]{64}/))
    throw new IllegalStateException("Exact project entry identity and script bundle are required")
String requestedScriptBundleSha256 = request.script_bundle_sha256 as String
Path projectRoot = boundProjectPath.parent
Path scriptsRoot = projectRoot.resolve("scripts").normalize()
Path requestedBundleRoot = scriptsRoot.resolve("bundle-${requestedScriptBundleSha256}").normalize()
Path bundleManifestPath = requestedBundleRoot.resolve("qupath-script-bundle.v1.json").normalize()
Path dataRoot = projectRoot.resolve("data").normalize()
Path scriptBundleEvidencePath = dataRoot
    .resolve("qupath-script-bundle.evidence.json").normalize()
if (requestedBundleRoot.parent != scriptsRoot ||
        bundleManifestPath.parent != requestedBundleRoot ||
        dataRoot.parent != projectRoot || scriptBundleEvidencePath.parent != dataRoot ||
        projectRoot.toRealPath() != projectRoot ||
        scriptsRoot.toRealPath() != scriptsRoot || requestedBundleRoot.toRealPath() != requestedBundleRoot ||
        bundleManifestPath.toRealPath() != bundleManifestPath ||
        dataRoot.toRealPath() != dataRoot ||
        scriptBundleEvidencePath.toRealPath() != scriptBundleEvidencePath ||
        Files.isSymbolicLink(scriptsRoot) || !Files.isDirectory(scriptsRoot) ||
        Files.isSymbolicLink(requestedBundleRoot) || !Files.isDirectory(requestedBundleRoot) ||
        Files.isSymbolicLink(bundleManifestPath) || !Files.isRegularFile(bundleManifestPath) ||
        Files.isSymbolicLink(dataRoot) || !Files.isDirectory(dataRoot) ||
        Files.isSymbolicLink(scriptBundleEvidencePath) || !Files.isRegularFile(scriptBundleEvidencePath))
    throw new IllegalStateException("Immutable QuPath script bundle path ancestry is invalid")
byte[] authenticatedManifestBytes = Files.readAllBytes(bundleManifestPath)
String authenticatedManifestSha256 = MessageDigest.getInstance("SHA-256")
    .digest(authenticatedManifestBytes).encodeHex().toString()
if (authenticatedManifestSha256 != requestedScriptBundleSha256)
    throw new IllegalStateException("Immutable QuPath script bundle manifest does not match the request")
Map authenticatedManifest = gson.fromJson(
    new String(authenticatedManifestBytes, StandardCharsets.UTF_8), Map.class
) as Map
if (authenticatedManifest == null ||
        authenticatedManifest.keySet() != ["schema_version", "assets"] as Set ||
        authenticatedManifest.schema_version != "qupath-script-bundle/v1" ||
        !(authenticatedManifest.assets instanceof List) ||
        (authenticatedManifest.assets as List).isEmpty())
    throw new IllegalStateException("Authenticated QuPath script bundle manifest is invalid")
final String verifierAssetPath = "general/00_verify_stage_script_bundle.groovy"
List verifierEvidence = (authenticatedManifest.assets as List).findAll { raw ->
    raw instanceof Map && raw.keySet() == ["path", "sha256"] as Set && raw.path == verifierAssetPath
}
Path verifierPath = requestedBundleRoot.resolve(verifierAssetPath).normalize()
if (verifierEvidence.size() != 1 ||
        !((verifierEvidence.first().sha256 as String) ==~ /[0-9a-f]{64}/) ||
        verifierPath.toRealPath() != verifierPath ||
        Files.isSymbolicLink(verifierPath) || !Files.isRegularFile(verifierPath) ||
        MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(verifierPath))
            .encodeHex().toString() != verifierEvidence.first().sha256)
    throw new IllegalStateException("Immutable QuPath script bundle verifier is invalid")
Closure verifyCurrentScriptBundle = evaluate(verifierPath.toFile()) as Closure
verifyCurrentScriptBundle(request, boundProjectPath, SCRIPT_ASSET_PATH, gson)
Closure tissueDetectorSupport = evaluate(requestedBundleRoot.resolve(
    "tissue_detection/tissue_detector_support.groovy").toFile()) as Closure
String requestedCanonicalUri = request.canonical_image_uri as String
URI canonicalUri
try {
    canonicalUri = new URI(requestedCanonicalUri).normalize()
} catch (Exception error) {
    throw new IllegalStateException("Canonical image URI is invalid", error)
}
List<String> liveCanonicalUris = boundEntry.getURIs()
    .collect { URI uri -> MifProjectStageBootstrapV1.mifCanonicalUri(uri) }
    .toSet().toList().sort()
Object liveRole = boundEntry.getMetadataMap().get("mif_pipeline.image_role")
Object liveDigest = boundEntry.getMetadataMap().get(
    "mif_pipeline.source_image_sha256"
)
if (canonicalUri.scheme != "file" || canonicalUri.query != null ||
        canonicalUri.fragment != null ||
        canonicalUri.toASCIIString() != requestedCanonicalUri ||
        liveRole != "primary_mif" || liveDigest != requestedCanonicalSha256 ||
        liveCanonicalUris != [requestedCanonicalUri])
    throw new IllegalStateException(
        "Request image authority does not match the open canonical mIF entry"
    )

Map result
if (request.tissue_setup?.action == "request_selection") {
    Map context = MifTissueSetupV1.selectionContext(boundProject, boundImageData, request.tissue_setup)
    result = [status: "needs_interaction", stage: STAGE, slide_id: envelope.slide_id,
        generation_id: null, interaction_reason: "tissue_detector_setup_required",
        affected_object_ids: [], message: "Choose a tissue detector and its settings.",
        allowed_actions: ["select_tissue_detector", "import_tissue_detector", "refresh_tissue_detectors"],
        resume_context: context, checkpoint_id: context.checkpoint_id, outputs: null, error: null]
} else {
    Map observation = MifTissueSetupV1.execute(boundProject, boundImageData, request.tissue_setup,
        tissueDetectorSupport)
    if (mifAuthenticatedHeadlessProjectStage && boundImageData.isChanged()) {
        boundEntry.saveImageData(boundImageData)
        boundProject.syncChanges()
    }
    Path observedPath = boundProjectPath.parent.resolve("classifiers/pixel_classifiers")
        .resolve(observation.classifier_name as String)
    byte[] observedBytes = MifTissueSetupV1.readBounded(observedPath)
    if (observedBytes.length != observation.classifier_byte_count ||
            MifProjectStageBootstrapV1.mifSha256(observedBytes) != observation.classifier_sha256)
        throw new IllegalStateException("Saved classifier changed before result publication")
    result = [status: "completed", stage: STAGE, slide_id: envelope.slide_id,
        generation_id: null, interaction_reason: null, affected_object_ids: [],
        message: null, allowed_actions: [], resume_context: null, checkpoint_id: null,
        outputs: [tissue_setup_observation: observation], error: null]
}
Map resultEnvelope = [schema_version: "roi-stage-result/v1",
    invocation_id: envelope.invocation_id, run_id: envelope.run_id,
    stage: STAGE, slide_id: envelope.slide_id, result: result, generation_manifest: null]
mifPublishProjectStageResult(resultPath, resultEnvelope)
