// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Compute requested cell measurements once and inventory the actual QuPath keys. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.nio.file.attribute.BasicFileAttributes
import java.nio.channels.Channels
import java.nio.channels.SeekableByteChannel
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.UUID
import qupath.lib.analysis.features.ObjectMeasurements
import qupath.lib.io.GsonTools
import org.locationtech.jts.io.WKBWriter
import org.mif.roi.RoiInventory
import java.util.function.Consumer
import org.mif.cells.NativeCellScope
import org.mif.cells.PopulationMembership
import org.mif.cells.MeasurementRows
import java.util.function.Function
import org.locationtech.jts.io.ByteOrderValues

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "cell_analysis/05_prepare_phenotyping_measurements.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
def gson = GsonTools.getInstance()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
def rowGson = GsonTools.getInstance().newBuilder().disableHtmlEscaping().serializeNulls().create()
Map envelope = gson.fromJson(Files.readString(Paths.get(args[0] as String)), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "measurements")
    throw new IllegalArgumentException("Invalid measurements request envelope")
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
Map config = request.resolved_config as Map
if (config == null) throw new IllegalArgumentException("Authenticated resolved_config is required")
String slideId = envelope.slide_id as String
String runId = envelope.run_id as String
String sectionId = request.section_id as String
String generationId = request.generation_id as String
if (!sectionId || !generationId) throw new IllegalArgumentException("Exact section and generation are required")
def md = { object -> object.getMetadata() }
List sections = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "tissue_detection" && m.get("generation_status") == "accepted" &&
        m.get("slide_id") == slideId && m.get("section_id") == sectionId &&
        m.get("generation_id") == generationId
}
if (sections.size() != 1) throw new IllegalStateException("Exactly one accepted current TissueSection is required")
// The segmentation owner binds one canonical population per accepted section.
// Automatic measurements remain separate numeric components of that population.
def cellProvider = NativeCellScope.open(boundImageData, sectionId).get()
String populationId = cellProvider.getPopulation()
if (!populationId || request.segmentation_population_id != populationId)
    throw new IllegalStateException("Measurements target a stale segmentation population")
Map selectedDatasets = gson.fromJson(boundImageData.getProperty("mif.cell_datasets").toString(), Map.class)
Path cellDatasetPath = Paths.get(selectedDatasets[sectionId] as String).toAbsolutePath().normalize()
if (request.segmentation_cell_dataset_sha256 != cellProvider.getDescriptionSha256())
    throw new IllegalStateException("Measurements require the accepted segmentation dataset; restore its selected base population before retrying")
if(request.cell_correction!=null) {
    Map selected=request.cell_correction as Map
    if(selected.keySet()!=["artifact_ref_sha256","cell_edits","geometry_revision"] as Set ||
            !(selected.artifact_ref_sha256==~ /[0-9a-f]{64}/) || !(selected.cell_edits instanceof Map))
        throw new IllegalArgumentException("Measurements require their exact accepted correction checkpoint")
    // The source state is authenticated by the project owner. Its existing
    // failure-recovery draft may add explicit deletions to this checkpoint.
    cellProvider.selectEdits(boundImageData,Paths.get(selected.cell_edits.path as String),
        selected.cell_edits.sha256 as String,true)
    if(cellProvider.geometryRevision < (selected.geometry_revision as Number).longValue())
        throw new IllegalStateException("The selected correction revision changed")
}
Set<String> domainKinds = ["selected_roi", "whole_tissue_section"] as Set<String>
MeasurementRows measurementRows = null
boolean stageOwnedMeasurementMutation = false

