"""Strict exact-cover results for native held-out classifier evaluation.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence

from mif_pipeline.pipeline_core.durable_records import DurableRecordError, canonical_json_bytes

from ._held_out_evaluation_request import (
    HeldOutEvaluationExchangeError,
    QuPathHeldOutEvaluationRequest,
    decode_qupath_held_out_evaluation_request,
)
from ._training_codec import (
    _MAX_CLASSIFIER_BYTES,
    _MAX_EXCHANGE_BYTES,
    _canonical_digest,
    _closed_mapping,
    _decode_canonical,
    _export_json,
    _finite,
    _freeze_json,
    _integer,
    _json_shape,
    _literal,
    _sha256,
    _text,
)

_RESULT_CORE_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "status",
        "request_envelope_sha256",
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
        "native_classifier_type",
        "pixel_sampling_method",
        "classifier_version",
        "project_relative_path",
        "launch_nonce_sha256",
        "qupath_version",
        "native_classifier_sha256",
        "native_classifier_byte_count",
        "entry_observations",
        "predictions",
    }
)
_RESULT_FIELDS = _RESULT_CORE_FIELDS | frozenset({"result_envelope_sha256"})
_OPTIONAL_RESULT_FIELDS = frozenset({"pixel_scores"})
_ENTRY_OBSERVATION_FIELDS = frozenset(
    {
        "project_entry_id",
        "expected_saved_state_sha256",
        "qpdata_before_sha256",
        "qpdata_after_sha256",
    }
)
_PIXEL_PREDICTION_FIELDS = frozenset(
    {
        "prediction_kind",
        "project_entry_id",
        "subject_id",
        "annotation_uuid",
        "owner_evidence_sha256",
        "normalized_roi_sha256",
        "declared_label_counts",
        "ignored_sample_count",
        "predicted_class_id",
        "roi_bounds",
    }
)
_OBJECT_PREDICTION_FIELDS = frozenset(
    {
        "prediction_kind",
        "project_entry_id",
        "cell_id",
        "source_snapshot_sha256",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
        "segmentation_generation_id",
        "measurement_generation_id",
        "cell_geometry_sha256",
        "nucleus_geometry_sha256",
        "measurement_vector_sha256",
        "owner_evidence_sha256",
        "predicted_class_id",
    }
)


# ==============================================================================
#                           Held out evaluation result
# ==============================================================================


def _request_entries(request: QuPathHeldOutEvaluationRequest) -> tuple[Mapping[str, object], ...]:
    return tuple(request.to_mapping()["evaluation_entries"])  # type: ignore[return-value]


def _validate_entry_observations(
    value: object, *, entries: tuple[Mapping[str, object], ...]
) -> tuple[Mapping[str, object], ...]:
    if type(value) is not list:
        raise HeldOutEvaluationExchangeError("entry observations must be an array")
    rows = tuple(
        _closed_mapping(raw, _ENTRY_OBSERVATION_FIELDS, "entry observation") for raw in value
    )
    if len(rows) != len(entries):
        raise HeldOutEvaluationExchangeError("entry observations lack exact cover")
    for row, entry in zip(rows, entries, strict=True):
        _literal(row["project_entry_id"], entry["project_entry_id"], "observed entry")
        _literal(
            row["expected_saved_state_sha256"],
            entry["saved_state_sha256"],
            "expected saved-state digest",
        )
        before = _sha256(row["qpdata_before_sha256"], "before qpdata digest")
        after = _sha256(row["qpdata_after_sha256"], "after qpdata digest")
        if before != entry["saved_state_sha256"] or after != before:
            raise HeldOutEvaluationExchangeError("saved qpdata changed during evaluation")
    return rows


def _count_mapping(value: object, declared: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise HeldOutEvaluationExchangeError("declared label counts must be an object")
    if set(value) != set(declared):
        raise HeldOutEvaluationExchangeError("declared label counts differ")
    for key, count in value.items():
        _integer(count, f"declared label count {key}", maximum=(1 << 63) - 1)
    return value


def _pixel_prediction(
    raw: object,
    *,
    target: Mapping[str, object],
    declared: tuple[str, ...],
) -> Mapping[str, object]:
    row = _closed_mapping(raw, _PIXEL_PREDICTION_FIELDS, "pixel prediction")
    _literal(row["prediction_kind"], "pixel", "pixel prediction kind")
    for name in (
        "project_entry_id",
        "subject_id",
        "annotation_uuid",
        "owner_evidence_sha256",
        "normalized_roi_sha256",
    ):
        _literal(row[name], target[name], f"pixel prediction {name}")
    counts = _count_mapping(row["declared_label_counts"], declared)
    ignored = _integer(row["ignored_sample_count"], "ignored sample count", maximum=(1 << 63) - 1)
    del ignored
    total = sum(counts.values())
    if total <= 0:
        raise HeldOutEvaluationExchangeError("pixel prediction has zero declared samples")
    best = max(counts.values())
    majority = next(class_id for class_id in declared if counts[class_id] == best)
    _literal(row["predicted_class_id"], majority, "pixel majority class")
    bounds = _closed_mapping(row["roi_bounds"], frozenset({"x", "y", "width", "height", "z", "t"}), "pixel ROI bounds")
    for name in ("x", "y"):
        _finite(bounds[name], f"pixel ROI {name}")
    for name in ("width", "height"):
        _finite(bounds[name], f"pixel ROI {name}", minimum=0, positive=True)
    for name in ("z", "t"):
        _integer(bounds[name], f"pixel ROI {name}")
    return row


def _object_prediction(
    raw: object,
    *,
    entry: Mapping[str, object],
    cell: Mapping[str, object],
    declared: tuple[str, ...],
) -> Mapping[str, object]:
    row = _closed_mapping(raw, _OBJECT_PREDICTION_FIELDS, "object prediction")
    _literal(row["prediction_kind"], "object", "object prediction kind")
    expected = {
        "project_entry_id": entry["project_entry_id"],
        "cell_id": cell["cell_id"],
        "source_snapshot_sha256": entry["source_snapshot_sha256"],
        "section_id": entry["section_id"],
        "analysis_domain_id": entry["analysis_domain_id"],
        "analysis_domain_revision_sha256": entry["analysis_domain_revision_sha256"],
        "domain_set_sha256": entry["domain_set_sha256"],
        "segmentation_generation_id": entry["segmentation_generation_id"],
        "measurement_generation_id": entry["measurement_generation_id"],
        "cell_geometry_sha256": cell["cell_geometry_sha256"],
        "nucleus_geometry_sha256": cell["nucleus_geometry_sha256"],
        "measurement_vector_sha256": cell["measurement_vector_sha256"],
        "owner_evidence_sha256": cell["owner_evidence_sha256"],
    }
    for name, value in expected.items():
        _literal(row[name], value, f"object prediction {name}")
    predicted = _text(row["predicted_class_id"], "object predicted class")
    if predicted not in declared:
        raise HeldOutEvaluationExchangeError("object prediction class is undeclared")
    return row


def _validate_predictions(
    value: object,
    *,
    request: QuPathHeldOutEvaluationRequest,
    entries: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    if type(value) is not list:
        raise HeldOutEvaluationExchangeError("predictions must be an array")
    declared = tuple(request.declared_class_order)
    expected: list[tuple[Mapping[str, object], Mapping[str, object] | None]] = []
    for entry in entries:
        if request.native_classifier_type == "pixel":
            expected.extend(
                (entry, target) for target in entry["targets"]  # type: ignore[union-attr]
            )
        else:
            expected.extend(
                (entry, cell) for cell in entry["evaluation_cells"]  # type: ignore[union-attr]
            )
    if len(value) != len(expected):
        raise HeldOutEvaluationExchangeError("predictions lack exact request cover")
    rows: list[Mapping[str, object]] = []
    for raw, (entry, item) in zip(value, expected, strict=True):
        assert item is not None
        if request.native_classifier_type == "pixel":
            rows.append(_pixel_prediction(raw, target=item, declared=declared))
        else:
            rows.append(_object_prediction(raw, entry=entry, cell=item, declared=declared))
    return tuple(rows)


def _validate_result_mapping(
    value: object, *, request: QuPathHeldOutEvaluationRequest
) -> Mapping[str, object]:
    """Validate result mapping for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(request) is not QuPathHeldOutEvaluationRequest:
        raise HeldOutEvaluationExchangeError("result requires one exact request")
    expected_request = decode_qupath_held_out_evaluation_request(request.to_bytes())
    fields = _RESULT_FIELDS | ({"pixel_scores"} if isinstance(value, Mapping) and "pixel_scores" in value else set())
    result = _closed_mapping(value, fields, "held-out evaluation result")
    _literal(
        result["schema_name"],
        "mif-qupath-held-out-classifier-evaluation-result",
        "result schema",
    )
    _literal(result["schema_version"], 2, "result schema version")
    _literal(result["status"], "completed", "result status")
    for name in (
        "request_envelope_sha256",
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
        "native_classifier_type",
        "pixel_sampling_method",
        "classifier_version",
        "project_relative_path",
        "launch_nonce_sha256",
    ):
        _literal(result[name], getattr(expected_request, name), f"result {name}")
    _literal(
        result["qupath_version"],
        expected_request.expected_qupath_version,
        "observed QuPath version",
    )
    _literal(
        result["native_classifier_sha256"],
        expected_request.native_classifier_sha256,
        "observed classifier digest",
    )
    _literal(
        result["native_classifier_byte_count"],
        expected_request.native_classifier_byte_count,
        "observed classifier byte count",
    )
    _sha256(result["native_classifier_sha256"], "observed classifier digest")
    _integer(
        result["native_classifier_byte_count"],
        "observed classifier byte count",
        minimum=1,
        maximum=_MAX_CLASSIFIER_BYTES,
    )
    entries = _request_entries(expected_request)
    _validate_entry_observations(result["entry_observations"], entries=entries)
    _validate_predictions(result["predictions"], request=expected_request, entries=entries)
    if "pixel_scores" in result:
        from mif_pipeline.compartment_analysis.precision_recall import validate_held_out_pixel_scores
        try:
            validate_held_out_pixel_scores(result["pixel_scores"], expected_request.to_mapping())
        except ValueError as exc:
            raise HeldOutEvaluationExchangeError(str(exc)) from exc
    _sha256(result["result_envelope_sha256"], "result envelope digest")
    unsigned = {key: result[key] for key in result if key != "result_envelope_sha256"}
    if result["result_envelope_sha256"] != _canonical_digest(unsigned):
        raise HeldOutEvaluationExchangeError("result envelope digest is invalid")
    return result


