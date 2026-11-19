// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE
/** Apply one accepted primary pixel classifier inside one canonical mIF TissueSection. */

import java.nio.charset.StandardCharsets
import java.nio.file.*
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.UUID
import org.mif.io.NativeClassifierFiles
import org.mif.roi.RoiInventory
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools
import com.google.gson.ToNumberPolicy

import static qupath.lib.scripting.QP.*

final String SCRIPT_ASSET_PATH = "compartment_analysis/02_apply_primary_compartment_classifier.groovy"
if (args.size() != 3) throw new IllegalArgumentException("Expected request, result and manifest paths")
Path requestPath = Paths.get(args[0] as String)
Path resultPath = Paths.get(args[1] as String)
Path manifestPath = Paths.get(args[2] as String)
// Preserve revision integers and real-valued settings in owner content digests.
def gson = GsonTools.getInstance().newBuilder().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
def canonicalGson = gson.newBuilder().serializeNulls().create()
def wireGson = GsonTools.getInstance().newBuilder().serializeNulls().setPrettyPrinting().create()
Map envelope = gson.fromJson(Files.readString(requestPath), Map) as Map
if (envelope.schema_version != "roi-stage-request/v2" || envelope.stage != "primary_classifier")
    throw new IllegalArgumentException("Invalid primary-classifier request envelope")
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
String analysisDomainId = request.analysis_domain_id as String
String analysisDomainRevisionSha256 = request.analysis_domain_revision_sha256 as String
String analysisDomainPathObjectId = request.analysis_domain_path_object_id as String
String generationId = request.generation_id as String
String classifierReviewStatus = request.classifier_review_status as String
String configHash = request.config_hash as String
if (!sectionId || !(analysisDomainId ==~ /[0-9a-f]{64}/) ||
    !(analysisDomainRevisionSha256 ==~ /[0-9a-f]{64}/) || !analysisDomainPathObjectId ||
    !(request.analysis_domain_revision instanceof Number) ||
    (request.analysis_domain_revision as Number).intValue() < 1 ||
    (request.analysis_domain_revision as Number).doubleValue() !=
        (request.analysis_domain_revision as Number).intValue() ||
    !(request.domain_set_sha256 ==~ /[0-9a-f]{64}/) ||
    !generationId || !(classifierReviewStatus in ["accepted", "rejected", "needs_review"]) ||
    !(configHash ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException("Exact domain revision, generation and material config hash are required")

def canonicalValue
canonicalValue = { Object value ->
    if (value instanceof Map) {
        Map sorted = new TreeMap<String, Object>()
        (value as Map).each { key, nested -> sorted.put(key as String, canonicalValue(nested)) }
        return sorted
    }
    if (value instanceof List) return (value as List).collect { canonicalValue(it) }
    value
}
def canonicalSha256 = { Object value ->
    MessageDigest.getInstance("SHA-256").digest(
        canonicalGson.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8)
    ).encodeHex().toString()
}
// MIF_PRIMARY_SETTINGS_JOIN_BEGIN
def validatePrimarySettings = { Map selectedRequest, Map admittedConfig, Closure digest ->
    Map settings = selectedRequest.primary_settings as Map
    Set<String> pairFields = ["primary_classes", "primary_classifier"] as Set
    Set<String> classFields = ["class_id", "display_name"] as Set
    Set<String> classifierFields = [
        "classifier_id", "mode", "channel_roles", "artifact_path",
        "classifier_version", "validation_domain_id", "classifier_sha256",
        "validation_status", "validation_record_id", "qc_passed", "qc_record_id",
        "review_policy", "training_regions_per_label", "require_ignore_training",
        "output_classes", "count_bounds", "class_count_bounds"
    ] as Set
    if (settings == null || settings.keySet() != pairFields ||
            !(settings.primary_classes instanceof List) ||
            (settings.primary_classes as List).isEmpty() ||
            (settings.primary_classes as List).any { row ->
                !(row instanceof Map) || row.keySet() != classFields
            } || !(settings.primary_classifier instanceof Map) ||
            (settings.primary_classifier as Map).keySet() != classifierFields)
        throw new IllegalStateException("Primary selected settings are not closed")
    Map selectedClassifier = settings.primary_classifier as Map
    String settingsDigest = selectedRequest.primary_settings_sha256 as String
    String sourceRef = selectedRequest.primary_settings_source_ref_sha256 as String
    if (!(settingsDigest ==~ /[0-9a-f]{64}/) || settingsDigest != digest(settings) ||
            !(sourceRef ==~ /[0-9a-f]{64}/))
        throw new IllegalStateException("Primary selected settings or source reference digest differs")
    def originalClasses = admittedConfig.primary_classes
    def originalClassifier = admittedConfig.primary_classifier
    if ((originalClasses == null) != (originalClassifier == null))
        throw new IllegalStateException("Primary admitted settings are incomplete")
    if (originalClasses != null) {
        // The accepted plan may change the optional Ignore role. We keep the
        // original biological classes and every other admitted setting exact.
        Map originalPolicy = new LinkedHashMap(originalClassifier as Map)
        Map selectedPolicy = new LinkedHashMap(selectedClassifier)
        if (originalPolicy != selectedPolicy &&
                (originalPolicy.mode != "train_new" || selectedPolicy.mode != "train_new"))
            throw new IllegalStateException("Only training plans may change the Ignore choice")
        ["output_classes", "require_ignore_training"].each {
            originalPolicy.remove(it)
            selectedPolicy.remove(it)
        }
        if (originalClasses != settings.primary_classes || originalPolicy != selectedPolicy)
            throw new IllegalStateException("Primary selected settings differ from admitted configuration")
    }
    if (selectedRequest.primary_classifier_output_classes != selectedClassifier.output_classes ||
            !(selectedRequest.classifier_application instanceof Map) ||
            selectedRequest.classifier_application.classifier_id != selectedClassifier.classifier_id)
        throw new IllegalStateException("Primary selected classifier or output inventory differs")
    settings
}
// MIF_PRIMARY_SETTINGS_JOIN_END
Map domainRevisionEvidence = request.analysis_domain_revision_evidence as Map
if (domainRevisionEvidence == null ||
        !(domainRevisionEvidence.kind in ["selected_roi", "whole_tissue_section"]) ||
        domainRevisionEvidence.analysis_domain_id != analysisDomainId ||
        domainRevisionEvidence.source_path_object_id != analysisDomainPathObjectId ||
        domainRevisionEvidence.project_entry_id != requestedEntryId ||
        domainRevisionEvidence.revision != request.analysis_domain_revision ||
        domainRevisionEvidence.content_sha256 != analysisDomainRevisionSha256)
    throw new IllegalStateException("Authenticated live domain revision evidence is invalid")
Map unsignedDomainRevisionEvidence = new TreeMap(domainRevisionEvidence)
unsignedDomainRevisionEvidence.remove("content_sha256")
// This is serialized geometry evidence, not part of the Task 6 revision digest.
unsignedDomainRevisionEvidence.remove("normalized_wkb_hex")
if (canonicalSha256(unsignedDomainRevisionEvidence) != analysisDomainRevisionSha256)
    throw new IllegalStateException("Authenticated live domain revision digest is stale")

def md = { object -> object.getMetadata() }
List sections = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_stage") == "tissue_detection" &&
        m.get("generation_status") == "accepted" &&
        m.get("review_status") == "accepted" &&
        m.get("slide_id") == slideId && m.get("section_id") == sectionId &&
        m.get("generation_id") == generationId
}
if (sections.size() != 1)
    throw new IllegalStateException("Exactly one accepted current TissueSection is required")