// MIF_MEASUREMENT_SETTINGS_JOIN_BEGIN
Closure validateMeasurementSettings = { Map settingsRequest ->
    Closure canonical = { Object value ->
        value instanceof String && value.length() > 0 && value == value.trim()
    }
    Closure nullableCanonical = { Object value -> value == null || canonical(value) }
    Set channelFields = ["channel_id", "name", "component", "fluorophore", "biomarker"] as Set
    Object rawChannels = settingsRequest.canonical_channels
    if (!(rawChannels instanceof List) || rawChannels.isEmpty() || rawChannels.size() > 1024)
        throw new IllegalStateException("Exact processed channels are required")
    Map channelsById = [:]
    rawChannels.each { raw ->
        if (!(raw instanceof Map) || raw.keySet() != channelFields ||
                !canonical(raw.channel_id) || !canonical(raw.name) ||
                !nullableCanonical(raw.component) || !nullableCanonical(raw.fluorophore) ||
                !nullableCanonical(raw.biomarker) || channelsById.containsKey(raw.channel_id))
            throw new IllegalStateException("Processed channels require unique closed canonical rows")
        channelsById[raw.channel_id] = raw
    }
    if (!canonical(settingsRequest.segmentation_model_version))
        throw new IllegalStateException("Exact segmentation model version is required")
    Object rawRequests = settingsRequest.feature_requests
    if (!(rawRequests instanceof List) || rawRequests.isEmpty() || rawRequests.size() > 1024)
        throw new IllegalStateException("Explicit feature requests are required")
    Set featureFields = ["feature_id", "measurement_name", "kind", "channel_id",
                         "biomarker", "compartment", "units", "opt_in"] as Set
    Set channelKinds = ["expression", "dapi", "autofluorescence"] as Set
    Set otherKinds = ["morphology", "nuclear_morphology", "contextual"] as Set
    Set compartments = ["nucleus", "cytoplasm", "membrane", "cell"] as Set
    Set statistics = ["Mean", "Median", "Min", "Max", "Std.Dev.", "Variance"] as Set
    Set featureIds = [] as Set
    Set measurementNames = [] as Set
    int expressionCount = 0
    rawRequests.each { raw ->
        if (!(raw instanceof Map) || raw.keySet() != featureFields ||
                !canonical(raw.feature_id) || !canonical(raw.measurement_name) ||
                !featureIds.add(raw.feature_id) || !measurementNames.add(raw.measurement_name) ||
                !nullableCanonical(raw.channel_id) || !nullableCanonical(raw.biomarker) ||
                !nullableCanonical(raw.compartment) || !nullableCanonical(raw.units) ||
                !(raw.opt_in instanceof Boolean))
            throw new IllegalStateException("Feature requests require unique closed canonical rows")
        if (channelKinds.contains(raw.kind)) {
            Map channel = channelsById[raw.channel_id] as Map
            if (channel == null || !compartments.contains(raw.compartment) ||
                    (raw.kind == "expression" ? raw.biomarker != channel.biomarker : raw.biomarker != null))
                throw new IllegalStateException("Feature channel or biomarker does not match processed channels")
            String prefix = "${(raw.compartment as String).capitalize()}: ${channel.name}: "
            String name = raw.measurement_name as String
            if (!name.startsWith(prefix) || !statistics.contains(name.substring(prefix.length())))
                throw new IllegalStateException("Feature measurement is not an exact supported QuPath key")
            if (raw.kind == "expression") expressionCount++
        } else if (!otherKinds.contains(raw.kind) || raw.channel_id != null || raw.biomarker != null) {
            throw new IllegalStateException("Feature kind or non-channel binding is invalid")
        }
    }
    if (expressionCount == 0)
        throw new IllegalStateException("At least one protein-marker intensity feature is required")
    settingsRequest.segmentation_model_version as String
}
// MIF_MEASUREMENT_SETTINGS_JOIN_END
String segmentationModelVersion = validateMeasurementSettings(request)
List requests = request.feature_requests as List
def server = getCurrentServer()
// The current codec supplies only columns from the accepted predecessor's
// identical image/cell/feature inputs. They must also remain selected natively
// at the same geometry revision; edited or deselected columns are recalculated.
Closure<Set<String>> reusableMeasurementNames = { ->
    if (!request.containsKey("measurement_reuse")) return Collections.emptySet()
    Object rawReuse = request.measurement_reuse
    if (!(rawReuse instanceof Map) || rawReuse.keySet() != [
            "component", "geometry_revision", "measurement_names", "producer_version"] as Set)
        throw new IllegalStateException("Measurement reuse requires its accepted predecessor selection")
    Map reuse = rawReuse as Map
    Object rawComponent = reuse.component
    Object revision = reuse.geometry_revision
    Object names = reuse.measurement_names
    if (!(rawComponent instanceof Map) || rawComponent.keySet() != ["path", "sha256"] as Set ||
            !(rawComponent.path instanceof String) || !rawComponent.path ||
            rawComponent.path != rawComponent.path.trim() ||
            !(rawComponent.sha256 instanceof String) || !(rawComponent.sha256 ==~ /[0-9a-f]{64}/) ||
            !(revision instanceof Number) || revision.longValue() < 0L ||
            revision.doubleValue() != (double) revision.longValue() ||
            !(names instanceof List) || names.isEmpty() ||
            names.any { !(it instanceof String) || !it || it != it.trim() } ||
            names.size() != names.toSet().size() ||
            !(reuse.producer_version instanceof String) || !reuse.producer_version ||
            reuse.producer_version != reuse.producer_version.trim())
        throw new IllegalStateException("Measurement reuse selection is incomplete")
    Path componentPath = Paths.get(rawComponent.path as String)
    if (!componentPath.isAbsolute() || componentPath != componentPath.toAbsolutePath().normalize())
        throw new IllegalStateException("Measurement reuse component requires its canonical absolute path")
    Set intensityNames = requests.findAll { it.kind in ["expression", "dapi", "autofluorescence"] }
        .collect { it.measurement_name } as Set
    if (!intensityNames.containsAll(names))
        throw new IllegalStateException("Measurement reuse differs from the selected intensity features")
    if (revision.longValue() != cellProvider.getGeometryRevision() ||
            reuse.producer_version != qupath.lib.common.GeneralTools.getVersion()?.toString())
        return Collections.emptySet()
    Map selected = cellProvider.getSelectedComponentMetadata().find {
        it.path == rawComponent.path
    } as Map
    if (selected == null) return Collections.emptySet()
    if (selected.sha256 != rawComponent.sha256 || selected.population_id != populationId ||
            selected.scope_key != sectionId)
        throw new IllegalStateException("Measurement reuse component differs from the current selected result")
    Map references = cellProvider.getSelectedComponentReferences()
    names.findAll { name ->
        references["measurement:" + name] == [(sectionId): rawComponent.path]
    } as Set<String>
}
Set<String> reusedMeasurementNames = reusableMeasurementNames()
// i) Resolve the requested intensity union once. The native API shares one
// rasterized mask set across this statistic/compartment/channel union; selected
// names below retain the exact requested combinations and full-resolution scale.
// A missing combination can overlap another reused combination in this union;
// keep QuPath's shared-mask operation rather than issuing per-feature reads.
Map statisticByName = ["Mean": ObjectMeasurements.Measurements.MEAN,
    "Median": ObjectMeasurements.Measurements.MEDIAN, "Min": ObjectMeasurements.Measurements.MIN,
    "Max": ObjectMeasurements.Measurements.MAX, "Std.Dev.": ObjectMeasurements.Measurements.STD_DEV,
    "Variance": ObjectMeasurements.Measurements.VARIANCE]
