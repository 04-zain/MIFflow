"""Strict completed results for native QuPath classifier training.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping

from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    canonical_json_bytes,
)

from ._training_codec import (
    _array,
    _finite,
    _text,
    _MAX_CLASSIFIER_BYTES,
    _MAX_EXCHANGE_BYTES,
    QuPathNativeTrainingExchangeError,
    _canonical_digest,
    _closed_mapping,
    _decode_canonical,
    _export_json,
    _freeze_json,
    _integer,
    _json_shape,
    _literal,
    _ordered_texts,
    _sha256,
)
from ._training_request import (
    QuPathNativeTrainingRequest,
    decode_qupath_native_training_request,
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
        "acquisition_record_sha256",
        "fencing_token_sha256",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "classifier_id",
        "classifier_kind",
        "native_classifier_type",
        "classifier_version",
        "resource_name",
        "project_relative_path",
        "qupath_version",
        "training_entry_ids",
        "training_material_evidence_sha256",
        "required_classes",
        "effective_output_classes",
        "feature_spec_sha256",
        "model_configuration_sha256",
        "random_seed",
        "observed_entry_sample_counts",
        "observed_class_sample_counts",
        "training_sample_count",
        "training_feature_count",
        "native_classifier_sha256",
        "native_classifier_byte_count",
    }
)
_RESULT_FIELDS = _RESULT_CORE_FIELDS | frozenset({"result_envelope_sha256"})


# ==============================================================================
#                                Training result
# ==============================================================================


def _request_entry_ids(request: QuPathNativeTrainingRequest) -> tuple[str, ...]:
    ids: list[str] = []
    for entry in request.to_mapping()["training_entries"]:
        if entry["entry_kind"] == "pixel":
            ids.append(entry["entry_state"]["project_entry_id"])
        else:
            ids.append(entry["snapshot"]["project_entry_id"])
    return tuple(sorted(set(ids)))


def _request_output_classes(
    request: QuPathNativeTrainingRequest,
) -> tuple[str, ...]:
    configuration = request.to_mapping()["model_configuration"]
    if configuration.get("configuration_schema") == "qupath-grouped-cross-validation/v1":
        configuration = configuration["candidates"][0]
    if configuration.get("configuration_schema") in {
        "qupath-kronos-rtrees/v2",
        "qupath-kronos-svm/v2",
    }:
        configuration = configuration["native_configuration"]
    return tuple(
        item["canonical_class_id"] for item in configuration["output_labels"]["ordered_labels"]
    )


def _count_mapping(value: object, *, keys: tuple[str, ...], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise QuPathNativeTrainingExchangeError(f"{label} must be an object")
    if tuple(value) != tuple(sorted(keys)) or set(value) != set(keys):
        raise QuPathNativeTrainingExchangeError(f"{label} keys are invalid")
    for key, count in value.items():
        _integer(count, f"{label}.{key}", maximum=(1 << 63) - 1)
    return value


def _validate_marker_statistics_receipt(value, request, configuration):
    """Retain the final fit's actual moments within its authenticated result."""
    import math
    import struct
    from mif_pipeline.orchestration.batch_training.deferred_cell_evidence import BIOLOGY_FIELDS

    encoder = configuration.get("encoder") or configuration.get("feature_source", {}).get("encoder")
    pending = [] if encoder is None else [row["marker_name"] for row in encoder["additional_markers"]
                                          if set(row) == BIOLOGY_FIELDS]
    names = [] if encoder is None else [name for name in encoder["marker_names"] if name in pending]
    if value is None:
        if names:
            raise QuPathNativeTrainingExchangeError("Automatically fitted markers require their final statistics receipt")
        return
    if not names:
        raise QuPathNativeTrainingExchangeError("Statistics receipt requires pending automatic markers")
    receipt = _closed_mapping(value, frozenset({"schema", "method", "population", "variance_ddof",
        "marker_names", "sources", "fitted", "number_encoding"}), "marker statistics receipt")
    method = "kronos-native-tissue-sample-moments/v1"
    _literal(receipt["schema"], "kronos-marker-statistics-receipt/v1", "statistics receipt schema")
    _literal(receipt["method"], method, "statistics method")
    _literal(receipt["number_encoding"], "ieee754-binary64-big-endian-hex", "statistics number encoding")

    def receipt_number(value, label, **limits):
        if type(value) is not str or len(value) != 16 or any(c not in "0123456789abcdef" for c in value):
            raise QuPathNativeTrainingExchangeError(f"{label} must encode one binary64 value")
        return _finite(struct.unpack(">d", bytes.fromhex(value))[0], label, **limits)

    _literal(receipt["population"], "native-pixel-centers-in-accepted-material-domain-tissue-union", "statistics population")
    _integer(receipt["variance_ddof"], "statistics ddof", minimum=1, maximum=1)
    if _array(receipt["marker_names"], "automatic marker names", nonempty=True) != names:
        raise QuPathNativeTrainingExchangeError("Statistics receipt marker order differs")
    owners = [entry.get("snapshot", entry.get("entry_state")) for entry in request.training_entries]
    by_entry = {}
    for owner in owners:
        by_entry.setdefault(owner["project_entry_id"], []).append(owner)
    bindings = {(row["project_entry_id"], row["analysis_domain_id"]): row
                for row in request.training_analysis_domain_bindings}
    positions = [encoder["marker_names"].index(name) for name in names]
    channel_ids = [encoder["channel_ids"][index] for index in positions]
    divisors = [encoder["intensity_divisors"][index] for index in positions]
    if "feature_source" in configuration:
        channel_names = [configuration["feature_source"]["channel_names"][index] for index in positions]
    else:
        channel_names = [request.feature_spec["ordered_channels"][index]["observed_qupath_channel_name"] for index in positions]
    totals = [(0, 0.0, 0.0) for name in names]
    seen = set()
    source_keys = set()
    for raw in _array(receipt["sources"], "statistics sources", nonempty=True):
        source = _closed_mapping(raw, frozenset({"method", "project_entry_id", "image_identity_sha256", "z", "t",
            "geometry_sha256", "channel_ids", "channel_names", "channel_indices", "intensity_divisors",
            "subject_ids", "regions", "moments"}), "statistics source")
        _literal(source["method"], method, "source statistics method")
        entry_id = _text(source["project_entry_id"], "statistics entry")
        matched = by_entry.get(entry_id, [])
        if (not matched or any(owner["split"] != "training" or owner["image_identity_sha256"] != source["image_identity_sha256"] for owner in matched)
                or _array(source["subject_ids"], "statistics subjects", nonempty=True) != sorted({owner["subject_id"] for owner in matched})):
            raise QuPathNativeTrainingExchangeError("Statistics source differs from training subjects/images")
        seen.add(entry_id)
        for key in ("image_identity_sha256", "geometry_sha256"):
            _sha256(source[key], "statistics " + key)
        for key in ("z", "t"):
            _integer(source[key], "statistics plane " + key, minimum=0)
        source_key = (entry_id, source["z"], source["t"])
        if source_key in source_keys:
            raise QuPathNativeTrainingExchangeError("Statistics source plane repeats")
        source_keys.add(source_key)
        source_divisors = [receipt_number(divisor, "statistics divisor", minimum=0.0, positive=True)
                           for divisor in _array(source["intensity_divisors"], "statistics divisors", nonempty=True)]
        if (source["channel_ids"] != channel_ids or source["channel_names"] != channel_names
                or source_divisors != divisors):
            raise QuPathNativeTrainingExchangeError("Statistics channels or divisors differ from the authored source")
        indices = _array(source["channel_indices"], "statistics channel indices", nonempty=True)
        for index in indices:
            _integer(index, "statistics channel index", minimum=0)
        if len(indices) != len(names) or len(set(indices)) != len(indices):
            raise QuPathNativeTrainingExchangeError("Statistics channel indices differ")
        for raw_region in _array(source["regions"], "statistics regions", nonempty=True):
            region = _closed_mapping(raw_region, frozenset({"analysis_domain_id", "analysis_domain_revision_sha256",
                "domain_set_sha256", "tissue_object_id", "material_object_id", "geometry_sha256"}), "statistics region")
            _sha256(region["analysis_domain_id"], "statistics analysis domain")
            binding = bindings.get((entry_id, region["analysis_domain_id"]))
            if binding is None or binding["project_entry_id"] != entry_id or any(region[key] != binding[key]
                    for key in ("analysis_domain_revision_sha256", "domain_set_sha256")):
                raise QuPathNativeTrainingExchangeError("Statistics region differs from authenticated training domains")
            _sha256(region["geometry_sha256"], "statistics region geometry")
            _text(region["tissue_object_id"], "statistics tissue object")
            _text(region["material_object_id"], "statistics material object")
        moments = _array(source["moments"], "statistics moments", nonempty=True)
        if len(moments) != len(names):
            raise QuPathNativeTrainingExchangeError("Statistics moment width differs")
        for index, raw_moment in enumerate(moments):
            moment = _closed_mapping(raw_moment, frozenset({"count", "mean", "m2"}), "statistics moment")
            n = _integer(moment["count"], "statistics count", minimum=1, maximum=(1 << 63) - 1)
            mean = receipt_number(moment["mean"], "statistics mean")
            m2 = receipt_number(moment["m2"], "statistics centered sum", minimum=0.0)
            n0, mean0, m20 = totals[index]
            delta = mean - mean0
            total = n0 + n
            totals[index] = (total, mean0 + delta * n / total, m20 + m2 + delta * delta * n0 * n / total)
    if seen != set(by_entry):
        raise QuPathNativeTrainingExchangeError("Statistics receipt lacks exact training entry coverage")
    fitted = _array(receipt["fitted"], "fitted statistics", nonempty=True)
    if len(fitted) != len(names):
        raise QuPathNativeTrainingExchangeError("Fitted statistics width differs")
    for name, total, raw in zip(names, totals, fitted, strict=True):
        row = _closed_mapping(raw, frozenset({"marker_name", "count", "mean", "std"}), "fitted marker statistics")
        _literal(row["marker_name"], name, "fitted marker")
        n = _integer(row["count"], "fitted pixel count", minimum=2, maximum=(1 << 63) - 1)
        mean = receipt_number(row["mean"], "fitted mean")
        std = receipt_number(row["std"], "fitted standard deviation", minimum=0.0, positive=True)
        try:
            mean32, std32 = struct.unpack(">ff", struct.pack(">ff", mean, std))
        except (OverflowError, struct.error) as exc:
            raise QuPathNativeTrainingExchangeError("Fitted statistics overflow float32") from exc
        if not math.isfinite(mean32) or not math.isfinite(std32) or std32 <= 0:
            raise QuPathNativeTrainingExchangeError("Fitted statistics must remain finite with positive SD in float32")
        if n != total[0] or not math.isclose(mean, total[1], rel_tol=1e-12, abs_tol=1e-14) or not math.isclose(std, math.sqrt(total[2] / (n - 1)), rel_tol=1e-12, abs_tol=1e-14):
            raise QuPathNativeTrainingExchangeError("Fitted statistics differ from pooled sample moments")


