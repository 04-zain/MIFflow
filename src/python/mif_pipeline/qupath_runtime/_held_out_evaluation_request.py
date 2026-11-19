"""Strict process projection for native held-out classifier evaluation.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
import struct
from uuid import UUID

from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityLayoutError,
    AuthorityMemberRefV2,
)
from mif_pipeline.pipeline_core.durable_records import DurableRecordError, canonical_json_bytes

from ._training_codec import (
    _MAX_CLASSIFIER_BYTES,
    _MAX_EXCHANGE_BYTES,
    QuPathNativeTrainingExchangeError as HeldOutEvaluationExchangeError,
    _array,
    _canonical_digest,
    _closed_mapping,
    _decode_canonical,
    _export_json,
    _finite,
    _freeze_json,
    _integer,
    _json_shape,
    _literal,
    _ordered_texts,
    _sha256,
    _text,
)

_REQUEST_CORE_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "checkpoint_entry_sha256",
        "fit_material_sha256",
        "classifier_id",
        "classifier_kind",
        "classifier_engine",
        "native_classifier_type",
        "pixel_sampling_method",
        "classifier_version",
        "project_relative_path",
        "native_classifier_sha256",
        "native_classifier_byte_count",
        "declared_class_order",
        "native_output_labels",
        "feature_spec",
        "evaluation_entries",
        "expected_qupath_version",
        "script_asset_id",
        "launch_nonce_sha256",
    }
)
_REQUEST_FIELDS = _REQUEST_CORE_FIELDS | frozenset(
    {"request_member_ref", "result_member_ref", "request_envelope_sha256"}
)
_OUTPUT_LABEL_FIELDS = frozenset(
    {
        "canonical_class_id",
        "qupath_label",
        "native_path_class_id",
        "ignored",
    }
)
_PIXEL_ENTRY_FIELDS = frozenset(
    {
        "entry_kind",
        "project_entry_id",
        "subject_id",
        "split",
        "image_identity_sha256",
        "saved_state_sha256",
        "channel_features",
        "targets",
    }
)
_PIXEL_TARGET_FIELDS = frozenset(
    {
        "project_entry_id",
        "subject_id",
        "annotation_uuid",
        "expected_class_id",
        "path_class_id",
        "normalized_roi_sha256",
        "material_parent_id",
        "material_parent_type",
        "material_parent_type_id",
        "material_parent_revision_sha256",
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
        "segmentation_generation_id",
        "measurement_generation_id",
        "owner_evidence_sha256",
    }
)
_OBJECT_ENTRY_FIELDS = frozenset(
    {
        "entry_kind",
        "project_entry_id",
        "subject_id",
        "split",
        "image_identity_sha256",
        "saved_state_sha256",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
        "segmentation_generation_id",
        "measurement_generation_id",
        "source_snapshot_sha256",
        "evaluation_cells",
    }
)
_OBJECT_CELL_FIELDS = frozenset(
    {
        "project_entry_id",
        "cell_id",
        "expected_class_id",
        "cell_geometry",
        "cell_geometry_sha256",
        "nucleus_geometry",
        "nucleus_geometry_sha256",
        "measurement_values",
        "measurement_vector_sha256",
        "owner_evidence_sha256",
    }
)
_SCRIPT_ASSET_ID = "qupath/scripts/general/04_evaluate_held_out_classifiers.groovy"
_ROLE = "classifier_held_out_evaluation"


# ==============================================================================
#                          Held out evaluation request
# ==============================================================================


def _evaluation_digest_view(value: object) -> list[object]:
    """Project JSON into one injective, cross-language ASCII digest view."""

    if value is None:
        return ["n"]
    if type(value) is bool:
        return ["b", "1" if value else "0"]
    if type(value) is int:
        return ["i", str(value)]
    if type(value) is float:
        finite = _finite(value, "evaluation digest double")
        return ["f", struct.pack(">d", finite).hex()]
    if type(value) is str:
        try:
            return ["s", value.encode("utf-8", errors="strict").hex()]
        except UnicodeError as exc:
            raise HeldOutEvaluationExchangeError("evaluation digest text is invalid") from exc
    if isinstance(value, Mapping):
        entries: list[list[object]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise HeldOutEvaluationExchangeError("evaluation digest object key is invalid")
            try:
                encoded_key = key.encode("utf-8", errors="strict").hex()
            except UnicodeError as exc:
                raise HeldOutEvaluationExchangeError(
                    "evaluation digest object key is invalid"
                ) from exc
            entries.append([encoded_key, _evaluation_digest_view(item)])
        entries.sort(key=lambda item: item[0])
        return ["m", entries]
    if isinstance(value, (list, tuple)):
        return ["l", [_evaluation_digest_view(item) for item in value]]
    raise HeldOutEvaluationExchangeError("evaluation digest value is invalid")


def _evaluation_digest(value: object) -> str:
    return _canonical_digest(_evaluation_digest_view(value))


def _validate_feature_spec(value: object, native_type: str) -> Mapping[str, object]:
    if native_type == "pixel":
        spec = _closed_mapping(
            value,
            frozenset({"schema", "ordered_channels"}),
            "pixel feature specification",
        )
        _literal(spec["schema"], "qupath-pixel-channel-features/v1", "pixel feature schema")
        rows = _array(spec["ordered_channels"], "ordered channels", nonempty=True)
        ids: list[str] = []
        indices: list[int] = []
        for ordinal, raw in enumerate(rows):
            row = _closed_mapping(
                raw,
                frozenset(
                    {
                        "canonical_channel_id",
                        "qupath_channel_index",
                        "observed_qupath_channel_name",
                    }
                ),
                f"ordered channel {ordinal}",
            )
            ids.append(_text(row["canonical_channel_id"], "canonical channel ID"))
            indices.append(_integer(row["qupath_channel_index"], "QuPath channel index"))
            _text(row["observed_qupath_channel_name"], "observed channel name")
        if len(set(ids)) != len(ids) or len(set(indices)) != len(indices):
            raise HeldOutEvaluationExchangeError("pixel channel features are ambiguous")
        return spec

    spec = _closed_mapping(
        value,
        frozenset({"schema", "ordered_measurements"}),
        "object feature specification",
    )
    _literal(spec["schema"], "qupath-object-feature-spec/v1", "object feature schema")
    rows = _array(spec["ordered_measurements"], "ordered measurements", nonempty=True)
    names: list[str] = []
    for ordinal, raw in enumerate(rows):
        row = _closed_mapping(
            raw,
            frozenset({"measurement_name", "semantic_role"}),
            f"ordered measurement {ordinal}",
        )
        names.append(_text(row["measurement_name"], "measurement name"))
        _text(row["semantic_role"], "measurement semantic role")
    if len(set(names)) != len(names):
        raise HeldOutEvaluationExchangeError("ordered measurements are ambiguous")
    return spec


def _validate_output_labels(
    value: object, *, native_type: str, declared: tuple[str, ...]
) -> tuple[Mapping[str, object], ...]:
    rows = tuple(
        _closed_mapping(raw, _OUTPUT_LABEL_FIELDS, f"native output label {ordinal}")
        for ordinal, raw in enumerate(_array(value, "native output labels", nonempty=True))
    )
    labels: list[int] = []
    ordinary: list[str] = []
    class_ids: list[str] = []
    native_path_classes: list[str] = []
    ignored_count = 0
    for row in rows:
        label = _integer(row["qupath_label"], "native output label")
        if native_type == "pixel" and label > 255:
            raise HeldOutEvaluationExchangeError("pixel output label exceeds UINT8")
        labels.append(label)
        class_id = _text(row["canonical_class_id"], "native output class")
        class_ids.append(class_id)
        native_path_class = _text(row["native_path_class_id"], "native output PathClass")
        native_path_classes.append(native_path_class)
        if type(row["ignored"]) is not bool:
            raise HeldOutEvaluationExchangeError("native output ignored flag must be boolean")
        if row["ignored"]:
            if native_type != "pixel" or class_id in declared:
                raise HeldOutEvaluationExchangeError("ignored output label is invalid")
            ignored_count += 1
        else:
            ordinary.append(class_id)
    if (
        len(set(labels)) != len(labels)
        or len(set(class_ids)) != len(class_ids)
        or len(set(native_path_classes)) != len(native_path_classes)
    ):
        raise HeldOutEvaluationExchangeError("native output labels are ambiguous")
    if set(ordinary) != set(declared) or len(ordinary) != len(declared):
        raise HeldOutEvaluationExchangeError("native output classes differ from declared classes")
    if native_type == "object" and (ignored_count or tuple(labels) != tuple(range(len(labels)))):
        raise HeldOutEvaluationExchangeError("object output labels are invalid")
    if native_type == "object" and native_path_classes != class_ids:
        raise HeldOutEvaluationExchangeError(
            "object native PathClasses differ from declared outputs"
        )
    if native_type == "pixel" and ignored_count > 1:
        raise HeldOutEvaluationExchangeError("pixel output has multiple ignored labels")
    return rows


def _split(value: object) -> str:
    split = _text(value, "evaluation split")
    if split not in {"validation", "test"}:
        raise HeldOutEvaluationExchangeError("evaluation entries must be validation or test")
    return split


def _optional_text(value: object, label: str) -> None:
    if value is not None:
        _text(value, label)


def _validate_geometry(value: object, label: str) -> Mapping[str, object]:
    if label == "nucleus_geometry" and isinstance(value, Mapping) and not value:
        return {}
    geometry = _closed_mapping(value, frozenset({"type", "coordinates"}), label)
    geometry_type = geometry["type"]
    if geometry_type not in {"Point", "Polygon", "MultiPolygon"}:
        raise HeldOutEvaluationExchangeError(f"{label} type is unsupported")

    def validate_node(node: object, depth: int) -> None:
        if depth == 0:
            point = _array(node, f"{label} point")
            if len(point) != 2:
                raise HeldOutEvaluationExchangeError(f"{label} point is invalid")
            _finite(point[0], f"{label} x")
            _finite(point[1], f"{label} y")
            return
        children = _array(node, f"{label} coordinates", nonempty=True)
        for child in children:
            validate_node(child, depth - 1)

    depth = {"Point": 0, "Polygon": 2, "MultiPolygon": 3}[geometry_type]
    validate_node(geometry["coordinates"], depth)
    return geometry


def _validate_pixel_entry(
    value: object,
    *,
    declared: tuple[str, ...],
    feature_spec: Mapping[str, object],
    truth_path_classes: Mapping[str, str],
) -> tuple[str, str]:
    """Validate pixel entry for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    entry = _closed_mapping(value, _PIXEL_ENTRY_FIELDS, "pixel evaluation entry")
    _literal(entry["entry_kind"], "pixel", "pixel entry kind")
    entry_id = _text(entry["project_entry_id"], "project entry ID")
    subject_id = _text(entry["subject_id"], "subject ID")
    _split(entry["split"])
    _sha256(entry["image_identity_sha256"], "image identity")
    _sha256(entry["saved_state_sha256"], "saved-state digest")
    if entry["channel_features"] != feature_spec:
        raise HeldOutEvaluationExchangeError("entry channel features differ")
    rows = tuple(
        _closed_mapping(raw, _PIXEL_TARGET_FIELDS, "pixel evaluation target")
        for raw in _array(entry["targets"], "pixel evaluation targets", nonempty=True)
    )
    keys: list[tuple[str, str, str]] = []
    for row in rows:
        _literal(row["project_entry_id"], entry_id, "target project entry")
        _literal(row["subject_id"], subject_id, "target subject")
        annotation = _text(row["annotation_uuid"], "target annotation UUID")
        try:
            if str(UUID(annotation)) != annotation:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise HeldOutEvaluationExchangeError("target annotation UUID is invalid") from exc
        expected_class = _text(row["expected_class_id"], "target expected class")
        if expected_class not in declared:
            raise HeldOutEvaluationExchangeError("target truth class is undeclared")
        path_class_id = _text(row["path_class_id"], "target PathClass ID")
        if path_class_id != truth_path_classes.get(expected_class):
            raise HeldOutEvaluationExchangeError(
                "target PathClass differs from its expected truth class"
            )
        _text(row["material_parent_id"], "target material parent ID")
        if row["material_parent_type"] not in {"analysis_domain", "primary", "secondary"}:
            raise HeldOutEvaluationExchangeError("target material parent type is invalid")
        _text(row["material_parent_type_id"], "target material parent type ID")
        if row["material_parent_type"] == "analysis_domain" and (
            row["material_parent_type_id"] != "AnalysisDomain"
            or row["material_parent_revision_sha256"] != row["analysis_domain_revision_sha256"]
        ):
            raise HeldOutEvaluationExchangeError(
                "target analysis-domain parent revision is inconsistent"
            )
        for name in (
            "normalized_roi_sha256",
            "material_parent_revision_sha256",
            "analysis_domain_id",
            "analysis_domain_revision_sha256",
            "domain_set_sha256",
            "owner_evidence_sha256",
        ):
            _sha256(row[name], f"target {name}")
        _optional_text(row["segmentation_generation_id"], "segmentation generation")
        _optional_text(row["measurement_generation_id"], "measurement generation")
        keys.append((entry_id, subject_id, annotation))
    if tuple(keys) != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise HeldOutEvaluationExchangeError("pixel targets are not canonical and unique")
    return entry_id, entry["split"]  # type: ignore[return-value]