Set intensityKinds = ["expression", "dapi", "autofluorescence"] as Set
List intensityRequests = requests.findAll {
    intensityKinds.contains(it.kind) && !reusedMeasurementNames.contains(it.measurement_name)
}
Set requestedStatistics = intensityRequests.collect {
    statisticByName[(it.measurement_name as String).substring((it.measurement_name as String).lastIndexOf(": ") + 2)]
} as Set
Set requestedCompartments = intensityRequests.collect {
    ObjectMeasurements.Compartments.valueOf((it.compartment as String).toUpperCase(Locale.ROOT))
} as Set
Set requestedChannelIds = intensityRequests.collect { it.channel_id } as Set
List<String> requestedChannelNames = request.canonical_channels.findAll {
    requestedChannelIds.contains(it.channel_id)
}.collect { it.name as String }
def measurementServer = requestedChannelNames.isEmpty() || requestedChannelNames.size() == server.nChannels() ? server :
    new qupath.lib.images.servers.TransformedServerBuilder(server)
        .extractChannels(requestedChannelNames as String[]).build()

// The compiled row calculator uses the existing QuPath statistic union and
// reads native morphology. The stage retains source/region admission and publication.
List markerRequests = requests.findAll { it.kind == "expression" }
List<String> featureIds = markerRequests.collect { it.feature_id as String }
List<String> measurementNames = markerRequests.collect { it.measurement_name as String }