def section = sections.first()
String tissueRoiId = md(section).get("roi_id") as String
if (!tissueRoiId) throw new IllegalStateException("TissueSection lacks stable roi_id")
def domain = getAnnotationObjects().find { object ->
    object.getID().toString() == analysisDomainPathObjectId
}
if (domain == null)
    throw new IllegalStateException("Exact analysis-domain PathObject is absent")
Map domainMetadata = md(domain)
boolean selectedDomain =
    domainMetadata.get("pipeline_stage") == "analysis_domain_selection" &&
    domainMetadata.get("analysis_domain_status") == "accepted" &&
    domainMetadata.get("analysis_domain_active") == "true" &&
    domainMetadata.get("analysis_domain_id") == analysisDomainId &&
    domainMetadata.get("analysis_domain_revision") ==
        Integer.toString((request.analysis_domain_revision as Number).intValue()) &&
    domainMetadata.get("analysis_domain_revision_sha256") == analysisDomainRevisionSha256 &&
    domainMetadata.get("analysis_domain_set_sha256") == request.domain_set_sha256 &&
    domainMetadata.get("analysis_domain_project_entry_id") == requestedEntryId &&
    domain.isLocked() && domain.getParent() == section
boolean wholeTissueDomain = domain == section
if (!(selectedDomain || wholeTissueDomain) ||
        !domain.getROI().getGeometry().coveredBy(section.getROI().getGeometry()))
    throw new IllegalStateException("Classifier domain is stale, inactive, or outside exact tissue")
