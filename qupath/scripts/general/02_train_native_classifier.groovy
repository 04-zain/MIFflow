import org.mif.cells.NativeCellScope
import java.util.function.Consumer
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonPrimitive
import com.google.gson.ToNumberPolicy
import java.awt.image.BufferedImage
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.Collections
import java.util.IdentityHashMap
import java.util.Locale
import java.util.TreeMap
import java.util.UUID
import org.bytedeco.opencv.global.opencv_core
import org.bytedeco.opencv.global.opencv_ml
import org.bytedeco.opencv.opencv_ml.SVM
import org.mif.classification.OpenCvTraining
import org.mif.classification.FeaturePreparation
import org.mif.classification.PixelTrainingRows
import org.mif.classification.ObjectTrainingRows
import org.mif.classification.ClassifierPrecisionRecall
import org.mif.classification.ClassifierEvaluation
import org.bytedeco.opencv.opencv_ml.TrainData
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.geom.Geometry
import qupath.lib.classifiers.object.ObjectClassifier
import qupath.lib.classifiers.pixel.PixelClassifier
import qupath.lib.classifiers.pixel.PixelClassifierMetadata
import qupath.lib.common.GeneralTools
import qupath.lib.images.ImageData
import qupath.lib.io.GsonTools
import qupath.lib.images.servers.ImageServerMetadata
import qupath.lib.images.servers.ImageServerMetadata.ChannelType
import qupath.lib.images.servers.PixelCalibration
import qupath.lib.images.servers.TileRequest
import qupath.lib.objects.PathObject
import qupath.lib.objects.PathObjects
import qupath.lib.objects.classes.PathClass
import qupath.lib.objects.classes.PathClassFactory
import qupath.lib.objects.hierarchy.PathObjectHierarchy
import qupath.lib.projects.Project
import qupath.lib.projects.ProjectImageEntry
import qupath.lib.projects.ResourceManager
import qupath.opencv.ml.OpenCVClassifiers
import qupath.opencv.ml.OpenCVClassifiers.OpenCVStatModel
import qupath.opencv.ml.objects.OpenCVMLClassifier
import qupath.opencv.ops.ImageDataOp
import qupath.opencv.ops.ImageDataServer
import qupath.opencv.ops.ImageOps
import qupath.opencv.ml.pixel.PixelClassifiers
import qupath.process.gui.commands.ml.BoundaryStrategy
import qupath.process.gui.commands.ml.PixelClassifierTraining

import static qupath.lib.scripting.QP.getProject


/**
 * Standalone, contained, training-only QuPath 0.7 native classifier fitter.
 *
 * Python owns request issuance and later artifact receipt.  This asset treats
 * the request as hostile bytes, reauthenticates the live project material,
 * fits once, writes one fresh project resource through QuPath's manager, and
 * emits only a completed observation outside the project.
 */