def captureDirectoryAncestry = { Path path, String label ->
    if (!path.isAbsolute() || path != path.toAbsolutePath().normalize())
        throw new IllegalStateException("${label} must be an absolute normalized native path")
    Path current = path.root
    if (current == null)
        throw new IllegalStateException("${label} has no filesystem root")
    List<Path> ancestry = [current]
    path.each { Path component ->
        current = current.resolve(component)
        ancestry << current
    }
    List retained = ancestry.collect { Path candidate ->
        BasicFileAttributes attributes
        try {
            attributes = Files.readAttributes(
                candidate, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS
            )
        } catch (IOException error) {
            throw new IllegalStateException("${label} ancestry is not an existing directory", error)
        }
        Object fileKey = attributes.fileKey()
        if (!attributes.isDirectory() || attributes.isSymbolicLink() ||
                attributes.isOther() || Files.isSymbolicLink(candidate) ||
                candidate.toRealPath(LinkOption.NOFOLLOW_LINKS) != candidate ||
                fileKey == null)
            throw new IllegalStateException("${label} ancestry is not a real directory")
        [path: candidate, file_key: fileKey]
    }
    retained
}
def validateDirectoryAncestry = { List retained, String label ->
    if (retained == null || retained.isEmpty())
        throw new IllegalStateException("${label} retained identity is absent")
    List current = captureDirectoryAncestry(retained.last().path as Path, label) as List
    if (current.size() != retained.size() || (0..<current.size()).any { index ->
        current[index].path != retained[index].path ||
            current[index].file_key != retained[index].file_key
    }) throw new IllegalStateException("${label} ancestry identity changed")
}
def ensureRealChildDirectory = { List parentIdentity, String leaf ->
    validateDirectoryAncestry(parentIdentity, "Measurement row output parent")
    Path parent = parentIdentity.last().path as Path
    Path child = parent.resolve(leaf).normalize()
    if (child.parent != parent)
        throw new IllegalStateException("Measurement row output path escapes its parent")
    try {
        Files.createDirectory(child)
    } catch (FileAlreadyExistsException ignored) { }
    List childIdentity = captureDirectoryAncestry(
        child, "Measurement row output directory"
    ) as List
    validateDirectoryAncestry(parentIdentity, "Measurement row output parent")
    [path: child, identity: childIdentity]
}
def captureRegularFile = { Path path, String label ->
    if (!path.isAbsolute() || path != path.toAbsolutePath().normalize())
        throw new IllegalStateException("${label} must be an absolute normalized native path")
    BasicFileAttributes attributes
    try {
        attributes = Files.readAttributes(
            path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS
        )
    } catch (IOException error) {
        throw new IllegalStateException("${label} is unavailable", error)
    }
    Object fileKey = attributes.fileKey()
    if (!attributes.isRegularFile() || attributes.isSymbolicLink() ||
            attributes.isOther() || Files.isSymbolicLink(path) || fileKey == null ||
            path.toRealPath(LinkOption.NOFOLLOW_LINKS) != path)
        throw new IllegalStateException("${label} is not one real regular file")
    [path: path, file_key: fileKey, size: attributes.size()]
}
def validateRegularFile = { Map retained, String label ->
    Map current = captureRegularFile(retained.path as Path, label) as Map
    if (current.file_key != retained.file_key || current.size != retained.size)
        throw new IllegalStateException("${label} identity changed")
}
def retainedInputStream = { Map identity, String label ->
    validateRegularFile(identity, label)
    SeekableByteChannel channel = Files.newByteChannel(
        identity.path as Path, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS
    )
    Channels.newInputStream(channel)
}
def readRetainedBytes = { Map identity, int maximumBytes, String label ->
    if ((identity.size as long) < 1L || (identity.size as long) > maximumBytes)
        throw new IllegalStateException("${label} exceeds its closed byte bound")
    InputStream stream = retainedInputStream(identity, label) as InputStream
    byte[] payload
    try {
        payload = stream.readNBytes(maximumBytes + 1)
    } finally {
        stream.close()
    }
    validateRegularFile(identity, label)
    if (payload.length != (identity.size as long))
        throw new IllegalStateException("${label} changed while reading")
    payload
}
Closure canonicalValue
canonicalValue = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap()
        (value as Map).each { key, item ->
            if (!(key instanceof String))
                throw new IllegalStateException("Authenticated run plan key is invalid")
            sorted[key as String] = canonicalValue(item)
        }
        return sorted
    }
    if (value instanceof List) return (value as List).collect { canonicalValue(it) }
    value
}
def authenticateAnalysisDomainBindings = {
    if (!mifAuthenticatedHeadlessProjectStage)
        throw new IllegalStateException("Measurement row publication requires authenticated project-stage authority")
    Path planPath = MifProjectStageBootstrapV1.environmentPath(
        mifProjectStageEnvironment, "MIF_QUPATH_PROJECT_STAGE_RUN_PLAN_PATH"
    )
    List planParentIdentity = captureDirectoryAncestry(
        planPath.parent, "Authenticated run plan parent"
    ) as List
    Map planIdentity = captureRegularFile(planPath, "Authenticated run plan") as Map
    byte[] planBytes = readRetainedBytes(
        planIdentity, 16 * 1024 * 1024, "Authenticated run plan"
    ) as byte[]
    validateDirectoryAncestry(planParentIdentity, "Authenticated run plan parent")
    Map plan = MifProjectStageBootstrapV1.parseMap(planBytes, "Authenticated run plan")
    String planSha256 = plan.plan_sha256 as String
    Map unsignedPlan = new TreeMap(plan)
    unsignedPlan.remove("plan_sha256")
    String calculatedPlanSha256 = MessageDigest.getInstance("SHA-256").digest(
        rowGson.toJson(canonicalValue(unsignedPlan)).getBytes(StandardCharsets.UTF_8)
    ).encodeHex().toString()
    if (!(planSha256 ==~ /[0-9a-f]{64}/) || planSha256 != calculatedPlanSha256 ||
            plan.schema_name != "mif-project-stage-run-plan" ||
            plan.schema_version != 2L ||
            plan.launch_mode != "headless_project_script" ||
            plan.target_script_asset_id != mifCurrentTargetScriptAssetId ||
            plan.script_bundle_sha256 != requestedScriptBundleSha256 ||
            plan.target_script_sha256 != request.script_asset_sha256 ||
            plan.project_uri != MifProjectStageBootstrapV1.mifCanonicalUri(
                boundProjectPath.toUri()
            ) || !(plan.targets instanceof List))
        throw new IllegalStateException("Authenticated measurement run plan is invalid")
    List matchingTargets = (plan.targets as List).findAll { raw ->
        raw instanceof Map && raw.project_entry_id == requestedEntryId
    }
    if (matchingTargets.size() != 1)
        throw new IllegalStateException("Authenticated measurement target is not unique")
    Map target = matchingTargets.first() as Map
    if (target.project_entry_id != requestedEntryId ||
            target.sample_id != request.sample_id ||
            target.slide_id != slideId || target.pipeline_run_id != runId ||
            target.image_role != "primary_mif" ||
            target.wire_stage_id != envelope.stage ||
            target.terminal_stage_id != envelope.stage ||
            target.request_codec_id != mifCurrentTargetScriptAssetId ||
            target.resolved_config_sha256 != request.config_hash ||
            !(target.analysis_domain_bindings instanceof List) ||
            (target.analysis_domain_bindings as List).isEmpty())
        throw new IllegalStateException("Authenticated measurement target differs from the request")
    Set bindingFields = [
        "project_entry_id", "sample_id", "slide_id", "analysis_domain_id",
        "analysis_domain_revision", "analysis_domain_revision_sha256",
        "domain_set_sha256"
    ] as Set
    List bindings = target.analysis_domain_bindings as List
    List bindingKeys = []
    bindings.each { raw ->
        if (!(raw instanceof Map) || raw.keySet() != bindingFields)
            throw new IllegalStateException("Authenticated analysis-domain binding is not closed")
        Map binding = raw as Map
        Number revision = binding.analysis_domain_revision as Number
        if (binding.project_entry_id != requestedEntryId ||
                binding.sample_id != request.sample_id ||
                binding.slide_id != slideId ||
                !((binding.analysis_domain_id as String) ==~ /[0-9a-f]{64}/) ||
                !(binding.analysis_domain_revision instanceof Number) ||
                revision.longValue() < 1L || revision.doubleValue() != revision.longValue() ||
                !((binding.analysis_domain_revision_sha256 as String) ==~ /[0-9a-f]{64}/) ||
                !((binding.domain_set_sha256 as String) ==~ /[0-9a-f]{64}/))
            throw new IllegalStateException("Authenticated analysis-domain binding is invalid")
        bindingKeys << [binding.analysis_domain_id, revision.longValue(), requestedEntryId]
    }
    if (bindingKeys != bindingKeys.toList().sort() || bindingKeys.toSet().size() != bindingKeys.size())
        throw new IllegalStateException("Authenticated analysis-domain bindings are not canonical")
    Set authenticatedDomainIds = bindings.collect {
        (it as Map).analysis_domain_id as String
    } as Set
    validateRegularFile(planIdentity, "Authenticated run plan")
    validateDirectoryAncestry(planParentIdentity, "Authenticated run plan parent")
    bindings
}