Geometry liveDomainGeometry = domain.getROI()?.getGeometry()
if (liveDomainGeometry == null || liveDomainGeometry.isEmpty() || !liveDomainGeometry.isValid())
    throw new IllegalStateException("Classifier live domain geometry is invalid")
if (domainRevisionEvidence.geometry_sha256 != null) {
    Geometry normalizedDomain = liveDomainGeometry.copy();
    normalizedDomain.normalize()
    String liveDomainGeometrySha256 = MessageDigest.getInstance("SHA-256").digest(
        new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedDomain)
    ).encodeHex().toString()
    if (liveDomainGeometrySha256 != domainRevisionEvidence.geometry_sha256)
        throw new IllegalStateException("Classifier live domain geometry differs from its accepted revision")
}
Map registrationContext = (request.registration_context ?: [:]) as Map
List assistanceScopes = (registrationContext.moving_assisted_scopes ?: []) as List
List matchingAssistance = assistanceScopes.findAll { scope ->
    scope instanceof Map && scope.stage == "primary_classifier" &&
        scope.analysis_domain_id == analysisDomainId && scope.structure_id == null &&
        scope.eligible_parent_ids == []
}
if (matchingAssistance.size() > 1)
    throw new IllegalStateException("Primary classifier has ambiguous moving assistance provenance")
Map movingAssistance = matchingAssistance ? matchingAssistance.first() as Map : null
if (movingAssistance != null) {
    if (registrationContext.mode != "warpy" ||
        movingAssistance.keySet() != ["stage", "analysis_domain_id", "structure_id", "eligible_parent_ids",
                                  "registration_id", "registration_qc", "decision_id",
                                  "decision_source", "decided_by"] as Set ||
        !(movingAssistance.registration_id instanceof String) || !(movingAssistance.registration_id as String).trim() ||
        !(movingAssistance.decision_id instanceof String) || !(movingAssistance.decision_id as String).trim() ||
        !(movingAssistance.decision_source in ["user", "config", "training"]) ||
        !(movingAssistance.decided_by instanceof String) || !(movingAssistance.decided_by as String).trim() ||
        !(movingAssistance.registration_qc instanceof Map) || movingAssistance.registration_qc.status != "passed")
        throw new IllegalStateException("Explicit moving assistance requires exact passed Warpy provenance")
}