class QuPathHeldOutEvaluationResult:
    """Deeply immutable exact-cover native predictions."""

    __slots__ = ("_mapping",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("held-out evaluation results are parser/issuer-created")

    @classmethod
    def _from_mapping(
        cls, value: object, *, request: QuPathHeldOutEvaluationRequest
    ) -> "QuPathHeldOutEvaluationResult":
        mapping = _validate_result_mapping(value, request=request)
        _json_shape(mapping)
        try:
            encoded = canonical_json_bytes(mapping)
        except DurableRecordError as exc:  # pragma: no cover
            raise HeldOutEvaluationExchangeError("result cannot be encoded") from exc
        if len(encoded) > _MAX_EXCHANGE_BYTES:
            raise HeldOutEvaluationExchangeError("result exceeds its byte bound")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_mapping", _freeze_json(dict(mapping)))
        return instance

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("held-out evaluation result is immutable")

    def __getattr__(self, name: str) -> object:
        if name in _OPTIONAL_RESULT_FIELDS:
            return self._mapping.get(name)
        if name in _RESULT_FIELDS:
            return self._mapping[name]
        raise AttributeError(name)

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is QuPathHeldOutEvaluationResult and self.to_mapping() == other.to_mapping()
        )

    def to_mapping(self) -> dict[str, object]:
        return _export_json(self._mapping)

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_mapping())
        except DurableRecordError as exc:  # pragma: no cover
            raise HeldOutEvaluationExchangeError("result cannot be encoded") from exc
        if len(payload) > _MAX_EXCHANGE_BYTES:
            raise HeldOutEvaluationExchangeError("result exceeds its byte bound")
        return payload