def publishMeasurementRows = { List authenticatedBindings ->
    // i) Bind source regions to accepted classifier inventories. Empty accepted
    // inventories remain available; absent results are not inferred as empty.
    Object rawPopulationSources = request.containsKey("phenotyping_population_sources") ?
        request.phenotyping_population_sources : []
    if (!(rawPopulationSources instanceof List))
        throw new IllegalStateException("Phenotyping region sources must be a list")
    List populationSources = rawPopulationSources as List
    Map bindingsByDomain = authenticatedBindings.collectEntries { [(it.analysis_domain_id): it] }
    Map objectsById = getAnnotationObjects().collectEntries { [(it.getID().toString()): it] }
    Set sourceClasses = new HashSet()
    List<PopulationMembership.Region> admittedRegions = new ArrayList<>()
    def tissueGeometry = sections.first().getROI().getGeometry()
    populationSources.each { rawSource ->
        Set sourceFields = ["kind", "class_ids", "analysis_domain_id", "analysis_domain_revision_sha256",
                            "source_result_sha256", "producer_attempt_id", "roi_inventory"] as Set
        if (!(rawSource instanceof Map) || rawSource.keySet() != sourceFields)
            throw new IllegalStateException("Phenotyping region source is incomplete")
        Map source = rawSource as Map
        Map domain = bindingsByDomain[source.analysis_domain_id] as Map
        boolean primarySource = source.kind == "primary_structure"
        if (source.kind != "primary_structure" || domain == null ||
                source.analysis_domain_revision_sha256 != domain.analysis_domain_revision_sha256 ||
                !((source.source_result_sha256 as String) ==~ /[0-9a-f]{64}/) ||
                !(source.class_ids instanceof List) || source.class_ids.isEmpty() ||
                source.class_ids.any { !(it instanceof String) || !it || it != it.trim() || it == "Ignore" } ||
                source.class_ids != source.class_ids.toSet().toList().sort() ||
                !(source.roi_inventory instanceof Map) || !(source.producer_attempt_id instanceof String))
            throw new IllegalStateException("Phenotyping region sources differ from current accepted analysis regions")
        source.class_ids.each { classId ->
            if (!sourceClasses.add([source.kind, source.analysis_domain_id, classId]))
                throw new IllegalStateException("Phenotyping source repeats a structure class")
        }
        List observedIds = []
        RoiInventory.read(MifProjectStageBootstrapV1.producerOutputRoot(request, config, source.producer_attempt_id as String), source.roi_inventory as Map,
                RoiInventory.member(source.producer_attempt_id as String), { RoiInventory.Row rawRegion ->
            def object = objectsById[rawRegion.pathObjectId()]
            Map metadata = object == null ? [:] : md(object)
            String classId = metadata.get(primarySource ? "primary_class_id" : "structure_id") as String
            def geometry = object?.getROI()?.getGeometry()
            if (object == null || geometry == null || geometry.isEmpty() || !geometry.isValid() ||
                    object.getParent() == null || object.getROI().getImagePlane() != sections.first().getROI().getImagePlane() ||
                    metadata.get("pipeline_stage") != (primarySource ? "primary_classifier" : "secondary_classifier") ||
                    metadata.get("pipeline_active") != "true" || metadata.get("classifier_status") != "accepted" ||
                    metadata.get("pipeline_run_id") != runId || metadata.get("generation_id") != generationId ||
                    metadata.get("slide_id") != slideId || metadata.get("section_id") != sectionId ||
                    metadata.get("analysis_domain_id") != source.analysis_domain_id ||
                    metadata.get("analysis_domain_revision_sha256") != source.analysis_domain_revision_sha256 ||
                    metadata.get("domain_set_sha256") != domain.domain_set_sha256 ||
                    metadata.get("roi_id") != rawRegion.roiId() || rawRegion.classId() != classId ||
                    (!source.class_ids.contains(classId) && !(primarySource && classId == "Ignore")))
                throw new IllegalStateException("A selected phenotyping region is missing or stale; refresh measurements")
            def normalized = geometry.copy()
            normalized.normalize()
            String geometrySha = MessageDigest.getInstance("SHA-256").digest(
                new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
            ).encodeHex().toString()
            Map liveRevision = [analysis_domain_id: source.analysis_domain_id,
                analysis_domain_revision_sha256: source.analysis_domain_revision_sha256,
                analysis_domain_set_sha256: domain.domain_set_sha256,
                classification: object.getClassification(), geometry_sha256: geometrySha,
                parent_path_object_id: object.getParent().getID().toString(),
                path_object_id: object.getID().toString(), pipeline_active: metadata.get("pipeline_active"),
                type: primarySource ? "primary" : "secondary", type_id: classId]
            String revisionSha = MessageDigest.getInstance("SHA-256").digest(
                rowGson.toJson(canonicalValue(liveRevision)).getBytes(StandardCharsets.UTF_8)
            ).encodeHex().toString()
            if (revisionSha != rawRegion.objectRevisionSha256() || revisionSha != metadata.get("object_revision_sha256"))
                throw new IllegalStateException("A phenotyping region changed after acceptance; refresh its classifier result")
            observedIds << rawRegion.roiId()
            if (primarySource)
                admittedRegions.add(PopulationMembership.Region.primary(source.analysis_domain_id as String,
                    rawRegion.roiId(), classId, object.getROI()))
        } as Consumer<RoiInventory.Row>)
        List currentIds = objectsById.values().findAll { object ->
            Map metadata = md(object)
            String classId = metadata.get(primarySource ? "primary_class_id" : "structure_id") as String
            metadata.get("pipeline_stage") == (primarySource ? "primary_classifier" : "secondary_classifier") &&
                metadata.get("pipeline_active") == "true" && metadata.get("slide_id") == slideId &&
                metadata.get("section_id") == sectionId && metadata.get("generation_id") == generationId &&
                metadata.get("analysis_domain_id") == source.analysis_domain_id &&
                (source.class_ids.contains(classId) || (primarySource && classId == "Ignore"))
        }.collect { md(it).get("roi_id") as String }.sort()
        if (currentIds != observedIds)
            throw new IllegalStateException("The accepted phenotyping region inventory changed; refresh its classifier result")
    }
    // Secondary structures are produced after phenotyping. Retain the typed
    // empty secondary column for existing readers, without collecting regions.
    // ii) Freeze population vocabularies once, then write only typed numeric
    // results aligned to the immutable base. No geometry or cell JSON is copied.
    Map<String,String> resultColumns = new LinkedHashMap<>()
    requests.each { resultColumns["measurement:" + it.measurement_name] = "float64" }
    resultColumns += [measurement_run_id:"string", measurement_generation_id:"string",
        "mif.measurements_stale":"bool", "population:detected_tissue":"bool",
        "population:primary_structure":"list_int32", "population:secondary_structure":"list_int32"]
    Map categoryCodes = [:]
    Map resultDictionaries = [:]
    ["primary_structure", "secondary_structure"].each { kind ->
        String field = "population:" + kind
        List labels = populationSources.findAll { it.kind == kind }.collectMany { it.class_ids }.toSet().toList().sort()
        categoryCodes[kind] = labels.withIndex().collectEntries { label, code -> [(label):code as Integer] }
        resultDictionaries[field] = [dictionary_id: "${populationId}/${runId}/${sectionId}/${kind}".toString(),
            field:field, version:1, entries:labels.withIndex().collect { label, code ->
                [code:code as Integer, label_id:label, name:label, color:null]
            }]
    }
    // Geometry membership and typed finite rows share one compiled per-request
    // calculator; authenticated region and dictionary admission above stays here.
    measurementRows = new MeasurementRows(measurementServer, requestedStatistics, requestedCompartments,
        requests.collect { it.measurement_name as String },
        new PopulationMembership(tissueGeometry, admittedRegions), categoryCodes, runId, generationId)
    // Runtime publication binds the chosen current workspace independently of
    // the retained scientific config and earlier segmentation/component inputs.
    Path outputRoot = MifProjectStageBootstrapV1.outputRoot(request, config)
    // Imports need not have materialized this sample's new analysis directory.
    // Reuse pinned child creation for only the missing suffix below the nearest
    // existing real ancestor; every existing and created directory stays pinned.
    List<String> missingOutputDirectories = []
    Path existingOutputAncestor = outputRoot
    while (!Files.exists(existingOutputAncestor, LinkOption.NOFOLLOW_LINKS)) {
        missingOutputDirectories.add(0, existingOutputAncestor.fileName.toString())
        existingOutputAncestor = existingOutputAncestor.parent
    }
    List outputRootIdentity = captureDirectoryAncestry(
        existingOutputAncestor, "Authenticated output root"
    ) as List
    missingOutputDirectories.each { String leaf ->
        outputRootIdentity = ensureRealChildDirectory(outputRootIdentity, leaf).identity as List
    }
    Map batchDirectory = ensureRealChildDirectory(outputRootIdentity, "batch") as Map
    Map rowsDirectory = ensureRealChildDirectory(batchDirectory.identity as List, "measurement-rows") as Map
    Path rowsRoot = rowsDirectory.path as Path
    List rowsRootIdentity = rowsDirectory.identity as List
    // The selected immutable base may remain in the original input workspace.
    // Its own retained parent/file identity and admitted digest bind that read.
    List baseParentIdentity = captureDirectoryAncestry(cellDatasetPath.parent, "Selected cell dataset parent") as List
    Map baseIdentity = captureRegularFile(cellDatasetPath, "Selected cell dataset") as Map
    byte[] baseBytes = readRetainedBytes(baseIdentity, 16 * 1024 * 1024, "Selected cell dataset") as byte[]
    String baseSha256 = MessageDigest.getInstance("SHA-256").digest(baseBytes).encodeHex().toString()
    if (baseSha256 != request.segmentation_cell_dataset_sha256)
        throw new IllegalStateException("Selected base cell dataset changed before measurement extraction")
    validateDirectoryAncestry(baseParentIdentity, "Selected cell dataset parent")
    Path componentRoot = rowsRoot.resolve("component-${UUID.randomUUID()}")
    String requestSha = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(Paths.get(args[0] as String))).encodeHex().toString()
    validateDirectoryAncestry(rowsRootIdentity, "Measurement component output directory")
    Set<String> measurementInputs = requests.collect { "measurement:" + it.measurement_name } as Set<String>
    def componentPath = cellProvider.writeComponent(componentRoot, requestSha, sectionId,
        sections.first().getROI().getGeometry().getEnvelopeInternal(), measurementInputs, resultColumns,
        rowGson.toJsonTree(resultDictionaries).getAsJsonObject(), { cell ->
            Map metadata = md(cell)
            if (!cell.isCell() || metadata.get("pipeline_stage") != "segmentation" ||
                    metadata.get("generation_id") != generationId || metadata.get("slide_id") != slideId ||
                    metadata.get("section_id") != sectionId || metadata.get("segmentation_population_id") != populationId ||
                    !metadata.get("cell_id") || !domainKinds.contains(metadata.get("analysis_domain_kind")) ||
                    !bindingsByDomain.containsKey(metadata.get("analysis_domain_id")))
                throw new IllegalStateException("Measurement cell differs from accepted segmentation and analysis authority")
            measurementRows.calculate(cell)
        } as Function)
    List componentParentIdentity = captureDirectoryAncestry(componentPath.parent, "Completed measurement component parent") as List
    Map componentIdentity = captureRegularFile(componentPath, "Completed measurement component") as Map
    byte[] componentBytes = readRetainedBytes(componentIdentity, 16 * 1024 * 1024, "Completed measurement component") as byte[]
    Map component = gson.fromJson(new String(componentBytes, StandardCharsets.UTF_8), Map.class)
    validateDirectoryAncestry(rowsRootIdentity, "Measurement component output directory")
    if (measurementRows.rowCount() != cellProvider.getCurrentRowCount())
        throw new IllegalStateException("Measured cells differ from the accepted base after saved cell deletions. Save the cell corrections and refresh measurements")
    if (measurementRows.rowCount() <= 0L)
        throw new IllegalStateException("No current canonical cells were measured")
    String componentSha = MessageDigest.getInstance("SHA-256").digest(componentBytes).encodeHex().toString()
    Map selectedComponents = resultColumns.collectEntries { field, type -> [(field):[(sectionId):componentPath.toString()]] }
    // We publish only sparse checkpoint rows; the clean base remains selected.
    // Label-only edits also require a delta even when geometry revision is zero.
    Map selectedEdits = null
    if (cellProvider.getEditCount() > 0) {
        Path editPath = cellProvider.publishEdits(rowsRoot.resolve("edits-${UUID.randomUUID()}"))
        Map editIdentity = captureRegularFile(editPath, "Completed sparse cell checkpoint") as Map
        byte[] editBytes = readRetainedBytes(editIdentity, 16 * 1024 * 1024, "Completed sparse cell checkpoint") as byte[]
        Map editDescription = gson.fromJson(new String(editBytes, StandardCharsets.UTF_8), Map.class)
        if ((editDescription.geometry_revision as Number).longValue() != (component.geometry_revision as Number).longValue())
            throw new IllegalStateException("Cell geometry changed after measurement calculation")
        selectedEdits = [path:editPath.toString(),sha256:MessageDigest.getInstance("SHA-256").digest(editBytes).encodeHex().toString()]
    }
    Map header = [schema_version:"mif-marker-intensity-dataset/v1", project_entry_id:requestedEntryId,
        slide_id:slideId, section_id:sectionId, image_role:"primary_mif",
        segmentation_generation_id:generationId, measurement_generation_id:generationId,
        segmentation_population_id:populationId, analysis_domain_ids:authenticatedBindings.collect { it.analysis_domain_id },
        feature_ids:featureIds, measurement_names:measurementNames, coordinate_system:"global_level0",
        population_sources:populationSources.collect { source -> source.subMap([
            "kind", "class_ids", "analysis_domain_id", "analysis_domain_revision_sha256", "source_result_sha256"
        ]) },
        cell_dataset:[path:cellDatasetPath.toString(),sha256:baseSha256],
        components:selectedComponents, accepted_component:[path:componentPath.toString(),sha256:componentSha], cell_edits:selectedEdits,
        geometry_revision:(component.geometry_revision as Number).longValue()]
    byte[] payload = rowGson.toJson(header).getBytes(StandardCharsets.UTF_8)
    String contentSha = MessageDigest.getInstance("SHA-256").digest(payload).encodeHex().toString()
    Path target = rowsRoot.resolve("${contentSha}.mif-marker-intensity-dataset.json")
    Path temporary = rowsRoot.resolve(".${UUID.randomUUID()}.json.tmp")
    try {
        Files.write(temporary, payload, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
        java.nio.channels.FileChannel.open(temporary, StandardOpenOption.WRITE).withCloseable { channel -> channel.force(true) }
        try { Files.createLink(target, temporary) }
        catch (FileAlreadyExistsException ignored) {
            Map existingIdentity = captureRegularFile(target, "Existing measurement selection") as Map
            if (!Arrays.equals(readRetainedBytes(existingIdentity, 16 * 1024 * 1024, "Existing measurement selection") as byte[], payload))
                throw new IllegalStateException("Measurement selection content-address collision")
        }
    } finally { Files.deleteIfExists(temporary) }
    validateDirectoryAncestry(rowsRootIdentity, "Measurement selection output directory")
    validateDirectoryAncestry(componentParentIdentity, "Completed measurement component parent")
    validateRegularFile(componentIdentity, "Completed measurement component")
    validateRegularFile(baseIdentity, "Selected cell dataset")
    captureRegularFile(target, "Published measurement selection")
    [descriptor:[schema_version:"mif-marker-intensity-dataset-ref/v1", content_schema_version:"mif-marker-intensity-dataset/v1",
        path:target.toString(),sha256:contentSha,byte_count:payload.length,row_count:measurementRows.rowCount()],
        component_path:componentPath.toString()]
}