def _validate_object_entry(
    value: object,
    *,
    declared: tuple[str, ...],
    feature_spec: Mapping[str, object],
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Validate object entry for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from mif_pipeline.orchestration.batch_training.deferred_cell_evidence import (
        EVIDENCE_FIELD, validate_deferred_evidence,
    )
    from mif_pipeline.orchestration.batch_training._shared import BatchTrainingError
    from mif_pipeline.orchestration.batch_training.qupath_kronos_cell import kronos_cell_measurements
    deferred = isinstance(value, Mapping) and EVIDENCE_FIELD in value
    entry = _closed_mapping(value, _OBJECT_ENTRY_FIELDS | ({EVIDENCE_FIELD} if deferred else set()),
                            "object evaluation entry")
    _literal(entry["entry_kind"], "object", "object entry kind")
    entry_id = _text(entry["project_entry_id"], "project entry ID")
    _text(entry["subject_id"], "subject ID")
    split = _split(entry["split"])
    for name in (
        "image_identity_sha256",
        "saved_state_sha256",
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
        "source_snapshot_sha256",
    ):
        _sha256(entry[name], f"object entry {name}")
    for name in (
        "section_id",
        "segmentation_generation_id",
        "measurement_generation_id",
    ):
        _text(entry[name], f"object entry {name}")
    width = len(feature_spec["ordered_measurements"])  # type: ignore[arg-type]
    cells = tuple(
        _closed_mapping(raw, _OBJECT_CELL_FIELDS, "evaluation cell")
        for raw in _array(entry["evaluation_cells"], "evaluation cells", nonempty=True)
    )
    if deferred:
        try:
            validate_deferred_evidence(entry[EVIDENCE_FIELD], (cell["cell_id"] for cell in cells))
            if feature_spec["ordered_measurements"] != kronos_cell_measurements():
                raise BatchTrainingError("Deferred inputs require the exact KRONOS feature schema")
        except (BatchTrainingError, KeyError, TypeError, ValueError) as exc:
            raise HeldOutEvaluationExchangeError(str(exc)) from exc
    keys: list[tuple[str, str]] = []
    for cell in cells:
        _literal(cell["project_entry_id"], entry_id, "cell project entry")
        cell_id = _text(cell["cell_id"], "stable cell ID")
        if _text(cell["expected_class_id"], "cell expected class") not in declared:
            raise HeldOutEvaluationExchangeError("cell truth class is undeclared")
        geometries: dict[str, Mapping[str, object]] = {}
        for geometry_name in ("cell_geometry", "nucleus_geometry"):
            geometry = _validate_geometry(cell[geometry_name], geometry_name)
            geometries[geometry_name] = geometry
            digest_name = f"{geometry_name}_sha256"
            _sha256(cell[digest_name], digest_name)
            if cell[digest_name] != _evaluation_digest(geometry):
                raise HeldOutEvaluationExchangeError(f"{geometry_name} digest differs")
        cell_is_point = geometries["cell_geometry"].get("type") == "Point"
        nucleus_is_point = geometries["nucleus_geometry"].get("type") == "Point"
        if cell_is_point != nucleus_is_point:
            raise HeldOutEvaluationExchangeError(
                "cell and nucleus geometry must both be Point locators or full geometry"
            )
        if cell_is_point and (cell["cell_geometry_sha256"] != cell["nucleus_geometry_sha256"]):
            raise HeldOutEvaluationExchangeError(
                "cell and nucleus Point geometry must be the same locator"
            )
        if deferred:
            if cell["measurement_values"] is not None or cell["measurement_vector_sha256"] is not None:
                raise HeldOutEvaluationExchangeError("Deferred evaluation cells require null embedding values and digest")
        else:
            values = _array(cell["measurement_values"], "measurement vector", nonempty=True)
            if len(values) != width:
                raise HeldOutEvaluationExchangeError("measurement vector width differs")
            for ordinal, item in enumerate(values):
                if type(item) is not float:
                    raise HeldOutEvaluationExchangeError(
                        f"measurement value {ordinal} must be a JSON double"
                    )
                _finite(item, f"measurement value {ordinal}")
            _sha256(cell["measurement_vector_sha256"], "measurement-vector digest")
            if cell["measurement_vector_sha256"] != _evaluation_digest(values):
                raise HeldOutEvaluationExchangeError("measurement-vector digest differs")
        _sha256(cell["owner_evidence_sha256"], "cell owner-evidence digest")
        keys.append((entry_id, cell_id))
    if tuple(keys) != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise HeldOutEvaluationExchangeError("evaluation cells are not canonical and unique")
    return entry_id, split, tuple(keys)


def _expected_runtime_exchange_ref(
    *, authority_layout_sha256: str, scope: Mapping[str, object], direction: str
) -> AuthorityMemberRefV2:
    key_sha256 = _canonical_digest(
        {
            "authority_layout_sha256": authority_layout_sha256,
            "family": "runtime_exchange",
            "scope": scope,
            "natural_key": {"direction": direction},
        }
    )
    try:
        return AuthorityMemberRefV2(
            authority_layout_sha256=authority_layout_sha256,
            family="runtime_exchange",
            key_sha256=key_sha256,
            suffix="json",
            media_type="application/json",
        )
    except AuthorityLayoutError as exc:  # pragma: no cover - closed constants
        raise HeldOutEvaluationExchangeError("runtime exchange ref is invalid") from exc


def _member_ref(
    value: object, *, layout: str, scope: Mapping[str, object], direction: str
) -> AuthorityMemberRefV2:
    try:
        parsed = AuthorityMemberRefV2.from_mapping(value, expected_layout_sha256=layout)
    except (AuthorityLayoutError, TypeError, ValueError) as exc:
        raise HeldOutEvaluationExchangeError("runtime exchange member ref is invalid") from exc
    if parsed != _expected_runtime_exchange_ref(
        authority_layout_sha256=layout, scope=scope, direction=direction
    ):
        raise HeldOutEvaluationExchangeError("runtime exchange member ref is foreign")
    return parsed


def _validate_request_mapping(
    value: object,
) -> tuple[Mapping[str, object], AuthorityMemberRefV2, AuthorityMemberRefV2]:
    """Validate request mapping for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    request = _closed_mapping(value, _REQUEST_FIELDS, "held-out evaluation request")
    _literal(
        request["schema_name"],
        "mif-qupath-held-out-classifier-evaluation-request",
        "request schema",
    )
    _literal(request["schema_version"], 2, "request schema version")
    for name in (
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "checkpoint_entry_sha256",
        "fit_material_sha256",
        "native_classifier_sha256",
        "launch_nonce_sha256",
        "request_envelope_sha256",
    ):
        _sha256(request[name], name)
    _text(request["classifier_id"], "classifier ID")
    kind = request["classifier_kind"]
    if kind not in {"pixel", "supervised_phenotype"}:
        raise HeldOutEvaluationExchangeError("classifier kind is invalid")
    _literal(request["classifier_engine"], "qupath_native", "classifier engine")
    native_type = "object" if kind == "supervised_phenotype" else "pixel"
    _literal(request["native_classifier_type"], native_type, "native classifier type")
    allowed_sampling = (
        {"qupath-native-classification-grid/v1", "kronos-complete-patch-grid/v1"}
        if native_type == "pixel"
        else {None}
    )
    if request["pixel_sampling_method"] not in allowed_sampling:
        raise HeldOutEvaluationExchangeError("pixel sampling method is unsupported")
    expected_version = f"mif-fit-{request['fit_execution_sha256']}"
    _literal(request["classifier_version"], expected_version, "classifier version")
    expected_path = (
        f"classifiers/object_classifiers/mif-{request['fit_execution_sha256']}.json"
        if native_type == "object"
        else f"classifiers/pixel_classifiers/mif-{request['fit_execution_sha256']}.json"
    )
    _literal(request["project_relative_path"], expected_path, "classifier path")
    path = PurePosixPath(expected_path)
    if path.is_absolute() or len(path.parts) != 3 or path.suffix != ".json":
        raise HeldOutEvaluationExchangeError("classifier path is invalid")
    _integer(
        request["native_classifier_byte_count"],
        "classifier byte count",
        minimum=1,
        maximum=_MAX_CLASSIFIER_BYTES,
    )
    declared = _ordered_texts(
        request["declared_class_order"], "declared class order", sorted_order=False
    )
    output_labels = _validate_output_labels(
        request["native_output_labels"], native_type=native_type, declared=declared
    )
    truth_path_classes = {
        _text(row["canonical_class_id"], "native output class"): _text(
            row["native_path_class_id"], "native output PathClass"
        )
        for row in output_labels
        if row["ignored"] is False
    }
    feature_spec = _validate_feature_spec(request["feature_spec"], native_type)
    entry_keys: list[tuple[str, ...]] = []
    exact_cells: list[tuple[str, str]] = []
    for raw in _array(request["evaluation_entries"], "evaluation entries", nonempty=True):
        if native_type == "pixel":
            entry_id, split = _validate_pixel_entry(
                raw,
                declared=declared,
                feature_spec=feature_spec,
                truth_path_classes=truth_path_classes,
            )
            entry_keys.append((entry_id,))
        else:
            entry_id, split, cells = _validate_object_entry(
                raw, declared=declared, feature_spec=feature_spec
            )
            exact_cells.extend(cells)
            entry_keys.append((entry_id, raw["section_id"], raw["analysis_domain_id"]))
    if tuple(entry_keys) != tuple(sorted(entry_keys)) or len(set(entry_keys)) != len(entry_keys):
        raise HeldOutEvaluationExchangeError("evaluation entries are not canonical and unique")
    if len(set(exact_cells)) != len(exact_cells):
        raise HeldOutEvaluationExchangeError("entry-scoped evaluation cells repeat")
    _literal(request["expected_qupath_version"], "0.7.0", "QuPath version")
    _literal(request["script_asset_id"], _SCRIPT_ASSET_ID, "evaluator asset")
    core = {
        key: request[key]
        for key in request
        if key not in {"request_member_ref", "result_member_ref", "request_envelope_sha256"}
    }
    request_identity = _evaluation_digest(core)
    scope = {
        "workspace_storage_scope_sha256": request["workspace_storage_scope_sha256"],
        "active_mutation_run_sha256": request["active_mutation_run_sha256"],
        "role": _ROLE,
        "fit_material_sha256": request["fit_material_sha256"],
        "request_identity_sha256": request_identity,
    }
    request_ref = _member_ref(
        request["request_member_ref"],
        layout=request["authority_layout_sha256"],  # type: ignore[arg-type]
        scope=scope,
        direction="request",
    )
    result_ref = _member_ref(
        request["result_member_ref"],
        layout=request["authority_layout_sha256"],  # type: ignore[arg-type]
        scope=scope,
        direction="result",
    )
    if request_ref == result_ref:
        raise HeldOutEvaluationExchangeError("request and result refs must differ")
    unsigned = {key: request[key] for key in request if key != "request_envelope_sha256"}
    if request["request_envelope_sha256"] != _evaluation_digest(unsigned):
        raise HeldOutEvaluationExchangeError("request envelope digest is invalid")
    return request, request_ref, result_ref


class QuPathHeldOutEvaluationRequest:
    """Deeply immutable validated evaluator request."""

    __slots__ = ("_mapping", "_request_member_ref", "_result_member_ref")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("held-out evaluation requests are parser/issuer-created")

    @classmethod
    def _from_mapping(cls, value: object) -> "QuPathHeldOutEvaluationRequest":
        mapping, request_ref, result_ref = _validate_request_mapping(value)
        _json_shape(mapping)
        try:
            encoded = canonical_json_bytes(mapping)
        except DurableRecordError as exc:  # pragma: no cover
            raise HeldOutEvaluationExchangeError("request cannot be encoded") from exc
        if len(encoded) > _MAX_EXCHANGE_BYTES:
            raise HeldOutEvaluationExchangeError("request exceeds its byte bound")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_mapping", _freeze_json(dict(mapping)))
        object.__setattr__(instance, "_request_member_ref", request_ref)
        object.__setattr__(instance, "_result_member_ref", result_ref)
        return instance

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("held-out evaluation request is immutable")

    def __getattr__(self, name: str) -> object:
        if name in _REQUEST_FIELDS:
            if name == "request_member_ref":
                return self._request_member_ref
            if name == "result_member_ref":
                return self._result_member_ref
            return self._mapping[name]
        raise AttributeError(name)

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is QuPathHeldOutEvaluationRequest
            and self.to_mapping() == other.to_mapping()
        )

    @property
    def request_identity_sha256(self) -> str:
        return _evaluation_digest(
            {key: _export_json(self._mapping[key]) for key in _REQUEST_CORE_FIELDS}
        )

    def to_mapping(self) -> dict[str, object]:
        return _export_json(self._mapping)

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_mapping())
        except DurableRecordError as exc:  # pragma: no cover
            raise HeldOutEvaluationExchangeError("request cannot be encoded") from exc
        if len(payload) > _MAX_EXCHANGE_BYTES:
            raise HeldOutEvaluationExchangeError("request exceeds its byte bound")
        return payload


def decode_qupath_held_out_evaluation_request(
    payload: bytes,
) -> QuPathHeldOutEvaluationRequest:
    return QuPathHeldOutEvaluationRequest._from_mapping(
        _decode_canonical(payload, "held-out evaluation request")
    )


def _issue_qupath_held_out_evaluation_request(
    *,
    core_fields: Mapping[str, object],
    member_ref_issuer: Callable[..., AuthorityMemberRefV2],
) -> QuPathHeldOutEvaluationRequest:
    core = _closed_mapping(core_fields, _REQUEST_CORE_FIELDS, "evaluation request core")
    _json_shape(core)
    request_identity = _evaluation_digest(core)
    scope = {
        "workspace_storage_scope_sha256": core["workspace_storage_scope_sha256"],
        "active_mutation_run_sha256": core["active_mutation_run_sha256"],
        "role": _ROLE,
        "fit_material_sha256": core["fit_material_sha256"],
        "request_identity_sha256": request_identity,
    }
    if not callable(member_ref_issuer):
        raise HeldOutEvaluationExchangeError("member-ref issuer is unavailable")
    try:
        request_ref = member_ref_issuer(
            family="runtime_exchange", scope=scope, natural_key={"direction": "request"}
        )
        result_ref = member_ref_issuer(
            family="runtime_exchange", scope=scope, natural_key={"direction": "result"}
        )
    except Exception as exc:
        raise HeldOutEvaluationExchangeError("member-ref issuance failed") from exc
    if (
        type(request_ref) is not AuthorityMemberRefV2
        or type(result_ref) is not AuthorityMemberRefV2
    ):
        raise HeldOutEvaluationExchangeError("member-ref issuer returned a foreign value")
    unsigned = {
        **dict(core),
        "request_member_ref": request_ref.to_mapping(),
        "result_member_ref": result_ref.to_mapping(),
    }
    return QuPathHeldOutEvaluationRequest._from_mapping(
        {**unsigned, "request_envelope_sha256": _evaluation_digest(unsigned)}
    )