def _validate_result_mapping(
    value: object,
    *,
    request: QuPathNativeTrainingRequest,
) -> Mapping[str, object]:
    """Validate result mapping for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(request) is not QuPathNativeTrainingRequest:
        raise QuPathNativeTrainingExchangeError("result requires one exact request")
    # Reparse the immutable bytes so a hostile in-process attribute mutation
    # cannot turn an unvalidated request object into result authority.
    expected_request = decode_qupath_native_training_request(request.to_bytes())
    search = (
        expected_request.model_configuration.get("configuration_schema")
        == "qupath-grouped-cross-validation/v1"
    )
    fields = _RESULT_FIELDS | {"model_selection_report"} if search else _RESULT_FIELDS
    fields |= {"marker_statistics_receipt"} if isinstance(value, Mapping) and "marker_statistics_receipt" in value else set()
    result = _closed_mapping(value, fields, "native-training result")
    _literal(result["schema_name"], "mif-qupath-native-training-result", "result schema")
    _literal(result["schema_version"], 2 if search else 1, "result schema version")
    if search:
        from mif_pipeline.pipeline_core.model_selection import (
            validate_selection_report,
            request_entries,
        )

        config = expected_request.model_configuration
        try:
            validate_selection_report(
                result["model_selection_report"],
                request_entries(expected_request.to_mapping()),
                tuple(expected_request.required_classes),
                len(config["candidates"]),
                config["folds"],
                expected_request.random_seed,
            )
            resources = result["model_selection_report"].get("fold_resources")
            if resources is not None and resources["fit_execution_sha256"] != expected_request.fit_execution_sha256:
                raise ValueError("CV review resources belong to another fit")
        except (ValueError, TypeError, KeyError) as exc:
            raise QuPathNativeTrainingExchangeError(
                f"Invalid model selection report: {exc}"
            ) from exc
    configuration = expected_request.model_configuration
    if search:
        configuration = configuration["candidates"][result["model_selection_report"]["selected_candidate_index"]]
    _validate_marker_statistics_receipt(result.get("marker_statistics_receipt"), expected_request, configuration)
    if "marker_statistics_receipt" in result and result["marker_statistics_receipt"] is None:
        raise QuPathNativeTrainingExchangeError("An optional statistics receipt cannot be null")
    _literal(result["status"], "completed", "result status")
    common = (
        "request_envelope_sha256",
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "acquisition_record_sha256",
        "fencing_token_sha256",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "classifier_id",
        "classifier_kind",
        "native_classifier_type",
        "classifier_version",
        "resource_name",
        "project_relative_path",
        "training_material_evidence_sha256",
        "model_configuration_sha256",
        "random_seed",
    )
    for name in common:
        _literal(result[name], getattr(expected_request, name), f"result {name}")
    _literal(
        result["qupath_version"],
        expected_request.expected_qupath_version,
        "observed QuPath version",
    )
    entry_ids = _request_entry_ids(expected_request)
    result_entry_ids = _ordered_texts(
        result["training_entry_ids"], "result training entries", sorted_order=True
    )
    if result_entry_ids != entry_ids:
        raise QuPathNativeTrainingExchangeError("result training entries differ")
    required = _ordered_texts(
        result["required_classes"], "result required classes", sorted_order=True
    )
    if required != tuple(expected_request.required_classes):
        raise QuPathNativeTrainingExchangeError("result required classes differ")
    output_classes = _ordered_texts(
        result["effective_output_classes"],
        "effective output classes",
        sorted_order=False,
    )
    if output_classes != _request_output_classes(expected_request):
        raise QuPathNativeTrainingExchangeError("effective output classes differ")
    _sha256(result["feature_spec_sha256"], "feature specification digest")
    if result["feature_spec_sha256"] != expected_request.feature_spec_sha256:
        raise QuPathNativeTrainingExchangeError("feature specification digest differs")
    entry_counts = _count_mapping(
        result["observed_entry_sample_counts"],
        keys=entry_ids,
        label="entry sample counts",
    )
    class_counts = _count_mapping(
        result["observed_class_sample_counts"],
        keys=output_classes,
        label="class sample counts",
    )
    total = _integer(
        result["training_sample_count"],
        "training sample count",
        minimum=1,
        maximum=(1 << 63) - 1,
    )
    if total != sum(entry_counts.values()) or total != sum(class_counts.values()):
        raise QuPathNativeTrainingExchangeError("training sample counts disagree")
    _integer(result["training_feature_count"], "training feature count", minimum=1)
    _sha256(result["native_classifier_sha256"], "native classifier digest")
    _integer(
        result["native_classifier_byte_count"],
        "native classifier byte count",
        minimum=1,
        maximum=_MAX_CLASSIFIER_BYTES,
    )
    _sha256(result["result_envelope_sha256"], "result envelope digest")
    unsigned = {key: result[key] for key in result if key != "result_envelope_sha256"}
    if result["result_envelope_sha256"] != _canonical_digest(unsigned):
        raise QuPathNativeTrainingExchangeError("result envelope digest is invalid")
    return result


class QuPathNativeTrainingResult:
    """Deeply immutable completed child-process observation."""

    __slots__ = ("_mapping",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("native-training results are parser/issuer-created")

    @classmethod
    def _from_mapping(
        cls,
        value: object,
        *,
        request: QuPathNativeTrainingRequest,
    ) -> "QuPathNativeTrainingResult":
        mapping = _validate_result_mapping(value, request=request)
        _json_shape(mapping)
        try:
            encoded = canonical_json_bytes(mapping)
        except DurableRecordError as exc:  # pragma: no cover - validator invariant
            raise QuPathNativeTrainingExchangeError("result cannot be encoded") from exc
        if len(encoded) > _MAX_EXCHANGE_BYTES:
            raise QuPathNativeTrainingExchangeError("result exceeds its byte bound")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_mapping", _freeze_json(dict(mapping)))
        return instance

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("native-training result is immutable")

    def __getattr__(self, name: str) -> object:
        if name in self._mapping and name in _RESULT_FIELDS | {"model_selection_report", "marker_statistics_receipt"}:
            return self._mapping[name]
        raise AttributeError(name)

    def __eq__(self, other: object) -> bool:
        return type(other) is QuPathNativeTrainingResult and self.to_mapping() == other.to_mapping()

    def to_mapping(self) -> dict[str, object]:
        return _export_json(self._mapping)

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_mapping())
        except DurableRecordError as exc:  # pragma: no cover - construction invariant
            raise QuPathNativeTrainingExchangeError("result cannot be encoded") from exc
        if len(payload) > _MAX_EXCHANGE_BYTES:
            raise QuPathNativeTrainingExchangeError("result exceeds its byte bound")
        return payload


def decode_qupath_native_training_result(
    payload: bytes, *, request: QuPathNativeTrainingRequest
) -> QuPathNativeTrainingResult:
    return QuPathNativeTrainingResult._from_mapping(
        _decode_canonical(payload, "native-training result"),
        request=request,
    )


def _issue_qupath_native_training_result(
    *,
    request: QuPathNativeTrainingRequest,
    qupath_version: str,
    observed_entry_sample_counts: Mapping[str, int],
    observed_class_sample_counts: Mapping[str, int],
    training_feature_count: int,
    native_classifier_sha256: str,
    native_classifier_byte_count: int,
    model_selection_report: Mapping[str, object] | None = None,
    marker_statistics_receipt: Mapping[str, object] | None = None,
) -> QuPathNativeTrainingResult:
    if type(request) is not QuPathNativeTrainingRequest:
        raise QuPathNativeTrainingExchangeError("result issuer requires one exact request")
    request = decode_qupath_native_training_request(request.to_bytes())
    entry_ids = _request_entry_ids(request)
    output_classes = _request_output_classes(request)
    entry_counts = dict(observed_entry_sample_counts)
    class_counts = dict(observed_class_sample_counts)
    _count_mapping(entry_counts, keys=entry_ids, label="entry sample counts")
    _count_mapping(class_counts, keys=output_classes, label="class sample counts")
    total = sum(entry_counts.values())
    core = {
        "schema_name": "mif-qupath-native-training-result",
        "schema_version": 1,
        "status": "completed",
        "request_envelope_sha256": request.request_envelope_sha256,
        "authority_layout_sha256": request.authority_layout_sha256,
        "workspace_storage_scope_sha256": request.workspace_storage_scope_sha256,
        "active_mutation_run_sha256": request.active_mutation_run_sha256,
        "project_identity_sha256": request.project_identity_sha256,
        "acquisition_record_sha256": request.acquisition_record_sha256,
        "fencing_token_sha256": request.fencing_token_sha256,
        "fit_plan_sha256": request.fit_plan_sha256,
        "fit_execution_sha256": request.fit_execution_sha256,
        "classifier_id": request.classifier_id,
        "classifier_kind": request.classifier_kind,
        "native_classifier_type": request.native_classifier_type,
        "classifier_version": request.classifier_version,
        "resource_name": request.resource_name,
        "project_relative_path": request.project_relative_path,
        "qupath_version": qupath_version,
        "training_entry_ids": list(entry_ids),
        "training_material_evidence_sha256": request.training_material_evidence_sha256,
        "required_classes": list(request.required_classes),
        "effective_output_classes": list(output_classes),
        "feature_spec_sha256": request.feature_spec_sha256,
        "model_configuration_sha256": request.model_configuration_sha256,
        "random_seed": request.random_seed,
        "observed_entry_sample_counts": entry_counts,
        "observed_class_sample_counts": class_counts,
        "training_sample_count": total,
        "training_feature_count": training_feature_count,
        "native_classifier_sha256": native_classifier_sha256,
        "native_classifier_byte_count": native_classifier_byte_count,
    }
    if (
        request.model_configuration.get("configuration_schema")
        == "qupath-grouped-cross-validation/v1"
    ):
        if model_selection_report is None:
            raise QuPathNativeTrainingExchangeError(
                "The grouped fit requires its complete model selection report"
            )
        core.update(schema_version=2, model_selection_report=model_selection_report)
    elif model_selection_report is not None:
        raise QuPathNativeTrainingExchangeError(
            "A fixed-model fit cannot claim cross-validation results"
        )
    if marker_statistics_receipt is not None:
        core["marker_statistics_receipt"] = _export_json(marker_statistics_receipt)
    return QuPathNativeTrainingResult._from_mapping(
        {**core, "result_envelope_sha256": _canonical_digest(core)},
        request=request,
    )