List authenticatedBindings = authenticateAnalysisDomainBindings() as List
Map measurementRowsPublication = publishMeasurementRows(authenticatedBindings) as Map
// A successful positive full-row publication contains every selected finite
// value. Failure counters are never published, so rates follow that invariant.
List observedMeasurements = requests.collect { feature ->
    [measurement_name: feature.measurement_name, missing_rate: 0.0d, non_finite_rate: 0.0d]
}
List inventory = requests.collect { value ->
    Map feature = value as Map
    String id = feature.feature_id as String
    String name = feature.measurement_name as String
    Object kind = feature.kind
    boolean derivedShape = MeasurementRows.isDerivedShapeMeasurement(name)
    boolean instanSegProduced = !derivedShape && (feature.kind in ["morphology", "nuclear_morphology"])
    [feature_id: id, measurement_name: name, kind: kind,
     channel_id: feature.channel_id, biomarker: feature.biomarker,
     compartment: feature.compartment, units: feature.units,
     opt_in: feature.opt_in == true, producer: derivedShape ? "mIF shape measurements" : (instanSegProduced ? "InstanSeg" : "QuPath"),
     producer_version: instanSegProduced ? segmentationModelVersion : (qupath.lib.common.GeneralTools.getVersion()?.toString() ?: "unknown"),
     missing_rate: 0.0d, non_finite_rate: 0.0d]
}
// Request admission requires nonempty unique features including a protein-marker
// intensity. The compiled calculator rejects any missing/nonfinite selected value
// before counting a row, and publication above has checked the positive full base
// count. The inventory therefore contains every requested finite feature here.
// Select only after every scientific and publication check succeeded. A failed
// calculation never changes accepted measurements or the human edit checkpoint.
NativeCellScope.selectMeasurementComponent(boundImageData, measurementRowsPublication.component_path as String)
stageOwnedMeasurementMutation = true

def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { input ->
        DigestInputStream stream = new DigestInputStream(input, digest)
        byte[] buffer = new byte[1024 * 1024]
        while (stream.read(buffer) >= 0) { }
    }
    digest.digest().encodeHex().toString()
}
def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
}
def persistCurrentEntryState = {
    if (!mifAuthenticatedHeadlessProjectStage ||
            !stageOwnedMeasurementMutation) return
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
Map outputs = [cell_count: measurementRows.rowCount(), section_id: sectionId,
    measurement_inventory: inventory, observed_measurements: observedMeasurements,
    measurement_keys: inventory.collect { it.measurement_name },
    coordinate_system: "global_level0"]
if (measurementRowsPublication != null)
    outputs += [measurement_rows: measurementRowsPublication.descriptor]
persistCurrentEntryState()
atomicJson(manifestPath, [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
    generations: [], object_outputs: outputs])
Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: generationId, interaction_reason: null, affected_object_ids: [], message: null,
    allowed_actions: [], resume_context: null, checkpoint_id: null, outputs: outputs, error: null]
mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
    generation_manifest: [path: manifestPath.fileName.toString(), sha256: sha256(manifestPath)]])