Map selectedPrimarySettings = validatePrimarySettings(request, config, canonicalSha256)
Map classifierConfig = selectedPrimarySettings.primary_classifier as Map
def sha256 = { Path path ->
    MessageDigest digest = MessageDigest.getInstance("SHA-256")
    path.toFile().withInputStream { input ->
        DigestInputStream stream = new DigestInputStream(input, digest)
        byte[] buffer = new byte[1024 * 1024]
        while (stream.read(buffer) >= 0) { }
    }
    digest.digest().encodeHex().toString()
}
Map classifierApplication = request.classifier_application as Map
Set<String> classifierApplicationFields = [
    "classifier_record_set_ref_sha256", "classifier_record_set_sha256",
    "output_id", "classifier_record_sha256", "classifier_id",
    "classifier_version", "classifier_kind", "application_request_sha256",
    "target_project_identity_sha256", "deployment_binding_set_sha256",
    "target_evidence_set_sha256", "project_relative_path", "source_model_path",
    "native_classifier_sha256", "native_classifier_byte_count",
    "pixel_classifier_definition", "pixel_classifier_definition_sha256"
] as Set
if (classifierApplication == null ||
        classifierApplication.keySet() != classifierApplicationFields ||
        classifierApplication.classifier_kind != "primary_compartment" ||
        classifierApplication.classifier_id != classifierConfig.classifier_id ||
        !(classifierApplication.native_classifier_byte_count instanceof Number) ||
        (classifierApplication.native_classifier_byte_count as Number).longValue() <= 0L ||
        classifierApplicationFields.findAll { it.endsWith("_sha256") }.any { name ->
            !(classifierApplication[name] ==~ /[0-9a-f]{64}/)
        } || !(classifierApplication.pixel_classifier_definition instanceof Map))
    throw new IllegalStateException("Validated primary classifier application is invalid")
String projectRelativePath = classifierApplication.project_relative_path as String
if (!(projectRelativePath ==~ /classifiers\/pixel_classifiers\/mif-[0-9a-f]{64}\.json/))
    throw new IllegalStateException("Primary classifier project-relative locator is invalid")
Path artifactPath = projectRoot.resolve(projectRelativePath).normalize()
// The authenticated application owns any imported source member. This copy
// runs under the ordinary native project-stage mutation authority, before fit
// application or saved-state publication, and never rewrites source provenance.
String classifierSha = NativeClassifierFiles.ensureModel(
    projectRoot, projectRelativePath, classifierApplication.source_model_path as String,
    classifierApplication.native_classifier_sha256 as String,
    (classifierApplication.native_classifier_byte_count as Number).longValue())
Map currentClassifierDefinition = classifierApplication.pixel_classifier_definition as Map
if (canonicalSha256(currentClassifierDefinition) !=
        classifierApplication.pixel_classifier_definition_sha256 ||
        currentClassifierDefinition.classifier_id != classifierApplication.classifier_id ||
        currentClassifierDefinition.version != classifierApplication.classifier_version ||
        currentClassifierDefinition.kind != "primary")
    throw new IllegalStateException("Primary classifier definition differs from the selected record")
String classifierVersion = classifierApplication.classifier_version as String
Map channelRoles = new TreeMap(currentClassifierDefinition.channel_roles as Map)

Map<String, String> labels = (selectedPrimarySettings.primary_classes as List<Map>).collectEntries {
    [(it.display_name as String): it.class_id as String]
}
Set<String> biologicalOutputClasses = labels.values() as Set<String>
def configuredInventory = classifierConfig.output_classes
def requestedInventory = request.primary_classifier_output_classes
if (!(configuredInventory instanceof List) ||
        !(requestedInventory instanceof List) ||
        requestedInventory != configuredInventory)
    throw new IllegalStateException(
        "Authenticated primary output inventory does not match primary classifier configuration"
    )
if (requestedInventory != currentClassifierDefinition.output_classes)
    throw new IllegalStateException(
        "Authenticated primary output inventory does not match the trained classifier definition"
    )
List outputInventory = requestedInventory as List
if (outputInventory == null || outputInventory.isEmpty() ||
        outputInventory.any { !(it instanceof String) || !(it as String).trim() } ||
        outputInventory.size() != (outputInventory as Set).size())
    throw new IllegalStateException("Primary classifier output inventory is invalid")
