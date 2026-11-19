"""Authenticated compact measurement-result artifacts for batch execution.

This owner is deliberately a read-only composition boundary. It revalidates an already
completed result with the existing workflow measurement validator, streams row values to
derive compact commitments, and binds the result to a current committed segmentation
authority. It neither retains inline cell values nor runs or mutates QuPath."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Final

from mif_pipeline.cell_analysis.measurement_plan import FeatureKind, MeasurementPlan
from mif_pipeline.cell_analysis.measurement_rows import (
    MARKER_INTENSITY_ROWS_SCHEMA,
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamExpectation,
    ValidatedMeasurementRowStream,
    validate_measurement_row_stream,
)
from mif_pipeline.pipeline_core import AnalysisConfig, ROIAnalysisError, StageStatus
from mif_pipeline.qupath_runtime.project_stage import ProjectStageTargetObservation
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from .batch_analysis_preflight import (
    BatchAnalysisPreflightError,
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from .batch_segmentation_artifacts import (
    ValidatedSegmentationResultProjection,
    decode_committed_segmentation_result,
)
from .project_stage_plan import (
    ProjectStageAuthorizedEntry,
    ProjectStageRecordError,
    ProjectStageTargetExecutionEvidence,
)
from .workflow import (
    MeasurementResultValidationContext,
    _mapping_sha256,
    ValidatedMeasurementResult,
    validate_measurement_result,
)

_CONTRACT_ID: Final[str] = "mif-batch-measurements-result-identity/v1"
_ARTIFACT_KIND: Final[str] = "measurements_result"
_SCHEMA_NAME: Final[str] = "roi-generation-manifest"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-measurements-result/v1"
_NODE_KIND: Final[str] = "measurements"
_GENERATION_MANIFEST_SCHEMA: Final[str] = "roi-generation-manifest/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JSON_DEPTH: Final[int] = 16
_MAX_JSON_ITEMS: Final[int] = 4096
_MAX_TEXT_LENGTH: Final[int] = 65_536
_MAX_SIGNED_INTEGER: Final[int] = (1 << 63) - 1

_GENERATION_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "generation_ids",
        "generations",
        "object_outputs",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "slide_id",
        "run_id",
        "generation_ids",
        "generation_id",
        "canonical_image_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "analysis_domain_path_object_id",
        "structure_id",
        "eligible_parent_id",
        "eligible_parent_ids",
        "action",
        "action_context",
        "classifier_reuse_decisions",
        "confirmed_marker_rules",
        "registration_context",
        "segmentation_population_id",
        "segmentation_population_ids",
        "feature_requests",
        "canonical_channels",
        "segmentation_model_version",
        "segmentation_cell_dataset_sha256",
        "sample_id",
    }
)
_FEATURE_FIELDS = (
    "feature_id",
    "measurement_name",
    "kind",
    "channel_id",
    "biomarker",
    "compartment",
    "units",
    "producer",
    "producer_version",
    "missing_rate",
    "non_finite_rate",
    "opt_in",
)
_CHANNEL_FIELDS = (
    "channel_id",
    "name",
    "component",
    "fluorophore",
    "biomarker",
)
_COMMITMENT_FIELDS = (
    "analysis_domain_id",
    "analysis_domain_revision_sha256",
    "cell_count",
    "cell_ids_sha256",
)
_MEASUREMENT_ROWS_REFERENCE_FIELDS = (
    "path",
    "content_schema_version",
    "sha256",
    "byte_count",
    "row_count",
    "cell_ids_sha256",
    "cell_to_domain_membership_sha256",
    "domain_cell_commitments_sha256",
    "feature_schema_sha256",
    "row_inventory_sha256",
    "reference_sha256",
)

_SCIENTIFIC_IDENTITY_FIELDS = (
    "sample_id",
    "slide_id",
    "image_role",
    "project_entry_id",
    "canonical_image_id",
    "canonical_image_sha256",
    "current_image_identity_sha256",
    "section_id",
    "tissue_generation_id",
    "tissue_path_object_id",
    "tissue_roi_id",
    "analysis_domain_binding_set_sha256",
    "domain_set_sha256",
    "segmentation_result_ref_sha256",
    "analysis_frame_ref_sha256",
    "segmentation_state_head_sha256",
    "active_plan_sha256",
    "segmentation_terminal_record_sha256",
    "segmentation_execution_exchange_sha256",
    "segmentation_generation_id",
    "segmentation_population_id",
    "segmentation_population_sha256",
    "segmentation_config_sha256",
    "segmentation_domain_sha256",
    "cell_count",
    "cell_ids_sha256",
    "cell_to_domain_membership_sha256",
    "cell_evidence_sha256",
    "domain_cell_commitments",
    "domain_cell_commitments_sha256",
    "canonical_channels",
    "canonical_channels_sha256",
    "measurement_generation_id",
    "generation_ids",
    "measurement_plan",
    "measurement_plan_sha256",
    "selected_measurement_names",
    "feature_request_set_sha256",
    "measurement_inventory",
    "measurement_inventory_sha256",
    "observed_measurements_sha256",
    "measurement_keys_sha256",
    "feature_schema_sha256",
    "measurement_rows",
    "object_outputs_sha256",
)
_TASK10_IDENTITY_FIELDS = (
    "node_id",
    "attempt_id",
    "attempt_authorization_sha256",
    "pipeline_run_id",
    "input_artifact_set_sha256",
    "prerequisite_artifact_ref_sha256s",
    "target_request_sha256",
    "authorized_entry_sha256",
    "generation_manifest_payload_sha256",
    "generation_manifest_byte_count",
    "result_payload_sha256",
    "result_byte_count",
    "process_evidence_sha256",
    "project_snapshot_sha256",
    "saved_state_disposition",
    "saved_state_before_sha256",
    "saved_state_after_sha256",
    "scientific_request_body_sha256",
    "scientific_request_body_byte_count",
    "resolved_config_sha256",
    "request_capability_sha256",
    "validation_input_authority_sha256",
    "execution_exchange_sha256",
    "validated_exchange_sha256",
    "adapter_spec_sha256",
    "projection_target_sha256",
    "script_sha256",
    "project_identity_sha256",
    "project_authority_head_sha256",
    "project_qpproj_snapshot_sha256",
    "project_entry_projection_sha256",
    "compatibility_signature_sha256",
    "request_codec_id",
    "request_codec_version",
)

_TOP_LEVEL_IDENTITY_FIELDS = frozenset(
    {
        *_SCIENTIFIC_IDENTITY_FIELDS,
        *_TASK10_IDENTITY_FIELDS,
        "measurement_scientific_identity_sha256",
        "task10_execution_identity_sha256",
    }
)
_IDENTITY_LEAF_FIELDS = {
    *(
        field
        for field in _TOP_LEVEL_IDENTITY_FIELDS
        if field
        not in {
            "domain_cell_commitments",
            "canonical_channels",
            "measurement_plan",
            "measurement_inventory",
            "measurement_rows",
        }
    ),
    *(f"domain_cell_commitments.{field}" for field in _COMMITMENT_FIELDS),
    *(f"canonical_channels.{field}" for field in _CHANNEL_FIELDS),
    "measurement_plan.schema_version",
    *(f"measurement_plan.features.{field}" for field in _FEATURE_FIELDS),
    *(f"measurement_inventory.{field}" for field in _FEATURE_FIELDS),
    *(f"measurement_rows.{field}" for field in _MEASUREMENT_ROWS_REFERENCE_FIELDS),
}
MEASUREMENTS_RESULT_IDENTITY_FIELDS: tuple[str, ...] = tuple(sorted(_IDENTITY_LEAF_FIELDS))


# ==============================================================================
#                          Batch measurement artifacts
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class ValidatedMeasurementRowsReference:
    """Decoder-only compact authority for one authenticated row member."""

    path: Path
    content_schema_version: str
    sha256: str
    byte_count: int
    row_count: int
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    domain_cell_commitments_sha256: str
    feature_schema_sha256: str
    row_inventory_sha256: str
    reference_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedMeasurementRowsReference is decoder-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("ValidatedMeasurementRowsReference cannot be subclassed")

    def __reduce__(self) -> object:
        raise TypeError("ValidatedMeasurementRowsReference is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedMeasurementRowsReference is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedMeasurementsResultArtifactRefProjection:
    """Decoder-only compact measurement projection without state authority."""

    artifact_ref: BatchArtifactRef
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    tissue_path_object_id: str
    tissue_roi_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    segmentation_generation_id: str
    segmentation_population_id: str
    segmentation_population_sha256: str
    measurement_generation_id: str
    generation_ids: tuple[str, ...]
    measurement_plan: MeasurementPlan
    canonical_channels: tuple[Mapping[str, object], ...]
    selected_measurement_names: tuple[str, ...]
    measurement_inventory: tuple[Mapping[str, object], ...]
    measurement_plan_sha256: str
    measurement_inventory_sha256: str
    feature_schema_sha256: str
    cell_count: int
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    domain_cell_commitments: tuple[Mapping[str, object], ...]
    domain_cell_commitments_sha256: str
    measurement_rows: ValidatedMeasurementRowsReference
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedMeasurementsResultArtifactRefProjection is decoder-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("ValidatedMeasurementsResultArtifactRefProjection cannot be subclassed")

    def __reduce__(self) -> object:
        raise TypeError("ValidatedMeasurementsResultArtifactRefProjection is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedMeasurementsResultArtifactRefProjection is nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedMeasurementsResultProjection:
    """Decoder-only compact measurement projection bound to batch state."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    tissue_path_object_id: str
    tissue_roi_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    segmentation_generation_id: str
    segmentation_population_id: str
    segmentation_population_sha256: str
    measurement_generation_id: str
    generation_ids: tuple[str, ...]
    measurement_plan: MeasurementPlan
    canonical_channels: tuple[Mapping[str, object], ...]
    selected_measurement_names: tuple[str, ...]
    measurement_inventory: tuple[Mapping[str, object], ...]
    measurement_plan_sha256: str
    measurement_inventory_sha256: str
    feature_schema_sha256: str
    cell_count: int
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    domain_cell_commitments: tuple[Mapping[str, object], ...]
    domain_cell_commitments_sha256: str
    measurement_rows: ValidatedMeasurementRowsReference
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str
    validated_exchange_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedMeasurementsResultProjection is decoder-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("ValidatedMeasurementsResultProjection cannot be subclassed")

    def __reduce__(self) -> object:
        raise TypeError("ValidatedMeasurementsResultProjection is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedMeasurementsResultProjection is nonserializable")