def decode_qupath_held_out_evaluation_result(
    payload: bytes, *, request: QuPathHeldOutEvaluationRequest
) -> QuPathHeldOutEvaluationResult:
    return QuPathHeldOutEvaluationResult._from_mapping(
        _decode_canonical(payload, "held-out evaluation result"), request=request
    )


def _issue_qupath_held_out_evaluation_result(
    *,
    request: QuPathHeldOutEvaluationRequest,
    qupath_version: str,
    observed_classifier_sha256: str,
    observed_classifier_byte_count: int,
    entry_observations: Sequence[Mapping[str, object]],
    predictions: Sequence[Mapping[str, object]],
) -> QuPathHeldOutEvaluationResult:
    if type(request) is not QuPathHeldOutEvaluationRequest:
        raise HeldOutEvaluationExchangeError("result issuer requires one exact request")
    request = decode_qupath_held_out_evaluation_request(request.to_bytes())
    core = {
        "schema_name": "mif-qupath-held-out-classifier-evaluation-result",
        "schema_version": 2,
        "status": "completed",
        "request_envelope_sha256": request.request_envelope_sha256,
        "authority_layout_sha256": request.authority_layout_sha256,
        "workspace_storage_scope_sha256": request.workspace_storage_scope_sha256,
        "active_mutation_run_sha256": request.active_mutation_run_sha256,
        "project_identity_sha256": request.project_identity_sha256,
        "fit_plan_sha256": request.fit_plan_sha256,
        "fit_execution_sha256": request.fit_execution_sha256,
        "checkpoint_entry_sha256": request.checkpoint_entry_sha256,
        "fit_material_sha256": request.fit_material_sha256,
        "classifier_id": request.classifier_id,
        "classifier_kind": request.classifier_kind,
        "native_classifier_type": request.native_classifier_type,
        "pixel_sampling_method": request.pixel_sampling_method,
        "classifier_version": request.classifier_version,
        "project_relative_path": request.project_relative_path,
        "launch_nonce_sha256": request.launch_nonce_sha256,
        "qupath_version": qupath_version,
        "native_classifier_sha256": observed_classifier_sha256,
        "native_classifier_byte_count": observed_classifier_byte_count,
        "entry_observations": [dict(item) for item in entry_observations],
        "predictions": [dict(item) for item in predictions],
    }
    return QuPathHeldOutEvaluationResult._from_mapping(
        {**core, "result_envelope_sha256": _canonical_digest(core)}, request=request
    )