Set<String> declaredOutputClasses = outputInventory as Set<String>
Set<String> biologicalWithIgnore = new LinkedHashSet<String>(biologicalOutputClasses)
biologicalWithIgnore.add("Ignore")
if (!(declaredOutputClasses == biologicalOutputClasses ||
        declaredOutputClasses == biologicalWithIgnore))
    throw new IllegalStateException(
        "Primary classifier output inventory must be configured biological classes with optional Ignore"
    )
if (classifierConfig.require_ignore_training == true &&
        !declaredOutputClasses.contains("Ignore"))
    throw new IllegalStateException(
        "Primary classifier requires Ignore output when Ignore training is required"
    )
Set<String> allowed = labels.findAll { displayName, classId ->
    declaredOutputClasses.contains(classId)
}.keySet() as Set<String>
if (declaredOutputClasses.contains("Ignore")) allowed.add("Ignore")

def classifier
Map kronosSupport=null
String owner = "primary_classifier|${runId}|${generationId}|${analysisDomainId}"
def hierarchy = getCurrentHierarchy()
List prior = getAnnotationObjects().findAll { object ->
    Map m = md(object)
    m.get("pipeline_owner") == owner && m.get("pipeline_run_id") == runId &&
        m.get("generation_id") == generationId
}
Set before = getAnnotationObjects().collect { it.getID() } as Set
// Our authenticated attempt owns one absent final table member.
Path roiOutputRoot = MifProjectStageBootstrapV1.outputRoot(request, config)
RoiInventory.prepareOutput(roiOutputRoot, envelope.invocation_id as String)
List created = []
try {
    if(Files.readString(artifactPath).contains("data.op.mif-kronos-native-v1")) {
        String helperName="compartment_analysis/kronos_bundle_support.groovy"
        def helperRows=authenticatedManifest.assets.findAll {it.path==helperName}
        Path helperPath=requestedBundleRoot.resolve(helperName)
        if(helperRows.size()!=1 || helperPath.toRealPath()!=helperPath || Files.size(helperPath)>1024*1024)
            throw new IllegalStateException("The registered KRONOS loader is unavailable")
        byte[] helperBytes=Files.readAllBytes(helperPath)
        if(MessageDigest.getInstance("SHA-256").digest(helperBytes).encodeHex().toString()!=helperRows[0].sha256)
            throw new IllegalStateException("The KRONOS loader differs from the selected script bundle")
        Map loader=new GroovyShell(this.class.classLoader).evaluate(new String(helperBytes,StandardCharsets.UTF_8),"kronos_bundle_support.groovy") as Map
        kronosSupport=loader.load(requestedBundleRoot)
        kronosSupport.resources.bind(boundImageData,[[id:analysisDomainId,geometry:domain.ROI.geometry,revision:analysisDomainRevisionSha256,split:"inference"]])
    }
    classifier = loadPixelClassifier(artifactPath.toString())
    selectObjects(domain)
    createAnnotationsFromPixelClassifier(classifier, 0.0d, 0.0d, "SPLIT")
    created = getAnnotationObjects().findAll { !before.contains(it.getID()) }
    if (created.isEmpty()) throw new IllegalStateException("Primary classifier created no compartment annotations")
    created.each { object ->
        String classification = object.getClassification()
        if (!allowed.contains(classification))
            throw new IllegalStateException("Primary classifier emitted unconfigured label ${classification}")
        if (!object.getROI().getGeometry().coveredBy(domain.getROI().getGeometry()))
            throw new IllegalStateException("Primary output crosses its exact analysis-domain boundary")
    }
    if (!prior.isEmpty()) hierarchy.removeObjects(prior, false)
    created.sort { a, b -> a.getROI().getGeometry().toText() <=> b.getROI().getGeometry().toText() }
    created.each { object ->
        String classification = object.getClassification()
        String primaryClassId
        if (classification == "Ignore") primaryClassId = "Ignore"
        else {
            primaryClassId = labels.get(classification)
            if (primaryClassId == null)
                throw new IllegalStateException("Primary classifier emitted unconfigured label ${classification}")
        }
        domain.addChildObject(object)
        Map m = md(object)
        String stableKey = "${owner}|${classification}|${object.getROI().getGeometry().toText()}"
        m.put("roi_id", "roi-${UUID.nameUUIDFromBytes(stableKey.getBytes(StandardCharsets.UTF_8))}")
        m.put("parent_roi_id", analysisDomainId)
        m.put("parent_path_object_id", analysisDomainPathObjectId)
        m.put("analysis_domain_id", analysisDomainId)
        m.put("analysis_domain_kind", domainRevisionEvidence.kind as String)
        m.put("analysis_domain_revision", Integer.toString(
            (request.analysis_domain_revision as Number).intValue()
        ))
        m.put("analysis_domain_revision_sha256", analysisDomainRevisionSha256)
        m.put("analysis_domain_path_object_id", analysisDomainPathObjectId)
        m.put("domain_set_sha256", request.domain_set_sha256 as String)
        m.put("material_parent_type", "analysis_domain")
        m.put("material_parent_type_id", "AnalysisDomain")
        m.put("material_parent_revision_sha256", analysisDomainRevisionSha256)
        m.put("slide_id", slideId);
        m.put("section_id", sectionId)
        m.put("generation_id", generationId);
        m.put("pipeline_stage", "primary_classifier")
        m.put("pipeline_owner", owner);
        m.put("pipeline_run_id", runId)
        m.put("classifier_status", classifierReviewStatus)
        m.put("review_status", classifierReviewStatus)
        m.put("pipeline_active", "true")
        m.put("roi_level", "primary");
        m.put("primary_class_id", primaryClassId)
        m.put("classifier_id", classifierApplication.classifier_id as String)
        m.put("classifier_version", classifierVersion)
        if (movingAssistance != null) {
            m.put("source", "registered_reference_assisted")
            m.put("registration_id", movingAssistance.registration_id as String)
            m.put("registration_qc", gson.toJson(movingAssistance.registration_qc))
            m.put("moving_assistance_decision_id", movingAssistance.decision_id as String)
            m.put("moving_assistance_decision_source", movingAssistance.decision_source as String)
            m.put("moving_assistance_decided_by", movingAssistance.decided_by as String)
        } else m.put("source", "classifier")
        m.put("coordinate_system", "global_level0")
        Geometry normalizedObject = object.getROI().getGeometry().copy();
        normalizedObject.normalize()
        String objectGeometrySha256 = MessageDigest.getInstance("SHA-256").digest(
            new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalizedObject)
        ).encodeHex().toString()
        m.put("geometry_sha256", objectGeometrySha256)
        m.put("object_revision_sha256", canonicalSha256([
            analysis_domain_id: analysisDomainId,
            analysis_domain_revision_sha256: analysisDomainRevisionSha256,
            analysis_domain_set_sha256: request.domain_set_sha256,
            classification: object.getClassification(),
            geometry_sha256: objectGeometrySha256,
            parent_path_object_id: object.getParent().getID().toString(),
            path_object_id: object.getID().toString(),
            pipeline_active: m.get("pipeline_active"),
            type: "primary",
            type_id: m.get("primary_class_id"),
        ]))
    }
} catch (Exception error) {
    if (!created.isEmpty()) hierarchy.removeObjects(created, true)
    throw error
} finally {
    if(kronosSupport!=null)kronosSupport.resources.closeImage(boundImageData)
}
fireHierarchyUpdate()