def _artifact_ref_projection(
    **values: object,
) -> ValidatedMeasurementsResultArtifactRefProjection:
    result = object.__new__(ValidatedMeasurementsResultArtifactRefProjection)
    for name in ValidatedMeasurementsResultArtifactRefProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _projection(**values: object) -> ValidatedMeasurementsResultProjection:
    result = object.__new__(ValidatedMeasurementsResultProjection)
    for name in ValidatedMeasurementsResultProjection.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _plain(
    value: object,
    *,
    _depth: int = 0,
    _remaining: list[int] | None = None,
    _active: set[int] | None = None,
) -> object:
    """Copy one bounded passive JSON value while rejecting cycles."""

    if _depth > _MAX_JSON_DEPTH:
        raise BatchArtifactError("measurements artifact evidence exceeds the JSON depth limit")
    remaining = [_MAX_JSON_ITEMS] if _remaining is None else _remaining
    remaining[0] -= 1
    if remaining[0] < 0:
        raise BatchArtifactError("measurements artifact evidence exceeds the JSON item limit")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > _MAX_SIGNED_INTEGER:
            raise BatchArtifactError(
                "measurements artifact evidence contains an out-of-range integer"
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise BatchArtifactError("measurements artifact evidence contains a non-finite number")
        return value
    if type(value) is str:
        if len(value) > _MAX_TEXT_LENGTH:
            raise BatchArtifactError("measurements artifact evidence contains overlong text")
        return value
    active = set() if _active is None else _active
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise BatchArtifactError("measurements artifact evidence requires string keys")
        identity = id(value)
        if identity in active:
            raise BatchArtifactError("measurements artifact evidence contains a JSON cycle")
        active.add(identity)
        try:
            result: dict[str, object] = {}
            for key in sorted(value):
                if len(key) > _MAX_TEXT_LENGTH:
                    raise BatchArtifactError(
                        "measurements artifact evidence contains an overlong key"
                    )
                result[key] = _plain(
                    value[key],
                    _depth=_depth + 1,
                    _remaining=remaining,
                    _active=active,
                )
            return result
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            length = len(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise BatchArtifactError(
                "measurements artifact evidence contains an invalid sequence"
            ) from exc
        if length > remaining[0]:
            raise BatchArtifactError("measurements artifact evidence exceeds the JSON item limit")
        identity = id(value)
        if identity in active:
            raise BatchArtifactError("measurements artifact evidence contains a JSON cycle")
        active.add(identity)
        try:
            result = [
                _plain(
                    value[index],
                    _depth=_depth + 1,
                    _remaining=remaining,
                    _active=active,
                )
                for index in range(length)
            ]
            if len(value) != length:
                raise BatchArtifactError(
                    "measurements artifact evidence sequence changed during validation"
                )
            return result
        except (IndexError, KeyError) as exc:
            raise BatchArtifactError(
                "measurements artifact evidence sequence changed during validation"
            ) from exc
        finally:
            active.remove(identity)
    raise BatchArtifactError("measurements artifact evidence must contain only finite JSON values")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as exc:
        raise BatchArtifactError(
            "measurements artifact evidence is not finite canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()






def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    if len(value) > _MAX_TEXT_LENGTH:
        raise BatchArtifactError(f"{field} exceeds the bounded text limit")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _sha256(value: object, field: str) -> str:
    result = _text(value, field)
    if _SHA256.fullmatch(result) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return result


def _nonnegative_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BatchArtifactError(f"{field} must be a nonnegative integer")
    return value


def _positive_integer(value: object, field: str) -> int:
    result = _nonnegative_integer(value, field)
    if result == 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return result


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    try:
        length = len(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise BatchArtifactError(f"{field} has invalid array length") from exc
    if length > _MAX_JSON_ITEMS:
        raise BatchArtifactError(f"{field} exceeds the bounded item limit")
    try:
        result = tuple(value[index] for index in range(length))
    except (IndexError, KeyError) as exc:
        raise BatchArtifactError(f"{field} changed during validation") from exc
    if len(value) != length:
        raise BatchArtifactError(f"{field} changed during validation")
    return result


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    if set(value) != fields:
        raise BatchArtifactError(f"{field} does not have its exact closed fields")
    return value


def _binding_set_sha256(bindings: tuple[AnalysisDomainBinding, ...]) -> str:
    return _digest({"analysis_domain_bindings": [item.to_mapping() for item in bindings]})


def _feature_schema_sha256(plan: MeasurementPlan) -> str:
    return _digest(
        [
            {
                "measurement": item.measurement,
                "role": item.role.value,
                "channel_id": item.channel_id,
                "feature_id": item.feature_id,
                "transform": item.transform,
                "weight": item.weight,
                "kind": item.kind,
                "biomarker": item.biomarker,
                "compartment": item.compartment,
                "units": item.units,
                "producer": item.producer,
                "producer_version": item.producer_version,
                "missing_rate": item.missing_rate,
                "non_finite_rate": item.non_finite_rate,
                "opt_in": item.opt_in,
            }
            for item in plan.features
        ]
    )


def _marker_plan(plan: MeasurementPlan) -> MeasurementPlan:
    """Return the exact mIF protein-marker columns carried by row bytes."""

    try:
        return MeasurementPlan(
            tuple(item for item in plan.features if item.kind == FeatureKind.EXPRESSION.value)
        )
    except ROIAnalysisError as exc:
        raise BatchArtifactError(
            "measurement rows require an explicit protein-marker plan"
        ) from exc


def _measurement_rows_reference_mapping(
    reference: ValidatedMeasurementRowsReference,
) -> dict[str, object]:
    return {
        field: (str(reference.path) if field == "path" else getattr(reference, field))
        for field in _MEASUREMENT_ROWS_REFERENCE_FIELDS
    }


def _issue_measurement_rows_reference(
    stream: ValidatedMeasurementRowStream,
    *,
    feature_schema_sha256: str,
) -> ValidatedMeasurementRowsReference:
    if type(stream) is not ValidatedMeasurementRowStream:
        raise BatchArtifactError("measurement rows reference requires validated stream authority")
    values: dict[str, object] = {
        "path": stream.descriptor.path,
        "content_schema_version": stream.descriptor.content_schema_version,
        "sha256": stream.descriptor.sha256,
        "byte_count": stream.descriptor.byte_count,
        "row_count": stream.descriptor.row_count,
        "cell_ids_sha256": stream.cell_ids_sha256,
        "cell_to_domain_membership_sha256": (stream.cell_to_domain_membership_sha256),
        "domain_cell_commitments_sha256": (stream.domain_cell_commitments_sha256),
        "feature_schema_sha256": feature_schema_sha256,
        "row_inventory_sha256": stream.row_inventory_sha256,
    }
    values["reference_sha256"] = _digest(
        {
            field: str(values[field]) if field == "path" else values[field]
            for field in _MEASUREMENT_ROWS_REFERENCE_FIELDS[:-1]
        }
    )
    result = object.__new__(ValidatedMeasurementRowsReference)
    for name in ValidatedMeasurementRowsReference.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _parse_measurement_rows_reference(
    value: object,
) -> ValidatedMeasurementRowsReference:
    mapping = _closed_mapping(
        value,
        frozenset(_MEASUREMENT_ROWS_REFERENCE_FIELDS),
        "measurement_rows",
    )
    raw_path = _text(mapping["path"], "measurement_rows.path")
    if not os.path.isabs(raw_path) or raw_path != os.path.normpath(raw_path):
        raise BatchArtifactError("measurement_rows.path must be absolute normalized native text")
    if mapping["content_schema_version"] != MARKER_INTENSITY_ROWS_SCHEMA:
        raise BatchArtifactError("measurement_rows.content_schema_version is unsupported")
    values: dict[str, object] = {
        "path": Path(raw_path),
        "content_schema_version": mapping["content_schema_version"],
        "sha256": _sha256(mapping["sha256"], "measurement_rows.sha256"),
        "byte_count": _positive_integer(mapping["byte_count"], "measurement_rows.byte_count"),
        "row_count": _positive_integer(mapping["row_count"], "measurement_rows.row_count"),
    }
    for field in (
        "cell_ids_sha256",
        "cell_to_domain_membership_sha256",
        "domain_cell_commitments_sha256",
        "feature_schema_sha256",
        "row_inventory_sha256",
        "reference_sha256",
    ):
        values[field] = _sha256(mapping[field], f"measurement_rows.{field}")
    if values["reference_sha256"] != _digest(
        {field: mapping[field] for field in _MEASUREMENT_ROWS_REFERENCE_FIELDS[:-1]}
    ):
        raise BatchArtifactError("measurement_rows reference digest differs")
    result = object.__new__(ValidatedMeasurementRowsReference)
    for name in ValidatedMeasurementRowsReference.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _identity_digest(identity: Mapping[str, object], fields: tuple[str, ...]) -> str:
    value = {field: identity[field] for field in fields}
    if fields == _SCIENTIFIC_IDENTITY_FIELDS and "segmentation_correction_ref_sha256" in identity:
        value["segmentation_correction_ref_sha256"] = identity["segmentation_correction_ref_sha256"]
    return _digest(value)


def measurements_result_identity_contract() -> ArtifactIdentityContract:
    """Return the one current owner contract for ``measurements_result``."""

    return ArtifactIdentityContract(
        contract_id=_CONTRACT_ID,
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=MEASUREMENTS_RESULT_IDENTITY_FIELDS,
        optional_fields=("segmentation_correction_ref_sha256",),
        required_runtime_names=("mif_pipeline", "qupath"),
    )




def _validated_evidence_sources(
    evidence: ProjectStageTargetExecutionEvidence,
) -> tuple[
    BatchNodeExecutionContext,
    ProjectStageAuthorizedEntry,
    ProjectStageTargetObservation,
    object,
]:
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError(
            "measurements result composition requires issuer-owned execution evidence"
        )
    try:
        validation_inputs = evidence.reopen_validation_inputs()
    except ProjectStageRecordError as exc:
        raise BatchArtifactError(
            "measurements execution evidence cannot reopen validation inputs"
        ) from exc
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    if (
        type(context) is not BatchNodeExecutionContext
        or type(authorized) is not ProjectStageAuthorizedEntry
        or type(observation) is not ProjectStageTargetObservation
        or context.authorization.attempt_id != authorized.attempt_id
        or context.authorization.authorization_sha256 != authorized.attempt_authorization_sha256
        or context.authorization.input_artifact_set_sha256 != authorized.input_artifact_set_sha256
        or artifact_ref_set_sha256(context.inputs) != authorized.input_artifact_set_sha256
        or context.node_id != authorized.node_id
        or context.adapter_spec.spec_sha256 != authorized.adapter_spec_sha256
        or context.adapter_spec.node_kind != _NODE_KIND
        or tuple(context.adapter_spec.declared_outputs) != (_ARTIFACT_KIND,)
        or context.adapter_spec.output_identity_contracts
        != (measurements_result_identity_contract(),)
        or authorized.wire_stage_id != _NODE_KIND
        or authorized.terminal_stage_id != _NODE_KIND
        or authorized.image_role != "primary_mif"
        or authorized.request_codec_id != QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value
        or authorized.request_codec_version != "2"
        or observation.node_id != authorized.node_id
        or observation.attempt_id != authorized.attempt_id
        or observation.attempt_authorization_sha256 != authorized.attempt_authorization_sha256
        or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
        or observation.project_entry_id != authorized.project_entry_id
        or observation.sample_id != authorized.sample_id
        or observation.slide_id != authorized.slide_id
        or observation.saved_state_before_sha256 != authorized.saved_state_before_sha256
        or observation.saved_state_disposition == "unattestable"
        or observation.saved_state_after_sha256 is None
        or evidence.execution_exchange_sha256 != authorized.exchange.exchange_sha256
        or observation.result_ref != authorized.exchange.result_ref
        or observation.generation_manifest_ref != authorized.exchange.generation_manifest_ref
    ):
        raise BatchArtifactError("measurements execution evidence sources disagree")
    # The immutable parsed result retains its original source-member identity.
    # The current input and scientific scope checks above establish this use.
    return context, authorized, observation, validation_inputs


def _measurement_context(
    *,
    segmentation: ValidatedSegmentationResultProjection,
    canonical_channels: tuple[Mapping[str, object], ...],
    feature_requests: tuple[object, ...],
    current_rows: ValidatedMeasurementRowStream,
) -> MeasurementResultValidationContext:
    try:
        return MeasurementResultValidationContext(
            section_id=segmentation.section_id,
            segmentation_population_id=segmentation.segmentation_population_id,
            # The row owner has checked the original base commitments and the
            # exact remaining IDs after this result's saved explicit deletions.
            segmentation_cell_count=current_rows.cell_count,
            segmentation_cell_ids_sha256=current_rows.cell_ids_sha256,
            segmentation_domain_cell_mapping_sha256=current_rows.cell_to_domain_membership_sha256,
            analysis_domain_bindings=tuple(
                {
                    "analysis_domain_id": item.analysis_domain_id,
                    "analysis_domain_revision_sha256": (item.analysis_domain_revision_sha256),
                }
                for item in segmentation.analysis_domain_bindings
            ),
            canonical_channels=canonical_channels,
            feature_requests=feature_requests,
        )
    except ROIAnalysisError as exc:
        raise BatchArtifactError("measurements scientific validation context is invalid") from exc


def validate_current_measurement_population(*, context, current, segmentation, target) -> None:
    """Join current project authorization to the exact accepted base population.

    The project-stage owner authenticates the latest saved-state transition,
    including later measurements, labels and accepted editor changes. Its
    target need not equal the historical post-segmentation image checksum.
    The native reader and retained row reader separately verify this accepted
    population's immutable dataset digest before using its cells.
    """
    input_digests = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    saved = target.saved_state_before_sha256
    if (
        type(segmentation) is not ValidatedSegmentationResultProjection
        or current.node_kind != "measurements"
        or current.node_scope != "sample"
        or current.state_head_sha256 != segmentation.state_head_sha256
        or current.active_plan_sha256 != segmentation.active_plan_sha256
        or current.validation_domain_id != segmentation.artifact_ref.scope.validation_domain_id
        or current.participant_ids != (segmentation.sample_id,)
        or current.project_entry_ids != (segmentation.project_entry_id,)
        or current.analysis_domain_bindings != segmentation.analysis_domain_bindings
        or current.configuration_bindings != segmentation.artifact_ref.configuration_bindings
        or current.input_artifact_ref_sha256s != input_digests
        or target.prerequisite_artifact_ref_sha256s != input_digests
        or target.sample_id != segmentation.sample_id
        or target.slide_id != segmentation.slide_id
        or target.project_entry_id != segmentation.project_entry_id
        or tuple(target.analysis_domain_bindings) != segmentation.analysis_domain_bindings
        or context.current_image_identity_sha256
        != segmentation.artifact_ref.owning_identity["current_image_identity_sha256"]
        or type(saved) is not str or re.fullmatch(r"[0-9a-f]{64}", saved) is None
    ):
        raise BatchArtifactError("measurements differ from the current authorized project population")


def build_measurements_result_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    segmentation_authority: BatchCommittedArtifactAuthority,
    scientific_result: ValidatedMeasurementResult | None = None,
    *,
    current_execution_authority: BatchCurrentExecutionAuthority,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    correction_authority: BatchCommittedArtifactAuthority | None = None,
) -> BatchNodeExecutionResult:
    """Compose one compact result from existing authenticated owners."""

    context, authorized, observation, validation_inputs = _validated_evidence_sources(evidence)
    if type(segmentation_authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("measurements require exact committed segmentation authority")
    if type(analysis_frame_authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("measurements require exact committed analysis-frame authority")
    try:
        current = validate_current_execution_authority(
            current_execution_authority,
            expected_context=context,
        )
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError("measurements require exact current execution authority") from exc
    if scientific_result is not None and type(scientific_result) is not ValidatedMeasurementResult:
        raise BatchArtifactError("measurements require the workflow owner's validated result")
    try:
        segmentation = decode_committed_segmentation_result(segmentation_authority)
    except BatchArtifactError as exc:
        raise BatchArtifactError("measurements segmentation source authority is invalid") from exc
    segmentation_refs = tuple(
        item for item in context.inputs if item.artifact_kind == "segmentation_result"
    )
    frame_refs = tuple(item for item in context.inputs if item.artifact_kind == "analysis_frame")
    correction_refs = tuple(item for item in context.inputs if item.artifact_kind == "segmentation_correction_result")
    correction = None
    if correction_refs:
        from .batch_cell_corrections import decode_committed_cell_correction
        if len(correction_refs) != 1 or correction_authority is None or correction_authority.artifact_ref != correction_refs[0]:
            raise BatchArtifactError("Measurements require their exact selected correction authority")
        correction = decode_committed_cell_correction(correction_authority, segmentation.artifact_ref)
    elif correction_authority is not None:
        raise BatchArtifactError("Measurements have an unselected cell correction")
    try:
        validate_committed_artifact_authority_set(
            (segmentation_authority, analysis_frame_authority, *((correction_authority,) if correction_authority is not None else ())),
            expected_active_plan_sha256=current.active_plan_sha256,
        )
    except BatchArtifactError as exc:
        raise BatchArtifactError("measurements input authority set is invalid") from exc
    validate_current_measurement_population(context=context, current=current,
        segmentation=segmentation, target=authorized)
    input_ref_sha256s = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    frame_ref = analysis_frame_authority.artifact_ref
    if (
        len(context.inputs) != 2 + len(correction_refs)
        or len(segmentation_refs) != 1
        or len(frame_refs) != 1
        or segmentation_refs[0] != segmentation.artifact_ref
        or frame_refs[0] != frame_ref
        or analysis_frame_authority.producer_node_kind != "analysis_preflight"
        or analysis_frame_authority.state_head_sha256 != current.state_head_sha256
        or segmentation_authority.state_head_sha256 != current.state_head_sha256
        or segmentation_authority.active_plan_sha256 != current.active_plan_sha256
    ):
        raise BatchArtifactError("measurements require the exact current segmentation prerequisite")
    if segmentation.artifact_ref.configuration_bindings != (
        ArtifactConfigurationBinding(authorized.sample_id, validation_inputs.resolved_config_sha256),
    ):
        raise BatchArtifactError("measurements target configuration differs from segmentation")

    request_material = _plain(validation_inputs.scientific_request_body)
    request = _closed_mapping(
        request_material,
        _REQUEST_FIELDS | (set(request_material) & {"phenotyping_population_sources", "measurement_reuse", "cell_correction"}),
        "measurements scientific request",
    )
    if (
        request["schema_version"] != "roi-workflow-invocation/v1"
        or request["stage"] != _NODE_KIND
        or request["sample_id"] != authorized.sample_id
        or request["slide_id"] != authorized.slide_id
        or request["run_id"] != authorized.pipeline_run_id
        or request["canonical_image_id"] != segmentation.canonical_image_id
        or request["section_id"] != segmentation.section_id
        or request["segmentation_population_id"] != segmentation.segmentation_population_id
        or _plain(request["segmentation_population_ids"])
        != {segmentation.section_id: segmentation.segmentation_population_id}
        or tuple(request["generation_ids"]) != (segmentation.tissue_generation_id,)
        or request["generation_id"] != segmentation.tissue_generation_id
        or any(
            request[name] is not None
            for name in (
                "analysis_domain_id",
                "analysis_domain_revision",
                "analysis_domain_revision_sha256",
                "analysis_domain_path_object_id",
                "structure_id",
                "eligible_parent_id",
                "action",
            )
        )
        or tuple(request["eligible_parent_ids"]) != ()
    ):
        raise BatchArtifactError("authenticated measurements request scope differs")
    expected_correction = (None if correction is None else dict(
        artifact_ref_sha256=correction_refs[0].artifact_ref_sha256,
        cell_edits=dict(correction["cell_edits"]), geometry_revision=correction["geometry_revision"]))
    if request.get("cell_correction") != expected_correction:
        raise BatchArtifactError("Measurements changed their selected cell checkpoint")
    feature_requests = _sequence(request["feature_requests"], "feature_requests")
    try:
        config = AnalysisConfig.from_mapping(_plain(validation_inputs.resolved_config))
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("resolved measurements configuration is invalid") from exc
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        channels = resolve_analysis_channels(
            config,
            frame_ref,
            sample_id=authorized.sample_id,
            validation_domain_id=segmentation.artifact_ref.scope.validation_domain_id,
        )
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError("measurements analysis-frame authority is invalid") from exc
    if (
        frame.spatial_frame.canonical_image_id != segmentation.canonical_image_id
        or frame.spatial_frame.canonical_image_sha256 != segmentation.canonical_image_sha256
        or validation_inputs.resolved_config_sha256 != _digest(config.to_mapping())
    ):
        raise BatchArtifactError("measurements processed frame or original configuration differs")
    ordered_channels = tuple(item.to_mapping() for item in channels)
    canonical_channels = tuple(sorted(ordered_channels, key=lambda item: item["channel_id"]))
    if (
        _plain(request["canonical_channels"]) != list(ordered_channels)
        or request["segmentation_model_version"] != segmentation.segmentation_model_version
        or request["segmentation_cell_dataset_sha256"] != segmentation.cell_evidence_sha256
    ):
        raise BatchArtifactError(
            "authenticated measurements processed channel or model settings differ"
        )
    result = evidence.wire_stage_result
    if result.status is not StageStatus.COMPLETED:
        if result.stage != _NODE_KIND or result.slide_id != authorized.slide_id:
            raise BatchArtifactError("non-completed measurements result has foreign scope")
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())

    manifest = _closed_mapping(
        evidence.generation_manifest,
        _GENERATION_MANIFEST_FIELDS,
        "measurements generation manifest",
    )
    if (
        result.stage != _NODE_KIND
        or result.slide_id != authorized.slide_id
        or result.outputs is None
        or manifest["schema_version"] != _GENERATION_MANIFEST_SCHEMA
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != _NODE_KIND
        or manifest["slide_id"] != authorized.slide_id
        or tuple(_sequence(manifest["generations"], "manifest.generations")) != ()
        or manifest["object_outputs"] != result.outputs
    ):
        raise BatchArtifactError("measurements result and generation manifest authority differ")
    generation_ids = tuple(
        _text(item, "generation_ids item")
        for item in _sequence(manifest["generation_ids"], "generation_ids")
    )
    if generation_ids != (segmentation.tissue_generation_id,) or generation_ids != (
        result.generation_id,
    ):
        raise BatchArtifactError("measurements require one exact generation")
    raw_scientific_output = result.outputs
    if not isinstance(raw_scientific_output, Mapping):
        raise BatchArtifactError("measurements scientific output is not an object")
    try:
        descriptor = MarkerIntensityRowsDescriptor.from_mapping(
            raw_scientific_output.get("measurement_rows")
        )
        expected_member_path = (
            Path(validation_inputs.operational_output_dir)
            / "batch"
            / "measurement-rows"
            / f"{descriptor.sha256}.mif-marker-intensity-dataset.json"
        )
        if descriptor.path != expected_member_path:
            raise BatchArtifactError(
                "measurement rows descriptor is not the canonical content-addressed member"
            )
        marker_requests: list[Mapping[str, object]] = []
        for item in feature_requests:
            if not isinstance(item, Mapping):
                raise BatchArtifactError("measurement feature request is not an object")
            try:
                kind = FeatureKind(item.get("kind"))
            except (TypeError, ValueError) as exc:
                raise BatchArtifactError(
                    "measurement feature kind is ambiguous or unsupported"
                ) from exc
            if kind is FeatureKind.EXPRESSION:
                marker_requests.append(item)
        expected_rows = MeasurementRowStreamExpectation(
            project_entry_id=authorized.project_entry_id,
            slide_id=authorized.slide_id,
            section_id=segmentation.section_id,
            # The accepted native producer binds both row-generation header
            # fields to the authenticated measurement request generation.
            segmentation_generation_id=generation_ids[0],
            measurement_generation_id=generation_ids[0],
            segmentation_population_id=segmentation.segmentation_population_id,
            cell_dataset_sha256=segmentation.cell_evidence_sha256,
            analysis_domain_bindings=tuple(
                {
                    "analysis_domain_id": item.analysis_domain_id,
                    "analysis_domain_revision_sha256": (item.analysis_domain_revision_sha256),
                }
                for item in segmentation.analysis_domain_bindings
            ),
            feature_ids=tuple(item["feature_id"] for item in marker_requests),
            measurement_names=tuple(item["measurement_name"] for item in marker_requests),
        )
        row_stream = validate_measurement_row_stream(
            descriptor=descriptor,
            authenticated_output_root=Path(validation_inputs.operational_output_dir),
            expected=expected_rows,
            expected_base_population=(segmentation.cell_count,
                segmentation.cell_ids_sha256, segmentation.domain_cell_mapping_sha256),
        )
        scientific_context = _measurement_context(
            segmentation=segmentation,
            canonical_channels=ordered_channels,
            feature_requests=feature_requests,
            current_rows=row_stream,
        )
        scientific_output = dict(raw_scientific_output)
        independently_validated = validate_measurement_result(
            scientific_output, scientific_context, row_stream
        )
    except ROIAnalysisError as exc:
        raise BatchArtifactError("measurement scientific owner validation failed") from exc
    if scientific_result is not None and independently_validated != scientific_result:
        raise BatchArtifactError("caller measurement projection differs from scientific owner")
    # The caller projection is only a cross-boundary consistency check.  All
    # retained owner material below comes from this wrapper's independent
    # reopen and validation, never from caller-carried fields.
    scientific_result = independently_validated

    marker_plan = _marker_plan(scientific_result.measurement_plan)
    marker_feature_schema_sha256 = _feature_schema_sha256(marker_plan)
    measurement_rows = _issue_measurement_rows_reference(
        row_stream,
        feature_schema_sha256=marker_feature_schema_sha256,
    )
    plan_mapping = scientific_result.measurement_plan.to_mapping()
    inventory = tuple(
        _plain(item)
        for item in _sequence(scientific_output["measurement_inventory"], "measurement_inventory")
    )
    commitments = tuple(_plain(item) for item in scientific_result.domain_cell_commitments)
    identity: dict[str, object] = {
        "sample_id": authorized.sample_id,
        "slide_id": authorized.slide_id,
        "image_role": authorized.image_role,
        "project_entry_id": authorized.project_entry_id,
        "canonical_image_id": segmentation.canonical_image_id,
        "canonical_image_sha256": segmentation.canonical_image_sha256,
        "current_image_identity_sha256": context.current_image_identity_sha256,
        "section_id": segmentation.section_id,
        "tissue_generation_id": segmentation.tissue_generation_id,
        "tissue_path_object_id": segmentation.tissue_path_object_id,
        "tissue_roi_id": segmentation.tissue_roi_id,
        "analysis_domain_binding_set_sha256": _binding_set_sha256(
            segmentation.analysis_domain_bindings
        ),
        "domain_set_sha256": segmentation.domain_set_sha256,
        "segmentation_result_ref_sha256": segmentation.artifact_ref.artifact_ref_sha256,
        "analysis_frame_ref_sha256": frame_ref.artifact_ref_sha256,
        "segmentation_state_head_sha256": segmentation.state_head_sha256,
        "active_plan_sha256": segmentation.active_plan_sha256,
        "segmentation_terminal_record_sha256": segmentation.terminal_record_sha256,
        "segmentation_execution_exchange_sha256": (segmentation.execution_exchange_sha256),
        "segmentation_generation_id": segmentation.generation_ids[0],
        "segmentation_population_id": segmentation.segmentation_population_id,
        "segmentation_population_sha256": segmentation.section_population_sha256,
        "segmentation_config_sha256": segmentation.segmentation_config_sha256,
        "segmentation_domain_sha256": segmentation.segmentation_domain_sha256,
        "cell_count": scientific_result.cell_count,
        "cell_ids_sha256": scientific_result.cell_ids_sha256,
        "cell_to_domain_membership_sha256": (scientific_result.domain_cell_mapping_sha256),
        "cell_evidence_sha256": segmentation.cell_evidence_sha256,
        "domain_cell_commitments": list(commitments),
        "domain_cell_commitments_sha256": (scientific_result.domain_cell_commitments_sha256),
        "canonical_channels": list(canonical_channels),
        "canonical_channels_sha256": _digest(list(canonical_channels)),
        "measurement_generation_id": generation_ids[0],
        "generation_ids": list(generation_ids),
        "measurement_plan": plan_mapping,
        "measurement_plan_sha256": _digest(plan_mapping),
        "selected_measurement_names": list(scientific_result.selected_measurement_names),
        "feature_request_set_sha256": _digest(list(feature_requests)),
        "measurement_inventory": list(inventory),
        "measurement_inventory_sha256": (scientific_result.measurement_inventory_sha256),
        "observed_measurements_sha256": (scientific_result.observed_measurements_sha256),
        "measurement_keys_sha256": scientific_result.measurement_keys_sha256,
        "feature_schema_sha256": _feature_schema_sha256(scientific_result.measurement_plan),
        "measurement_rows": _measurement_rows_reference_mapping(measurement_rows),
        "object_outputs_sha256": scientific_result.object_outputs_sha256,
        "node_id": authorized.node_id,
        "attempt_id": authorized.attempt_id,
        "attempt_authorization_sha256": authorized.attempt_authorization_sha256,
        "pipeline_run_id": authorized.pipeline_run_id,
        "input_artifact_set_sha256": authorized.input_artifact_set_sha256,
        "prerequisite_artifact_ref_sha256s": list(authorized.prerequisite_artifact_ref_sha256s),
        "target_request_sha256": authorized.target_request_sha256,
        "authorized_entry_sha256": authorized.authorized_entry_sha256,
        "generation_manifest_payload_sha256": (observation.generation_manifest_payload_sha256),
        "generation_manifest_byte_count": (observation.generation_manifest_byte_count),
        "result_payload_sha256": observation.result_payload_sha256,
        "result_byte_count": observation.result_byte_count,
        "process_evidence_sha256": observation.process_evidence_sha256,
        "project_snapshot_sha256": observation.project_snapshot_sha256,
        "saved_state_disposition": observation.saved_state_disposition,
        "saved_state_before_sha256": authorized.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "scientific_request_body_sha256": (validation_inputs.scientific_request_body_sha256),
        "scientific_request_body_byte_count": (
            validation_inputs.scientific_request_body_byte_count
        ),
        "resolved_config_sha256": validation_inputs.resolved_config_sha256,
        "request_capability_sha256": validation_inputs.request_capability_sha256,
        "validation_input_authority_sha256": validation_inputs.authority_sha256,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "projection_target_sha256": context.projection_target_sha256,
        "script_sha256": context.script_sha256,
        "project_identity_sha256": context.project_identity_sha256,
        "project_authority_head_sha256": context.project_authority_head_sha256,
        "project_qpproj_snapshot_sha256": context.project_qpproj_snapshot_sha256,
        "project_entry_projection_sha256": context.project_entry_projection_sha256,
        "compatibility_signature_sha256": context.compatibility_signature_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }
    if correction_refs:
        identity["segmentation_correction_ref_sha256"] = correction_refs[0].artifact_ref_sha256
    identity["measurement_scientific_identity_sha256"] = _identity_digest(
        identity, _SCIENTIFIC_IDENTITY_FIELDS
    )
    identity["task10_execution_identity_sha256"] = _identity_digest(
        identity, _TASK10_IDENTITY_FIELDS
    )
    contract = measurements_result_identity_contract()
    artifact = BatchArtifactRef(
        artifact_id="measurements-" + scientific_result.object_outputs_sha256[:48],
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authorized.node_id,
        producer_attempt_id=authorized.attempt_id,
        producer_attempt_authorization_sha256=(authorized.attempt_authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=segmentation.artifact_ref.scope,
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": segmentation.artifact_ref.runtime_versions["qupath"],
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=segmentation.artifact_ref.configuration_bindings,
    )
    return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, (artifact,))


def _parse_inventory(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    rows = tuple(
        _closed_mapping(item, frozenset(_FEATURE_FIELDS), f"{field} row")
        for item in _sequence(value, field)
    )
    if not rows:
        raise BatchArtifactError(f"{field} must be nonempty")
    for index, item in enumerate(rows):
        for name in (
            "feature_id",
            "measurement_name",
            "kind",
            "producer",
            "producer_version",
        ):
            _text(item[name], f"{field}[{index}].{name}")
        for name in ("channel_id", "biomarker", "compartment", "units"):
            _optional_text(item[name], f"{field}[{index}].{name}")
    return rows


def _parse_commitments(
    value: object,
    bindings: tuple[AnalysisDomainBinding, ...],
    cell_count: int,
) -> tuple[Mapping[str, object], ...]:
    rows = tuple(
        _closed_mapping(item, frozenset(_COMMITMENT_FIELDS), "domain commitment")
        for item in _sequence(value, "domain_cell_commitments")
    )
    ids = tuple(_sha256(item["analysis_domain_id"], "analysis_domain_id") for item in rows)
    expected_ids = tuple(item.analysis_domain_id for item in bindings)
    if ids != tuple(sorted(ids)) or ids != expected_ids or len(set(ids)) != len(ids):
        raise BatchArtifactError("domain cell commitments do not exactly cover the canonical scope")
    by_id = {item.analysis_domain_id: item for item in bindings}
    total = 0
    for item in rows:
        domain_id = item["analysis_domain_id"]
        if (
            _sha256(
                item["analysis_domain_revision_sha256"],
                "analysis_domain_revision_sha256",
            )
            != by_id[domain_id].analysis_domain_revision_sha256
        ):
            raise BatchArtifactError("domain commitment revision differs")
        total += _nonnegative_integer(item["cell_count"], "domain cell_count")
        _sha256(item["cell_ids_sha256"], "domain cell_ids_sha256")
    if total != cell_count:
        raise BatchArtifactError("domain commitment counts differ from cell_count")
    return rows


def validate_measurements_result_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedMeasurementsResultArtifactRefProjection:
    """Validate compact owner data without asserting batch-state availability.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if type(artifact_ref) is not BatchArtifactRef:
        raise BatchArtifactError("measurements result requires an exact artifact ref")
    ref = validate_artifact_ref(artifact_ref)
    contract = measurements_result_identity_contract()
    if (
        ref.artifact_kind != _ARTIFACT_KIND
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or set(ref.runtime_versions) != {"mif_pipeline", "qupath"}
        or ref.runtime_versions.get("mif_pipeline") != MIF_PIPELINE_BATCH_RUNTIME_VERSION
    ):
        raise BatchArtifactError("measurements result lacks its locatorless owner contract")
    _text(ref.runtime_versions.get("qupath"), "runtime_versions.qupath")
    identity = _closed_mapping(
        ref.owning_identity, _TOP_LEVEL_IDENTITY_FIELDS | (set(ref.owning_identity) & {"segmentation_correction_ref_sha256"}), "measurements identity"
    )
    sample_id = _text(identity["sample_id"], "sample_id")
    slide_id = _text(identity["slide_id"], "slide_id")
    project_entry_id = _text(identity["project_entry_id"], "project_entry_id")
    section_id = _text(identity["section_id"], "section_id")
    canonical_image_id = _text(identity["canonical_image_id"], "canonical_image_id")
    canonical_image_sha256 = _sha256(identity["canonical_image_sha256"], "canonical_image_sha256")
    tissue_generation_id = _text(identity["tissue_generation_id"], "tissue_generation_id")
    tissue_path_object_id = _text(identity["tissue_path_object_id"], "tissue_path_object_id")
    tissue_roi_id = _text(identity["tissue_roi_id"], "tissue_roi_id")
    bindings = ref.scope.analysis_domain_bindings
    cell_count = _positive_integer(identity["cell_count"], "cell_count")
    generation_ids = tuple(
        _text(item, "generation_ids item")
        for item in _sequence(identity["generation_ids"], "generation_ids")
    )
    measurement_generation_id = _text(
        identity["measurement_generation_id"], "measurement_generation_id"
    )
    selected_names = tuple(
        _text(item, "selected_measurement_names item")
        for item in _sequence(identity["selected_measurement_names"], "selected_measurement_names")
    )
    channels = tuple(
        _closed_mapping(item, frozenset(_CHANNEL_FIELDS), "canonical channel")
        for item in _sequence(identity["canonical_channels"], "canonical_channels")
    )
    channel_ids = tuple(_text(item["channel_id"], "channel_id") for item in channels)
    for index, channel in enumerate(channels):
        for field in ("name", "component", "fluorophore", "biomarker"):
            _optional_text(channel[field], f"canonical_channels[{index}].{field}")
    if channel_ids != tuple(sorted(channel_ids)) or len(set(channel_ids)) != len(channel_ids):
        raise BatchArtifactError("canonical channels must be sorted and unique")
    inventory = _parse_inventory(identity["measurement_inventory"], "measurement_inventory")
    try:
        plan = MeasurementPlan.from_mapping(identity["measurement_plan"], channels)
    except ROIAnalysisError as exc:
        raise BatchArtifactError("measurement plan is invalid") from exc
    commitments = _parse_commitments(identity["domain_cell_commitments"], bindings, cell_count)
    measurement_rows = _parse_measurement_rows_reference(identity["measurement_rows"])
    plan_mapping = plan.to_mapping()
    inventory_plain = [dict(item) for item in inventory]
    commitment_plain = [dict(item) for item in commitments]
    request_projection = [
        {
            field: item[field]
            for field in _FEATURE_FIELDS
            if field
            not in {
                "producer",
                "producer_version",
                "missing_rate",
                "non_finite_rate",
            }
        }
        for item in inventory
    ]
    observed_projection = sorted(
        (
            {
                "measurement_name": item["measurement_name"],
                "missing_rate": item["missing_rate"],
                "non_finite_rate": item["non_finite_rate"],
            }
            for item in inventory
        ),
        key=lambda item: item["measurement_name"],
    )

    for field in (
        "active_plan_sha256",
        "adapter_spec_sha256",
        "analysis_domain_binding_set_sha256",
        "attempt_authorization_sha256",
        "authorized_entry_sha256",
        "canonical_channels_sha256",
        "cell_evidence_sha256",
        "cell_ids_sha256",
        "cell_to_domain_membership_sha256",
        "compatibility_signature_sha256",
        "current_image_identity_sha256",
        "domain_cell_commitments_sha256",
        "domain_set_sha256",
        "execution_exchange_sha256",
        "feature_request_set_sha256",
        "feature_schema_sha256",
        "generation_manifest_payload_sha256",
        "input_artifact_set_sha256",
        "measurement_inventory_sha256",
        "measurement_keys_sha256",
        "measurement_plan_sha256",
        "measurement_scientific_identity_sha256",
        "object_outputs_sha256",
        "observed_measurements_sha256",
        "process_evidence_sha256",
        "project_authority_head_sha256",
        "project_entry_projection_sha256",
        "project_identity_sha256",
        "project_qpproj_snapshot_sha256",
        "project_snapshot_sha256",
        "projection_target_sha256",
        "request_capability_sha256",
        "resolved_config_sha256",
        "result_payload_sha256",
        "saved_state_after_sha256",
        "saved_state_before_sha256",
        "scientific_request_body_sha256",
        "script_sha256",
        "segmentation_config_sha256",
        "segmentation_domain_sha256",
        "segmentation_execution_exchange_sha256",
        "segmentation_population_sha256",
        "segmentation_result_ref_sha256",
        "segmentation_state_head_sha256",
        "segmentation_terminal_record_sha256",
        "target_request_sha256",
        "task10_execution_identity_sha256",
        "validated_exchange_sha256",
        "validation_input_authority_sha256",
    ):
        _sha256(identity[field], field)
    prerequisite_sha256s = tuple(
        _sha256(item, "prerequisite_artifact_ref_sha256s item")
        for item in _sequence(
            identity["prerequisite_artifact_ref_sha256s"],
            "prerequisite_artifact_ref_sha256s",
        )
    )
    correction_digests = (() if "segmentation_correction_ref_sha256" not in identity else
                          (_sha256(identity["segmentation_correction_ref_sha256"], "segmentation_correction_ref_sha256"),))
    resolved_config_sha256 = identity["resolved_config_sha256"]
    segmentation_population_id = _text(
        identity["segmentation_population_id"], "segmentation_population_id"
    )
    segmentation_generation_id = _text(
        identity["segmentation_generation_id"], "segmentation_generation_id"
    )
    for field in ("node_id", "attempt_id", "pipeline_run_id"):
        _text(identity[field], field)
    if (
        ref.artifact_id != "measurements-" + str(identity["object_outputs_sha256"])[:48]
        or ref.scope.validation_domain_id is None
        or ref.scope.sample_id != sample_id
        or ref.scope.slide_id != slide_id
        or ref.scope.project_entry_id != project_entry_id
        or not bindings
        or any(
            item.sample_id != sample_id
            or item.slide_id != slide_id
            or item.project_entry_id != project_entry_id
            or item.domain_set_sha256 != identity["domain_set_sha256"]
            for item in bindings
        )
        or _binding_set_sha256(bindings) != identity["analysis_domain_binding_set_sha256"]
        or identity["image_role"] != "primary_mif"
        or generation_ids != (measurement_generation_id,)
        or measurement_generation_id != tissue_generation_id
        or not segmentation_population_id.startswith("segmentation-population/v2/")
        or selected_names != plan.exact_measurement_names
        or len(set(selected_names)) != len(selected_names)
        or plan_mapping["features"] != inventory_plain
        or _digest(plan_mapping) != identity["measurement_plan_sha256"]
        # These three hashes belong to the workflow result and use its JSON codec.
        or _mapping_sha256(inventory_plain) != identity["measurement_inventory_sha256"]
        or _digest(request_projection) != identity["feature_request_set_sha256"]
        or _mapping_sha256(observed_projection) != identity["observed_measurements_sha256"]
        or _mapping_sha256(list(selected_names)) != identity["measurement_keys_sha256"]
        or _feature_schema_sha256(plan) != identity["feature_schema_sha256"]
        or measurement_rows.row_count != cell_count
        or measurement_rows.cell_ids_sha256 != identity["cell_ids_sha256"]
        or measurement_rows.cell_to_domain_membership_sha256
        != identity["cell_to_domain_membership_sha256"]
        or measurement_rows.domain_cell_commitments_sha256
        != identity["domain_cell_commitments_sha256"]
        or measurement_rows.feature_schema_sha256 != _feature_schema_sha256(_marker_plan(plan))
        or _digest(commitment_plain) != identity["domain_cell_commitments_sha256"]
        or _digest([dict(item) for item in channels]) != identity["canonical_channels_sha256"]
        or prerequisite_sha256s
        != tuple(
            sorted(
                (identity["segmentation_result_ref_sha256"], identity["analysis_frame_ref_sha256"], *correction_digests)
            )
        )
    ):
        raise BatchArtifactError("measurements result identity relationships differ")
    expected_input_set_sha256 = _digest({"artifact_ref_sha256s": list(prerequisite_sha256s)})
    if (
        identity["input_artifact_set_sha256"] != expected_input_set_sha256
        or identity["request_codec_id"] != QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value
        or identity["request_codec_version"] != "2"
        or identity["saved_state_disposition"]
        not in {"authenticated_unchanged", "authenticated_changed"}
        or (
            (identity["saved_state_before_sha256"] == identity["saved_state_after_sha256"])
            != (identity["saved_state_disposition"] == "authenticated_unchanged")
        )
        or _positive_integer(
            identity["generation_manifest_byte_count"],
            "generation_manifest_byte_count",
        )
        <= 0
        or _positive_integer(identity["result_byte_count"], "result_byte_count") <= 0
        or _positive_integer(
            identity["scientific_request_body_byte_count"],
            "scientific_request_body_byte_count",
        )
        <= 0
        or ref.producer_node_id != identity["node_id"]
        or ref.producer_attempt_id != identity["attempt_id"]
        or ref.producer_attempt_authorization_sha256 != identity["attempt_authorization_sha256"]
        or ref.compatibility_signature_sha256 != identity["compatibility_signature_sha256"]
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0]
        != ArtifactConfigurationBinding(sample_id, resolved_config_sha256)
        or _identity_digest(identity, _SCIENTIFIC_IDENTITY_FIELDS)
        != identity["measurement_scientific_identity_sha256"]
        or _identity_digest(identity, _TASK10_IDENTITY_FIELDS)
        != identity["task10_execution_identity_sha256"]
    ):
        raise BatchArtifactError("measurements result compact evidence differs")
    return _artifact_ref_projection(
        artifact_ref=ref,
        sample_id=sample_id,
        slide_id=slide_id,
        project_entry_id=project_entry_id,
        canonical_image_id=canonical_image_id,
        canonical_image_sha256=canonical_image_sha256,
        section_id=section_id,
        tissue_generation_id=tissue_generation_id,
        tissue_path_object_id=tissue_path_object_id,
        tissue_roi_id=tissue_roi_id,
        analysis_domain_bindings=bindings,
        segmentation_generation_id=segmentation_generation_id,
        segmentation_population_id=segmentation_population_id,
        segmentation_population_sha256=identity["segmentation_population_sha256"],
        measurement_generation_id=measurement_generation_id,
        generation_ids=generation_ids,
        measurement_plan=plan,
        canonical_channels=tuple(dict(item) for item in channels),
        selected_measurement_names=selected_names,
        measurement_inventory=inventory,
        measurement_plan_sha256=identity["measurement_plan_sha256"],
        measurement_inventory_sha256=identity["measurement_inventory_sha256"],
        feature_schema_sha256=identity["feature_schema_sha256"],
        cell_count=cell_count,
        cell_ids_sha256=identity["cell_ids_sha256"],
        cell_to_domain_membership_sha256=identity["cell_to_domain_membership_sha256"],
        domain_cell_commitments=commitments,
        domain_cell_commitments_sha256=identity["domain_cell_commitments_sha256"],
        measurement_rows=measurement_rows,
        saved_state_before_sha256=identity["saved_state_before_sha256"],
        saved_state_after_sha256=identity["saved_state_after_sha256"],
        execution_exchange_sha256=identity["execution_exchange_sha256"],
        validated_exchange_sha256=identity["validated_exchange_sha256"],
    )


def decode_committed_measurements_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedMeasurementsResultProjection:
    """Decode one current measurement result from exact committed authority."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("measurements result decoding requires committed state authority")
    validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    if authority.producer_node_kind != _NODE_KIND:
        raise BatchArtifactError("measurements result producer kind is invalid")
    ref_projection = validate_measurements_result_artifact_ref(authority.artifact_ref)
    if authority.execution_exchange_sha256 != ref_projection.execution_exchange_sha256:
        raise BatchArtifactError("committed attempt exchange differs from measurements identity")
    values = {
        name: getattr(ref_projection, name)
        for name in ValidatedMeasurementsResultArtifactRefProjection.__dataclass_fields__
    }
    values.update(
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
    )
    return _projection(**values)


__all__ = [
    "MEASUREMENTS_RESULT_IDENTITY_FIELDS",
    "ValidatedMeasurementRowsReference",
    "ValidatedMeasurementsResultArtifactRefProjection",
    "ValidatedMeasurementsResultProjection",
    "build_measurements_result_execution_result",
    "decode_committed_measurements_result",
    "measurements_result_identity_contract",
    "validate_measurements_result_artifact_ref",
]
