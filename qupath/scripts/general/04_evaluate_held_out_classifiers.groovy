import org.mif.cells.NativeCellScope
import org.mif.classification.ClassifierPrecisionRecall
import org.mif.classification.ClassifierEvaluation
import org.mif.classification.ObjectTrainingRows
import java.util.function.Consumer
import com.google.gson.Gson
import com.google.gson.GsonBuilder
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
import java.util.TreeMap
import java.util.UUID
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import qupath.lib.classifiers.object.ObjectClassifier
import qupath.lib.classifiers.object.ObjectClassifiers
import qupath.lib.classifiers.pixel.PixelClassifier
import qupath.lib.common.GeneralTools
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServer
import qupath.lib.measurements.MeasurementList
import qupath.lib.measurements.MeasurementListFactory
import qupath.lib.objects.classes.PathClass
import qupath.lib.objects.PathObject
import qupath.lib.objects.PathObjects
import qupath.lib.projects.Project
import qupath.lib.projects.ProjectIO
import qupath.lib.projects.ProjectImageEntry
import qupath.lib.regions.RegionRequest
import qupath.lib.roi.ROIs
import qupath.opencv.ml.pixel.PixelClassifiers
import qupath.opencv.ml.pixel.PixelClassifierTools


/** Read-only, exact-cover QuPath 0.7 held-out classifier evaluator. */
final class MifHeldOutEvaluatorAssetV1 {
    private static Map kronosSupport=null
    private static Map objectSupport=null
    private static Map cellFeatureSupport=null
    private static Map cellWorkers=null
    private static Path objectFeatureCache=null
    private static final Gson GSON = new GsonBuilder()
        .disableHtmlEscaping()
        .serializeNulls()
        .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE)
        .create()
    private static final int MAX_REQUEST_BYTES = 16 * 1024 * 1024
    private static final long MAX_CLASSIFIER_BYTES =
        64L * 1024L * 1024L * 1024L
    private static final String SCRIPT_ASSET_ID =
        "qupath/scripts/general/04_evaluate_held_out_classifiers.groovy"
    private static final String PROJECT_IDENTITY_KEY =
        "mif_pipeline.project_identity_record_v1"
    private static final Set<String> REQUEST_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256",
        "workspace_storage_scope_sha256", "active_mutation_run_sha256",
        "project_identity_sha256", "fit_plan_sha256",
        "fit_execution_sha256", "checkpoint_entry_sha256",
        "fit_material_sha256", "classifier_id", "classifier_kind",
        "classifier_engine", "native_classifier_type", "pixel_sampling_method",
        "classifier_version",
        "project_relative_path", "native_classifier_sha256",
        "native_classifier_byte_count", "declared_class_order",
        "native_output_labels", "feature_spec", "evaluation_entries",
        "expected_qupath_version", "script_asset_id", "launch_nonce_sha256",
        "request_member_ref", "result_member_ref",
        "request_envelope_sha256",
    ] as Set<String>
    private static final Set<String> MEMBER_REF_FIELDS = [
        "schema_name", "schema_version", "authority_layout_sha256", "family",
        "key_sha256", "suffix", "media_type",
    ] as Set<String>
    private static final Set<String> OUTPUT_LABEL_FIELDS = [
        "canonical_class_id", "qupath_label", "native_path_class_id",
        "ignored",
    ] as Set<String>
    private static final Set<String> PIXEL_ENTRY_FIELDS = [
        "entry_kind", "project_entry_id", "subject_id", "split",
        "image_identity_sha256", "saved_state_sha256", "channel_features",
        "targets",
    ] as Set<String>
    private static final Set<String> PIXEL_TARGET_FIELDS = [
        "project_entry_id", "subject_id", "annotation_uuid",
        "expected_class_id", "path_class_id", "normalized_roi_sha256",
        "material_parent_id", "material_parent_type",
        "material_parent_type_id", "material_parent_revision_sha256",
        "analysis_domain_id", "analysis_domain_revision_sha256",
        "domain_set_sha256", "segmentation_generation_id",
        "measurement_generation_id", "owner_evidence_sha256",
    ] as Set<String>
    private static final Set<String> OBJECT_ENTRY_FIELDS = [
        "entry_kind", "project_entry_id", "subject_id", "split",
        "image_identity_sha256", "saved_state_sha256", "section_id",
        "analysis_domain_id", "analysis_domain_revision_sha256",
        "domain_set_sha256", "segmentation_generation_id",
        "measurement_generation_id", "source_snapshot_sha256",
        "evaluation_cells",
    ] as Set<String>
    private static final Set<String> OBJECT_CELL_FIELDS = [
        "project_entry_id", "cell_id", "expected_class_id", "cell_geometry",
        "cell_geometry_sha256", "nucleus_geometry",
        "nucleus_geometry_sha256", "measurement_values",
        "measurement_vector_sha256", "owner_evidence_sha256",
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
        require(value instanceof Long, "${label} must be an integer")
        long result = value as long
        require(result >= minimum && result <= Integer.MAX_VALUE,
            "${label} must be an in-range integer")
        result as int
    }

    private static long requireLong(Object value, String label,
                                    long minimum = 0L) {
        require(value instanceof Long && (value as long) >= minimum,
            "${label} must be an in-range integer")
        value as long
    }

    private static double requireFinite(Object value, String label) {
        require(value instanceof Number &&
            Double.isFinite((value as Number).doubleValue()),
            "${label} must be finite")
        (value as Number).doubleValue()
    }

    private static boolean requireBoolean(Object value, String label) {
        require(value instanceof Boolean, "${label} must be boolean")
        value as boolean
    }

    private static List<String> requireTextList(Object value, String label) {
        require(value instanceof List && !(value as List).isEmpty(),
            "${label} must be a nonempty list")
        List<String> result = (value as List).collect {
            requireText(it, label)
        }
        require(result.toSet().size() == result.size(),
            "${label} must be unique")
        result
    }

    private static Object canonicalValue(Object value) {
        if (value instanceof Map) {
            Map<String, Object> sorted = new TreeMap<>()
            (value as Map).each { key, nested ->
                require(key instanceof String, "canonical JSON key is invalid")
                sorted[key as String] = canonicalValue(nested)
            }
            return sorted
        }
        if (value instanceof List)
            return (value as List).collect { canonicalValue(it) }
        if (value instanceof Double || value instanceof Float)
            require(Double.isFinite((value as Number).doubleValue()),
                "canonical JSON contains a nonfinite number")
        value
    }

    private static byte[] canonicalBytes(Object value) {
        GSON.toJson(canonicalValue(value)).getBytes(StandardCharsets.UTF_8)
    }

    private static Object evaluationDigestValue(Object value) {
        if (value == null)
            return ["n"]
        if (value instanceof Boolean)
            return ["b", value as boolean ? "1" : "0"]
        if (value instanceof Byte || value instanceof Short ||
                value instanceof Integer || value instanceof Long)
            return ["i", value.toString()]
        if (value instanceof Double || value instanceof Float) {
            double number = requireFinite(value, "evaluation digest double")
            String bits = Long.toHexString(
                Double.doubleToRawLongBits(number)).padLeft(16, "0")
            return ["f", bits]
        }
        if (value instanceof String)
            return ["s", (value as String).getBytes(StandardCharsets.UTF_8)
                .encodeHex().toString()]
        if (value instanceof Map) {
            List<List> entries = []
            (value as Map).each { key, item ->
                require(key instanceof String,
                    "evaluation digest object key is invalid")
                entries << [
                    (key as String).getBytes(StandardCharsets.UTF_8)
                        .encodeHex().toString(),
                    evaluationDigestValue(item),
                ]
            }
            entries.sort { left, right ->
                (left[0] as String) <=> (right[0] as String)
            }
            return ["m", entries]
        }
        if (value instanceof List)
            return ["l", (value as List).collect {
                evaluationDigestValue(it)
            }]
        throw new IllegalStateException("evaluation digest value is invalid")
    }

    private static String evaluationSha256(Object value) {
        sha256Bytes(canonicalBytes(evaluationDigestValue(value)))
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

    private static byte[] readBoundedRequest(Path requestPath) {
        ByteArrayOutputStream output = new ByteArrayOutputStream()
        try (FileChannel channel = FileChannel.open(requestPath,
                StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)) {
            require(channel.size() > 0L && channel.size() <= MAX_REQUEST_BYTES,
                "held-out evaluation request size is invalid")
            ByteBuffer buffer = ByteBuffer.allocate(64 * 1024)
            while (true) {
                int remaining = MAX_REQUEST_BYTES + 1 - output.size()
                require(remaining > 0,
                    "held-out evaluation request size is invalid")
                buffer.clear()
                buffer.limit(Math.min(buffer.capacity(), remaining))
                int count = channel.read(buffer)
                if (count < 0)
                    break
                if (count > 0)
                    output.write(buffer.array(), 0, count)
            }
        } catch (IllegalStateException error) {
            throw error
        } catch (IOException error) {
            throw new IllegalStateException(
                "held-out evaluation request is not a bounded regular file",
                error)
        }
        byte[] bytes = output.toByteArray()
        require(bytes.length > 0, "held-out evaluation request is empty")
        bytes
    }

    private static Map parseCanonicalRequest(Path requestPath) {
        byte[] bytes = readBoundedRequest(requestPath)
        Object parsed
        try {
            parsed = GSON.fromJson(
                strictUtf8(bytes, "held-out evaluation request"), Object)
        } catch (RuntimeException error) {
            throw new IllegalStateException(
                "held-out evaluation request JSON is invalid", error)
        }
        requireClosed(parsed, REQUEST_FIELDS, "held-out evaluation request")
        parsed as Map
    }

    private static void validateFeatureSpec(Map request) {
        Map feature = request.feature_spec as Map
        if (request.native_classifier_type == "pixel") {
            requireClosed(feature, ["schema", "ordered_channels"] as Set,
                "pixel feature specification")
            require(feature.schema == "qupath-pixel-channel-features/v1" &&
                feature.ordered_channels instanceof List &&
                !(feature.ordered_channels as List).isEmpty(),
                "pixel feature specification is invalid")
            List<Map> rows = feature.ordered_channels as List<Map>
            rows.each { row ->
                requireClosed(row, [
                    "canonical_channel_id", "qupath_channel_index",
                    "observed_qupath_channel_name",
                ] as Set, "ordered channel")
                requireText(row.canonical_channel_id, "canonical channel ID")
                requireInteger(row.qupath_channel_index,
                    "QuPath channel index")
                requireText(row.observed_qupath_channel_name,
                    "observed channel name")
            }
            require(rows.collect { it.canonical_channel_id }.toSet().size() ==
                    rows.size() &&
                rows.collect { it.qupath_channel_index }.toSet().size() ==
                    rows.size(), "pixel channel features are ambiguous")
        } else {
            requireClosed(feature, ["schema", "ordered_measurements"] as Set,
                "object feature specification")
            require(feature.schema == "qupath-object-feature-spec/v1" &&
                feature.ordered_measurements instanceof List &&
                !(feature.ordered_measurements as List).isEmpty(),
                "object feature specification is invalid")
            List<String> names =
                (feature.ordered_measurements as List<Map>).collect { row ->
                    requireClosed(row,
                        ["measurement_name", "semantic_role"] as Set,
                        "ordered measurement")
                    requireText(row.semantic_role, "measurement semantic role")
                    requireText(row.measurement_name, "measurement name")
                }
            require(names.toSet().size() == names.size(),
                "ordered measurements are ambiguous")
        }
    }

    private static List<Map> validateOutputLabels(Map request,
                                                   List<String> declared) {
        require(request.native_output_labels instanceof List &&
            !(request.native_output_labels as List).isEmpty(),
            "native output labels are empty")
        List<Map> rows = request.native_output_labels as List<Map>
        List<Integer> labels = []
        List<String> classes = []
        List<String> nativePathClasses = []
        List<String> ordinary = []
        int ignored = 0
        rows.each { row ->
            requireClosed(row, OUTPUT_LABEL_FIELDS, "native output label")
            String classId = requireText(row.canonical_class_id,
                "native output class")
            int label = requireInteger(row.qupath_label, "native output label")
            String nativePathClass = requireText(row.native_path_class_id,
                "native output PathClass")
            boolean isIgnored = requireBoolean(row.ignored,
                "native output ignored flag")
            require(request.native_classifier_type == "object" || label <= 255,
                "pixel output label exceeds UINT8")
            labels << label
            classes << classId
            nativePathClasses << nativePathClass
            if (isIgnored) {
                require(request.native_classifier_type == "pixel" &&
                    !declared.contains(classId), "ignored output is invalid")
                ignored++
            } else {
                ordinary << classId
            }
        }
        require(labels.toSet().size() == labels.size() &&
            classes.toSet().size() == classes.size() &&
            nativePathClasses.toSet().size() == nativePathClasses.size() &&
            ordinary.toSet() == declared.toSet() &&
            ordinary.size() == declared.size(),
            "native output labels differ from declared classes")
        if (request.native_classifier_type == "object")
            require(ignored == 0 && labels == (0..<labels.size()).toList() &&
                nativePathClasses == classes,
                "object output labels are invalid")
        else
            require(ignored <= 1, "pixel output has multiple ignored labels")
        rows
    }

    private static void validateGeometry(Object raw, String label) {
        if (label == "nucleus_geometry" && raw instanceof Map && (raw as Map).isEmpty())
            return
        requireClosed(raw, ["type", "coordinates"] as Set, label)
        Map geometry = raw as Map
        require(geometry.type in ["Point", "Polygon", "MultiPolygon"],
            "${label} type is unsupported")
        Closure validateNode
        validateNode = { Object node, int depth ->
            require(node instanceof List && !(node as List).isEmpty(),
                "${label} coordinates are invalid")
            if (depth == 0) {
                require((node as List).size() == 2,
                    "${label} point is invalid")
                requireFinite((node as List)[0], "${label} x")
                requireFinite((node as List)[1], "${label} y")
            } else {
                (node as List).each { validateNode(it, depth - 1) }
            }
        }
        int depth = geometry.type == "Point" ? 0 :
            (geometry.type == "Polygon" ? 2 : 3)
        validateNode(geometry.coordinates, depth)
    }

    private static List<Double> pointLocatorCoordinates(Map row,
                                                         double liveX,
                                                         double liveY) {
        boolean cellPoint = row.cell_geometry.type == "Point"
        boolean nucleusPoint = row.nucleus_geometry.type == "Point"
        require(cellPoint == nucleusPoint,
            "cell and nucleus geometry must both be Point locators or full geometry")
        if (!cellPoint)
            return null
        require(evaluationSha256(row.cell_geometry) ==
                evaluationSha256(row.nucleus_geometry),
            "cell and nucleus Point geometry must be the same locator")
        List coordinates = row.cell_geometry.coordinates as List
        double x = (coordinates[0] as Number).doubleValue()
        double y = (coordinates[1] as Number).doubleValue()
        require(Double.doubleToRawLongBits(x) ==
                Double.doubleToRawLongBits(liveX) &&
            Double.doubleToRawLongBits(y) ==
                Double.doubleToRawLongBits(liveY),
            "live evaluation cell centroid differs from the Point locator")
        [x, y]
    }

    private static void requireLiveObjectDigests(Map row,
                                                  Object liveCellGeometry,
                                                  Object liveNucleusGeometry,
                                                  List observed,boolean deferred=false) {
        if (row.cell_geometry.type != "Point") {
            require(liveCellGeometry instanceof Map &&
                liveNucleusGeometry instanceof Map &&
                evaluationSha256(liveCellGeometry) ==
                    row.cell_geometry_sha256 &&
                evaluationSha256(liveNucleusGeometry) ==
                    row.nucleus_geometry_sha256,
                "live evaluation cell geometry differs from the request")
        }
        if(!deferred)require(evaluationSha256(observed) == row.measurement_vector_sha256,
            "live evaluation measurement vector differs from the request")
    }

    private static List<Map> validateEntries(Map request,
                                              List<String> declared) {
        require(request.evaluation_entries instanceof List &&
            !(request.evaluation_entries as List).isEmpty(),
            "evaluation entries are empty")
        boolean objectFamily = request.native_classifier_type == "object"
        List<Map> entries = request.evaluation_entries as List<Map>
        List<String> entryIds = []
        List<List<String>> objectKeys = []
        Set<String> exactCells = [] as Set<String>
        entries.each { entry ->
            boolean deferred=objectFamily && entry instanceof Map && entry.containsKey("deferred_feature_evidence")
            requireClosed(entry,
                objectFamily ? OBJECT_ENTRY_FIELDS+(deferred ? ["deferred_feature_evidence"] : []) : PIXEL_ENTRY_FIELDS,
                "evaluation entry")
            require(entry.entry_kind == (objectFamily ? "object" : "pixel"),
                "evaluation entry family differs")
            String entryId = requireText(entry.project_entry_id,
                "project entry ID")
            requireText(entry.subject_id, "subject ID")
            require(entry.split in ["validation", "test"],
                "evaluation entry is not held out")
            requireDigest(entry.image_identity_sha256, "image identity")
            requireDigest(entry.saved_state_sha256, "saved-state digest")
            entryIds << entryId
            if (objectFamily) {
                ["section_id", "segmentation_generation_id",
                    "measurement_generation_id"].each {
                    requireText(entry[it as String], it as String)
                }
                ["analysis_domain_id", "analysis_domain_revision_sha256",
                    "domain_set_sha256", "source_snapshot_sha256"].each {
                    requireDigest(entry[it as String], it as String)
                }
                objectKeys << [entryId, entry.section_id as String, entry.analysis_domain_id as String]
                require(entry.evaluation_cells instanceof List &&
                    !(entry.evaluation_cells as List).isEmpty(),
                    "evaluation cells are empty")
                List<String> cellIds = []
                int width =
                    (request.feature_spec.ordered_measurements as List).size()
                (entry.evaluation_cells as List<Map>).each { cell ->
                    requireClosed(cell, OBJECT_CELL_FIELDS, "evaluation cell")
                    require(cell.project_entry_id == entryId,
                        "evaluation cell belongs to a foreign entry")
                    String cellId = requireText(cell.cell_id, "stable cell ID")
                    require(declared.contains(requireText(cell.expected_class_id,
                        "cell expected class")), "cell truth class is undeclared")
                    ["cell_geometry", "nucleus_geometry"].each { name ->
                        validateGeometry(cell[name as String], name as String)
                        require(requireDigest(cell["${name}_sha256"],
                                "${name} digest") ==
                            evaluationSha256(cell[name as String]),
                            "${name} digest differs")
                    }
                    boolean cellPoint = cell.cell_geometry.type == "Point"
                    boolean nucleusPoint =
                        cell.nucleus_geometry.type == "Point"
                    require(cellPoint == nucleusPoint,
                        "cell and nucleus geometry must both be Point locators or full geometry")
                    if (cellPoint)
                        require(cell.cell_geometry_sha256 ==
                                cell.nucleus_geometry_sha256,
                            "cell and nucleus Point geometry must be the same locator")
                    if(deferred) {
                        require(cell.measurement_values==null && cell.measurement_vector_sha256==null,
                            "Deferred evaluation cells must not contain embedding values")
                    } else {
                    require(cell.measurement_values instanceof List &&
                        (cell.measurement_values as List).size() == width,
                        "measurement vector width differs")
                    (cell.measurement_values as List).each {
                        require(it instanceof Double,
                            "measurement value must be a JSON double")
                        requireFinite(it, "measurement value")
                    }
                    require(requireDigest(cell.measurement_vector_sha256,
                            "measurement-vector digest") ==
                        evaluationSha256(cell.measurement_values),
                        "measurement-vector digest differs")
                    }
                    requireDigest(cell.owner_evidence_sha256,
                        "cell owner-evidence digest")
                    cellIds << cellId
                    require(exactCells.add(entryScopedCellKey(entryId, cellId)),
                        "entry-scoped evaluation cell repeats")
                }
                require(cellIds == cellIds.toSorted() &&
                    cellIds.toSet().size() == cellIds.size(),
                    "evaluation cells are not canonical")
                if(deferred) {
                    Map evidence=entry.deferred_feature_evidence
                    requireClosed(evidence,["schema","source_intent_sha256","automatic_marker_names","cell_input_sha256"].toSet(),
                        "deferred evaluation input evidence")
                    require(evidence.schema=="kronos-cell-input-evidence/v1" && evidence.automatic_marker_names instanceof List &&
                        !evidence.automatic_marker_names.isEmpty() &&
                        evidence.automatic_marker_names.every {it instanceof String && it.trim()} &&
                        evidence.automatic_marker_names.toSet().size()==evidence.automatic_marker_names.size() &&
                        evidence.cell_input_sha256 instanceof Map && evidence.cell_input_sha256.keySet()==cellIds.toSet(),
                        "Deferred evaluation input inventory differs")
                    requireDigest(evidence.source_intent_sha256,"deferred evaluation source intent")
                    evidence.cell_input_sha256.values().each {requireDigest(it,"deferred raw cell input")}
                }
            } else {
                require(java.util.Arrays.equals(canonicalBytes(
                        entry.channel_features),
                    canonicalBytes(request.feature_spec)),
                    "entry channel features differ")
                require(entry.targets instanceof List &&
                    !(entry.targets as List).isEmpty(),
                    "pixel evaluation targets are empty")
                List<String> targetKeys = []
                (entry.targets as List<Map>).each { row ->
                    requireClosed(row, PIXEL_TARGET_FIELDS,
                        "pixel evaluation target")
                    require(row.project_entry_id == entryId &&
                        row.subject_id == entry.subject_id,
                        "pixel target identity is foreign")
                    String annotation = requireText(row.annotation_uuid,
                        "annotation UUID")
                    try {
                        require(UUID.fromString(annotation).toString() == annotation,
                            "annotation UUID is not canonical")
                    } catch (IllegalArgumentException error) {
                        throw new IllegalStateException(
                            "annotation UUID is invalid", error)
                    }
                    require(declared.contains(requireText(row.expected_class_id,
                        "target expected class")), "target truth is undeclared")
                    expectedPixelTruthPathClass(row,
                        request.native_output_labels as List<Map>)
                    requireText(row.material_parent_id,
                        "target material parent ID")
                    require(row.material_parent_type in
                        ["analysis_domain", "primary", "secondary"],
                        "target material parent type is invalid")
                    requireText(row.material_parent_type_id,
                        "target material parent type ID")
                    ["normalized_roi_sha256",
                        "material_parent_revision_sha256",
                        "analysis_domain_id",
                        "analysis_domain_revision_sha256",
                        "domain_set_sha256", "owner_evidence_sha256"].each {
                        requireDigest(row[it as String], it as String)
                    }
                    ["segmentation_generation_id",
                        "measurement_generation_id"].each { name ->
                        require(row[name as String] == null ||
                            requireText(row[name as String], name as String),
                            "${name} is invalid")
                    }
                    targetKeys << "${entryId}\u0000${entry.subject_id}\u0000${annotation}"
                }
                require(targetKeys == targetKeys.toSorted() &&
                    targetKeys.toSet().size() == targetKeys.size(),
                    "pixel targets are not canonical and unique")
            }
        }
        List<List<String>> sortedObjectKeys = objectKeys.toSorted { List<String> left, List<String> right ->
            (left[0] <=> right[0]) ?: (left[1] <=> right[1]) ?: (left[2] <=> right[2])
        }
        require(objectFamily ? objectKeys == sortedObjectKeys && objectKeys.toSet().size() == objectKeys.size() :
            entryIds == entryIds.toSorted() && entryIds.toSet().size() == entryIds.size(),
            "evaluation entries are not canonical and unique")
        entries
    }

    private static Map validateRequest(Map request) {
        require(request.schema_name ==
                "mif-qupath-held-out-classifier-evaluation-request" &&
            request.schema_version == 2L,
            "held-out evaluation request schema is invalid")
        ["authority_layout_sha256", "workspace_storage_scope_sha256",
            "active_mutation_run_sha256", "project_identity_sha256",
            "fit_plan_sha256", "fit_execution_sha256",
            "checkpoint_entry_sha256", "fit_material_sha256",
            "native_classifier_sha256", "launch_nonce_sha256",
            "request_envelope_sha256"].each {
            requireDigest(request[it as String], it as String)
        }
        requireText(request.classifier_id, "classifier ID")
        require(request.classifier_kind in ["pixel", "supervised_phenotype"] &&
            request.classifier_engine == "qupath_native",
            "classifier family is invalid")
        String nativeType = request.classifier_kind == "supervised_phenotype" ?
            "object" : "pixel"
        require(request.native_classifier_type == nativeType,
            "native classifier family differs")
        require(nativeType=="pixel" ? request.pixel_sampling_method in ["qupath-native-classification-grid/v1","kronos-complete-patch-grid/v1"] : request.pixel_sampling_method==null,
            "pixel sampling method differs")
        String fit = request.fit_execution_sha256 as String
        require(request.classifier_version == "mif-fit-${fit}",
            "classifier version differs")
        String expectedPath =
            "classifiers/${nativeType == "pixel" ? "pixel_classifiers" :
                "object_classifiers"}/mif-${fit}.json"
        require(request.project_relative_path == expectedPath,
            "classifier path is not the deterministic standard resource")
        requireLong(request.native_classifier_byte_count,
            "native classifier byte count", 1L)
        List<String> declared = requireTextList(request.declared_class_order,
            "declared class order")
        validateFeatureSpec(request)
        validateOutputLabels(request, declared)
        validateEntries(request, declared)
        require(request.expected_qupath_version == "0.7.0" &&
            request.script_asset_id == SCRIPT_ASSET_ID,
            "QuPath or evaluator asset identity differs")
        Map unsigned = new TreeMap(request)
        String envelope = requireDigest(unsigned.remove(
            "request_envelope_sha256"), "request envelope digest")
        require(evaluationSha256(unsigned) == envelope,
            "request envelope digest is invalid")
        request
    }

    private static String requestIdentity(Map request) {
        Map core = new TreeMap(request)
        core.remove("request_member_ref")
        core.remove("result_member_ref")
        core.remove("request_envelope_sha256")
        evaluationSha256(core)
    }

    private static void validateRuntimeRef(Map request, Object raw,
                                           String direction, Path path) {
        requireClosed(raw, MEMBER_REF_FIELDS,
            "${direction} runtime exchange ref")
        Map ref = raw as Map
        Map scope = [
            workspace_storage_scope_sha256:
                request.workspace_storage_scope_sha256,
            active_mutation_run_sha256:
                request.active_mutation_run_sha256,
            role: "classifier_held_out_evaluation",
            fit_material_sha256: request.fit_material_sha256,
            request_identity_sha256: requestIdentity(request),
        ]
        Map preimage = [
            authority_layout_sha256: request.authority_layout_sha256,
            family: "runtime_exchange",
            scope: scope,
            natural_key: [direction: direction],
        ]
        String key = requireDigest(ref.key_sha256,
            "${direction} runtime exchange key")
        require(ref.schema_name == "mif-authority-member-ref" &&
            ref.schema_version == 2L &&
            ref.authority_layout_sha256 == request.authority_layout_sha256 &&
            ref.family == "runtime_exchange" && ref.suffix == "json" &&
            ref.media_type == "application/json" &&
            key == canonicalSha256(preimage) &&
            path.fileName.toString() == "runtime_exchange-${key}.json",
            "${direction} runtime exchange ref is invalid")
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
        Map api=new GroovyShell(MifHeldOutEvaluatorAssetV1.class.classLoader).evaluate(new String(helperBytes,StandardCharsets.UTF_8),relative) as Map
        [api:api,bundle:bundle]
    }

    private static void requireExactCover(List expectedKeys, List actualKeys,
                                          String label) {
        require(expectedKeys == actualKeys &&
            expectedKeys.toSet().size() == expectedKeys.size(),
            "${label} does not exactly cover the request")
    }

    private static String entryScopedCellKey(String entryId, String cellId) {
        "${requireText(entryId, "project entry ID")}\u0000" +
            requireText(cellId, "stable cell ID")
    }

    private static void requireUnchangedLiveObservation(Map before, Map after,
                                                         String label) {
        require(before == after, "${label} changed during held-out evaluation")
    }

    private static void requireUnchangedEntryObservation(Map observation) {
        require(observation.qpdata_before_sha256 ==
                observation.expected_saved_state_sha256 &&
            observation.qpdata_after_sha256 ==
                observation.qpdata_before_sha256 &&
            observation.hierarchy_event_count_after ==
                observation.hierarchy_event_count_before,
            "project entry changed during held-out evaluation")
    }

    private static boolean nativePixelLabelsMatch(Map actual,
                                                   List<Map> labels) {
        Set<Integer> expected = labels.collect {
            (it.qupath_label as Number).intValue()
        }.toSet()
        actual.keySet() == expected && labels.every { row ->
            actual[(row.qupath_label as Number).intValue()]?.toString() ==
                row.native_path_class_id
        }
    }

    private static String expectedPixelTruthPathClass(Map row,
                                                       List<Map> labels) {
        String classId = requireText(row.expected_class_id,
            "target expected class")
        List<Map> matches = labels.findAll {
            !(it.ignored as boolean) && it.canonical_class_id == classId
        }
        require(matches.size() == 1,
            "pixel target truth lacks one non-ignored native output")
        String expected = requireText(matches.first().native_path_class_id,
            "native truth PathClass")
        require(requireText(row.path_class_id, "target PathClass ID") == expected,
            "pixel target PathClass differs from its truth class")
        expected
    }

    private static void requirePixelLiveTruthClassification(
            String livePathClass, Map row, List<Map> labels) {
        require(livePathClass == expectedPixelTruthPathClass(row, labels),
            "live pixel PathClass differs from its requested truth")
    }

    private static void validateProjectIdentity(Project project, Map request) {
        Object raw = project.getMetadata().get(PROJECT_IDENTITY_KEY)
        require(raw instanceof String,
            "managed project identity metadata is missing")
        Object identity
        try {
            identity = GSON.fromJson(raw as String, Object)
        } catch (RuntimeException error) {
            throw new IllegalStateException("project identity metadata is invalid",
                error)
        }
        require(identity instanceof Map &&
            (identity as Map).project_identity_sha256 ==
                request.project_identity_sha256,
            "command project identity differs from the request")
    }

    private static ProjectImageEntry resolveEntry(Project project,
                                                  String entryId) {
        List matches = project.getImageList().findAll {
            it.getID().toString() == entryId
        }
        require(matches.size() == 1,
            "evaluation project entry ${entryId} is missing or duplicated")
        matches.first() as ProjectImageEntry
    }

    private static void validateLiveEntry(ProjectImageEntry entry, Map owner) {
        Map metadata = entry.getMetadataMap()
        require(metadata.get("mif_pipeline.image_role") == "primary_mif" &&
            metadata.get("mif_pipeline.source_image_sha256") ==
                owner.image_identity_sha256,
            "evaluation entry is not the exact canonical primary mIF image")
    }

    private static String normalizedRoiSha256(PathObject object) {
        require(object.getROI() != null && !object.getROI().isEmpty(),
            "requested live ROI is absent")
        Geometry geometry = object.getROI().getGeometry()
        require(geometry != null && !geometry.isEmpty() && geometry.isValid() &&
            geometry.getCoordinates().every {
                Double.isFinite(it.x) && Double.isFinite(it.y)
            }, "requested live ROI is invalid")
        Geometry normalized = geometry.copy()
        normalized.normalize()
        byte[] bytes = new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false)
            .write(normalized)
        sha256Bytes(bytes)
    }

    private static void validatePixelMaterialParent(PathObject parent,
                                                    Map row) {
        require(parent != null && parent.getID().toString() ==
                row.material_parent_id,
            "pixel target has a foreign material parent")
        Map metadata = parent.getMetadata()
        String type = row.material_parent_type as String
        if (type == "analysis_domain") {
            boolean selectedDomain =
                metadata.get("pipeline_stage") ==
                    "analysis_domain_selection" &&
                metadata.get("analysis_domain_status") == "accepted" &&
                metadata.get("analysis_domain_active") == "true" &&
                metadata.get("analysis_domain_id") == row.analysis_domain_id &&
                metadata.get("analysis_domain_revision_sha256") ==
                    row.analysis_domain_revision_sha256 &&
                metadata.get("analysis_domain_set_sha256") == row.domain_set_sha256
            // Whole-tissue domains use the accepted section object itself,
            // as in the training snapshot; the saved-state and parent UUID
            // checks bind this exact section to the frozen domain evidence.
            boolean wholeTissueDomain =
                metadata.get("pipeline_stage") == "tissue_detection" &&
                metadata.get("generation_status") == "accepted" &&
                metadata.get("review_status") == "accepted"
            require(row.material_parent_type_id == "AnalysisDomain" &&
                row.material_parent_revision_sha256 == row.analysis_domain_revision_sha256 &&
                parent.isLocked() && (selectedDomain || wholeTissueDomain),
                "pixel target analysis domain is stale")
            return
        }
        String typeId = type == "primary" ?
            metadata.get("primary_class_id") as String :
            metadata.get("structure_id") as String
        require(metadata.get("pipeline_stage") ==
                (type == "primary" ? "primary_classifier" :
                    "secondary_classifier") &&
            metadata.get("pipeline_active") == "true" &&
            metadata.get("analysis_domain_id") == row.analysis_domain_id &&
            metadata.get("analysis_domain_revision_sha256") ==
                row.analysis_domain_revision_sha256 &&
            metadata.get("domain_set_sha256") == row.domain_set_sha256 &&
            typeId == row.material_parent_type_id &&
            parent.getParent() != null,
            "pixel target material parent is stale")
        String revision = canonicalSha256([
            analysis_domain_id: metadata.get("analysis_domain_id"),
            analysis_domain_revision_sha256:
                metadata.get("analysis_domain_revision_sha256"),
            analysis_domain_set_sha256: metadata.get("domain_set_sha256"),
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
            "pixel target material parent revision differs")
    }

    private static void validatePixelCompatibility(ImageData imageData,
                                                   PixelClassifier classifier,
                                                   Map request, Map entry) {
        require(classifier.supportsImage(imageData),
            "pixel classifier does not support the requested image")
        List channels = imageData.getServer().getMetadata().getChannels()
        (entry.channel_features.ordered_channels as List<Map>).each { row ->
            int index = (row.qupath_channel_index as Number).intValue()
            require(index >= 0 && index < channels.size() &&
                channels[index].getName() == row.observed_qupath_channel_name,
                "live image channels differ from the request")
        }
        Map<Integer, ?> actual = classifier.getMetadata()
            .getClassificationLabels()
        List<Map> labels = request.native_output_labels as List<Map>
        Map compared=actual
        if(request.pixel_sampling_method=="kronos-complete-patch-grid/v1") {
            require(actual.containsKey(255) && (actual[255]==null || actual[255]==PathClass.NULL_CLASS),
                "KRONOS no-prediction label is invalid")
            compared=new LinkedHashMap(actual);
            compared.remove(255)
        }
        require(nativePixelLabelsMatch(compared, labels),
            "pixel classifier output labels differ from the request")
    }

    private static Object withClassificationServer(
            ImageServer<BufferedImage> server, Closure operation) {
        try {
            return operation.call()
        } finally {
            server.close()
        }
    }

    private static List<Map> evaluatePixelEntry(ImageData imageData,
                                                PixelClassifier classifier,
                                                Map request, Map entry, List scoreObservations) {
        validatePixelCompatibility(imageData, classifier, request, entry)
        Map<String, PathObject> annotations = new TreeMap<>()
        imageData.getHierarchy().getAnnotationObjects().each { object ->
            String id = object.getID().toString()
            require(!annotations.containsKey(id),
                "live hierarchy repeats an annotation UUID")
            annotations[id] = object
        }
        Map<Integer, Map> labelRows =
            (request.native_output_labels as List<Map>).collectEntries {
                row -> [((row.qupath_label as Number).intValue()): row]
            }
        List<String> declared = request.declared_class_order as List<String>
        int patchSize = request.pixel_sampling_method=="kronos-complete-patch-grid/v1" ?
            ClassifierEvaluation.requireKronosPatchSize(kronosSupport.resources.describe(classifier).convention.patch_size) : 0
        ImageServer<BufferedImage> classificationServer =
            PixelClassifierTools.createPixelClassificationServer(
                imageData, classifier)
        return withClassificationServer(classificationServer) { ->
            double downsample = classificationServer.getDownsampleForResolution(0)
            require(Double.isFinite(downsample) && downsample > 0.0d,
                "pixel classifier resolution is invalid")
            List predictions = (entry.targets as List<Map>).collect { row ->
                PathObject annotation = annotations[row.annotation_uuid as String]
                require(annotation != null,
                    "live held-out annotation is absent")
                requirePixelLiveTruthClassification(
                    annotation.getClassification(), row,
                    request.native_output_labels as List<Map>)
                require(normalizedRoiSha256(annotation) ==
                        row.normalized_roi_sha256,
                    "live held-out annotation differs from the request")
                PathObject parent = annotation.getParent()
                require(parent != null &&
                    annotation.getROI().getGeometry().coveredBy(
                        parent.getROI().getGeometry()),
                    "held-out annotation escaped its material parent")
                validatePixelMaterialParent(parent, row)
                def roi = annotation.getROI()
                int x = Math.max(0, Math.floor(roi.getBoundsX()) as int)
                int y = Math.max(0, Math.floor(roi.getBoundsY()) as int)
                int maxX = Math.min(classificationServer.getWidth(),
                    Math.ceil(roi.getBoundsX() + roi.getBoundsWidth()) as int)
                int maxY = Math.min(classificationServer.getHeight(),
                    Math.ceil(roi.getBoundsY() + roi.getBoundsHeight()) as int)
                require(maxX > x && maxY > y,
                    "held-out target has no bounded pixel region")
                RegionRequest region = RegionRequest.createInstance(
                    classificationServer.getPath(), downsample,
                    x, y, maxX - x, maxY - y, roi.getZ(), roi.getT())
                def votes = request.pixel_sampling_method=="kronos-complete-patch-grid/v1" ?
                    ClassifierEvaluation.kronosPatches(classificationServer,roi,downsample,labelRows,declared,patchSize) :
                    ClassifierEvaluation.nativePixels(classificationServer, roi, region, labelRows, declared)
                Map<String, Long> counts = votes.counts() as Map<String, Long>
                long ignored = votes.ignored() as long
                String predicted = ClassifierEvaluation.majorityLabel(counts, declared)
                [
                    prediction_kind: "pixel",
                    project_entry_id: row.project_entry_id,
                    subject_id: row.subject_id,
                    annotation_uuid: row.annotation_uuid,
                    owner_evidence_sha256: row.owner_evidence_sha256,
                    normalized_roi_sha256: row.normalized_roi_sha256,
                    declared_label_counts: counts,
                    ignored_sample_count: ignored,
                    predicted_class_id: predicted,
                    // Exact level-0 annotation bounds, captured with this held-out prediction.
                    roi_bounds: [x: roi.getBoundsX(), y: roi.getBoundsY(),
                        width: roi.getBoundsWidth(), height: roi.getBoundsHeight(),
                        z: roi.getZ(), t: roi.getT()],
                ]
            }
            if (patchSize == 0 && (entry.targets as List<Map>).every { row ->
                    def roi = annotations[row.annotation_uuid as String].ROI
                    roi.z == 0 && roi.t == 0 }) {
                Map grouped = [:]
                (entry.targets as List<Map>).each { row ->
                    grouped.computeIfAbsent(row.expected_class_id) { [] }.add(
                        annotations[row.annotation_uuid as String].ROI.geometry)
                }
                Map regions = grouped.collectEntries { key, geometries ->
                    [(key): org.locationtech.jts.operation.union.UnaryUnionOp.union(geometries)] }
                Map labels = labelRows.collectEntries { code, label -> [(code):label.canonical_class_id] }
                Map scores = ClassifierPrecisionRecall.evaluate(imageData, classifier,
                    classificationServer, regions, labels, declared)
                if (scores != null) scoreObservations.add(scores + [
                    project_entry_id:entry.project_entry_id, subject_id:entry.subject_id,
                    split:entry.split, fold:null, model_sha256:request.native_classifier_sha256])
            }
            return predictions
        }
    }

    private static void validateObjectScope(ImageData imageData,
                                            PathObject cell, Map entry) {
        Map metadata = cell.getMetadata()
        require(metadata.get("pipeline_stage") == "segmentation" &&
            metadata.get("section_id") == entry.section_id &&
            metadata.get("analysis_domain_id") == entry.analysis_domain_id &&
            metadata.get("analysis_domain_revision_sha256") ==
                entry.analysis_domain_revision_sha256 &&
            metadata.get("domain_set_sha256") == entry.domain_set_sha256 &&
            metadata.get("generation_id") ==
                entry.segmentation_generation_id &&
            metadata.get("measurement_generation_id") ==
                entry.measurement_generation_id,
            "live evaluation cell scope is stale")
        List<PathObject> sections = []
        Set<PathObject> seen = Collections.newSetFromMap(
            new IdentityHashMap<PathObject, Boolean>())
        PathObject root = imageData.getHierarchy().getRootObject()
        boolean datasetCell = imageData.getProperty("mif.cell_datasets") != null &&
            metadata.get("mif.cell_id") == metadata.get("cell_id")
        if (datasetCell) {
            sections = imageData.getHierarchy().getAnnotationObjects().findAll { candidate ->
                candidate.getMetadata().get("pipeline_stage") == "tissue_detection" &&
                candidate.getMetadata().get("roi_id") == metadata.get("tissue_roi_id") &&
                candidate.getMetadata().get("generation_status") == "accepted" &&
                candidate.getMetadata().get("review_status") == "accepted" &&
                candidate.getMetadata().get("generation_id") == metadata.get("generation_id")
            }
        }
        PathObject cursor = datasetCell ? root : cell.getParent()
        while (cursor != null && cursor != root) {
            require(seen.add(cursor), "cell hierarchy contains a parent cycle")
            if (cursor.getMetadata().get("pipeline_stage") ==
                    "tissue_detection")
                sections << cursor
            cursor = cursor.getParent()
        }
        require(cursor == root && sections.size() == 1 &&
            (!datasetCell || sections.first().getParent() == root) &&
            sections.first().getMetadata().get("section_id") ==
                entry.section_id,
            "evaluation cell lacks one current TissueSection")
    }

    private static List<Map> evaluateObjectEntry(ImageData imageData,
                                                 ObjectClassifier classifier,
                                                 Map request, Map entry) {
        List<String> declared = request.declared_class_order as List<String>
        List<Map> labels = request.native_output_labels as List<Map>
        List<String> classifierClasses = classifier.getPathClasses().collect {
            it.toString()
        }
        require(classifierClasses == labels.collect {
                it.native_path_class_id as String
            }, "object classifier output classes differ from the request")
        Set<String> requestedIds = (entry.evaluation_cells as List).collect { it.cell_id as String } as Set
        Set<String> observedIds = new HashSet<>()
        Map<String, PathObject> liveById = new TreeMap<>()
        NativeCellScope.forEach(imageData, { cell ->
            if (!cell.isCell()) return
            Object id = cell.getMetadata().get("cell_id")
            if (id instanceof String) {
                require(observedIds.add(id as String),
                    "live entry repeats a stable cell ID")
                if (requestedIds.contains(id as String)) liveById[id as String] = cell
            }
        } as Consumer)
        List<String> measurementNames =
            (request.feature_spec.ordered_measurements as List<Map>).collect {
                it.measurement_name as String
        }
        Map featureSource=objectSupport.featureSource(classifier) as Map
        boolean deferred=entry.deferred_feature_evidence!=null
        require(!deferred || featureSource!=null,"Deferred evaluation requires the saved fitted KRONOS source")
        if(featureSource!=null) {
            require(cellFeatureSupport.featureSchema()==request.feature_spec.ordered_measurements,
                "The evaluated cell encoder and feature schema differ")
            List requestedCells=(entry.evaluation_cells as List).collect{row ->
                PathObject cell=liveById[row.cell_id as String]
                require(cell!=null,"A requested KRONOS evaluation cell is absent")
                validateObjectScope(imageData,cell,entry);cell
            }
            cellFeatureSupport.prepare(imageData,requestedCells,featureSource,
                [image_sha256:entry.image_identity_sha256,segmentation_generation_id:entry.segmentation_generation_id],
                objectFeatureCache,cellWorkers,null,entry.deferred_feature_evidence)
        }
        List<PathObject> detachedCells = []
        List<PathObject> liveCells = []
        List<Map> liveObservations = []
        (entry.evaluation_cells as List<Map>).each { row ->
            PathObject live = liveById[row.cell_id as String]
            require(live != null, "requested live evaluation cell is absent")
            validateObjectScope(imageData, live, entry)
            require(live.getROI() != null && !live.getROI().isEmpty() &&
                Double.isFinite(live.getROI().getCentroidX()) &&
                Double.isFinite(live.getROI().getCentroidY()),
                "live evaluation cell ROI is invalid")
            List<Double> pointLocator = pointLocatorCoordinates(row,
                live.getROI().getCentroidX(), live.getROI().getCentroidY())
            Object liveCellGeometry = null
            Object liveNucleusGeometry = null
            if (pointLocator == null) {
                liveCellGeometry = ObjectTrainingRows.geometry(
                    live.getROI().getGeometry())
                liveNucleusGeometry = live.getNucleusROI() == null ? [:] :
                    ObjectTrainingRows.geometry(live.getNucleusROI().getGeometry())
            }
            List<Double> observed = measurementNames.collect { name ->
                Object value = live.getMeasurements().get(name)
                require(value instanceof Number &&
                    Double.isFinite((value as Number).doubleValue()),
                    "live evaluation cell lacks measurement ${name}")
                (value as Number).doubleValue()
            }
            if(deferred) {
                require(row.measurement_values==null && row.measurement_vector_sha256==null &&
                    live.metadata.get("mif.kronos_cell_input_sha256")==entry.deferred_feature_evidence.cell_input_sha256[row.cell_id],
                    "Live deferred evaluation inputs were not authenticated before encoding")
            } else {
            List expected = row.measurement_values as List
            require(observed.size() == expected.size() &&
                observed.indices.every { index ->
                    Double.doubleToRawLongBits(observed[index]) ==
                    Double.doubleToRawLongBits(
                            (expected[index] as Number).doubleValue())
                },
                "live evaluation measurement vector differs from the request")
            }
            requireLiveObjectDigests(row, liveCellGeometry,
                liveNucleusGeometry, observed,deferred)
            MeasurementList measurements =
                MeasurementListFactory.createMeasurementList(
                    measurementNames.size(),
                    MeasurementList.MeasurementListType.DOUBLE)
            measurementNames.eachWithIndex { name, int index ->
                measurements.put(name, observed[index])
            }
            def detachedCellRoi = pointLocator == null ?
                live.getROI().duplicate() : ROIs.createPointsROI(
                    pointLocator[0], pointLocator[1],
                    live.getROI().getImagePlane())
            def detachedNucleusRoi = pointLocator == null ?
                live.getNucleusROI()?.duplicate() : ROIs.createPointsROI(
                    pointLocator[0], pointLocator[1],
                    live.getROI().getImagePlane())
            PathObject detached = PathObjects.createCellObject(
                detachedCellRoi, detachedNucleusRoi, null, measurements)
            detachedCells << detached
            liveCells << live
            liveObservations << [
                classification: live.getClassification(),
                measurements: new TreeMap<String, Number>(
                    live.getMeasurements()),
            ]
        }
        Map missing = classifier.getMissingFeatures(imageData, detachedCells)
        require(missing.isEmpty(),
            "object classifier lacks a requested ordered measurement")
        long hierarchyEventsBefore = imageData.getHierarchy().getEventCount()
        int classifiedCount = classifier.classifyObjects(imageData, detachedCells, true)
        require(classifiedCount == detachedCells.size(),
            "object classifier returned incomplete detached cover")
        require(imageData.getHierarchy().getEventCount() == hierarchyEventsBefore,
            "object evaluation changed the live hierarchy")
        liveCells.eachWithIndex { live, int index ->
            requireUnchangedLiveObservation(liveObservations[index], [
                classification: live.getClassification(),
                measurements: new TreeMap<String, Number>(
                    live.getMeasurements()),
            ], "live cell")
        }
        List<Map> predictions = []
        detachedCells.eachWithIndex { detached, int index ->
            Map row = (entry.evaluation_cells as List<Map>)[index]
            String predicted = detached.getClassification()
            require(declared.contains(predicted),
                "object classifier produced an undeclared class")
            predictions << [
                prediction_kind: "object",
                project_entry_id: entry.project_entry_id,
                cell_id: row.cell_id,
                source_snapshot_sha256: entry.source_snapshot_sha256,
                section_id: entry.section_id,
                analysis_domain_id: entry.analysis_domain_id,
                analysis_domain_revision_sha256:
                    entry.analysis_domain_revision_sha256,
                domain_set_sha256: entry.domain_set_sha256,
                segmentation_generation_id:
                    entry.segmentation_generation_id,
                measurement_generation_id:
                    entry.measurement_generation_id,
                cell_geometry_sha256: row.cell_geometry_sha256,
                nucleus_geometry_sha256: row.nucleus_geometry_sha256,
                measurement_vector_sha256: row.measurement_vector_sha256,
                owner_evidence_sha256: row.owner_evidence_sha256,
                predicted_class_id: predicted,
            ]
        }
        predictions
    }

    private static Map evaluateEntry(ProjectImageEntry projectEntry,
                                     Map entry, Map request,
                                     Object classifier, List scoreObservations) {
        validateLiveEntry(projectEntry, entry)
        Path qpdataPath = projectEntry.getEntryPath().toAbsolutePath()
            .normalize().resolve("data.qpdata")
        require(Files.isRegularFile(qpdataPath, LinkOption.NOFOLLOW_LINKS) &&
            !Files.isSymbolicLink(qpdataPath),
            "saved data.qpdata is absent or linked")
        String qpdataBeforeSha256 = sha256File(qpdataPath)
        require(qpdataBeforeSha256 == entry.saved_state_sha256,
            "saved data.qpdata differs from the requested state")
        ImageData<BufferedImage> imageData =
            projectEntry.readImageData() as ImageData<BufferedImage>
        long hierarchyEventsBefore = imageData.getHierarchy().getEventCount()
        List<Map> predictions
        try {
            if(request.pixel_sampling_method=="kronos-complete-patch-grid/v1") {
                Map objects=imageData.hierarchy.annotationObjects.collectEntries {[(it.ID.toString()):it]}
                Map scopes=[:]
                for(Map row:entry.targets) {
                    def annotation=objects[row.annotation_uuid];
                    def parent=annotation?.parent
                    require(annotation!=null,"A held-out reference annotation is absent")
                    validatePixelMaterialParent(parent,row)
                    scopes[row.material_parent_id]=[id:row.material_parent_id,geometry:parent.ROI.geometry,
                        revision:row.material_parent_revision_sha256,split:entry.split]
                }
                kronosSupport.resources.bind(imageData,scopes.values().toList())
            }
            predictions = request.native_classifier_type == "pixel" ?
                evaluatePixelEntry(imageData, classifier as PixelClassifier,
                    request, entry, scoreObservations) :
                evaluateObjectEntry(imageData, classifier as ObjectClassifier,
                    request, entry)
            String qpdataAfterSha256 = sha256File(qpdataPath)
            Map observation = [
                project_entry_id: entry.project_entry_id,
                expected_saved_state_sha256: entry.saved_state_sha256,
                qpdata_before_sha256: qpdataBeforeSha256,
                qpdata_after_sha256: qpdataAfterSha256,
            ]
            requireUnchangedEntryObservation(observation + [
                hierarchy_event_count_before: hierarchyEventsBefore,
                hierarchy_event_count_after:
                    imageData.getHierarchy().getEventCount(),
            ])
            return [
                observation: observation,
                predictions: predictions,
            ]
        } finally {
            try { if(kronosSupport!=null)kronosSupport.resources.closeImage(imageData) }
            finally { imageData.close() }
        }
    }

    private static void writeCompletedResult(Path resultPath, Map request,
                                             String qupathVersion,
                                             List<Map> observations,
                                             List<Map> predictions,
                                             Path classifierPath, List scoreObservations) {
        Map result = [
            schema_name:
                "mif-qupath-held-out-classifier-evaluation-result",
            schema_version: 2,
            status: "completed",
            request_envelope_sha256: request.request_envelope_sha256,
            authority_layout_sha256: request.authority_layout_sha256,
            workspace_storage_scope_sha256:
                request.workspace_storage_scope_sha256,
            active_mutation_run_sha256:
                request.active_mutation_run_sha256,
            project_identity_sha256: request.project_identity_sha256,
            fit_plan_sha256: request.fit_plan_sha256,
            fit_execution_sha256: request.fit_execution_sha256,
            checkpoint_entry_sha256: request.checkpoint_entry_sha256,
            fit_material_sha256: request.fit_material_sha256,
            classifier_id: request.classifier_id,
            classifier_kind: request.classifier_kind,
            native_classifier_type: request.native_classifier_type,
            pixel_sampling_method: request.pixel_sampling_method,
            classifier_version: request.classifier_version,
            project_relative_path: request.project_relative_path,
            launch_nonce_sha256: request.launch_nonce_sha256,
            qupath_version: qupathVersion,
            native_classifier_sha256: sha256File(classifierPath),
            native_classifier_byte_count: Files.size(classifierPath),
            entry_observations: observations,
            predictions: predictions,
        ]
        if (!scoreObservations.isEmpty() && scoreObservations.size() == request.evaluation_entries.size()) {
            result.pixel_scores = ["validation", "test"].collect { split ->
                List rows = scoreObservations.findAll { it.split == split }.collect { row ->
                    new LinkedHashMap(row).findAll { key, value -> key != "split" } }
                rows.isEmpty() ? null : ClassifierPrecisionRecall.evidence(split,
                    request.fit_execution_sha256 as String, request.declared_class_order as List<String>, rows)
            }.findAll { it != null }
        }
        result.result_envelope_sha256 = canonicalSha256(result)
        byte[] bytes = canonicalBytes(result)
        require(!Files.exists(resultPath, LinkOption.NOFOLLOW_LINKS),
            "held-out evaluation result member is not fresh")
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

    static void execute(String[] arguments) {
        require(arguments != null && arguments.length == 3,
            "held-out evaluator requires request, qpproj, and result paths")
        List<Path> paths = arguments.collect { raw ->
            Path path = Path.of(requireText(raw, "evaluator argument"))
            require(path.isAbsolute() &&
                path == path.toAbsolutePath().normalize(),
                "evaluator paths must be absolute and normalized")
            path
        }
        Path requestPath = paths[0]
        Path projectPath = paths[1]
        Path resultPath = paths[2]
        require(Files.isRegularFile(projectPath, LinkOption.NOFOLLOW_LINKS) &&
            !Files.isSymbolicLink(projectPath) &&
            requestPath.parent == resultPath.parent &&
            requestPath != resultPath,
            "evaluator command paths are invalid")
        Map request = validateRequest(parseCanonicalRequest(requestPath))
        validateRuntimeRef(request, request.request_member_ref,
            "request", requestPath)
        validateRuntimeRef(request, request.result_member_ref,
            "result", resultPath)
        Project<BufferedImage> project = ProjectIO.loadProject(
            projectPath.toFile(), BufferedImage.class)
        require(project.getPath().toAbsolutePath().normalize() == projectPath,
            "loaded project differs from the command qpproj")
        validateProjectIdentity(project, request)
        Path projectRoot = projectPath.parent
        require(!requestPath.startsWith(projectRoot) &&
            !resultPath.startsWith(projectRoot),
            "held-out exchange members cannot be inside the project")
        Path classifierPath = projectRoot.resolve(
            request.project_relative_path as String).normalize()
        String familyDirectory = request.native_classifier_type == "pixel" ?
            "pixel_classifiers" : "object_classifiers"
        Path expectedParent = projectRoot.resolve("classifiers")
            .resolve(familyDirectory).normalize()
        require(classifierPath.parent == expectedParent &&
            classifierPath.startsWith(projectRoot) &&
            Files.isRegularFile(classifierPath, LinkOption.NOFOLLOW_LINKS) &&
            !Files.isSymbolicLink(classifierPath),
            "native classifier path is not the exact standard resource")
        long classifierBytes = Files.size(classifierPath)
        require(classifierBytes > 0L &&
            classifierBytes <= MAX_CLASSIFIER_BYTES &&
            classifierBytes == request.native_classifier_byte_count &&
            sha256File(classifierPath) == request.native_classifier_sha256,
            "native classifier bytes differ from the request")
        String qupathVersion = GeneralTools.getVersion()
        require(qupathVersion == request.expected_qupath_version,
            "running QuPath version differs from the request")
        boolean advanced=request.pixel_sampling_method=="kronos-complete-patch-grid/v1"
        if(advanced) {
            require(Files.readString(classifierPath).contains("data.op.mif-kronos-native-v1"),
                "The evaluated model is not the recorded KRONOS patch classifier")
            kronosSupport=loadKronosSupport(projectPath)
        }
        if(request.native_classifier_type=="object") {
            objectSupport=loadRegisteredSupport(projectPath,null,"cell_analysis/supervised_classifier_support.groovy").api
            objectFeatureCache=requestPath.parent
        }
        Object classifier = request.native_classifier_type == "pixel" ?
            PixelClassifiers.readClassifier(classifierPath) :
            ObjectClassifiers.readClassifier(classifierPath)
        require((request.native_classifier_type == "pixel" &&
                classifier instanceof PixelClassifier) ||
            (request.native_classifier_type == "object" &&
                classifier instanceof ObjectClassifier),
            "native classifier family differs after reopening")
        if(request.native_classifier_type=="object" && objectSupport.featureSource(classifier)!=null) {
            cellFeatureSupport=loadRegisteredSupport(projectPath,null,"cell_analysis/kronos_cell_features.groovy").api
            cellWorkers=loadRegisteredSupport(projectPath,null,"compartment_analysis/kronos_worker_client.groovy").api
        }
        List<Map> observations = []
        List<Map> predictions = []
        List<Map> scoreObservations = []
        (request.evaluation_entries as List<Map>).each { entry ->
            ProjectImageEntry projectEntry = resolveEntry(project,
                entry.project_entry_id as String)
            Map evaluated = evaluateEntry(projectEntry, entry, request,
                classifier, scoreObservations)
            observations << (evaluated.observation as Map)
            predictions.addAll(evaluated.predictions as List<Map>)
        }
        List<String> expectedKeys = []
        (request.evaluation_entries as List<Map>).each { entry ->
            List<Map> rows = request.native_classifier_type == "pixel" ?
                entry.targets as List<Map> :
                entry.evaluation_cells as List<Map>
            rows.each { row ->
                expectedKeys << (request.native_classifier_type == "pixel" ?
                    "${entry.project_entry_id}/${row.subject_id}/${row.annotation_uuid}" :
                    "${entry.project_entry_id}/${row.cell_id}")
            }
        }
        List<String> actualKeys = predictions.collect { row ->
            request.native_classifier_type == "pixel" ?
                "${row.project_entry_id}/${row.subject_id}/${row.annotation_uuid}" :
                "${row.project_entry_id}/${row.cell_id}"
        }
        requireExactCover(expectedKeys, actualKeys, "prediction")
        writeCompletedResult(resultPath, request, qupathVersion,
            observations, predictions, classifierPath, scoreObservations)
    }
}


MifHeldOutEvaluatorAssetV1.execute(args as String[])