def atomicJson = { Path path, Object value ->
    Files.createDirectories(path.parent)
    Path temp = path.resolveSibling(".${path.fileName}.${UUID.randomUUID()}.tmp")
    Files.writeString(temp, wireGson.toJson(value) + "\n", StandardCharsets.UTF_8)
    try { Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
    catch (AtomicMoveNotSupportedException ignored) { Files.move(temp, path, StandardCopyOption.REPLACE_EXISTING) }
}
def persistCurrentEntryState = {
    if (!mifAuthenticatedHeadlessProjectStage ||
            !boundImageData.isChanged()) return
    boundEntry.saveImageData(boundImageData)
    boundProject.syncChanges()
}
String reviewStatus = classifierReviewStatus
// We sort the existing collection once and stream its four typed columns.
// The completed table precedes saved-state and envelope publication.
created.sort { a, b -> (md(a).get("roi_id") as String) <=> (md(b).get("roi_id") as String) }
Map roiInventory = RoiInventory.write(roiOutputRoot, envelope.invocation_id as String, created,
    { object -> new RoiInventory.Row(md(object).get("roi_id") as String,
        object.getID().toString(), md(object).get("object_revision_sha256") as String,
        md(object).get("primary_class_id") as String) } as java.util.function.Function)
Map outputs = [
    roi_inventory: roiInventory,
    object_count: created.size(), section_id: sectionId,
    analysis_domain_id: analysisDomainId,
    analysis_domain_revision: (request.analysis_domain_revision as Number).intValue(),
    analysis_domain_revision_sha256: analysisDomainRevisionSha256,
    analysis_domain_path_object_id: analysisDomainPathObjectId,
    domain_set_sha256: request.domain_set_sha256,
    coordinate_system: "global_level0", classifier_id: classifierApplication.classifier_id,
    classifier_version: classifierVersion, classifier_sha256: classifierSha,
    classifier_kind: classifierApplication.classifier_kind,
    classifier_record_set_ref_sha256: classifierApplication.classifier_record_set_ref_sha256,
    classifier_record_set_sha256: classifierApplication.classifier_record_set_sha256,
    selected_output_id: classifierApplication.output_id,
    selected_classifier_record_sha256: classifierApplication.classifier_record_sha256,
    application_request_sha256: classifierApplication.application_request_sha256,
    target_project_identity_sha256: classifierApplication.target_project_identity_sha256,
    deployment_binding_set_sha256: classifierApplication.deployment_binding_set_sha256,
    target_evidence_set_sha256: classifierApplication.target_evidence_set_sha256,
    project_relative_path: projectRelativePath,
    native_classifier_sha256: classifierApplication.native_classifier_sha256,
    native_classifier_byte_count: classifierApplication.native_classifier_byte_count,
    pixel_classifier_definition_sha256: classifierApplication.pixel_classifier_definition_sha256,
    validation_domain_id: currentClassifierDefinition.domain_id,
    channel_roles: channelRoles, material_context_hash: configHash,
    validation_status: classifierConfig.validation_status,
    validation_record_id: classifierConfig.validation_record_id,
    qc_passed: classifierConfig.qc_passed,
    qc_record_id: classifierConfig.qc_record_id,
    material_context_matches: true,
    review_status: reviewStatus, review_action: reviewStatus == "accepted" ? "accept" : null
]
outputs.classifier_definition = currentClassifierDefinition
persistCurrentEntryState()
atomicJson(manifestPath, [schema_version: "roi-generation-manifest/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, generation_ids: [generationId],
    generations: [], object_outputs: outputs])
String manifestSha = sha256(manifestPath)
Map result = [status: "completed", stage: envelope.stage, slide_id: slideId,
    generation_id: generationId, interaction_reason: null, affected_object_ids: [],
    message: null, allowed_actions: [], resume_context: null, checkpoint_id: null,
    outputs: outputs, error: null]
mifPublishProjectStageResult(resultPath, [schema_version: "roi-stage-result/v1", invocation_id: envelope.invocation_id,
    run_id: runId, stage: envelope.stage, slide_id: slideId, result: result,
    generation_manifest: [path: manifestPath.fileName.toString(), sha256: manifestSha]])