final class MifNativeTrainingAssetV1 {
    private static Map supervisedSupport
    private static Map cellFeatureSupport
    private static Map cellWorkers
    private static Map markerStatistics
    private static Path objectFeatureCache
    private static final Gson GSON = new GsonBuilder()
        .disableHtmlEscaping()
        .serializeNulls()
        .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE)
        .create()
    private static final int MAX_REQUEST_BYTES = 16 * 1024 * 1024
    private static final long MAX_CLASSIFIER_BYTES =
        64L * 1024L * 1024L * 1024L
    private static final long MAX_PIXEL_TILE_REQUESTS = 250000L
    private static final String PIXEL_RESOURCE_MESSAGE =
        "Training would need too much memory. Use smaller training regions or a coarser training resolution."
    private static final String SCRIPT_ASSET_ID =
        "qupath/scripts/general/02_train_native_classifier.groovy"
    private static final String PROJECT_IDENTITY_KEY =
        "mif_pipeline.project_identity_record_v1"
    private static final Set<String> ENVIRONMENT_NAMES = [
        "MIF_QUPATH_NATIVE_TRAINING_CONTEXT_VERSION",
        "MIF_QUPATH_NATIVE_TRAINING_REQUEST_PATH",
        "MIF_QUPATH_NATIVE_TRAINING_RESULT_PATH",
        "MIF_QUPATH_NATIVE_TRAINING_REQUEST_SHA256",
        "MIF_QUPATH_PROJECT_LAUNCH_NONCE",
    ] as Set<String>
    private static final Set<String> REQUEST_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256",
        "workspace_storage_scope_sha256", "active_mutation_run_sha256",
        "project_identity_sha256", "acquisition_record_sha256",
        "acquisition_session_challenge_sha256", "artifact_inventory_sha256",
        "mutation_request_sha256", "mutation_authorization_sha256",
        "fencing_token_sha256", "fit_plan_sha256", "fit_execution_sha256",
        "classifier_id", "classifier_kind", "classifier_role", "classifier_engine",
        "native_classifier_type", "classifier_version", "resource_name",
        "project_relative_path", "required_classes",
        "eligible_parent_classes", "eligible_material_parent_type_ids", "training_entries",
        "training_analysis_domain_bindings",
        "training_material_evidence_sha256", "feature_spec",
        "feature_spec_sha256",
        "model_configuration", "model_configuration_sha256", "random_seed",
        "expected_qupath_version", "script_asset_id", "script_sha256",
        "request_member_ref", "result_member_ref", "launch_nonce_sha256",
        "request_envelope_sha256",
    ] as Set<String>
    private static final Set<String> MEMBER_REF_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256", "family",
        "key_sha256", "suffix", "media_type",
    ] as Set<String>
    private static final Set<String> BINDING_FIELDS = [
        "project_entry_id", "sample_id", "slide_id", "analysis_domain_id",
        "analysis_domain_revision", "analysis_domain_revision_sha256",
        "domain_set_sha256",
    ] as Set<String>
    private static final Set<String> ENTRY_STATE_FIELDS = [
        "project_entry_id", "sample_id", "slide_id", "subject_id", "split",
        "image_identity_sha256", "analysis_domain_binding_sha256s",
        "analysis_domain_binding_set_sha256",
        "source_predecessor_artifact_ref_sha256s",
        "saved_state_before_sha256", "saved_state_after_sha256",
        "post_binding_saved_state_sha256", "entry_snapshot_set_sha256",
        "entry_state_sha256",
    ] as Set<String>
    private static final Set<String> PIXEL_ROW_FIELDS = [
        "schema_version", "project_entry_id", "annotation_uuid", "class_id",
        "path_class_id", "normalized_roi_sha256", "material_parent_id",
        "material_parent_type", "material_parent_type_id",
        "material_parent_revision_sha256", "split", "analysis_domain_id",
        "analysis_domain_revision_sha256", "domain_set_sha256", "cell_ids",
        "cell_evidence_sha256s", "segmentation_generation_id",
        "measurement_generation_id", "cell_selection_rule",
        "cell_selection_sha256", "evidence_sha256",
    ] as Set<String>
    private static final Set<String> RTREES_FIELDS = [
        "max_depth", "min_sample_count", "use_1se_rule", "active_var_count",
        "max_trees", "termination_epsilon",
        "calculate_variable_importance", "cv_folds", "use_surrogates",
    ] as Set<String>
    private static final Set<String> OUTPUT_LABEL_FIELDS = [
        "canonical_class_id", "qupath_label",
    ] as Set<String>
    private static final Set<String> OBJECT_SNAPSHOT_FIELDS = [
        "project_entry_id", "sample_id", "slide_id", "subject_id", "split",
        "image_identity_sha256", "section_id", "pipeline_run_id",
        "segmentation_generation_id", "measurement_generation_id",
        "analysis_domain_binding_sha256s",
        "analysis_domain_binding_set_sha256", "coordinate_system",
        "point_selection_radius_level0",
        "training_cell_to_analysis_domain_mapping_sha256", "class_ids",
        "feature_schema", "point_evidence", "eligible_cells",
        "mapped_evidence", "snapshot_sha256",
    ] as Set<String>
    private static final Set<String> OBJECT_TRAINING_CELL_FIELDS = [
        "cell_id", "expected_class_id", "analysis_domain_id", "slide_id",
        "section_id", "measurement_values", "measurement_vector_sha256",
    ] as Set<String>
    private static final Set<String> OBJECT_POINT_FIELDS = [
        "point_id", "cell_id", "slide_id", "section_id", "x_level0", "y_level0",
        "class_id", "evidence_set", "spatial_region_id",
    ] as Set<String>
    private static final Set<String> OBJECT_MAPPED_POINT_FIELDS =
        (OBJECT_POINT_FIELDS + [
            "cell_id", "cell_x_level0", "cell_y_level0",
            "distance_level0",
        ]) as Set<String>
    private static final Set<String> OBJECT_CELL_FIELDS = [
        "cell_id", "slide_id", "section_id", "analysis_domain_id",
        "analysis_domain_kind", "x_level0", "y_level0", "x_um", "y_um",
        "nucleus_geometry", "cell_geometry", "primary_compartment",
        "secondary_roi_ids", "marker_measurements", "marker_states",
        "primary_phenotype", "phenotype_components", "phenotype_source",
        "cluster_id", "tissue_roi_id", "tissue_generation_id",
        "phenotyping_generation_id", "phenotype_confidence", "cluster_label",
    ] as Set<String>
    private static final Set<String> OBJECT_CELL_MEMBERSHIP_FIELDS = [
        "pipeline_run_id", "membership_run_id", "membership_generation_id",
    ] as Set<String>

    private static void require(boolean condition, String message) {
        if (!condition)
            throw new IllegalStateException(message)
    }

    private static void requireClosed(Object value, Set<String> fields,
                                      String label) {
        require(value instanceof Map && (value as Map).keySet() == fields,
            "${label} is not a closed object")
    }

    private static String requireText(Object value, String label) {
        require(value instanceof String && !(value as String).isEmpty() &&
            value == (value as String).trim() &&
            (value as String).indexOf("\u0000") < 0 &&
            (value as String).getBytes(StandardCharsets.UTF_8).length <= 4096,
            "${label} must be nonblank text")
        value as String
    }

    private static String requireDigest(Object value, String label) {
        String result = requireText(value, label)
        require(result ==~ /[0-9a-f]{64}/,
            "${label} must be lowercase SHA-256")
        result
    }

    private static int requireInteger(Object value, String label,
                                      int minimum = 0) {
        require(value instanceof Long,
            "${label} must be an integer")
        long result = value as long
        require(result >= minimum && result <= Integer.MAX_VALUE,
            "${label} must be an in-range integer")
        (int) result
    }

    private static int requireOutputLabel(Object value,
                                          boolean objectFamily) {
        int result = requireInteger(value, "qupath_label")
        require(objectFamily || result <= 255,
            "pixel qupath_label exceeds UINT8 serialization")
        result
    }

    private static double requireFinite(Object value, String label,
                                        double minimum = 0.0d,
                                        boolean positive = false) {
        require(value instanceof Number,
            "${label} must be numeric")
        double result = (value as Number).doubleValue()
        require(Double.isFinite(result) && result >= minimum &&
            (!positive || result > minimum),
            "${label} must be finite and in range")
        result
    }

    private static boolean requireBoolean(Object value, String label) {
        require(value instanceof Boolean, "${label} must be boolean")
        value as boolean
    }

    private static List<String> requireTextList(Object value, String label,
                                                boolean sorted = false) {
        require(value instanceof List && !(value as List).isEmpty(),
            "${label} must be a nonempty list")
        List<String> result = (value as List).collect {
            requireText(it, label)
        }
        require(result.toSet().size() == result.size(),
            "${label} must be unique")
        if (sorted)
            require(result == result.toSorted(), "${label} must be sorted")
        result
    }

    private static Object canonicalValue(Object value) {
        if (value instanceof Map) {
            Map<String, Object> sorted = new TreeMap<>()
            (value as Map).each { key, nested ->
                require(key instanceof String, "canonical JSON key is invalid")
                sorted.put(key as String, canonicalValue(nested))
            }
            return sorted
        }
        if (value instanceof List)
            return (value as List).collect { canonicalValue(it) }
        if (value instanceof Double || value instanceof Float)
            require(Double.isFinite((value as Number).doubleValue()),
                "canonical JSON contains a nonfinite number")
        return value
    }

    private static byte[] canonicalBytes(Object value) {
        GSON.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8)
    }

    private static String sha256Bytes(byte[] value) {
        MessageDigest.getInstance("SHA-256").digest(value)
            .encodeHex().toString()
    }

    private static String canonicalSha256(Object value) {
        sha256Bytes(canonicalBytes(value))
    }

    private static String sha256File(Path path) {
        MessageDigest digest = MessageDigest.getInstance("SHA-256")
        path.toFile().withInputStream { input ->
            DigestInputStream stream = new DigestInputStream(input, digest)
            byte[] buffer = new byte[1024 * 1024]
            while (stream.read(buffer) >= 0) { }
        }
        digest.digest().encodeHex().toString()
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

    private static Path environmentPath(Map<String, String> environment,
                                        String name) {
        String raw = requireText(environment[name], name)
        Path path
        try {
            path = Path.of(raw)
        } catch (RuntimeException error) {
            throw new IllegalStateException("${name} is not a native path", error)
        }
        require(path.isAbsolute() && path == path.toAbsolutePath().normalize(),
            "${name} must be absolute and normalized")
        path
    }

    /** Check existing selected-family ancestry; QuPath creates missing folders. */
    private static Path validateClassifierDirectory(Path projectRoot,
                                                     String nativeFamily) {
        require(nativeFamily in ["pixel", "object"] &&
            Files.isDirectory(projectRoot, LinkOption.NOFOLLOW_LINKS) &&
            !Files.isSymbolicLink(projectRoot),
            "native classifier project root or family is invalid")
        Path classifierRoot = projectRoot.resolve("classifiers")
        Path familyRoot = classifierRoot.resolve("${nativeFamily}_classifiers")
        [classifierRoot, familyRoot].each { path ->
            require(Files.notExists(path, LinkOption.NOFOLLOW_LINKS) ||
                (Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS) &&
                    !Files.isSymbolicLink(path)),
                "native classifier parent is linked or not a directory")
        }
        familyRoot
    }

    private static final class AuthenticatedRequest {
        final Map request

        AuthenticatedRequest(Map request) {
            this.request = request
        }
    }

    private static byte[] readAuthenticatedRequestBytes(Path requestPath) {
        ByteArrayOutputStream output = new ByteArrayOutputStream()
        try (FileChannel channel = FileChannel.open(requestPath,
                StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)) {
            long openedSize = channel.size()
            require(openedSize > 0L && openedSize <= MAX_REQUEST_BYTES,
                "native-training request size is invalid")
            ByteBuffer buffer = ByteBuffer.allocate(64 * 1024)
            while (true) {
                int remaining = MAX_REQUEST_BYTES + 1 - output.size()
                require(remaining > 0,
                    "native-training request size is invalid")
                buffer.clear()
                buffer.limit(Math.min(buffer.capacity(), remaining))
                int count = channel.read(buffer)
                if (count < 0)
                    break
                if (count == 0)
                    continue
                output.write(buffer.array(), 0, count)
                require(output.size() <= MAX_REQUEST_BYTES,
                    "native-training request size is invalid")
            }
        } catch (IllegalStateException error) {
            throw error
        } catch (IOException error) {
            throw new IllegalStateException(
                "native-training request member is not a bounded regular file",
                error)
        }
        byte[] bytes = output.toByteArray()
        require(bytes.length > 0,
            "native-training request size is invalid")
        bytes
    }

    private static AuthenticatedRequest parseCanonicalRequest(
            Path requestPath, String expectedSha256) {
        byte[] bytes = readAuthenticatedRequestBytes(requestPath)
        require(sha256Bytes(bytes) == requireDigest(expectedSha256,
                "published native-training request SHA-256"),
            "native-training request bytes differ from the published member")
        Object parsed
        try {
            parsed = GSON.fromJson(strictUtf8(bytes, "native-training request"),
                Object)
        } catch (RuntimeException error) {
            throw new IllegalStateException("native-training request JSON is invalid",
                error)
        }
        requireClosed(parsed, REQUEST_FIELDS, "native-training request")
        Map request = parsed as Map
        requireDigest(request.request_envelope_sha256,
            "request_envelope_sha256")
        new AuthenticatedRequest(request)
    }

    private static void validateRuntimeExchangeRef(Map request, Object rawRef,
                                                   String direction,
                                                   Path memberPath) {
        requireClosed(rawRef, MEMBER_REF_FIELDS,
            "${direction} runtime exchange ref")
        Map ref = rawRef as Map
        require(ref.schema_name == "mif-authority-member-ref" &&
            ref.schema_version == 2L &&
            ref.authority_layout_sha256 == request.authority_layout_sha256 &&
            ref.family == "runtime_exchange" && ref.suffix == "json" &&
            ref.media_type == "application/json",
            "${direction} runtime exchange ref is invalid")
        String key = requireDigest(ref.key_sha256,
            "${direction} runtime exchange key")
        require(memberPath.fileName.toString() ==
            "runtime_exchange-${key}.json",
            "${direction} runtime exchange physical member is invalid")
    }

    /** Match the runtime's 32-byte launch identity after decoding its transport text.
     * We carry the nonce as hexadecimal in the environment and hash the decoded
     * bytes, using the same representation as the prepared launch authority.
     */
    private static void validateLaunchNonce(Map request, String rawNonce) {
        require(rawNonce != null && rawNonce ==~ /[0-9a-f]{64}/,
            "native-training launch nonce must contain 64 lowercase hexadecimal characters")
        byte[] nonceBytes = rawNonce.decodeHex()
        require(sha256Bytes(nonceBytes) ==
            requireDigest(request.launch_nonce_sha256, "launch_nonce_sha256"),
            "native-training launch nonce is foreign")
    }

    private static void validateProjectIdentity(Project project, Map request) {
        Object raw = project.getMetadata().get(PROJECT_IDENTITY_KEY)
        require(raw instanceof String,
            "managed project identity metadata is missing")
        byte[] bytes = (raw as String).getBytes(StandardCharsets.UTF_8)
        Map identity
        try {
            identity = GSON.fromJson(strictUtf8(bytes, "project identity"),
                Map) as Map
        } catch (RuntimeException error) {
            throw new IllegalStateException("project identity metadata is invalid",
                error)
        }
        Set<String> fields = [
            "schema_name", "schema_version", "authority_layout_sha256",
            "project_identity_ref", "project_uuid", "origin_kind",
            "origin_qpproj_sha256", "predecessor_project_identity_sha256",
            "project_identity_sha256",
        ] as Set<String>
        requireClosed(identity, fields, "project identity metadata")
        Map unsigned = new TreeMap(identity)
        Object rawRef = unsigned.remove("project_identity_ref")
        String supplied = requireDigest(
            unsigned.remove("project_identity_sha256"),
            "project identity SHA-256")
        requireClosed(rawRef, MEMBER_REF_FIELDS, "project identity ref")
        Map ref = rawRef as Map
        Map refPreimage = [
            authority_layout_sha256: request.authority_layout_sha256,
            family: "project_identity",
            scope: [project_identity_sha256: supplied],
            natural_key: [:],
        ]
        require(ref.schema_name == "mif-authority-member-ref" &&
            ref.schema_version == 2L &&
            ref.authority_layout_sha256 == request.authority_layout_sha256 &&
            ref.family == "project_identity" && ref.suffix == "json" &&
            ref.media_type == "application/json" &&
            requireDigest(ref.key_sha256, "project identity ref key") ==
                canonicalSha256(refPreimage),
            "project identity ref is invalid")
        UUID projectUuid
        try {
            projectUuid = UUID.fromString(
                requireText(identity.project_uuid, "project UUID"))
        } catch (IllegalArgumentException error) {
            throw new IllegalStateException("project UUID is invalid", error)
        }
        require(projectUuid.toString() == identity.project_uuid &&
            projectUuid.version() == 4 && projectUuid.variant() == 2,
            "project UUID is not canonical RFC UUIDv4")
        String origin = identity.origin_kind as String
        boolean originShape =
            (origin == "created" && identity.origin_qpproj_sha256 == null &&
                identity.predecessor_project_identity_sha256 == null) ||
            (origin == "adopted_existing" &&
                identity.origin_qpproj_sha256 instanceof String &&
                identity.predecessor_project_identity_sha256 == null) ||
            (origin == "cloned_as_new" &&
                identity.origin_qpproj_sha256 instanceof String &&
                identity.predecessor_project_identity_sha256 instanceof String)
        require(originShape,
            "project identity origin evidence is invalid")
        if (identity.origin_qpproj_sha256 != null)
            requireDigest(identity.origin_qpproj_sha256,
                "origin qpproj SHA-256")
        if (identity.predecessor_project_identity_sha256 != null)
            requireDigest(identity.predecessor_project_identity_sha256,
                "predecessor project identity SHA-256")
        require(identity.schema_name == "mif-qupath-project-identity" &&
            identity.schema_version == 2L &&
            identity.authority_layout_sha256 ==
                request.authority_layout_sha256 &&
            supplied == request.project_identity_sha256 &&
            supplied == canonicalSha256(unsigned) &&
            java.util.Arrays.equals(bytes, canonicalBytes(identity)),
            "live project identity differs from the request")
    }

    private static List<Map> validateOutputLabels(Map configuration,
                                                  List<String> requiredClasses,
                                                  boolean objectFamily) {
        Object raw = configuration.output_labels
        requireClosed(raw, ["schema", "ordered_labels"] as Set<String>,
            "output_labels")
        Map output = raw as Map
        require(output.schema == "qupath-output-labels/v1" &&
            output.ordered_labels instanceof List &&
            !(output.ordered_labels as List).isEmpty(),
            "output_labels are invalid")
        List<Map> orderedLabels = (output.ordered_labels as List).collect {
            requireClosed(it, OUTPUT_LABEL_FIELDS, "output label")
            Map label = it as Map
            requireText(label.canonical_class_id, "canonical_class_id")
            requireOutputLabel(label.qupath_label, objectFamily)
            label
        }
        require(orderedLabels.collect { it.canonical_class_id }.toSet().size() ==
            orderedLabels.size() &&
            orderedLabels.collect { it.qupath_label }.toSet().size() ==
                orderedLabels.size(), "output labels must be unique")
        if (objectFamily) {
            // QuPath's OpenCVMLClassifier indexes this list with the native
            // integer response.  There is deliberately no response remap.
            orderedLabels.eachWithIndex { label, int index ->
                require(label.qupath_label == index,
                    "object qupath_label must equal its exact tuple position")
            }
            require(orderedLabels.collect { it.canonical_class_id } ==
                requiredClasses,
                "object output labels must equal required class order")
        }
        orderedLabels
    }

    private static Map validateRTrees(Map configuration) {
        requireClosed(configuration.rtrees, RTREES_FIELDS, "RTrees parameters")
        Map rtrees = configuration.rtrees as Map
        requireInteger(rtrees.max_depth, "max_depth", 1)
        requireInteger(rtrees.min_sample_count, "min_sample_count", 1)
        requireBoolean(rtrees.use_1se_rule, "use_1se_rule")
        requireInteger(rtrees.active_var_count, "active_var_count")
        int maxTrees = requireInteger(rtrees.max_trees, "max_trees")
        double epsilon = requireFinite(rtrees.termination_epsilon,
            "termination_epsilon")
        requireBoolean(rtrees.calculate_variable_importance,
            "calculate_variable_importance")
        require(requireInteger(rtrees.cv_folds, "cv_folds") == 0 &&
            requireBoolean(rtrees.use_surrogates, "use_surrogates") == false &&
            (maxTrees > 0 || epsilon > 0.0d),
            "RTrees termination or fixed controls are invalid")
        rtrees
    }

    private static Map validateSvm(Map configuration) {
        requireClosed(configuration.svm, [
            "model_type", "kernel", "c", "gamma", "max_iterations",
            "termination_epsilon",
        ] as Set<String>, "SVM parameters")
        Map svm = configuration.svm as Map
        require(svm.model_type == "C_SVC" && svm.kernel in ["LINEAR", "RBF"],
            "SVM model type or kernel is unsupported")
        requireFinite(svm.c, "SVM C", 0.0d, true)
        if (svm.kernel == "LINEAR")
            require(svm.gamma == null, "linear SVM gamma must be null")
        else
            requireFinite(svm.gamma, "SVM gamma", 0.0d, true)
        requireInteger(svm.max_iterations, "SVM maximum iterations", 1)
        requireFinite(svm.termination_epsilon, "SVM tolerance", 0.0d, true)
        svm
    }

    /** Check the model controls separately from the measured input convention. */
    private static void validateHead(Map config, boolean objectFamily) {
        String head=modelKey(config,objectFamily)
        if(head=="rtrees") { validateRTrees(config); return }
        if(head=="svm") { validateSvm(config); return }
        Map lr=config.logistic_regression as Map
        requireClosed(lr,["learning_rate","iterations","regularization","training_method","termination_epsilon"] as Set,"LR controls")
        requireFinite(lr.learning_rate,"LR learning rate",0d,true)
        requireInteger(lr.iterations,"LR iterations",1)
        require(lr.regularization in ["L2","DISABLE"] && lr.training_method=="BATCH","LR regularization or training method is unsupported")
        requireFinite(lr.termination_epsilon,"LR termination tolerance",0d,true)
    }

    /** Regenerate frozen embeddings before authenticating their measured values. */
    private static void prepareObjectFeatures(Map config,ImageData image,List cells,Map owner) {
        if(config.feature_source==null)return
        require(cellFeatureSupport!=null && cellWorkers!=null,"The cell encoder support is unavailable")
        cellFeatureSupport.prepare(image,cells,config.feature_source,
            [image_sha256:owner.image_identity_sha256,segmentation_generation_id:owner.segmentation_generation_id],
            objectFeatureCache,cellWorkers,null,owner.deferred_feature_evidence)
    }

    private static Map loadKronosSupport(Path projectFile, String expectedNativeScriptSha256=null) {
        Map loaded=loadRegisteredSupport(projectFile,expectedNativeScriptSha256,"compartment_analysis/kronos_bundle_support.groovy")
        loaded.api.load(loaded.bundle)
    }

    private static Map loadRegisteredSupport(Path projectFile,String expectedNativeScriptSha256,String relative) {
        Path root=projectFile.parent
        Path evidencePath=root.resolve("data/qupath-script-bundle.evidence.json")
        require(Files.isRegularFile(evidencePath,LinkOption.NOFOLLOW_LINKS) && Files.size(evidencePath)<=1024*1024,
            "The selected project script bundle is unavailable")
        Map evidence=new GsonBuilder().create().fromJson(Files.readString(evidencePath),Map.class)
        require(evidence.schema_version=="qupath-script-bundle-evidence/v1" && evidence.project_file==projectFile.toString(),
            "The script-bundle evidence belongs to a different project")
        String digest=requireDigest(evidence.bundle_sha256,"script bundle digest")
        Path bundle=root.resolve("scripts").resolve("bundle-"+digest)
        Path manifest=bundle.resolve("qupath-script-bundle.v1.json")
        require(bundle.toRealPath()==bundle && manifest.toRealPath()==manifest && Files.size(manifest)<=1024*1024,
            "The selected script bundle has an invalid path")
        byte[] manifestBytes=Files.readAllBytes(manifest)
        require(MessageDigest.getInstance("SHA-256").digest(manifestBytes).encodeHex().toString()==digest,
            "The script bundle manifest changed")
        Map content=new GsonBuilder().create().fromJson(new String(manifestBytes,StandardCharsets.UTF_8),Map.class)
        require(content.schema_version=="qupath-script-bundle/v1" && content.assets instanceof List,
            "The script bundle manifest is unsupported")
        if(expectedNativeScriptSha256!=null) {
            List mainRows=content.assets.findAll {it.path=="general/02_train_native_classifier.groovy"}
            require(mainRows.size()==1 && mainRows[0].sha256==expectedNativeScriptSha256,
                "The native fit script and project bundle are different revisions")
        }
        List matches=content.assets.findAll {it.path==relative}
        Path helper=bundle.resolve(relative)
        require(matches.size()==1 && helper.toRealPath()==helper && Files.size(helper)<=1024*1024,
            "The KRONOS bundle loader is absent")
        byte[] helperBytes=Files.readAllBytes(helper)
        require(MessageDigest.getInstance("SHA-256").digest(helperBytes).encodeHex().toString()==matches[0].sha256,
            "The KRONOS bundle loader differs from the registered asset")
        Map api=new GroovyShell(MifNativeTrainingAssetV1.class.classLoader).evaluate(new String(helperBytes,StandardCharsets.UTF_8),relative) as Map
        [api:api,bundle:bundle]
    }

    private static boolean isKronos(Map configuration) {
        configuration.configuration_schema in ["qupath-kronos-rtrees/v2","qupath-kronos-svm/v2"]
    }

    private static Map nativeConfiguration(Map full) {
        if(full.configuration_schema=="qupath-grouped-cross-validation/v1") {
            requireClosed(full,["configuration_schema","folds","scoring","candidates"] as Set,"grouped search configuration")
            int count=requireInteger(full.folds,"folds")
            require((count==0 || count>=2 && count<=10) && full.scoring=="subject_weighted_macro_f1/v1",
                "The grouped search fold count or scoring rule is invalid")
            require(full.candidates instanceof List && full.candidates.size()>=1 && full.candidates.size()<=12,
                "Choose 1–12 explicit candidate settings")
            List signatures=[],payloads=[]
            for(Map candidate:full.candidates) {
                require(candidate.configuration_schema in ["qupath-pixel-rtrees/v1","qupath-pixel-svm/v1","qupath-kronos-rtrees/v2","qupath-kronos-svm/v2", "qupath-object-rtrees/v1", "qupath-object-svm/v1", "qupath-object-logistic-regression/v1"],
                    "Grouped search does not admit nested searches or foreign classifier families")
                Map nativeConfig=nativeConfiguration(candidate)
                Map signature=new LinkedHashMap(candidate)
                Map nativeSignature=new LinkedHashMap(nativeConfig)
                nativeSignature.remove("rtrees");
                nativeSignature.remove("svm")
                nativeSignature.remove("logistic_regression")
                if(isKronos(candidate)) {signature.remove("graph");
                    signature.native_configuration=nativeSignature}
                else signature=nativeSignature
                signatures.add(signature);
                payloads.add(new Gson().toJson(candidate))
            }
            require(signatures.every{it==signatures.first()} && payloads.toSet().size()==payloads.size(),
                "Search candidates change data definitions or duplicate settings")
            return nativeConfiguration(full.candidates.first() as Map)
        }
        if(!isKronos(full)) return full
        requireClosed(full,["configuration_schema","native_configuration","encoder","graph"] as Set,"KRONOS configuration")
        require(full.native_configuration instanceof Map,"KRONOS native configuration is absent")
        Map config=full.native_configuration as Map
        String method=full.configuration_schema=="qupath-kronos-svm/v2"?"svm":"rtrees"
        require(config.configuration_schema=="qupath-pixel-${method}/v1","KRONOS native estimator differs from its selected route")
        require(config.maximum_training_samples==0L && config.reweight_samples==false && config.boundary_strategy=="skip" &&
            config.pca_retained_variance==0d && config.pca_normalize==false,"KRONOS requires unique patch training without pixel sampling, boundary labels or PCA")
        require(full.encoder instanceof Map,"KRONOS encoder settings are absent")
        Map encoder=full.encoder
        ["package_directory"].each { field ->
            require(Path.of(requireText(encoder[field],field)).isAbsolute(),"KRONOS runtime locations must be absolute local paths")
        }
        List command=requireTextList(encoder.worker_command,"KRONOS worker command",false)
        require(command.size()<=8 && Path.of(command[0]).isAbsolute(),"KRONOS application command is invalid")
        requireDigest(encoder.package_sha256,"KRONOS package digest");
        require(encoder.worker_release=="mif-pipeline-prototype-7","The KRONOS processing program needs the same application installation")
        List channels=requireTextList(encoder.channel_ids,"KRONOS channels",false)
        List markers=requireTextList(encoder.marker_names,"KRONOS markers",false)
        require(channels.size()<=32 && channels.toSet().size()==channels.size() && markers.size()==channels.size() &&
            markers.toSet().size()==markers.size(),"KRONOS channel/marker orders are invalid")
        require(encoder.intensity_divisors instanceof List && encoder.intensity_divisors.size()==channels.size() &&
            encoder.pixel_size_um instanceof List && encoder.pixel_size_um.size()==2,"KRONOS quantitative arrays disagree")
        (encoder.intensity_divisors+encoder.pixel_size_um).each {requireFinite(it,"KRONOS scale",0d,true)}
        require(encoder.pixel_size_um.every {Math.abs(it.doubleValue()-config.resolution_um_per_pixel.doubleValue())<=1e-9},
            "KRONOS and native sampling resolutions differ")
        if(encoder.nuclear_channel!=null)require(requireInteger(encoder.nuclear_channel,"nuclear channel")<channels.size(),"Invalid nuclear channel")
        require(requireInteger(encoder.threads,"KRONOS threads",1)<=8,"Too many KRONOS worker threads")
        int patchSize=requireInteger(encoder.patch_size,"KRONOS patch size")
        require(patchSize in [32,64,128,256],"KRONOS patch size must be 32, 64, 128 or 256")
        long tokenSide=patchSize.intdiv(16)
        long tokens=tokenSide*tokenSide*channels.size()+17L
        require(4L*(36L*tokens*tokens+12L*tokens*768L)<=requireInteger(encoder.activation_budget_bytes,"KRONOS activation budget",1),
            "The selected channels exceed the KRONOS activation budget")
        requireClosed(full.graph,["hidden_width","epochs","batch_size","learning_rate","weight_decay","random_seed","max_working_bytes"] as Set,"KRONOS graph")
        Map graph=full.graph
        require(requireInteger(graph.hidden_width,"graph width",1)<=768 && requireInteger(graph.epochs,"graph epochs",1)<=1000 &&
            requireInteger(graph.batch_size,"graph batch size",1)<=4096,"A graph setting exceeds its supported range")
        requireInteger(graph.random_seed,"graph seed");
        requireInteger(graph.max_working_bytes,"graph memory budget",1)
        require(requireFinite(graph.learning_rate,"graph learning rate",0d,true)<=1d && requireFinite(graph.weight_decay,"graph weight decay")<=1d,
            "Graph learning parameters are invalid")
        config
    }

    private static String modelKey(Map configuration, boolean objectFamily) {
        String family = objectFamily ? "object" : "pixel"
        if (configuration.configuration_schema == "qupath-${family}-rtrees/v1")
            return "rtrees"
        if (objectFamily && configuration.configuration_schema == "qupath-object-logistic-regression/v1")
            return "logistic_regression"
        require(configuration.configuration_schema == "qupath-${family}-svm/v1",
            "native model configuration schema is unsupported")
        "svm"
    }

    /** Bind material scope to the explicit role, independently of biological names. */
    private static List<String> validateMaterialParentScope(Map request,
                                                           boolean objectFamily) {
        String role = requireText(request.classifier_role, "classifier_role")
        List<String> parents = requireTextList(request.eligible_parent_classes,
            "eligible_parent_classes", true)
        if (objectFamily) {
            require(role == "supervised_phenotype" && parents == ["Cell"] &&
                request.eligible_material_parent_type_ids instanceof List &&
                request.eligible_material_parent_type_ids.isEmpty(),
                "object training role or material scope differs")
            return []
        }
        require(role in ["primary_compartment", "secondary_structure"],
            "pixel training classifier role is invalid")
        List<String> material = requireTextList(request.eligible_material_parent_type_ids,
            "eligible_material_parent_type_ids", true)
        List<String> expected = role == "primary_compartment" ? ["AnalysisDomain"] : parents
        require(material == expected &&
            (role != "primary_compartment" || parents == ["Tissue"]),
            "pixel training role or material scope differs")
        material
    }

    private static Map validateRequest(Map request) {
        require(request.schema_name == "mif-qupath-native-training-request" &&
            request.schema_version == 1L &&
            request.classifier_engine == "qupath_native" &&
            request.expected_qupath_version == "0.7.0" &&
            request.script_asset_id == SCRIPT_ASSET_ID,
            "native-training request schema or fixed runtime is invalid")
        [
            "authority_layout_sha256", "workspace_storage_scope_sha256",
            "active_mutation_run_sha256", "project_identity_sha256",
            "acquisition_record_sha256",
            "acquisition_session_challenge_sha256",
            "artifact_inventory_sha256", "mutation_request_sha256",
            "mutation_authorization_sha256", "fencing_token_sha256",
            "fit_plan_sha256", "fit_execution_sha256",
            "training_material_evidence_sha256",
            "feature_spec_sha256", "model_configuration_sha256",
            "script_sha256",
            "request_envelope_sha256",
        ].each { requireDigest(request[it as String], it as String) }
        requireText(request.classifier_id, "classifier_id")
        int randomSeed = requireInteger(request.random_seed, "random_seed")
        String fitExecution = request.fit_execution_sha256 as String
        String resourceName = "mif-${fitExecution}"
        require(request.classifier_version == "mif-fit-${fitExecution}" &&
            request.resource_name == resourceName,
            "native classifier resource identity is not deterministic")
        boolean objectFamily = request.classifier_kind ==
            "supervised_phenotype"
        require((objectFamily && request.native_classifier_type == "object") ||
            (request.classifier_kind == "pixel" &&
                request.native_classifier_type == "pixel"),
            "classifier kind and native family differ")
        String expectedPath = objectFamily ?
            "classifiers/object_classifiers/mif-${fitExecution}.json" :
            "classifiers/pixel_classifiers/mif-${fitExecution}.json"
        require(request.project_relative_path == expectedPath,
            "native classifier target is not deterministic")
        List<String> requiredClasses = requireTextList(
            request.required_classes, "required_classes", true)
        validateMaterialParentScope(request, objectFamily)
        require(request.model_configuration instanceof Map,
            "model configuration must be an object")
        Map configuration = nativeConfiguration(request.model_configuration as Map)
        validateHead(configuration, objectFamily)
        List<Map> labels = validateOutputLabels(configuration,
            requiredClasses, objectFamily)
        validateFeatureSpec(request, objectFamily)
        List candidates=request.model_configuration.configuration_schema=="qupath-grouped-cross-validation/v1" ? request.model_configuration.candidates : [request.model_configuration]
        for(Map candidate:candidates) {
            Map current=nativeConfiguration(candidate)
            validateHead(current,objectFamily)
            List<Map> candidateLabels=validateOutputLabels(current,requiredClasses,objectFamily)
            validateModelConfiguration(current,candidateLabels,requiredClasses,objectFamily)
            if(isKronos(candidate)) {
                require(!objectFamily,"KRONOS applies to tissue patches, not supervised cell objects")
                require(candidate.encoder.channel_ids==request.feature_spec.ordered_channels.collect {it.canonical_channel_id},
                    "KRONOS marker channels differ from the observed image channel order")
                require(candidate.graph.random_seed==request.random_seed,"The graph seed differs from the accepted model seed")
            }
        }
        validateModelConfiguration(configuration, labels, requiredClasses,
            objectFamily)
        if (objectFamily)
            require(request.feature_spec.normalization ==
                    configuration.normalization &&
                request.feature_spec.getOrDefault("pca_normalize",true)==configuration.getOrDefault("pca_normalize",true) &&
                request.feature_spec.pca_retained_variance ==
                    configuration.pca_retained_variance,
                "object feature preprocessing differs from configuration")
        require(randomSeed >= 0, "random seed is invalid")
        request
    }

    private static void validateFeatureSpec(Map request,
                                            boolean objectFamily) {
        Map feature = request.feature_spec as Map
        if (objectFamily) {
            Set fields = ["schema", "ordered_measurements", "normalization", "pca_retained_variance"] as Set
            if (feature.containsKey("pca_normalize")) {
                requireBoolean(feature.pca_normalize, "pca_normalize")
                fields.add("pca_normalize")
            }
            requireClosed(feature, fields, "object feature_spec")
            require(feature.schema == "qupath-object-feature-spec/v1" &&
                feature.ordered_measurements instanceof List &&
                !(feature.ordered_measurements as List).isEmpty(),
                "object feature_spec is invalid")
            List<String> names = (feature.ordered_measurements as List).collect {
                requireClosed(it, ["measurement_name", "semantic_role"] as Set,
                    "ordered measurement")
                requireText((it as Map).semantic_role, "semantic_role")
                requireText((it as Map).measurement_name, "measurement_name")
            }
            require(names.toSet().size() == names.size(),
                "ordered measurement names must be unique")
        } else {
            requireClosed(feature, ["schema", "ordered_channels"] as Set,
                "pixel feature_spec")
            require(feature.schema == "qupath-pixel-channel-features/v1" &&
                feature.ordered_channels instanceof List &&
                !(feature.ordered_channels as List).isEmpty(),
                "pixel feature_spec is invalid")
            List<Map> rows = feature.ordered_channels as List<Map>
            rows.each { row ->
                requireClosed(row, [
                    "canonical_channel_id", "qupath_channel_index",
                    "observed_qupath_channel_name",
                ] as Set, "ordered channel")
                requireText(row.canonical_channel_id, "canonical_channel_id")
                requireInteger(row.qupath_channel_index,
                    "qupath_channel_index")
                // Observed names are literal source evidence, including spaces.
                Object observedName = row.observed_qupath_channel_name
                require(observedName instanceof String &&
                    !(observedName as String).isEmpty() &&
                    (observedName as String).indexOf("\u0000") < 0 &&
                    (observedName as String).getBytes(StandardCharsets.UTF_8).length <= 4096,
                    "observed_qupath_channel_name must be bounded nonempty text")
            }
            require(rows.collect { it.canonical_channel_id }.toSet().size() ==
                rows.size() &&
                rows.collect { it.qupath_channel_index }.toSet().size() ==
                    rows.size(), "ordered channel identities must be unique")
        }
    }

    private static void validateModelConfiguration(Map configuration,
                                                   List<Map> labels,
                                                   List<String> requiredClasses,
                                                   boolean objectFamily) {
        String model = modelKey(configuration, objectFamily)
        if (objectFamily) {
            Set objectFields = [
                "configuration_schema", "object_filter",
                "training_annotations", "output_classes", "normalization",
                "pca_retained_variance", "multiclass", "output_labels",
                model,
            ] as Set
            if (configuration.containsKey("pca_normalize")) {
                objectFields.add("pca_normalize")
                requireBoolean(configuration.pca_normalize,"pca_normalize")
            }
            if(configuration.containsKey("feature_source")) {
                objectFields.add("feature_source")
                require(configuration.feature_source instanceof Map &&
                    configuration.feature_source.schema=="kronos-cell-feature-source/v1",
                    "KRONOS cell features require the exact encoder input convention")
            }
            requireClosed(configuration, objectFields, "object model configuration")
            require(configuration.object_filter == "cells" &&
                configuration.training_annotations == "points" &&
                configuration.output_classes == "selected" &&
                configuration.multiclass == false,
                "object model configuration fixed controls are invalid")
        } else {
            boolean classify = configuration.boundary_strategy == "classify"
            Set fields = [
                "configuration_schema", "resolution_um_per_pixel",
                "image_features",
                "maximum_training_samples", "reweight_samples",
                "normalization", "pca_retained_variance", "pca_normalize",
                "boundary_strategy", "boundary_thickness_px", "output_labels",
                model,
            ] as Set
            if (classify) fields.add("boundary_class_id")
            requireClosed(configuration, fields,
                "pixel model configuration")
            require(configuration.boundary_strategy in ["skip", "classify"],
                "pixel model configuration fixed controls are invalid")
            requireFinite(configuration.resolution_um_per_pixel,
                "resolution_um_per_pixel", 0.0d, true)
            Map imageFeatures = configuration.image_features as Map
            requireClosed(imageFeatures, ["include_intensity", "scales"] as Set,
                "pixel image features")
            requireBoolean(imageFeatures.include_intensity, "include_intensity")
            require(imageFeatures.scales instanceof List,
                "Image feature scales must be a list of sigma values and their filters.")
            // Reject invalid scale groups before extracting any training pixels.
            org.mif.classification.PixelFeatureOps.create([0] as int[],
                imageFeatures.include_intensity as boolean,
                org.mif.classification.PixelFeatureOps.scales(imageFeatures.scales),
                configuration.resolution_um_per_pixel.doubleValue())
            requireInteger(configuration.maximum_training_samples,
                "maximum_training_samples")
            requireBoolean(configuration.reweight_samples,
                "reweight_samples")
            requireBoolean(configuration.pca_normalize, "pca_normalize")
            double thickness = requireFinite(configuration.boundary_thickness_px,
                "boundary_thickness_px")
            List<String> ordinary = labels.collect {
                it.canonical_class_id as String
            }
            double pixelPca = requireFinite(
                configuration.pca_retained_variance,
                "pca_retained_variance")
            require(!(pixelPca == 0.0d &&
                configuration.pca_normalize as boolean),
                "pixel PCA normalization requires PCA")
            if (classify) {
                int boundary = requireInteger(configuration.boundary_class_id,
                    "boundary_class_id")
                require(thickness > 0.0d &&
                    labels.count { it.qupath_label == boundary } == 1 &&
                    ordinary.findAll { classId ->
                        labels.find { it.canonical_class_id == classId }
                            .qupath_label != boundary
                    }.toSet() == requiredClasses.toSet(),
                    "pixel boundary output closure is invalid")
            } else {
                require(thickness == 0.0d &&
                    ordinary.toSet() == requiredClasses.toSet(),
                    "pixel skip-boundary output closure is invalid")
            }
        }
        require(configuration.normalization in
            ["none", "mean_variance", "min_max"],
            "normalization is unsupported")
        double pca = requireFinite(configuration.pca_retained_variance,
            "pca_retained_variance")
        require(pca <= 1.0d, "pca_retained_variance is invalid")
    }

    private static List<Map> validateBindings(Map request) {
        require(request.training_analysis_domain_bindings instanceof List &&
            !(request.training_analysis_domain_bindings as List).isEmpty(),
            "training analysis-domain bindings are empty")
        List<Map> bindings =
            request.training_analysis_domain_bindings as List<Map>
        bindings.each { binding ->
            requireClosed(binding, BINDING_FIELDS,
                "training analysis-domain binding")
            ["project_entry_id", "sample_id", "slide_id"].each {
                requireText(binding[it], "binding ${it}")
            }
            requireDigest(binding.analysis_domain_id, "analysis_domain_id")
            requireInteger(binding.analysis_domain_revision,
                "analysis_domain_revision", 1)
            requireDigest(binding.analysis_domain_revision_sha256,
                "analysis_domain_revision_sha256")
            requireDigest(binding.domain_set_sha256, "domain_set_sha256")
        }
        require(bindings.collect { canonicalSha256(it) }.toSet().size() ==
            bindings.size(), "training analysis-domain bindings repeat")
        List<Map> canonical = bindings.toSorted { left, right ->
            (left.sample_id <=> right.sample_id) ?:
                (left.slide_id <=> right.slide_id) ?:
                (left.analysis_domain_id <=> right.analysis_domain_id) ?:
                (left.analysis_domain_revision <=>
                    right.analysis_domain_revision) ?:
                (left.project_entry_id <=> right.project_entry_id)
        }
        require(bindings == canonical,
            "training analysis-domain bindings are not canonical")
        bindings
    }

    private static List<Map> validateTrainingEntries(Map request,
                                                     List<Map> bindings) {
        require(request.training_entries instanceof List &&
            !(request.training_entries as List).isEmpty(),
            "training entries are empty")
        boolean objectFamily = request.native_classifier_type == "object"
        List<Map> entries = request.training_entries as List<Map>
        List<String> entryIds = []
        List<List<String>> objectKeys = []
        entries.each { entry ->
            if (objectFamily) {
                requireClosed(entry,
                    ["entry_kind", "snapshot", "training_cells"] as Set,
                    "object training entry")
                require(entry.entry_kind == "object",
                    "object training entry tag is invalid")
                validateObjectEntry(entry, request, bindings)
                entryIds << (entry.snapshot as Map).project_entry_id as String
                objectKeys << [entry.snapshot.project_entry_id as String,
                    entry.snapshot.section_id as String]
            } else {
                requireClosed(entry, [
                    "entry_kind", "entry_state", "evidence_rows",
                    "channel_features",
                ] as Set, "pixel training entry")
                require(entry.entry_kind == "pixel" &&
                    java.util.Arrays.equals(
                        canonicalBytes(entry.channel_features),
                        canonicalBytes(request.feature_spec)),
                    "pixel training entry channel projection changed")
                validatePixelEntry(entry, request, bindings)
                entryIds << (entry.entry_state as Map).project_entry_id as String
            }
        }
        def keys = objectFamily ? objectKeys : entryIds
        def canonicalKeys = objectFamily ? objectKeys.toSorted { left, right ->
            (left[0] <=> right[0]) ?: (left[1] <=> right[1])
        } : entryIds.toSorted()
        require(keys == canonicalKeys && keys.toSet().size() == keys.size(),
            "training entry IDs must be canonical and unique")
        require(bindings.collect { it.project_entry_id }.toSet() ==
            entryIds.toSet(),
            "analysis-domain bindings do not cover training entries")
        if (objectFamily) {
            require(entries.collectMany { it.snapshot.analysis_domain_binding_sha256s }.toSorted() ==
                bindings.collect { canonicalSha256(it) }.toSorted(),
                "object sections do not cover exact pooled domain bindings")
            Set observedClasses = entries.collectMany {
                it.training_cells.collect { cell -> cell.expected_class_id }
            } as Set
            require(observedClasses == (request.required_classes as List).toSet(),
                "Add saved training points for every declared cell type before fitting")
        }
        entries
    }

    /** Keep exact evidence bindings while checking the declared material inventory. */
    private static void validatePixelEntry(Map entry, Map request,
                                           List<Map> bindings) {
        requireClosed(entry.entry_state, ENTRY_STATE_FIELDS,
            "pixel training entry state")
        Map state = entry.entry_state as Map
        ["project_entry_id", "sample_id", "slide_id", "subject_id"].each {
            name -> requireText(state[name], "pixel entry state ${name}")
        }
        require(state.split == "training",
            "pixel entry is not training-only")
        [
            "image_identity_sha256",
            "analysis_domain_binding_set_sha256",
            "saved_state_before_sha256", "saved_state_after_sha256",
            "post_binding_saved_state_sha256", "entry_snapshot_set_sha256",
        ].each { name ->
            requireDigest(state[name], "pixel entry state ${name}")
        }
        require(state.analysis_domain_binding_sha256s instanceof List &&
            !(state.analysis_domain_binding_sha256s as List).isEmpty() &&
            state.source_predecessor_artifact_ref_sha256s instanceof List &&
            !(state.source_predecessor_artifact_ref_sha256s as List).isEmpty(),
            "pixel entry digest inventories are empty")
        List<String> bindingDigests =
            (state.analysis_domain_binding_sha256s as List).collect {
                requireDigest(it, "pixel entry binding digest")
            }
        List<String> predecessorDigests =
            (state.source_predecessor_artifact_ref_sha256s as List).collect {
                requireDigest(it, "pixel entry predecessor digest")
            }
        require(bindingDigests == bindingDigests.toSorted() &&
            bindingDigests.toSet().size() == bindingDigests.size() &&
            predecessorDigests == predecessorDigests.toSorted() &&
            predecessorDigests.toSet().size() == predecessorDigests.size(),
            "pixel entry digest inventories are not canonical")
        Map stateUnsigned = new TreeMap(state)
        String stateSha = requireDigest(stateUnsigned.remove("entry_state_sha256"),
            "entry_state_sha256")
        require(canonicalSha256(stateUnsigned) == stateSha,
            "pixel entry state digest is invalid")
        require(entry.evidence_rows instanceof List &&
            !(entry.evidence_rows as List).isEmpty(),
            "pixel entry evidence is empty")
        List<Map> rows = entry.evidence_rows as List<Map>
        rows.each { row ->
            requireClosed(row, PIXEL_ROW_FIELDS, "pixel evidence row")
            String annotationId = requireText(row.annotation_uuid,
                "pixel annotation UUID")
            try {
                require(UUID.fromString(annotationId).toString() == annotationId,
                    "pixel annotation UUID is not canonical")
            } catch (IllegalArgumentException error) {
                throw new IllegalStateException(
                    "pixel annotation UUID is invalid", error)
            }
            require(row.schema_version == "training-evidence-snapshot/v2" &&
                row.project_entry_id == state.project_entry_id &&
                row.split == "training" &&
                (request.required_classes as List).contains(row.class_id),
                "pixel evidence row is foreign or held out")
            require((request.eligible_material_parent_type_ids as List).contains(
                row.material_parent_type_id),
                "pixel evidence material parent is not eligible")
            requireText(row.path_class_id, "pixel evidence PathClass ID")
            requireText(row.material_parent_id, "pixel material parent ID")
            [
                "normalized_roi_sha256", "material_parent_revision_sha256",
                "analysis_domain_id", "analysis_domain_revision_sha256",
                "domain_set_sha256", "cell_selection_sha256",
            ].each { name -> requireDigest(row[name], name as String) }
            require(row.material_parent_type in
                    ["analysis_domain", "primary", "secondary"] &&
                ((row.material_parent_type == "analysis_domain" &&
                    row.material_parent_type_id == "AnalysisDomain" &&
                    row.material_parent_revision_sha256 ==
                        row.analysis_domain_revision_sha256) ||
                 (row.material_parent_type != "analysis_domain" &&
                    row.material_parent_type_id != "AnalysisDomain")),
                "pixel material-parent type is invalid")
            require(row.cell_ids instanceof List &&
                row.cell_evidence_sha256s instanceof List,
                "pixel cell evidence inventories must be arrays")
            List<String> cellIds = (row.cell_ids as List).collect {
                requireText(it, "pixel selected cell ID")
            }
            List<String> cellEvidence =
                (row.cell_evidence_sha256s as List).collect {
                    requireDigest(it, "pixel selected cell evidence")
                }
            require(cellIds == cellIds.toSorted() &&
                cellIds.toSet().size() == cellIds.size() &&
                cellEvidence.toSet().size() == cellEvidence.size() &&
                cellEvidence.size() == cellIds.size() &&
                row.cell_selection_rule ==
                    "explicit-hierarchy-descendant-cells/v1",
                "pixel selected-cell inventory is invalid")
            if (cellIds.isEmpty())
                require(row.segmentation_generation_id == null &&
                    row.measurement_generation_id == null,
                    "empty pixel cell selection declares a generation")
            else {
                requireText(row.segmentation_generation_id,
                    "pixel segmentation generation")
                requireText(row.measurement_generation_id,
                    "pixel measurement generation")
            }
            Map selection = [
                cell_ids: cellIds,
                cell_evidence_sha256s: cellEvidence,
                segmentation_generation_id: row.segmentation_generation_id,
                measurement_generation_id: row.measurement_generation_id,
                cell_selection_rule: row.cell_selection_rule,
            ]
            require(canonicalSha256(selection) == row.cell_selection_sha256,
                "pixel cell-selection digest is invalid")
            Map unsigned = new TreeMap(row)
            String evidence = requireDigest(unsigned.remove("evidence_sha256"),
                "evidence_sha256")
            require(canonicalSha256(unsigned) == evidence,
                "pixel evidence row digest is invalid")
            List<Map> matchingBindings = bindings.findAll { binding ->
                binding.project_entry_id == state.project_entry_id &&
                    binding.analysis_domain_id == row.analysis_domain_id &&
                    binding.analysis_domain_revision_sha256 ==
                        row.analysis_domain_revision_sha256 &&
                    binding.domain_set_sha256 == row.domain_set_sha256
            }
            require(matchingBindings.size() == 1,
                "pixel evidence row has no exact analysis-domain binding")
        }
        require(rows.collect { it.annotation_uuid }.toSet().size() == rows.size() &&
            rows == rows.toSorted { left, right ->
                (left.annotation_uuid <=> right.annotation_uuid) ?:
                    (left.evidence_sha256 <=> right.evidence_sha256)
            } &&
            canonicalSha256(rows) == state.entry_snapshot_set_sha256,
            "pixel evidence inventory is invalid")
        List<Map> ownBindings = bindings.findAll {
            it.project_entry_id == state.project_entry_id
        }
        require(canonicalSha256(ownBindings) ==
            state.analysis_domain_binding_set_sha256 &&
            ownBindings.collect { canonicalSha256(it) }.toSorted() ==
                state.analysis_domain_binding_sha256s &&
            ownBindings.every { binding ->
                binding.sample_id == state.sample_id &&
                    binding.slide_id == state.slide_id
            },
            "pixel entry domain bindings are invalid")
    }

    private static boolean sameFiniteDouble(Object left, Object right) {
        left instanceof Number && right instanceof Number &&
            Double.isFinite((left as Number).doubleValue()) &&
            Double.isFinite((right as Number).doubleValue()) &&
            Double.doubleToLongBits((left as Number).doubleValue()) ==
                Double.doubleToLongBits((right as Number).doubleValue())
    }

    private static boolean pendingCellStatistics(Map configuration) {
        Map source=nativeConfiguration(configuration)?.feature_source
        (source?.encoder?.additional_markers ?: []).any {it instanceof Map &&
            it.keySet()==["marker_name","marker_full_name","compartment","family","family_desc"].toSet()}
    }

    private static void validateDeferredInputShape(Map evidence,List ids) {
        requireClosed(evidence,["schema","source_intent_sha256","automatic_marker_names","cell_input_sha256"].toSet(),
            "deferred cell input evidence")
        require(evidence.schema=="kronos-cell-input-evidence/v1","Deferred cell input schema is unsupported")
        requireDigest(evidence.source_intent_sha256,"deferred cell source intent")
        List names=requireTextList(evidence.automatic_marker_names,"automatic marker names",true)
        require(names.toSet().size()==names.size() && evidence.cell_input_sha256 instanceof Map &&
            evidence.cell_input_sha256.keySet()==ids.toSet(),"Deferred cell input inventory differs")
        evidence.cell_input_sha256.values().each {requireDigest(it,"raw cell input")}
    }

    private static void validateObjectSnapshotAuthorization(
        Map entry, Map request, List<Map> ownBindings) {
        Map snapshot = entry.snapshot as Map
        boolean deferred=snapshot.containsKey("deferred_feature_evidence")
        List<Map> featureRows = snapshot.feature_schema as List<Map>
        require(java.util.Arrays.equals(canonicalBytes(featureRows),
                canonicalBytes(request.feature_spec.ordered_measurements)) &&
            snapshot.class_ids == request.required_classes,
            "object snapshot class or feature authority changed")
        List<String> measurementNames = featureRows.collect { row ->
            requireClosed(row, ["measurement_name", "semantic_role"] as Set,
                "object snapshot feature")
            requireText(row.semantic_role, "object feature semantic role")
            requireText(row.measurement_name, "object measurement name")
        }

        require(snapshot.point_evidence instanceof List,
            "object snapshot point evidence must be a list")
        List<Map> points = snapshot.point_evidence as List<Map>
        Map<String, Map> pointsById = new TreeMap<>()
        points.each { point ->
            requireClosed(point, OBJECT_POINT_FIELDS,
                "object point evidence")
            String pointId = requireText(point.point_id, "point ID")
            require(!pointsById.containsKey(pointId) &&
                point.slide_id == snapshot.slide_id &&
                point.section_id == snapshot.section_id &&
                point.evidence_set == "training" &&
                (request.required_classes as List).contains(point.class_id),
                "object point evidence is foreign, held out, or repeated")
            requireFinite(point.x_level0, "point x", -Double.MAX_VALUE)
            requireFinite(point.y_level0, "point y", -Double.MAX_VALUE)
            requireText(point.spatial_region_id, "point spatial region")
            pointsById[pointId] = point
        }
        require(points.collect { it.point_id } ==
            points.collect { it.point_id }.toSorted(),
            "object point evidence is not canonical")

        require(snapshot.eligible_cells instanceof List,
            "object snapshot eligible cells must be a list")
        List<Map> eligibleCells = snapshot.eligible_cells as List<Map>
        Map<String, Map> eligibleById = new TreeMap<>()
        Set<String> boundDomainIds = ownBindings.collect {
            it.analysis_domain_id as String
        } as Set<String>
        eligibleCells.each { cell ->
            require(cell instanceof Map &&
                ((cell as Map).keySet() == OBJECT_CELL_FIELDS ||
                 (cell as Map).keySet() ==
                    (OBJECT_CELL_FIELDS + OBJECT_CELL_MEMBERSHIP_FIELDS)),
                "eligible object cell is not a closed record")
            String cellId = requireText(cell.cell_id, "eligible cell ID")
            require(!eligibleById.containsKey(cellId) &&
                cell.slide_id == snapshot.slide_id &&
                cell.section_id == snapshot.section_id &&
                boundDomainIds.contains(cell.analysis_domain_id) &&
                cell.analysis_domain_kind in
                    ["selected_roi", "whole_tissue_section"],
                "eligible object cell is foreign or repeated")
            requireFinite(cell.x_level0, "eligible cell x",
                -Double.MAX_VALUE)
            requireFinite(cell.y_level0, "eligible cell y",
                -Double.MAX_VALUE)
            require(cell.marker_measurements instanceof Map,
                "eligible cell marker measurements are absent")
            Map measurements = cell.marker_measurements as Map
            if(deferred)require(measurements.isEmpty(),"Deferred cell evidence cannot contain precomputed embeddings")
            (deferred ? [] : measurementNames).each { name ->
                require(measurements.containsKey(name),
                    "eligible cell lacks ordered measurement ${name}")
                requireFinite(measurements[name],
                    "eligible cell measurement ${name}",
                    -Double.MAX_VALUE)
            }
            if ((cell as Map).keySet().containsAll(
                    OBJECT_CELL_MEMBERSHIP_FIELDS)) {
                OBJECT_CELL_MEMBERSHIP_FIELDS.each { name ->
                    requireText(cell[name], "eligible cell ${name}")
                }
            }
            eligibleById[cellId] = cell
        }
        List<String> eligibleIds = eligibleCells.collect {
            it.cell_id as String
        }
        require(eligibleIds == eligibleIds.toSorted(),
            "eligible object cells are not canonical")
        if(deferred)validateDeferredInputShape(snapshot.deferred_feature_evidence as Map,eligibleIds)

        require(snapshot.mapped_evidence instanceof List,
            "object snapshot mapped evidence must be a list")
        List<Map> mapped = snapshot.mapped_evidence as List<Map>
        Map<String, List<Map>> mappedByCell = new TreeMap<>()
        mapped.each { item ->
            requireClosed(item, OBJECT_MAPPED_POINT_FIELDS,
                "mapped object point evidence")
            String pointId = requireText(item.point_id, "mapped point ID")
            String cellId = requireText(item.cell_id, "mapped cell ID")
            Map point = pointsById[pointId]
            Map cell = eligibleById[cellId]
            require(point != null && cell != null &&
                OBJECT_POINT_FIELDS.every { field ->
                    item[field] == point[field]
                } && item.evidence_set == "training" &&
                sameFiniteDouble(item.cell_x_level0, cell.x_level0) &&
                sameFiniteDouble(item.cell_y_level0, cell.y_level0),
                "mapped point differs from exact point/cell evidence")
            requireFinite(item.distance_level0, "mapped point distance")
            if (!mappedByCell.containsKey(cellId))
                mappedByCell[cellId] = []
            mappedByCell[cellId] << item
        }
        List<String> mappedPointIds = mapped.collect {
            it.point_id as String
        }
        require(mappedPointIds == mappedPointIds.toSorted() &&
            mappedPointIds.toSet().size() == mappedPointIds.size() &&
            mappedPointIds == points.collect { it.point_id } &&
            mapped.collect { it.cell_id }.toSet() == eligibleIds.toSet(),
            "mapped object evidence coverage is invalid")

        Map<String, String> cellToDomain = new TreeMap<>()
        eligibleCells.each { cell ->
            cellToDomain[cell.cell_id as String] =
                cell.analysis_domain_id as String
        }
        require(canonicalSha256(cellToDomain) ==
            snapshot.training_cell_to_analysis_domain_mapping_sha256,
            "object cell-to-domain authorization digest is invalid")

        List<Map> trainingCells = entry.training_cells as List<Map>
        require(trainingCells.collect { it.cell_id } == eligibleIds,
            "native object training cells do not cover exact eligible cells")
        trainingCells.each { row ->
            Map cell = eligibleById[row.cell_id as String]
            List<Map> cellEvidence = mappedByCell[row.cell_id as String]
            Set<String> classes = cellEvidence?.collect {
                it.class_id as String
            }?.toSet() ?: [] as Set<String>
            require(cell != null && classes.size() == 1 &&
                row.expected_class_id == classes.first() &&
                row.analysis_domain_id == cell.analysis_domain_id &&
                row.slide_id == cell.slide_id &&
                row.section_id == cell.section_id,
                "native object cell label or scope differs from evidence")
            if(deferred) {
                require(row.measurement_values==null && row.measurement_vector_sha256==null,
                    "Deferred cell evidence must not contain an embedding vector")
                return
            }
            List values = row.measurement_values as List
            List expected = measurementNames.collect { name ->
                (cell.marker_measurements as Map)[name]
            }
            require(values.size() == expected.size() &&
                values.indices.every { index ->
                    sameFiniteDouble(values[index], expected[index])
                }, "native object measurement vector differs from evidence")
        }
    }

    private static void validateObjectEntry(Map entry, Map request,
                                            List<Map> bindings) {
        boolean deferred=entry.snapshot instanceof Map && entry.snapshot.containsKey("deferred_feature_evidence")
        requireClosed(entry.snapshot, OBJECT_SNAPSHOT_FIELDS+(deferred ? ["deferred_feature_evidence"] : []),
            "object training snapshot")
        require(deferred==pendingCellStatistics(request.model_configuration as Map),
            "Only pending automatic cell statistics permit deferred input evidence")
        Map snapshot = entry.snapshot as Map
        [
            "project_entry_id", "sample_id", "slide_id", "subject_id",
            "section_id", "pipeline_run_id", "segmentation_generation_id",
            "measurement_generation_id",
        ].each { name ->
            requireText(snapshot[name], "object snapshot ${name}")
        }
        require(snapshot.split == "training" &&
            snapshot.coordinate_system == "global_level0",
            "object snapshot is not training-only global-level-0 evidence")
        requireFinite(snapshot.point_selection_radius_level0,
            "point selection radius", 0.0d, true)
        requireDigest(snapshot.snapshot_sha256, "snapshot_sha256")
        require(entry.training_cells instanceof List,
            "object training cells must be a list")
        List<Map> cells = entry.training_cells as List<Map>
        int features = (request.feature_spec.ordered_measurements as List).size()
        cells.each { cell ->
            requireClosed(cell, OBJECT_TRAINING_CELL_FIELDS,
                "object training cell")
            requireText(cell.cell_id, "cell_id")
            require((request.required_classes as List).contains(
                cell.expected_class_id),
                "object cell class is undeclared")
            require(cell.slide_id == snapshot.slide_id &&
                cell.section_id == snapshot.section_id,
                "object training cell is foreign")
            if(deferred) {
                require(cell.measurement_values==null && cell.measurement_vector_sha256==null,
                    "Deferred training cells must not contain embedding values")
            } else {
            require(cell.measurement_values instanceof List &&
                (cell.measurement_values as List).size() == features,
                "object measurement vector width is invalid")
            (cell.measurement_values as List).each {
                requireFinite(it, "measurement value", -Double.MAX_VALUE)
            }
            requireDigest(cell.measurement_vector_sha256,
                "measurement_vector_sha256")
            }
        }
        require(cells.collect { it.cell_id } ==
            cells.collect { it.cell_id }.toSorted() &&
            cells.collect { it.cell_id }.toSet().size() == cells.size(),
            "object cell IDs must be canonical and unique")
        List<Map> ownBindings = bindings.findAll {
            it.project_entry_id == snapshot.project_entry_id &&
                canonicalSha256(it) in snapshot.analysis_domain_binding_sha256s
        }
        require(canonicalSha256(ownBindings) ==
            snapshot.analysis_domain_binding_set_sha256 &&
            ownBindings.collect { canonicalSha256(it) }.toSorted() ==
                snapshot.analysis_domain_binding_sha256s &&
            ownBindings.every { binding ->
                binding.sample_id == snapshot.sample_id &&
                    binding.slide_id == snapshot.slide_id
            },
            "object entry domain bindings are invalid")
        validateObjectSnapshotAuthorization(entry, request, ownBindings)
    }

    private static ProjectImageEntry resolveEntry(Project project,
                                                  String entryId) {
        List matches = project.getImageList().findAll {
            it.getID().toString() == entryId
        }
        require(matches.size() == 1,
            "training project entry ${entryId} is missing or duplicated")
        matches.first() as ProjectImageEntry
    }

    private static void validateLiveEntry(ProjectImageEntry entry,
                                          Map owner) {
        Map metadata = entry.getMetadataMap()
        require(metadata.get("mif_pipeline.image_role") == "primary_mif" &&
            metadata.get("mif_pipeline.source_image_sha256") ==
                owner.image_identity_sha256 &&
            metadata.get("mif_pipeline.sample_id") == owner.sample_id &&
            metadata.get("mif_pipeline.slide_id") == owner.slide_id,
            "training entry is not the exact canonical primary mIF image")
    }

    private static String normalizedRoiSha256(PathObject object) {
        require(object.getROI() != null && !object.getROI().isEmpty(),
            "requested live ROI is absent")
        def geometry = object.getROI().getGeometry()
        require(geometry != null && !geometry.isEmpty() && geometry.isValid() &&
            geometry.getCoordinates().every {
                Double.isFinite(it.x) && Double.isFinite(it.y)
            }, "requested live ROI is invalid")
        def normalized = geometry.copy()
        normalized.normalize()
        byte[] bytes = new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false)
            .write(normalized)
        sha256Bytes(bytes)
    }

    private static Map exactDomainBinding(Map request, String entryId,
                                          String domainId) {
        List<Map> matches =
            (request.training_analysis_domain_bindings as List<Map>).findAll {
                it.project_entry_id == entryId &&
                    it.analysis_domain_id == domainId
            }
        require(matches.size() == 1,
            "live scope lacks one exact analysis-domain binding")
        matches.first()
    }

    private static PathObject validatePixelMaterialScope(
            PathObject parent, Map row, Map state, Map request,
            PathObject hierarchyRoot) {
        Map binding = exactDomainBinding(request,
            state.project_entry_id as String,
            row.analysis_domain_id as String)
        require(binding.analysis_domain_revision_sha256 ==
                row.analysis_domain_revision_sha256 &&
            binding.domain_set_sha256 == row.domain_set_sha256,
            "live material scope differs from its domain binding")
        List<PathObject> selectedDomains = []
        List<PathObject> tissueSections = []
        Set<PathObject> seen = Collections.newSetFromMap(
            new IdentityHashMap<PathObject, Boolean>())
        PathObject cursor = parent
        while (cursor != null && cursor != hierarchyRoot) {
            require(seen.add(cursor),
                "material parent hierarchy contains a cycle")
            Map metadata = cursor.getMetadata()
            if (metadata.get("pipeline_stage") ==
                    "analysis_domain_selection") {
                require(metadata.get("analysis_domain_status") == "accepted" &&
                    metadata.get("analysis_domain_active") == "true" &&
                    metadata.get("analysis_domain_id") ==
                        row.analysis_domain_id &&
                    metadata.get("analysis_domain_revision") ==
                        Integer.toString((binding.analysis_domain_revision as
                            Number).intValue()) &&
                    metadata.get("analysis_domain_revision_sha256") ==
                        row.analysis_domain_revision_sha256 &&
                    metadata.get("analysis_domain_set_sha256") ==
                        row.domain_set_sha256 &&
                    metadata.get("analysis_domain_project_entry_id") ==
                        state.project_entry_id && cursor.isLocked(),
                    "material parent reaches a foreign or stale analysis domain")
                normalizedRoiSha256(cursor)
                selectedDomains << cursor
            }
            if (metadata.get("pipeline_stage") == "tissue_detection") {
                require(metadata.get("generation_status") == "accepted" &&
                    metadata.get("review_status") == "accepted" &&
                    metadata.get("slide_id") == state.slide_id,
                    "material parent reaches a stale TissueSection")
                normalizedRoiSha256(cursor)
                tissueSections << cursor
            }
            cursor = cursor.getParent()
        }
        require(cursor == hierarchyRoot && tissueSections.size() == 1 &&
            tissueSections.first().getParent() == hierarchyRoot,
            "material parent lacks one current TissueSection ancestry")
        require(selectedDomains.size() <= 1,
            "material parent reaches multiple requested analysis domains")
        PathObject domain = selectedDomains.isEmpty() ?
            tissueSections.first() : selectedDomains.first()
        if (!selectedDomains.isEmpty())
            require(domain.getParent() == tissueSections.first(),
                "selected analysis domain is reparented")
        if (row.material_parent_type == "analysis_domain")
            require(parent == domain,
                "live analysis-domain material parent identity changed")
        domain
    }

    private static String liveMaterialParentRevision(PathObject parent,
                                                     Map row) {
        Map metadata = parent.getMetadata()
        String type = row.material_parent_type as String
        String typeId = type == "primary" ?
            metadata.get("primary_class_id") as String :
            metadata.get("structure_id") as String
        require(type in ["primary", "secondary"] &&
            metadata.get("pipeline_stage") ==
                (type == "primary" ? "primary_classifier" :
                    "secondary_classifier") &&
            (type != "secondary" ||
                metadata.get("candidate_status") == "candidate") &&
            metadata.get("pipeline_active") == "true" &&
            metadata.get("analysis_domain_id") == row.analysis_domain_id &&
            metadata.get("analysis_domain_revision_sha256") ==
                row.analysis_domain_revision_sha256 &&
            metadata.get("domain_set_sha256") == row.domain_set_sha256 &&
            typeId == row.material_parent_type_id &&
            parent.getParent() != null,
            "live material parent metadata differs from evidence")
        String revision = canonicalSha256([
            analysis_domain_id: metadata.get("analysis_domain_id"),
            analysis_domain_revision_sha256:
                metadata.get("analysis_domain_revision_sha256"),
            analysis_domain_set_sha256:
                metadata.get("domain_set_sha256"),
            classification: parent.getClassification(),
            geometry_sha256: normalizedRoiSha256(parent),
            parent_path_object_id: parent.getParent().getID().toString(),
            path_object_id: parent.getID().toString(),
            pipeline_active: metadata.get("pipeline_active"),
            type: type,
            type_id: typeId,
        ])
        require(metadata.get("object_revision_sha256") == revision &&
            row.material_parent_revision_sha256 == revision,
            "live material parent revision differs from evidence")
        revision
    }

    private static boolean sameTypedJson(Object left, Object right) {
        if (left instanceof Map && right instanceof Map) {
            Map leftMap = left as Map
            Map rightMap = right as Map
            return leftMap.keySet() == rightMap.keySet() &&
                leftMap.keySet().every { key ->
                    sameTypedJson(leftMap[key], rightMap[key])
                }
        }
        if (left instanceof List && right instanceof List) {
            List leftList = left as List
            List rightList = right as List
            return leftList.size() == rightList.size() &&
                leftList.indices.every { index ->
                    sameTypedJson(leftList[index], rightList[index])
                }
        }
        if (left instanceof Number && right instanceof Number) {
            double leftValue = (left as Number).doubleValue()
            double rightValue = (right as Number).doubleValue()
            return Double.isFinite(leftValue) &&
                Double.isFinite(rightValue) &&
                Double.doubleToRawLongBits(leftValue) ==
                    Double.doubleToRawLongBits(rightValue)
        }
        left == right &&
            (left == null || left.getClass() == right.getClass())
    }

    private static void validateObjectCellScope(
            ImageData<BufferedImage> imageData, PathObject cell,
            Map expectedCell, Map snapshot, Map request) {
        PathObject root = imageData.getHierarchy().getRootObject()
        List<PathObject> tissueSections = []
        Set<PathObject> seen = Collections.newSetFromMap(
            new IdentityHashMap<PathObject, Boolean>())
        boolean datasetCell = imageData.getProperty("mif.cell_datasets") != null &&
            cell.getMetadata().get("mif.cell_id") == cell.getMetadata().get("cell_id")
        String tissueRoiId = datasetCell ?
            requireText(cell.getMetadata().get("tissue_roi_id"), "dataset cell tissue ROI") :
            expectedCell.tissue_roi_id as String
        String tissueGenerationId = datasetCell ?
            requireText(cell.getMetadata().get("tissue_generation_id"), "dataset cell tissue generation") :
            expectedCell.tissue_generation_id as String
        if (datasetCell) {
            tissueSections = imageData.getHierarchy().getAnnotationObjects().findAll { candidate ->
                candidate.getMetadata().get("pipeline_stage") == "tissue_detection" &&
                candidate.getMetadata().get("roi_id") == tissueRoiId
            }
        }
        PathObject cursor = datasetCell ? root : cell.getParent()
        while (cursor != null && cursor != root) {
            require(seen.add(cursor),
                "cell hierarchy contains a parent cycle")
            if (cursor.getMetadata().get("pipeline_stage") ==
                    "tissue_detection")
                tissueSections << cursor
            cursor = cursor.getParent()
        }
        require(cursor == root && tissueSections.size() == 1,
            "cell must reach exactly one current TissueSection ancestor")
        PathObject section = tissueSections.first()
        Map sectionMetadata = section.getMetadata()
        require((datasetCell || cell.getParent() == section) &&
            section.getParent() == root &&
            sectionMetadata.get("generation_status") == "accepted" &&
            sectionMetadata.get("review_status") == "accepted" &&
            sectionMetadata.get("slide_id") == snapshot.slide_id &&
            sectionMetadata.get("section_id") == snapshot.section_id &&
            sectionMetadata.get("generation_id") ==
                snapshot.segmentation_generation_id &&
            tissueRoiId == sectionMetadata.get("roi_id") &&
            tissueGenerationId ==
                sectionMetadata.get("generation_id"),
            "live cell TissueSection ancestry is stale or reparented")
        normalizedRoiSha256(section)

        Map binding = exactDomainBinding(request,
            snapshot.project_entry_id as String,
            expectedCell.analysis_domain_id as String)
        Map cellMetadata = cell.getMetadata()
        require(cellMetadata.get("analysis_domain_revision") ==
                Integer.toString((binding.analysis_domain_revision as
                    Number).intValue()) &&
            cellMetadata.get("analysis_domain_revision_sha256") ==
                binding.analysis_domain_revision_sha256 &&
            cellMetadata.get("domain_set_sha256") ==
                binding.domain_set_sha256,
            "live cell analysis-domain binding is stale")
        if (expectedCell.analysis_domain_kind == "selected_roi") {
            List<PathObject> domainIdentityMatches = imageData.getHierarchy()
                .getAnnotationObjects().findAll { candidate ->
                    Map metadata = candidate.getMetadata()
                    metadata.get("pipeline_stage") ==
                        "analysis_domain_selection" &&
                        metadata.get("analysis_domain_id") ==
                            expectedCell.analysis_domain_id
                }
            require(domainIdentityMatches.size() == 1,
                "requested current analysis domain is absent or duplicated")
            PathObject domain = domainIdentityMatches.first()
            Map metadata = domain.getMetadata()
            require(metadata.get("pipeline_stage") ==
                    "analysis_domain_selection" &&
                metadata.get("analysis_domain_status") == "accepted" &&
                metadata.get("analysis_domain_active") == "true" &&
                metadata.get("analysis_domain_revision") ==
                    Integer.toString((binding.analysis_domain_revision as
                        Number).intValue()) &&
                metadata.get("analysis_domain_revision_sha256") ==
                    binding.analysis_domain_revision_sha256 &&
                metadata.get("analysis_domain_set_sha256") ==
                    binding.domain_set_sha256 &&
                metadata.get("analysis_domain_project_entry_id") ==
                    snapshot.project_entry_id && domain.isLocked() &&
                domain.getParent() == section,
                "requested current analysis domain is stale or reparented")
            normalizedRoiSha256(domain)
        } else {
            require(expectedCell.analysis_domain_kind ==
                    "whole_tissue_section",
                "live cell analysis-domain kind is invalid")
        }
    }

    private static List<PathObject> descendants(PathObject parent) {
        List<PathObject> result = []
        List<PathObject> pending = new ArrayList<>(parent.getChildObjects())
        while (!pending.isEmpty()) {
            PathObject current = pending.remove(pending.size() - 1)
            result.add(current)
            pending.addAll(current.getChildObjects())
        }
        result
    }

    private static Map selectedCellEvidence(PathObject cell,
                                            PathObject annotation,
                                            Map row) {
        Map metadata = cell.getMetadata()
        String cellId = requireText(metadata.get("cell_id"),
            "selected cell stable ID")
        def cellGeometry = cell.getROI()?.getGeometry()
        def annotationGeometry = annotation.getROI()?.getGeometry()
        require(metadata.get("pipeline_stage") == "segmentation" &&
            metadata.get("analysis_domain_id") in
                [null, row.analysis_domain_id] &&
            metadata.get("generation_id") ==
                row.segmentation_generation_id &&
            metadata.get("measurement_generation_id") ==
                row.measurement_generation_id &&
            cell.getParent() != null && cellGeometry != null &&
            !cellGeometry.isEmpty() && cellGeometry.isValid() &&
            annotationGeometry != null &&
            cellGeometry.coveredBy(annotationGeometry),
            "selected cell is stale or outside its training annotation")
        Map payload = [
            stable_cell_id: cellId,
            path_object_id: cell.getID().toString(),
            path_class_id: cell.getClassification(),
            normalized_roi_sha256: normalizedRoiSha256(cell),
            parent_path_object_id: cell.getParent()?.getID()?.toString(),
            segmentation_generation_id: metadata.get("generation_id"),
            segmentation_population_id:
                metadata.get("segmentation_population_id"),
            segmentation_config_sha256:
                metadata.get("segmentation_config_sha256"),
            segmentation_domain_sha256:
                metadata.get("segmentation_domain_sha256"),
            measurement_generation_id:
                metadata.get("measurement_generation_id"),
        ]
        [stable_cell_id: cellId, evidence_sha256: canonicalSha256(payload)]
    }

    private static ImageData<BufferedImage> filteredPixelView(
        ImageData<BufferedImage> live, Map entry, Map request) {
        Map state = entry.entry_state as Map
        Map<String, PathObject> annotations = new TreeMap<>()
        live.getHierarchy().getAnnotationObjects().each { annotation ->
            String id = annotation.getID().toString()
            require(!annotations.containsKey(id),
                "live hierarchy repeats an annotation UUID")
            annotations[id] = annotation
        }
        PathObjectHierarchy filteredHierarchy = new PathObjectHierarchy()
        List<Map> rows = entry.evidence_rows as List<Map>
        rows.each { row ->
            PathObject annotation = annotations[row.annotation_uuid as String]
            require(annotation != null &&
                annotation.getClassification() == row.path_class_id &&
                normalizedRoiSha256(annotation) == row.normalized_roi_sha256,
                "live training annotation differs from its evidence row")
            PathObject parent = annotation.getParent()
            require(parent != null && parent.getID().toString() ==
                    row.material_parent_id &&
                annotation.getROI().getGeometry().coveredBy(
                    parent.getROI().getGeometry()),
                "live training annotation has a foreign material parent")
            validatePixelMaterialScope(parent, row, state, request,
                live.getHierarchy().getRootObject())
            if (row.material_parent_type != "analysis_domain")
                liveMaterialParentRevision(parent, row)
            List<Map> cellEvidence = descendants(annotation)
                .findAll { it.isCell() }
                .collect { selectedCellEvidence(it, annotation, row) }
                .sort { it.stable_cell_id }
            Map selection = [
                cell_ids: cellEvidence.collect { it.stable_cell_id },
                cell_evidence_sha256s:
                    cellEvidence.collect { it.evidence_sha256 },
                segmentation_generation_id:
                    row.segmentation_generation_id,
                measurement_generation_id:
                    row.measurement_generation_id,
                cell_selection_rule: row.cell_selection_rule,
            ]
            require(selection.cell_ids == row.cell_ids &&
                selection.cell_evidence_sha256s ==
                    row.cell_evidence_sha256s &&
                canonicalSha256(selection) == row.cell_selection_sha256,
                "live training-cell selection differs from evidence")
            PathClass pathClass = PathClassFactory.getPathClass(
                row.path_class_id as String)
            filteredHierarchy.addObject(
                PathObjects.createAnnotationObject(
                    annotation.getROI().duplicate(), pathClass))
        }
        new ImageData<BufferedImage>(live.getServer(), filteredHierarchy,
            live.getImageType())
    }

    private static org.mif.classification.PixelFeatureOps.Prepared pixelFeatureOp(Map featureSpec,
                                              Map config,
                                              List<ImageData<BufferedImage>> live) {
        List<Map> rows = featureSpec.ordered_channels as List<Map>
        live.each { imageData ->
            def channels = imageData.getServer().getMetadata().getChannels()
            rows.each { row ->
                int index = (row.qupath_channel_index as Number).intValue()
                require(index < channels.size() &&
                    channels[index].getName() ==
                        row.observed_qupath_channel_name,
                    "live QuPath channel index/name differs from the request")
            }
        }
        Map recipe = config.image_features as Map
        org.mif.classification.PixelFeatureOps.create(
            rows.collect { (it.qupath_channel_index as Number).intValue() } as int[],
            recipe.include_intensity as boolean,
            org.mif.classification.PixelFeatureOps.scales(recipe.scales),
            (config.resolution_um_per_pixel as Number).doubleValue())
    }

    private static Map<String, String> pixelPathClassByCanonicalClass(
        Map request, List<Map> labels) {
        Map<String, Set<String>> observed = new LinkedHashMap<>()
        (request.required_classes as List<String>).each {
            observed[it] = new LinkedHashSet<String>()
        }
        (request.training_entries as List<Map>).each { entry ->
            (entry.evidence_rows as List<Map>).each { row ->
                Set<String> pathClasses = observed[row.class_id as String]
                require(pathClasses != null,
                    "pixel evidence class is absent from required classes")
                pathClasses.add(row.path_class_id as String)
            }
        }
        Map config = nativeConfiguration(request.model_configuration as Map)
        Integer boundaryLabel = config.boundary_strategy == "classify" ?
            (config.boundary_class_id as Number).intValue() : null
        Map<String, String> result = new LinkedHashMap<>()
        labels.each { label ->
            String classId = label.canonical_class_id as String
            int nativeLabel = (label.qupath_label as Number).intValue()
            Set<String> pathClasses = observed[classId]
            if (boundaryLabel != null && nativeLabel == boundaryLabel) {
                require(pathClasses == null,
                    "pixel boundary class cannot also be an evidence class")
                result[classId] = classId
            } else {
                require(pathClasses != null && pathClasses.size() == 1,
                    "pixel canonical class lacks one exact PathClass binding")
                result[classId] = pathClasses.first()
            }
        }
        require(result.values().toSet().size() == result.size(),
            "pixel output classes collapse to the same PathClass")
        result
    }

    private static Map<PathClass, Integer> pixelLabelMap(
        List<Map> labels, Map<String, String> pathClassByCanonicalClass) {
        Map<PathClass, Integer> result = new LinkedHashMap<>()
        labels.each { label ->
            result.put(
                PathClassFactory.getPathClass(pathClassByCanonicalClass[
                    label.canonical_class_id as String]),
                (label.qupath_label as Number).intValue())
        }
        result
    }

    private static Throwable closeOwnedPixelResource(Object resource,
                                                      Throwable failure) {
        if (resource == null)
            return failure
        try {
            resource.close()
        } catch (Throwable error) {
            if (failure == null)
                return error
            failure.addSuppressed(error)
        }
        failure
    }

    private static long pixelPreparationBytes(long candidatePixels,
                                               long largestTilePixels,
                                               int features,
                                               int sourceChannels) {
        require(candidatePixels >= 0L && largestTilePixels >= 0L &&
            features > 0 && sourceChannels > 0, PIXEL_RESOURCE_MESSAGE)
        try {
            long aggregate = Math.multiplyExact(32L,
                Math.multiplyExact(candidatePixels, (long) features + 1L))
            long scratch = Math.multiplyExact(64L,
                Math.multiplyExact(largestTilePixels,
                    (long) features + sourceChannels + 1L))
            return Math.addExact(aggregate, scratch)
        } catch (ArithmeticException ignored) {
            throw new IllegalStateException(PIXEL_RESOURCE_MESSAGE)
        }
    }

    private static Map preflightPixelResources(ImageDataOp featureOp,
                                               List<ImageData<BufferedImage>> views,
                                               PixelCalibration resolution,
                                               int expectedFeatures,
                                               qupath.lib.regions.Padding featurePadding,
                                               boolean areaOnlyLabels = false) {
        require(expectedFeatures > 0 && !views.isEmpty(),
            PIXEL_RESOURCE_MESSAGE)
        List<ImageDataServer<BufferedImage>> servers = []
        List<Long> expectedTileCounts = []
        List<Long> entryPixels = []
        long tileRequests = 0L
        long candidatePixels = 0L
        long largestTilePixels = 0L
        int sourceChannels = 0
        try {
            // QuPath's TileRequest grid is the checked product of these exact
            // native level dimensions, preferred tile sizes and planes.
            views.each { view ->
                ImageDataServer<BufferedImage> server =
                    ImageOps.buildServer(view, featureOp, resolution)
                servers << server
                ImageServerMetadata metadata = server.getMetadata()
                require(server.nResolutions() == 1 &&
                    server.getPreferredDownsamples().length == 1 &&
                    metadata.getChannels().size() == expectedFeatures,
                    "pixel feature-server metadata differs from training features")
                def level = metadata.getLevel(0)
                int width = level.getWidth()
                int height = level.getHeight()
                int tileWidth = metadata.getPreferredTileWidth()
                int tileHeight = metadata.getPreferredTileHeight()
                int zCount = server.nZSlices()
                int tCount = server.nTimepoints()
                double downsample = level.getDownsample()
                require(width > 0 && height > 0 && tileWidth > 0 &&
                    tileHeight > 0 && zCount > 0 && tCount > 0 &&
                    Double.isFinite(downsample) && downsample > 0d,
                    PIXEL_RESOURCE_MESSAGE)
                int channels = view.getServer().getMetadata().getChannels().size()
                require(channels > 0, PIXEL_RESOURCE_MESSAGE)
                sourceChannels = Math.max(sourceChannels, channels)
                try {
                    long columns = 1L + ((long) width - 1L) / tileWidth
                    long rows = 1L + ((long) height - 1L) / tileHeight
                    long count = Math.multiplyExact(
                        Math.multiplyExact(columns, rows),
                        Math.multiplyExact((long) zCount, (long) tCount))
                    tileRequests = Math.addExact(tileRequests, count)
                    expectedTileCounts << count
                } catch (ArithmeticException ignored) {
                    throw new IllegalStateException(PIXEL_RESOURCE_MESSAGE)
                }
                require(tileRequests <= MAX_PIXEL_TILE_REQUESTS,
                    PIXEL_RESOURCE_MESSAGE)
            }
            // The count gate precedes QuPath's potentially large tile list.
            servers.eachWithIndex { server, int index ->
                def tiles = server.getTileRequestManager().getAllTileRequests()
                long entryCount = 0L
                long entryCandidatePixels = 0L
                tiles.each { TileRequest tile ->
                    entryCount++
                    require(tile.getLevel() == 0 && tile.getZ() >= 0 &&
                        tile.getZ() < server.nZSlices() && tile.getT() >= 0 &&
                        tile.getT() < server.nTimepoints() &&
                        Double.isFinite(tile.getDownsample()) &&
                        tile.getDownsample() > 0d &&
                        tile.getTileWidth() > 0 && tile.getTileHeight() > 0 &&
                        tile.getImageWidth() > 0 && tile.getImageHeight() > 0,
                        PIXEL_RESOURCE_MESSAGE)
                    def candidates = server.getImageData().getHierarchy()
                        .getAllObjectsForRegion(tile.getRegionRequest(), null)
                    if (candidates != null && !candidates.isEmpty()) {
                        try {
                            // Fractional downsampling independently rounds
                            // source-region edges. Bound the raster size from
                            // its public RegionRequest as well as tile extent.
                            double widthAtResolution =
                                tile.getImageWidth() / tile.getDownsample()
                            double heightAtResolution =
                                tile.getImageHeight() / tile.getDownsample()
                            require(Double.isFinite(widthAtResolution) &&
                                Double.isFinite(heightAtResolution) &&
                                widthAtResolution > 0d &&
                                heightAtResolution > 0d &&
                                widthAtResolution <= Integer.MAX_VALUE &&
                                heightAtResolution <= Integer.MAX_VALUE,
                                PIXEL_RESOURCE_MESSAGE)
                            long boundedWidth = Math.max(
                                (long) tile.getTileWidth(),
                                (long) Math.ceil(widthAtResolution))
                            long boundedHeight = Math.max(
                                (long) tile.getTileHeight(),
                                (long) Math.ceil(heightAtResolution))
                            long pixels = Math.multiplyExact(
                                boundedWidth, boundedHeight)
                            require(Math.multiplyExact(pixels,
                                (long) expectedFeatures) <= Integer.MAX_VALUE,
                                PIXEL_RESOURCE_MESSAGE)
                            long paddedPixels = Math.multiplyExact(
                                Math.addExact(boundedWidth, (long) featurePadding.getXSum()),
                                Math.addExact(boundedHeight, (long) featurePadding.getYSum()))
                            // Admit even the small mask only after its tile's
                            // existing scratch bound fits the process budget.
                            require(pixelPreparationBytes(0L, paddedPixels,
                                expectedFeatures, sourceChannels) <=
                                Runtime.getRuntime().maxMemory() * 3L / 4L,
                                PIXEL_RESOURCE_MESSAGE)
                            // Retained matrices contain painted reference pixels,
                            // not the unlabelled remainder of every touched tile.
                            // Keep full padded tile scratch and all copy margins.
                            long trainingPixels = areaOnlyLabels ?
                                PixelTrainingRows.boundAreaRows(candidates,
                                    tile.getRegionRequest(), (int) boundedWidth,
                                    (int) boundedHeight) : pixels
                            entryCandidatePixels = Math.addExact(
                                entryCandidatePixels, trainingPixels)
                            candidatePixels = Math.addExact(candidatePixels,
                                trainingPixels)
                            largestTilePixels = Math.max(
                                largestTilePixels, paddedPixels)
                        } catch (ArithmeticException ignored) {
                            throw new IllegalStateException(PIXEL_RESOURCE_MESSAGE)
                        }
                    }
                }
                require(entryCount == expectedTileCounts[index],
                    "pixel native tile grid differs from its metadata")
                entryPixels << entryCandidatePixels
            }
            long estimatedBytes = pixelPreparationBytes(candidatePixels,
                largestTilePixels, expectedFeatures, sourceChannels)
            // The managed launch sets the heap from its admitted memory lease.
            // Leave a quarter for the runtime and other live heap allocations.
            long preparationBudgetBytes = Runtime.getRuntime().maxMemory() * 3L / 4L
            require(estimatedBytes <= preparationBudgetBytes,
                PIXEL_RESOURCE_MESSAGE + " Estimated preparation: " + estimatedBytes +
                " bytes; allocated preparation budget: " + preparationBudgetBytes + " bytes.")
            return [entry_candidate_pixels: entryPixels,
                candidate_pixels: candidatePixels,
                largest_tile_pixels: largestTilePixels,
                tile_requests: tileRequests,
                estimated_bytes: estimatedBytes,
                feature_count: expectedFeatures]
        } finally {
            servers.reverseEach { it.close() }
        }
    }

    private static Map preparePixelFit(Map request,
                                       List<ImageData<BufferedImage>> views,
                                       List<String> entryIds,
                                       List<Map> labels) {
        Map config = nativeConfiguration(request.model_configuration as Map)
        def prepared = pixelFeatureOp(request.feature_spec as Map, config, views)
        ImageDataOp baseOp = prepared.operation()
        PixelClassifierTraining helper = new PixelClassifierTraining(baseOp)
        PixelCalibration resolution = new PixelCalibration.Builder()
            .pixelSizeMicrons(
                (config.resolution_um_per_pixel as Number).doubleValue(),
                (config.resolution_um_per_pixel as Number).doubleValue())
            .build()
        helper.setResolution(resolution)
        Map<String, String> pathClassByCanonicalClass =
            pixelPathClassByCanonicalClass(request, labels)
        if (config.boundary_strategy == "skip") {
            helper.setBoundaryStrategy(
                BoundaryStrategy.getSkipBoundaryStrategy())
        } else {
            Map boundaryLabel = labels.find {
                it.qupath_label == config.boundary_class_id
            } as Map
            helper.setBoundaryStrategy(
                BoundaryStrategy.getClassifyBoundaryStrategy(
                    PathClassFactory.getPathClass(
                        pathClassByCanonicalClass[
                            boundaryLabel.canonical_class_id as String]),
                    (config.boundary_thickness_px as Number).doubleValue()))
        }
        Map<PathClass, Integer> labelMap = pixelLabelMap(labels,
            pathClassByCanonicalClass)
        Map preflight = preflightPixelResources(baseOp, views, resolution,
            prepared.featureCount(), prepared.padding(), config.boundary_strategy == "skip")
        def rows = PixelTrainingRows.extract(helper, views, entryIds, labelMap)
        Map<Integer, String> classByLabel = labels.collectEntries {
            [((it.qupath_label as Number).intValue()): it.canonical_class_id as String]
        }
        def settings = new FeaturePreparation.Settings(
            FeaturePreparation.normalization(config.normalization as String),
            (config.pca_retained_variance as Number).doubleValue(), config.pca_normalize as boolean)
        def fit = PixelTrainingRows.prepare(rows, entryIds, preflight.entry_candidate_pixels as List,
            preflight.feature_count as int, config.maximum_training_samples as int,
            request.random_seed as int, config.reweight_samples as boolean, classByLabel, settings, baseOp)
        [train_data: fit.trainData(), serialized_op: fit.serializedOp(), resolution: resolution,
            labels: labelMap, entry_counts: fit.entryCounts(), class_counts: fit.classCounts(),
            sample_count: fit.sampleCount(), feature_count: fit.featureCount(), close: { fit.close() }]
    }

    private static Map validateLiveObjectCells(ImageData<BufferedImage> imageData,
                                               Map entry, Map request,
                                               List<String> measurementNames,
                                               Map<String, Integer>
                                                   responseIndexByClassId, Map<String, PathObject> preparedCells = [:]) {
        Map snapshot = entry.snapshot as Map
        Map<String, Map> eligibleById = (snapshot.eligible_cells as List<Map>)
            .collectEntries { cell ->
                [(cell.cell_id as String): cell]
            } as Map<String, Map>
        Set<String> eligibleDomainIds = eligibleById.values().collect {
            it.analysis_domain_id as String
        } as Set<String>
        Set<String> requestedIds = (entry.training_cells as List).collect { it.cell_id as String } as Set
        Set<String> observedIds = new TreeSet<>()
        Map<String, PathObject> liveById = new TreeMap<>()
        NativeCellScope.forEach(imageData, { cell ->
            if (!cell.isCell()) return
            Map metadata = cell.getMetadata()
            if (metadata.get("pipeline_stage") == "segmentation" &&
                    metadata.get("slide_id") == snapshot.slide_id &&
                    metadata.get("section_id") == snapshot.section_id &&
                    metadata.get("generation_id") ==
                        snapshot.segmentation_generation_id &&
                    metadata.get("measurement_generation_id") ==
                        snapshot.measurement_generation_id &&
                    eligibleDomainIds.contains(
                        metadata.get("analysis_domain_id") as String) &&
                    requestedIds.contains(metadata.get("cell_id") as String)) {
                String cellId = requireText(metadata.get("cell_id"),
                    "live stable cell ID")
                require(observedIds.add(cellId),
                    "live population repeats a stable cell ID")
                liveById[cellId] = preparedCells[cellId] ?: cell
            }
        } as Consumer)
        require(observedIds == eligibleById.keySet(),
            "live eligible stable-cell inventory differs from the request")
        List<PathObject> orderedCells = []
        List<Integer> responses = []
        (entry.training_cells as List<Map>).each { row ->
            PathObject cell = liveById[row.cell_id as String]
            require(cell != null, "authorized stable cell is missing")
            Map metadata = cell.getMetadata()
            Map expectedCell = eligibleById[row.cell_id as String]
            Map liveCellGeometry = ObjectTrainingRows.geometry(
                cell.getROI()?.getGeometry())
            Map liveNucleusGeometry = cell.getNucleusROI() == null ? [:] :
                ObjectTrainingRows.geometry(cell.getNucleusROI().getGeometry())
            require(metadata.get("pipeline_stage") == "segmentation" &&
                metadata.get("pipeline_run_id") == snapshot.pipeline_run_id &&
                metadata.get("slide_id") == row.slide_id &&
                metadata.get("section_id") == row.section_id &&
                metadata.get("generation_id") ==
                    snapshot.segmentation_generation_id &&
                metadata.get("measurement_generation_id") ==
                    snapshot.measurement_generation_id &&
                metadata.get("analysis_domain_id") == row.analysis_domain_id &&
                metadata.get("analysis_domain_kind") ==
                    expectedCell.analysis_domain_kind &&
                cell.getROI() != null && !cell.getROI().isEmpty() &&
                sameFiniteDouble(cell.getROI().getCentroidX(),
                    expectedCell.x_level0) &&
                sameFiniteDouble(cell.getROI().getCentroidY(),
                    expectedCell.y_level0) &&
                sameTypedJson(liveCellGeometry,
                    expectedCell.cell_geometry) &&
                sameTypedJson(liveNucleusGeometry,
                    expectedCell.nucleus_geometry),
                "live stable cell scope differs from the request")
            validateObjectCellScope(imageData, cell, expectedCell, snapshot,
                request)
            List<Double> observed = measurementNames.collect { name ->
                Object value = cell.getMeasurements().get(name)
                require(value instanceof Number &&
                    Double.isFinite((value as Number).doubleValue()),
                    "live cell lacks finite measurement ${name}")
                (value as Number).doubleValue()
            }
            if(snapshot.deferred_feature_evidence!=null) {
                require(row.measurement_values==null && row.measurement_vector_sha256==null &&
                    metadata.get("mif.kronos_cell_input_sha256")==snapshot.deferred_feature_evidence.cell_input_sha256[row.cell_id],
                    "Live deferred cell input was not authenticated before encoding")
            } else {
            List expected = row.measurement_values as List
            require(observed.size() == expected.size() &&
                observed.indices.every { index ->
                    Double.doubleToLongBits(observed[index]) ==
                        Double.doubleToLongBits(
                            (expected[index] as Number).doubleValue())
                },
                "live ordered measurement vector differs from the request")
            }
            Integer response = responseIndexByClassId[
                row.expected_class_id as String]
            require(response != null,
                "object response class is absent from output order")
            orderedCells << cell
            responses << response
        }
        [cells: orderedCells, responses: responses]
    }

    private static Map fitObjectClassifier(Map request,
                                           List<Map> entries,
                                           List<ImageData<BufferedImage>> live,
                                           List<Map> orderedLabels) {
        Map config = nativeConfiguration(request.model_configuration as Map)
        Map statistics=[configuration:config,receipt:null]
        if(config.feature_source!=null) {
            require(markerStatistics!=null,"Automatic marker statistics support is unavailable")
            entries.each {entry ->
                if(entry.snapshot.deferred_feature_evidence!=null)
                    cellFeatureSupport.validateInputEvidence(entry.snapshot.deferred_feature_evidence,config.feature_source,
                        entry.snapshot.eligible_cells.collect {it.cell_id})
            }
            statistics=markerStatistics.resolve(config,request,live)
            config=statistics.configuration
        }
        List<Map> measurementRows =
            request.feature_spec.ordered_measurements as List<Map>
        List<String> measurementNames = measurementRows.collect {
            it.measurement_name as String
        }
        Map<String, Integer> responseIndexByClassId = new LinkedHashMap<>()
        orderedLabels.eachWithIndex { label, int index ->
            require(label.qupath_label == index,
                "object qupath_label changed from its response position")
            responseIndexByClassId[label.canonical_class_id as String] = index
        }
        List<ObjectTrainingRows.Entry> selectedEntries = []
        entries.eachWithIndex { entry, int index ->
            Set selectedIds=entry.training_cells.collect {it.cell_id} as Set
            if (selectedIds.isEmpty()) {
                selectedEntries.add(new ObjectTrainingRows.Entry(
                    entry.snapshot.project_entry_id as String, live[index], [], [] as int[]))
                return
            }
            List embeddingCells=[]
            NativeCellScope.forEach(live[index], { cell ->
                if (cell.isCell() && selectedIds.contains(cell.metadata.get("cell_id"))) embeddingCells.add(cell)
            } as Consumer)
            prepareObjectFeatures(config,live[index],embeddingCells,entry.snapshot as Map)
            Map selected = validateLiveObjectCells(live[index], entry, request,
                measurementNames, responseIndexByClassId,
                embeddingCells.collectEntries { [(it.metadata.get("cell_id") as String):it] })
            List<PathObject> cells = selected.cells as List<PathObject>
            selectedEntries.add(new ObjectTrainingRows.Entry(
                entry.snapshot.project_entry_id as String, live[index], cells,
                selected.responses as int[]))
        }
        def settings = new FeaturePreparation.Settings(
            FeaturePreparation.normalization(config.normalization as String),
            (config.pca_retained_variance as Number).doubleValue(), config.getOrDefault("pca_normalize",true) as boolean)
        def fit = ObjectTrainingRows.prepare(selectedEntries, measurementNames,
            orderedLabels.collect { it.canonical_class_id as String }, settings)
        [train_data: fit.trainData(), extractor: fit.extractor(),
            configuration:config,marker_statistics_receipt:statistics.receipt,
            ordered_path_classes: orderedLabels.collect { PathClassFactory.getPathClass(it.canonical_class_id as String) },
            entry_counts: fit.entryCounts(), class_counts: fit.classCounts(),
            sample_count: fit.sampleCount(), feature_count: fit.featureCount(), close: { fit.close() }]
    }

    private static void assertReservedResourceAbsent(Project project,
                                                     Map request,
                                                     ResourceManager.Manager manager,
                                                     Path targetPath) {
        String resourceName = request.resource_name as String
        Path projectPath = project.getPath().toAbsolutePath().normalize()
        Path projectRoot = projectPath.parent
        Path familyRoot = validateClassifierDirectory(projectRoot,
            request.native_classifier_type as String)
        require(targetPath.parent == familyRoot &&
            targetPath.fileName.toString() == "${resourceName}.json",
            "native classifier target parent is not the standard project root")
        require(!manager.contains(resourceName) &&
            !Files.exists(targetPath,
                java.nio.file.LinkOption.NOFOLLOW_LINKS),
            "reserved native classifier resource already exists")
    }

    /** Fit validated tissue controls and transfer the native model to QuPath. */
    private static OpenCVStatModel trainRTrees(Map configuration,
                                               int randomSeed,
                                               TrainData trainData) {
        Map settings = configuration.rtrees as Map
        def model = OpenCvTraining.fitForest(trainData,
            new OpenCvTraining.ForestSettings(settings.max_depth as int,
                settings.min_sample_count as int, settings.use_1se_rule as boolean,
                settings.active_var_count as int, settings.calculate_variable_importance as boolean,
                settings.cv_folds as int, settings.use_surrogates as boolean,
                settings.max_trees as int, settings.termination_epsilon as double), randomSeed)
        try { return OpenCVClassifiers.wrapStatModel(model) }
        catch (Throwable error) { model.close(); throw error }
    }

    /** Preserve optional tissue class balancing while wrapping the fitted SVM. */
    private static OpenCVStatModel trainSvm(Map configuration,
                                           int randomSeed,
                                           TrainData trainData) {
        // The request owner validates every final and fold candidate before fitting.
        Map settings = configuration.svm as Map
        def model = OpenCvTraining.fitSvm(trainData,
            new OpenCvTraining.SvmSettings(settings.kernel == "LINEAR" ? SVM.LINEAR : SVM.RBF,
                settings.c as double, settings.kernel == "RBF" ? settings.gamma as double : 0d,
                settings.max_iterations as int, settings.termination_epsilon as double,
                configuration.reweight_samples == true), randomSeed)
        try { return OpenCVClassifiers.wrapStatModel(model) }
        catch (Throwable error) { model.close(); throw error }
    }

    private static ObjectClassifier enableRtreesObjectVoteConfidence(
            JsonElement serialized) {
        require(serialized instanceof JsonObject,
            "native object classifier serialization is not an object")
        JsonObject payload = serialized.deepCopy() as JsonObject
        require(payload.keySet() == [
            "object_classifier_type", "featureExtractor", "classifier",
            "pathClasses", "requestProbabilityEstimate", "filter", "timestamp",
        ] as Set<String>,
            "native object classifier serialization shape is unsupported")
        JsonElement family = payload.get("object_classifier_type")
        require(family instanceof JsonPrimitive &&
            (family as JsonPrimitive).isString() &&
            family.getAsString() == "OpenCVMLClassifier",
            "native object classifier family is unsupported")
        JsonElement model = payload.get("classifier")
        require(model instanceof JsonObject &&
            (model as JsonObject).keySet() == ["class", "statmodel"] as Set<String>,
            "native object classifier model shape is unsupported")
        JsonElement modelFamily = (model as JsonObject).get("class")
        require(modelFamily instanceof JsonPrimitive &&
            (modelFamily as JsonPrimitive).isString() &&
            modelFamily.getAsString() == "RTrees" &&
            (model as JsonObject).get("statmodel") instanceof JsonObject,
            "native object classifier model is not RTrees")
        JsonElement estimate = payload.get("requestProbabilityEstimate")
        require(estimate instanceof JsonPrimitive &&
            (estimate as JsonPrimitive).isBoolean() &&
            !estimate.getAsBoolean(),
            "native object classifier confidence flag is unsupported")
        payload.addProperty("requestProbabilityEstimate", true)
        Gson qupathGson = GsonTools.getInstance()
        // QuPath's adapter mutates the input JsonObject with a transient
        // feature-extractor subtype key. Keep the expected serialized tree
        // untouched so only this boolean may differ on reserialization.
        ObjectClassifier enabled = qupathGson.fromJson(payload.deepCopy(),
            ObjectClassifier.class) as ObjectClassifier
        require(enabled instanceof OpenCVMLClassifier &&
            qupathGson.toJsonTree(enabled, ObjectClassifier.class) == payload,
            "native object classifier confidence round-trip changed the model")
        enabled
    }

    private static Map fitSelectionCandidate(Map request,List<ImageData> images,List<ImageData> views,Map support) {
        Map config=nativeConfiguration(request.model_configuration as Map)
        boolean advanced=isKronos(request.model_configuration as Map)
        List<Map> labels=config.output_labels.ordered_labels
        Map fit=null;
        Object model=null
        try {
            fit=advanced ? support.training.prepare(request,images,support) :
                preparePixelFit(request,views,request.training_entries.collect{it.entry_state.project_entry_id},labels)
            Object classifier
            if(advanced) {
                model=support.training.fitNative(config,fit.train_data,(request.random_seed as Number).intValue())
                classifier=fit.classifier(model)
            } else {
                model=modelKey(config,false)=="svm" ? trainSvm(config,(request.random_seed as Number).intValue(),fit.train_data as TrainData) :
                    trainRTrees(config,(request.random_seed as Number).intValue(),fit.train_data as TrainData)
                def metadata=new PixelClassifierMetadata.Builder().inputResolution(fit.resolution as PixelCalibration)
                    .inputShape(512,512).setChannelType(ChannelType.CLASSIFICATION)
                    .classificationLabels((fit.labels as Map).collectEntries {pathClass,label -> [(label):pathClass]}).build()
                classifier=PixelClassifiers.createClassifier(model,fit.serialized_op,metadata,true)
            }
            Map ownedFit=fit;
            Object ownedModel=model
            return [classifier:classifier,fit:fit,advanced:advanced,
                authored_configuration:request.model_configuration,configuration:fit.configuration ?: request.model_configuration,
                close:{
                    try {ownedFit.close()}
                    finally {if(advanced)ownedModel.close();
                    else ownedModel.getStatModel().close()}
                }]
        } catch(Throwable error) {
            if(fit!=null)fit.close()
            if(model!=null) {if(advanced)model.close();
                else model.getStatModel().close()}
            throw error
        }
    }

    private static Map scoreSelectionEntries(Map model,List<Map> rows,List<String> classes,Map support,List referenceObservations,List scoreObservations) {
        Map result=new TreeMap()
        Map config=nativeConfiguration(model.configuration)
        int patchSize = model.advanced ?
            ClassifierEvaluation.requireKronosPatchSize(support.resources.describe(model.classifier).convention.patch_size) : 0
        Map labels=config.output_labels.ordered_labels.collectEntries{[(it.qupath_label as int):it.canonical_class_id]}
        for(Map row:rows) {
            ImageData image=row.image
            Map objects=image.hierarchy.annotationObjects.collectEntries{[(it.ID.toString()):it]}
            Map geometries=[:]
            Map referenceRois=new TreeMap()
            Map referenceClasses=new TreeMap()
            for(Map evidence:row.entry.evidence_rows) {
                def annotation=objects[evidence.annotation_uuid]
                require(annotation!=null && annotation.ROI!=null && annotation.ROI.isArea(),"The fold reference annotation is absent or not an area")
                require(annotation.classification==evidence.path_class_id,"The fold reference label changed")
                geometries.computeIfAbsent(evidence.class_id){[]}.add(annotation.ROI.geometry)
                referenceRois[evidence.annotation_uuid]=annotation.ROI
                referenceClasses[evidence.annotation_uuid]=evidence.class_id
            }
            Map regions=geometries.collectEntries{key,list -> [(key):org.locationtech.jts.operation.union.UnaryUnionOp.union(list)]}
            List keys=regions.keySet().toList()
            for(int i=0;i<keys.size();i++)for(int j=i+1;j<keys.size();j++)
                require(regions[keys[i]].intersection(regions[keys[j]]).area<=1e-9,"Reference classes overlap; correct conflicting annotations before cross-validation")
            if(model.advanced) {
                Map scopes=[:]
                for(Map evidence:row.entry.evidence_rows) {
                    def parent=objects[evidence.material_parent_id]
                    require(parent!=null && parent.ROI!=null,"The fold reference parent is absent")
                    scopes[evidence.material_parent_id]=[id:evidence.material_parent_id,geometry:parent.ROI.geometry,
                        revision:evidence.analysis_domain_revision_sha256,split:"validation"]
                }
                support.resources.bind(image,scopes.values().toList())
            }
            def server=qupath.opencv.ml.pixel.PixelClassifierTools.createPixelClassificationServer(image,model.classifier)
            try {
                def counted = ClassifierEvaluation.pixelConfusionWithReferences(server, regions, labels,
                    classes, patchSize, referenceRois, referenceClasses)
                result[row.entry_id] = counted.matrix()
                if (!model.advanced && referenceRois.values().every { it.z == 0 && it.t == 0 }) {
                    Map scores = ClassifierPrecisionRecall.evaluate(image, model.classifier, server,
                        regions, labels, classes)
                    if (scores != null) scoreObservations.add(scores + [
                        project_entry_id: row.entry_id, subject_id: row.subject_id])
                }
                for(Map evidence:row.entry.evidence_rows) {
                    def roi=referenceRois[evidence.annotation_uuid]
                    referenceObservations.add([project_entry_id:row.entry_id, subject_id:row.subject_id,
                        annotation_uuid:evidence.annotation_uuid, reference_class:evidence.class_id,
                        normalized_roi_sha256:evidence.normalized_roi_sha256,
                        roi_bounds:[x:roi.boundsX,y:roi.boundsY,width:roi.boundsWidth,height:roi.boundsHeight,z:roi.z,t:roi.t],
                        declared_label_counts:counted.referenceCounts()[evidence.annotation_uuid]])
                }
            } finally {server.close();
                if(model.advanced)support.resources.closeImage(image)}
        }
        result
    }

    /** Refit the full normalizer/PCA/head chain within each subject fold. */
    private static Map fitGroupedObjects(Map request,List<ImageData> images,Map api) {
        List rows=[]
        request.training_entries.eachWithIndex {entry,int i ->
            require(entry.snapshot.split=="training","Only development subjects enter object tuning")
            rows.add([entry_id:canonicalSha256([entry.snapshot.project_entry_id,entry.snapshot.section_id]),
                subject_id:entry.snapshot.subject_id,
                classes:entry.training_cells.collect{it.expected_class_id}.unique().sort(),entry:entry,image:images[i]])
        }
        Map search=request.model_configuration
        Map fitted=api.run(rows,request.required_classes,search.candidates,(search.folds as Number).intValue(),
            (request.random_seed as Number).intValue(),
            {candidate,training,seed ->
                Map inner=new LinkedHashMap(request);inner.model_configuration=candidate
                inner.training_entries=training.collect{it.entry}
                Map fit=fitObjectClassifier(inner,inner.training_entries,training.collect{it.image},candidate.output_labels.ordered_labels)
                Object classifier=null
                try {classifier=supervisedSupport.train(fit.configuration,seed as int,fit)}
                catch(Throwable error) {fit.close();throw error}
                [classifier:classifier,fit:fit,configuration:fit.configuration,authored_configuration:candidate,request:inner,
                    close:{try {supervisedSupport.close(classifier)} finally {fit.close()}}]
            },
            {model,held ->
                Map predictions=new TreeMap()
                List<String> names=request.feature_spec.ordered_measurements.collect{it.measurement_name}
                Map responseIndex=request.required_classes.withIndex().collectEntries{value,index->[(value):index]}
                for(Map row:held) {
                    Set ids=row.entry.training_cells.collect{it.cell_id} as Set
                    if (ids.isEmpty()) {
                        predictions[row.entry_id] = ClassifierEvaluation.objectConfusion(
                            [], [], request.required_classes as List<String>)
                        continue
                    }
                    List liveCells=[]
                    NativeCellScope.forEach(row.image, { cell ->
                        if(cell.isCell() && ids.contains(cell.metadata.get("cell_id"))) liveCells.add(cell)
                    } as Consumer)
                    prepareObjectFeatures(model.configuration,row.image,liveCells,row.entry.snapshot)
                    Map selected=validateLiveObjectCells(row.image,row.entry,model.request,names,responseIndex,
                        liveCells.collectEntries {[(it.metadata.get("cell_id") as String):it]})
                    List detached=selected.cells.collect {cell ->
                        def copy=PathObjects.createCellObject(cell.ROI.duplicate(),cell.nucleusROI?.duplicate())
                        names.each{name->copy.measurementList.put(name,cell.measurementList.get(name))};copy
                    }
                    require(model.classifier.classifyObjects(row.image,detached,true)==detached.size(),"Object fold predictions lack exact coverage")
                    predictions[row.entry_id] = ClassifierEvaluation.objectConfusion(
                        selected.responses as List, detached.collect { it.classification as String },
                        request.required_classes as List<String>)
                }
                predictions
            },{model->model.close()})
        Map finalModel=fitted.final_model
        new LinkedHashMap(finalModel.fit)+[classifier:finalModel.classifier,close:finalModel.close,model_selection_report:fitted.report]
    }

    private static Map fitGroupedSelection(Map request,List<ImageData> images,List<ImageData> views,Map support,Map api,
            Map reviewPolicy,Map reviewScope,Path projectRoot,Path scratchRoot) {
        Map config=request.model_configuration
        List rows=[]
        request.training_entries.eachWithIndex {entry,int i ->
            require(entry.entry_state.split=="training","Only development subjects can enter the tuning operation")
            rows.add([entry_id:entry.entry_state.project_entry_id,subject_id:entry.entry_state.subject_id,
                classes:entry.evidence_rows.collect{it.class_id}.unique().sort(),entry:entry,image:images[i],view:views[i]])
        }
        Map<Integer,List> candidateReferences=[:]
        Map<Integer,List> candidateScores=[:]
        Map assignment=org.mif.classification.GroupedCrossValidation.folds(rows,(config.folds as Number).intValue(),
            (request.random_seed as Number).intValue())
        List foldIds=assignment.values().toList().unique().sort()
        Path scratch=Files.createTempDirectory(scratchRoot,"cv-review-")
        Map<Integer,List<Map>> candidateFiles=[:]
        Map subjectMatrices=new TreeMap()
        int bestCandidate=-1
        double bestScore=Double.NEGATIVE_INFINITY
        Map fitted=null
        try {
        fitted=api.run(rows,request.required_classes,config.candidates,(config.folds as Number).intValue(),
            (request.random_seed as Number).intValue(),
            {candidate,training,seed ->
                Map inner=new LinkedHashMap(request);
            inner.model_configuration=candidate;
            inner.training_entries=training.collect{it.entry}
                fitSelectionCandidate(inner,training.collect{it.image},training.collect{it.view},support)
            },
            {model,held ->
                int candidate=config.candidates.indexOf(model.authored_configuration)
                Map predictions=scoreSelectionEntries(model,held,request.required_classes,support,
                    candidateReferences.computeIfAbsent(candidate){[]},
                    candidateScores.computeIfAbsent(candidate){[]})
                int fold=assignment[held.first().subject_id] as int
                Path file=scratch.resolve("candidate-${candidate}-fold-${fold}.json")
                byte[] encoded=GsonTools.getInstance().toJson(model.classifier,PixelClassifier.class).getBytes(StandardCharsets.UTF_8)
                require(encoded.length<=128L*1024*1024,"A cross-validation review classifier exceeds the interactive model size limit.")
                Files.write(file,encoded,StandardOpenOption.CREATE_NEW)
                Set heldIds=held.collect {it.entry_id} as Set
                reviewPolicy.attach(file,reviewScope+[entries:reviewScope.entries.findAll {key,value->heldIds.contains(key)}])
                candidateFiles.computeIfAbsent(candidate){[]}.add([fold:fold,path:file,
                    held_out_subject_ids:held.collect {it.subject_id}.unique().sort()])
                int width=request.required_classes.size()
                held.each {row->
                    List matrix=org.mif.classification.GroupedCrossValidation.matrix(predictions[row.entry_id],width)
                    List pooled=subjectMatrices.computeIfAbsent(row.subject_id){(0..<width).collect {(0..<width).collect {0L}}}
                    for(int i=0;i<width;i++)for(int j=0;j<width;j++)pooled[i][j]+=matrix[i][j]
                }
                if(fold==foldIds.last()) {
                    double score=org.mif.classification.GroupedCrossValidation.score(subjectMatrices,width)
                    if(score>bestScore) {
                        if(bestCandidate>=0) {
                            candidateFiles.remove(bestCandidate).each {Files.delete(it.path as Path)}
                            candidateScores.remove(bestCandidate)
                        }
                        bestCandidate=candidate;bestScore=score
                    } else {
                        candidateFiles.remove(candidate).each {Files.delete(it.path as Path)}
                        candidateScores.remove(candidate)
                    }
                    subjectMatrices.clear()
                }
                predictions
            },
            {model -> model.close()})
        int selected=(fitted.report.selected_candidate_index as Number).intValue()
        require(selected==bestCandidate,"The retained fold models differ from the selected candidate.")
        Path directory=projectRoot.resolve("classifiers/cv_review")
        require(!Files.isSymbolicLink(directory.parent) && !Files.isSymbolicLink(directory),
            "The cross-validation review resource directory is unsafe.")
        Files.createDirectories(directory)
        List models=[]
        candidateFiles[selected].sort {it.fold}.each {row->
            String relative="classifiers/cv_review/mif-${request.fit_execution_sha256}-fold-${row.fold}.json"
            Path target=projectRoot.resolve(relative)
            require(!Files.exists(target,java.nio.file.LinkOption.NOFOLLOW_LINKS),"A reserved fold review model already exists.")
            Files.move(row.path as Path,target)
            byte[] bytes=Files.readAllBytes(target)
            models.add([fold:row.fold,project_relative_path:relative,sha256:sha256Bytes(bytes),byte_count:bytes.length,
                held_out_subject_ids:row.held_out_subject_ids])
        }
        fitted.report.fold_resources=[schema:"mif-cv-fold-resources/v1",fit_execution_sha256:request.fit_execution_sha256,
            selected_candidate_index:selected,models:models]
        List references=candidateReferences[selected]
        references.each {it.fold=fitted.report.folds_by_subject[it.subject_id]}
        references.sort {a,b -> a.subject_id <=> b.subject_id ?: a.project_entry_id <=> b.project_entry_id ?: a.annotation_uuid <=> b.annotation_uuid}
        fitted.report.reference_observations=[membership:"sample_center",sample_unit:isKronos(config.candidates[selected])?"patches":"pixels",rows:references]
        List scoreRows = candidateScores[selected] ?: []
        if (!scoreRows.isEmpty() && scoreRows.size() == rows.size()) {
            scoreRows.each { row ->
                row.fold = fitted.report.folds_by_subject[row.subject_id]
                row.model_sha256 = models.find { it.fold == row.fold }.sha256
            }
            scoreRows.sort { a,b -> a.project_entry_id <=> b.project_entry_id }
            fitted.report.pixel_scores = ClassifierPrecisionRecall.evidence("cross_validation",
                request.fit_execution_sha256 as String, request.required_classes as List<String>, scoreRows)
        }
        Map finalModel=fitted.final_model
        return new LinkedHashMap(finalModel.fit)+[classifier:finalModel.classifier,close:finalModel.close,
            model_selection_report:fitted.report]
        } catch(Throwable error) {
            if(fitted!=null)fitted.final_model.close()
            throw error
        } finally {
            Files.list(scratch).withCloseable {files->files.forEach {Files.deleteIfExists(it)}}
            Files.deleteIfExists(scratch)
        }
    }

    private static void writeCompletedResult(Path resultPath, Map request,
                                             String qupathVersion,
                                             List<String> entryIds,
                                             Map fit,
                                             Path targetPath) {
        long byteCount = Files.size(targetPath)
        require(Files.isRegularFile(targetPath) &&
            !Files.isSymbolicLink(targetPath) && byteCount > 0L &&
            byteCount <= MAX_CLASSIFIER_BYTES,
            "native classifier target is not a bounded regular file")
        Map result = [
            schema_name: "mif-qupath-native-training-result",
            schema_version: 1,
            status: "completed",
            request_envelope_sha256: request.request_envelope_sha256,
            authority_layout_sha256: request.authority_layout_sha256,
            workspace_storage_scope_sha256:
                request.workspace_storage_scope_sha256,
            active_mutation_run_sha256: request.active_mutation_run_sha256,
            project_identity_sha256: request.project_identity_sha256,
            acquisition_record_sha256: request.acquisition_record_sha256,
            fencing_token_sha256: request.fencing_token_sha256,
            fit_plan_sha256: request.fit_plan_sha256,
            fit_execution_sha256: request.fit_execution_sha256,
            classifier_id: request.classifier_id,
            classifier_kind: request.classifier_kind,
            native_classifier_type: request.native_classifier_type,
            classifier_version: request.classifier_version,
            resource_name: request.resource_name,
            project_relative_path: request.project_relative_path,
            qupath_version: qupathVersion,
            training_entry_ids: entryIds.unique(false).sort(),
            training_material_evidence_sha256:
                request.training_material_evidence_sha256,
            required_classes: request.required_classes,
            effective_output_classes:
                (nativeConfiguration(request.model_configuration as Map).output_labels.ordered_labels as List)
                    .collect { it.canonical_class_id },
            feature_spec_sha256: request.feature_spec_sha256,
            model_configuration_sha256:
                request.model_configuration_sha256,
            random_seed: request.random_seed,
            observed_entry_sample_counts: fit.entry_counts,
            observed_class_sample_counts: fit.class_counts,
            training_sample_count: fit.sample_count,
            training_feature_count: fit.feature_count,
            native_classifier_sha256: sha256File(targetPath),
            native_classifier_byte_count: byteCount,
        ]
        require((fit.entry_counts as Map).values().sum() == fit.sample_count &&
            (fit.class_counts as Map).values().sum() == fit.sample_count &&
            (fit.sample_count as int) > 0 && (fit.feature_count as int) > 0,
            "native training result sample counts are inconsistent")
        if(request.model_configuration.configuration_schema=="qupath-grouped-cross-validation/v1") {
            require(fit.model_selection_report instanceof Map,"Grouped tuning did not return its selection report")
            result.schema_version=2;
            result.model_selection_report=fit.model_selection_report
        }
        if(fit.marker_statistics_receipt!=null)result.marker_statistics_receipt=fit.marker_statistics_receipt
        result.result_envelope_sha256 = canonicalSha256(result)
        byte[] bytes = canonicalBytes(result)
        require(!Files.exists(resultPath),
            "native-training result member is not fresh")
        Files.createDirectories(resultPath.parent)
        Path temporary = resultPath.resolveSibling(
            ".${resultPath.fileName}.${UUID.randomUUID()}.tmp")
        Files.write(temporary, bytes)
        try {
            Files.move(temporary, resultPath, StandardCopyOption.ATOMIC_MOVE)
        } catch (AtomicMoveNotSupportedException ignored) {
            Files.move(temporary, resultPath)
        }
    }

    static void execute(Project<BufferedImage> project,
                        Map<String, String> environment) {
        require(project != null,
            "native training requires the project supplied by the command")
        Set<String> present = environment.keySet().findAll { name ->
            String upper = name.toUpperCase(Locale.ROOT)
            upper.startsWith("MIF_QUPATH_NATIVE_TRAINING_") ||
                upper == "MIF_QUPATH_PROJECT_LAUNCH_NONCE"
        } as Set<String>
        require(present == ENVIRONMENT_NAMES &&
            environment["MIF_QUPATH_NATIVE_TRAINING_CONTEXT_VERSION"] == "1",
            "native-training environment names are incomplete or unknown")
        Path requestPath = environmentPath(environment,
            "MIF_QUPATH_NATIVE_TRAINING_REQUEST_PATH")
        Path resultPath = environmentPath(environment,
            "MIF_QUPATH_NATIVE_TRAINING_RESULT_PATH")
        require(requestPath.parent == resultPath.parent &&
            requestPath != resultPath,
            "native-training exchange members are not siblings")
        AuthenticatedRequest authenticated = parseCanonicalRequest(requestPath,
            environment["MIF_QUPATH_NATIVE_TRAINING_REQUEST_SHA256"])
        Map request = validateRequest(authenticated.request)
        validateRuntimeExchangeRef(request, request.request_member_ref,
            "request", requestPath)
        validateRuntimeExchangeRef(request, request.result_member_ref,
            "result", resultPath)
        validateLaunchNonce(request,
            environment["MIF_QUPATH_PROJECT_LAUNCH_NONCE"])
        String qupathVersion = GeneralTools.getVersion()
        require(qupathVersion == request.expected_qupath_version,
            "running QuPath version differs from the request")
        validateProjectIdentity(project, request)
        List<Map> bindings = validateBindings(request)
        List<Map> entries = validateTrainingEntries(request, bindings)
        List<String> entryIds = entries.collect { entry ->
            ((request.native_classifier_type == "pixel" ?
                entry.entry_state : entry.snapshot) as Map)
                .project_entry_id as String
        }
        List<ProjectImageEntry> projectEntries = entryIds.collect {
            resolveEntry(project, it)
        }
        Path projectPath = project.getPath().toAbsolutePath().normalize()
        require(Files.isRegularFile(projectPath) &&
            !Files.isSymbolicLink(projectPath),
            "running project path is not a regular qpproj")
        Path projectRoot = projectPath.parent
        require(!requestPath.startsWith(projectRoot) &&
            !resultPath.startsWith(projectRoot),
            "native-training exchange members cannot be inside the project")
        Path targetPath = projectRoot.resolve(
            request.project_relative_path as String).normalize()
        require(targetPath.startsWith(projectRoot) &&
            targetPath.fileName.toString() ==
                "mif-${request.fit_execution_sha256}.json",
            "native classifier target escaped the project resource root")
        ResourceManager.Manager manager =
            request.native_classifier_type == "pixel" ?
                project.getPixelClassifiers() :
                project.getObjectClassifiers()
        assertReservedResourceAbsent(project, request, manager, targetPath)
        // Authenticate deterministic review dependencies before fitting or
        // publishing a model, then reuse the captured scope during attachment.
        Map pixelReview = request.native_classifier_type == "pixel" ?
            loadRegisteredSupport(projectPath, request.script_sha256 as String,
                "compartment_analysis/classifier_review_policy.groovy").api : null
        Map reviewScope = null
        List<ImageData<BufferedImage>> live = []
        List<ImageData<BufferedImage>> filteredViews = []
        Map fit
        if(request.native_classifier_type=="object") {
            supervisedSupport=loadRegisteredSupport(projectPath,request.script_sha256 as String,
                "cell_analysis/supervised_classifier_support.groovy").api
            Map base=nativeConfiguration(request.model_configuration as Map)
            if(base.feature_source!=null) {
                cellFeatureSupport=loadRegisteredSupport(projectPath,request.script_sha256 as String,
                    "cell_analysis/kronos_cell_features.groovy").api
                cellWorkers=loadRegisteredSupport(projectPath,request.script_sha256 as String,
                    "compartment_analysis/kronos_worker_client.groovy").api
                markerStatistics=loadRegisteredSupport(projectPath,request.script_sha256 as String,
                    "compartment_analysis/kronos_marker_statistics.groovy").api
                objectFeatureCache=requestPath.parent
            }
        }
        Map kronosSupport=null
        Object kronosNativeModel=null
        OpenCVStatModel conventionalPixelModel=null
        Object classifier
        try {
            projectEntries.eachWithIndex { entry, int index ->
                Map owner = (request.native_classifier_type == "pixel" ?
                    entries[index].entry_state : entries[index].snapshot) as Map
                validateLiveEntry(entry, owner)
                live << (entry.readImageData() as ImageData<BufferedImage>)
            }
            if (pixelReview != null)
                reviewScope = pixelReview.capture(request, live) as Map
            List<Map> labels =
                nativeConfiguration(request.model_configuration as Map).output_labels.ordered_labels as List<Map>
            if(request.model_configuration.configuration_schema=="qupath-grouped-cross-validation/v1") {
                if(request.native_classifier_type=="pixel")
                    for(int i=0;i<entries.size();i++)filteredViews.add(filteredPixelView(live[i],request.training_entries[i],request))
                Map base=request.model_configuration.candidates.first()
                if(isKronos(base))kronosSupport=loadKronosSupport(projectPath,request.script_sha256 as String)
                Map cvApi=loadRegisteredSupport(projectPath,request.script_sha256 as String,
                    "compartment_analysis/grouped_cross_validation.groovy").api
                fit=request.native_classifier_type=="object" ? fitGroupedObjects(request,live,cvApi) :
                    fitGroupedSelection(request,live,filteredViews,kronosSupport,cvApi,pixelReview,reviewScope,projectRoot,requestPath.parent)
                classifier=fit.classifier
            } else {
                if (request.native_classifier_type == "pixel") {
                    entries.eachWithIndex { entry, int index ->
                        filteredViews << filteredPixelView(live[index], entry,
                            request)
                    }
                    if(isKronos(request.model_configuration as Map)) {
                        kronosSupport=loadKronosSupport(projectPath,request.script_sha256 as String)
                        fit=kronosSupport.training.prepare(request,live,kronosSupport)
                    } else fit = preparePixelFit(request, filteredViews, entryIds, labels)
                } else {
                    fit = fitObjectClassifier(request, entries, live, labels)
                }
                Map configuration = nativeConfiguration(request.model_configuration as Map)
                boolean svmModel = modelKey(configuration,
                    request.native_classifier_type == "object") == "svm"
                OpenCVStatModel model = null
                if(kronosSupport!=null) {
                    kronosNativeModel=kronosSupport.training.fitNative(configuration,fit.train_data as TrainData,(request.random_seed as Number).intValue())
                    classifier=fit.classifier(kronosNativeModel)
                } else if(request.native_classifier_type=="object") {
                    classifier=supervisedSupport.train(fit.configuration,(request.random_seed as Number).intValue(),fit)
                } else model = svmModel ?
                    trainSvm(configuration, (request.random_seed as Number).intValue(),
                        fit.train_data as TrainData) :
                    trainRTrees(configuration, (request.random_seed as Number).intValue(),
                        fit.train_data as TrainData)
                conventionalPixelModel=model
                if (kronosSupport!=null) {
                    require(classifier instanceof PixelClassifier,"The KRONOS final resource is not a native pixel classifier")
                } else if (request.native_classifier_type == "pixel") {
                    Map<Integer, PathClass> classificationLabels =
                        (fit.labels as Map<PathClass, Integer>).collectEntries {
                            pathClass, label -> [(label): pathClass]
                        }
                    PixelClassifierMetadata metadata =
                        new PixelClassifierMetadata.Builder()
                            .inputResolution(fit.resolution as PixelCalibration)
                            .inputShape(512, 512)
                            .setChannelType(ChannelType.CLASSIFICATION)
                            .classificationLabels(classificationLabels)
                            .build()
                    classifier = PixelClassifiers.createClassifier(model,
                        fit.serialized_op as ImageDataOp, metadata, true)
                }
            }
            String resourceName = request.resource_name as String
            assertReservedResourceAbsent(project, request, manager, targetPath)
            manager.put(resourceName, classifier)
            if (pixelReview != null)
                pixelReview.attach(targetPath, fit.model_selection_report?.fold_resources ?
                    reviewScope+[fold_resources:fit.model_selection_report.fold_resources] : reviewScope)
            Object reopened = manager.get(resourceName)
            require((request.native_classifier_type == "pixel" &&
                    reopened instanceof PixelClassifier) ||
                (request.native_classifier_type == "object" &&
                    reopened instanceof ObjectClassifier),
                "reopened native classifier family differs from the request")
            require(Files.isRegularFile(targetPath,
                java.nio.file.LinkOption.NOFOLLOW_LINKS),
                "native resource manager did not create the reserved leaf")
            writeCompletedResult(resultPath, request, qupathVersion,
                entryIds, fit, targetPath)
        } finally {
            if(request.model_configuration.configuration_schema=="qupath-grouped-cross-validation/v1") {
                if(fit!=null)fit.close()
            } else if(kronosSupport!=null) {
                try { kronosNativeModel?.close() } finally { if(fit!=null)fit.close() }
            } else if(request.native_classifier_type=="object") {
                try {if(classifier!=null)supervisedSupport.close(classifier)}
                finally {if(fit!=null)fit.close()}
            } else {
                try {conventionalPixelModel?.getStatModel()?.close()}
                finally {if(fit!=null)fit.close()}
            }
            filteredViews.reverseEach { view ->
                try { view.close() } catch (Exception ignored) { }
            }
            live.reverseEach { imageData ->
                try { imageData.close() } catch (Exception ignored) { }
            }
        }
    }
}


if (System.getenv("MIF_IMAGE_CACHE_BYTES") != null) {
    qupath.lib.images.servers.ImageServerProvider.setCache(
        com.github.benmanes.caffeine.cache.Caffeine.newBuilder()
            .maximumSize(0L).build().asMap(), BufferedImage)
}
MifNativeTrainingAssetV1.execute(
    getProject() as Project<BufferedImage>, System.getenv())
