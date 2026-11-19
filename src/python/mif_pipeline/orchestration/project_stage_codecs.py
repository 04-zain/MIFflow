"""Closed request-codec ownership for authenticated project-stage execution.

Scientific stage owners project their existing single-image request mapping into one codec.
This module binds that mapping to a scheduler attempt without granting the mapping any
retained-storage or native-path authority.

The `ProjectStageRunIntent` record carries `authority_layout_sha256`, `batch_id`,
`run_plan_id`, `stage_id`, `stage_version` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.stage_result import StageResult
from mif_pipeline.qupath_runtime.exchange import (
    ExchangeContractError,
    validate_canonical_image_uri,
)
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from .batch_artifacts import (
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
)
from .batch_manifest import ResolvedBatchSample
from .batch_state import BatchWorkspaceLease
from .project_stage_plan import (
    ProjectStageCommittedExchangeReopener,
    ProjectStageLaunchMode,
    ProjectStageRecordError,
    ProjectStageRunRequest,
    ProjectStageTargetRequest,
    _asset,
    _digest,
    _domains,
    _project_uri,
    _project_stage_operation_output_dir,
    _safe_id,
    _sha,
    _sorted_unique_sha,
    _sorted_unique_text,
    _text,
)

_MAX_JSON_DEPTH = 32
_MAX_JSON_TEXT_BYTES = 4 * 1024 * 1024
_MAX_EXCHANGE_BYTES = 16 * 1024 * 1024
# Each JSON value or container requires at least one encoded byte.
_MAX_JSON_ITEMS = _MAX_EXCHANGE_BYTES
_CODEC_ISSUER = object()
_REGISTRY_ISSUER = object()
_STAGE_OWNER_CODEC_ISSUER = object()
_RESERVED_BODY_FIELDS = frozenset(
    {
        "project_file",
        "project_entry_id",
        "canonical_image_uri",
        "canonical_image_sha256",
        "script_bundle_sha256",
        "script_asset_path",
        "script_asset_sha256",
        "exchange_version",
        "resolved_config",
        "config_hash",
        "config_path",
        "operational_output_dir",
        "producer_output_dirs",
    }
)
_ASSET_WIRE_STAGE = MappingProxyType(
    {
        QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value: "tissue_setup",
        QuPathScriptAsset.DETECT_TISSUE_SECTIONS.value: "tissue_detection",
        QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value: "analysis_domain_selection",
        QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value: "training_evidence_snapshot",
        QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER.value: "primary_classifier",
        QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS.value: "secondary_classifier",
        QuPathScriptAsset.RETIRE_SAVED_RESULTS.value: "saved_result_retirement",
        QuPathScriptAsset.CORRECT_KNOWN_CELL.value: "segmentation_correction",
        QuPathScriptAsset.RUN_INSTANSEG.value: "segmentation",
        QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value: "measurements",
        QuPathScriptAsset.SNAPSHOT_SUPERVISED_TRAINING_EVIDENCE.value: (
            "supervised_training_evidence_snapshot"
        ),
        QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value: "phenotyping",
        QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES.value: "supervised_phenotyping",
        QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value: "exports",
        QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value: "cell_labels_import",
        QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value: "semantic_validation",
        QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value: "semantic_validation",
    }
)
# The export script has two independent owners: the domain-scoped semantic raw
# snapshot and the final global hierarchy snapshot used by phenotype handoff.
_APPLICATION_CODEC_MULTIPLICITY_BY_ASSET = MappingProxyType(
    {
        asset_id: (
            2
            if asset_id
            in {
                QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value,
                QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value,
            }
            else 1
        )
        for asset_id in _ASSET_WIRE_STAGE
    }
)
_APPLICATION_CODEC_BINDING_IDENTITIES = frozenset(
    {
        ("saved_result_retirement", QuPathScriptAsset.RETIRE_SAVED_RESULTS.value,
         "saved_result_retirement", "saved_result_retirement"),
        ("segmentation_correction", QuPathScriptAsset.CORRECT_KNOWN_CELL.value,
         "segmentation_correction", "segmentation_correction"),
        (
            "tissue_setup",
            QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value,
            "tissue_setup",
            "tissue_setup",
        ),
        (
            "tissue_candidate",
            QuPathScriptAsset.DETECT_TISSUE_SECTIONS.value,
            "tissue_detection",
            "tissue_candidate",
        ),
        (
            "analysis_domain_selection",
            QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value,
            "analysis_domain_selection",
            "analysis_domain_selection",
        ),
        (
            "primary_training_evidence_snapshot",
            QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value,
            "training_evidence_snapshot",
            "primary_training_evidence_snapshot",
        ),
        (
            "secondary_training_evidence_snapshot",
            QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value,
            "training_evidence_snapshot",
            "secondary_training_evidence_snapshot",
        ),
        (
            "primary_classifier_inference",
            QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER.value,
            "primary_classifier",
            "primary_classifier_inference",
        ),
        (
            "secondary_classifier_inference",
            QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS.value,
            "secondary_classifier",
            "secondary_classifier_inference",
        ),
        ("segmentation", QuPathScriptAsset.RUN_INSTANSEG.value, "segmentation", "segmentation"),
        (
            "measurements",
            QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value,
            "measurements",
            "measurements",
        ),
        (
            "supervised_training_evidence_snapshot",
            QuPathScriptAsset.SNAPSHOT_SUPERVISED_TRAINING_EVIDENCE.value,
            "supervised_training_evidence_snapshot",
            "supervised_training_evidence_snapshot",
        ),
        (
            "marker_rule_application",
            QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value,
            "phenotyping",
            "marker_rule_application",
        ),
        (
            "supervised_phenotype_inference",
            QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES.value,
            "supervised_phenotyping",
            "supervised_phenotype_inference",
        ),
        (
            "semantic_raw_inventory_snapshot",
            QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value,
            "exports",
            "semantic_raw_inventory_snapshot",
        ),
        (
            "clustering_label_import",
            QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value,
            "cell_labels_import",
            "clustering_label_import",
        ),
        (
            "semantic_evidence",
            QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value,
            "semantic_validation",
            "semantic_evidence",
        ),
        (
            "semantic_finalization",
            QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value,
            "semantic_validation",
            "semantic_finalization",
        ),
        (
            "phenotype_handoff",
            QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value,
            "exports",
            "phenotype_handoff",
        ),
    }
)
_CONFIG_CONSUMING_ASSETS = frozenset(
    {
        QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER.value,
        QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS.value,
        QuPathScriptAsset.RUN_INSTANSEG.value,
        QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value,
        QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value,
        QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value,
        QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value,
    }
)
_ASSET_OWNER_REQUEST_FIELDS = MappingProxyType(
    {
        QuPathScriptAsset.CORRECT_KNOWN_CELL.value: frozenset(
            {"schema_version", "stage", "run_id", "slide_id", "sample_id", "correction_request", "scope", "outline"}
        ),
        QuPathScriptAsset.RETIRE_SAVED_RESULTS.value: frozenset(
            {"schema_version", "stage", "run_id", "slide_id", "sample_id", "retirement", "source_ref_sha256s"}
        ),
        QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value: frozenset(
            {"tissue_setup", "tissue_parameters"}
        ),
        QuPathScriptAsset.DETECT_TISSUE_SECTIONS.value: frozenset(
            {
                "tissue_resolution_selection",
                "native_pyramid_inventory",
                "native_level_read_attestation",
                "config",
                "detector_settings_hash",
                "tissue_setup_result_ref_sha256",
                "tissue_setup_result_sha256",
                "detector_validation",
                "generation_history",
                "accepted_generation_id",
                "max_display_number",
                "review_required",
                "new_native_read",
            }
        ),
        QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value: frozenset({"accepted_tissue_sections"}),
        QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value: frozenset(
            {"pixel_training_evidence_requests"}
        ),
        QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER.value: frozenset(
            {"classifier_application", "primary_classifier_output_classes"}
        ),
        QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS.value: frozenset(
            {
                "parent_sources",
                "predecessor_result_ref_sha256",
                "primary_domain_status",
                "classifier_application",
                "structure_id",
            }
        ),
        QuPathScriptAsset.RUN_INSTANSEG.value: frozenset({"analysis_domain_manifests"}),
        QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value: frozenset({"feature_requests"}),
        QuPathScriptAsset.SNAPSHOT_SUPERVISED_TRAINING_EVIDENCE.value: frozenset(
            {"supervised_training_evidence_requests"}
        ),
        QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value: frozenset(
            {"phenotype_assignment_source"}
        ),
        QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES.value: frozenset({"feature_schema"}),
        QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value: frozenset(
            {"selected_measurement_names"}
        ),
        QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value: frozenset({"selected_display_view"}),
        QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value: frozenset(
            {"raw_export_sha256"}
        ),
        QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value: frozenset({"semantic_finalization"}),
    }
)


# ==============================================================================
#                              Project stage codecs
# ==============================================================================


def _asset_consumes_resolved_config(target_script_asset_id: str) -> bool:
    return target_script_asset_id in _CONFIG_CONSUMING_ASSETS


class ProjectStageCodecError(ProjectStageRecordError):
    """A project-stage request codec or its issued authority is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectStageCodecValidationResult:
    """An owner-validated result with an authenticated terminal override."""

    execution_result: BatchNodeExecutionResult
    terminal_stage_result: StageResult

    def __post_init__(self) -> None:
        if (
            type(self.execution_result) is not BatchNodeExecutionResult
            or type(self.terminal_stage_result) is not StageResult
        ):
            raise ProjectStageCodecError(
                "project-stage validation result requires exact result values"
            )


JsonValue = None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_export_json(item) for item in value]
    return value


def _freeze_json(
    value: object,
    *,
    field: str,
    depth: int = 0,
    budget: list[int] | None = None,
) -> JsonValue:
    if depth > _MAX_JSON_DEPTH:
        raise ProjectStageCodecError(f"{field} exceeds the JSON nesting bound")
    remaining = [_MAX_JSON_ITEMS] if budget is None else budget
    remaining[0] -= 1
    if remaining[0] < 0:
        raise ProjectStageCodecError(f"{field} exceeds the JSON item bound")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > (1 << 63) - 1:
            raise ProjectStageCodecError(f"{field} contains an out-of-range integer")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ProjectStageCodecError(f"{field} contains a non-finite number")
        return value
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ProjectStageCodecError(f"{field} contains invalid text") from exc
        if len(encoded) > _MAX_JSON_TEXT_BYTES:
            raise ProjectStageCodecError(f"{field} contains overlong text")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ProjectStageCodecError(f"{field} must have string keys")
        frozen: dict[str, JsonValue] = {}
        for key in sorted(value):
            frozen[key] = _freeze_json(
                value[key],
                field=f"{field}.{key}",
                depth=depth + 1,
                budget=remaining,
            )
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            _freeze_json(
                item,
                field=field,
                depth=depth + 1,
                budget=remaining,
            )
            for item in value
        )
    raise ProjectStageCodecError(f"{field} must contain only finite JSON values")


def _canonical_frozen_mapping(
    value: object, *, field: str
) -> tuple[Mapping[str, JsonValue], bytes]:
    frozen = _freeze_json(value, field=field)
    if not isinstance(frozen, Mapping) or not frozen:
        raise ProjectStageCodecError(f"{field} must be a non-empty JSON object")
    conflict = sorted(set(frozen) & _RESERVED_BODY_FIELDS)
    if conflict:
        raise ProjectStageCodecError(
            f"{field} contains runtime-reserved field(s): {', '.join(conflict)}"
        )
    try:
        payload = canonical_json_bytes(_export_json(frozen))
    except Exception as exc:
        raise ProjectStageCodecError(f"{field} is not canonical JSON") from exc
    if not payload or len(payload) > _MAX_EXCHANGE_BYTES:
        raise ProjectStageCodecError(f"{field} exceeds the exchange byte bound")
    return frozen, payload


def _canonical_config(sample: ResolvedBatchSample) -> tuple[Mapping[str, JsonValue], bytes]:
    if type(sample) is not ResolvedBatchSample:
        raise ProjectStageCodecError("target intent requires a resolved batch sample")
    frozen = _freeze_json(sample.config.to_mapping(), field="resolved_config")
    if not isinstance(frozen, Mapping):
        raise ProjectStageCodecError("resolved configuration must be one JSON object")
    payload = canonical_json_bytes(_export_json(frozen))
    if hashlib.sha256(payload).hexdigest() != sample.resolved_config_sha256:
        raise ProjectStageCodecError("resolved configuration digest differs")
    return frozen, payload


@dataclass(frozen=True, slots=True)
class ProjectStageCodecBinding:
    adapter_spec_sha256: str
    target_script_asset_id: str
    wire_stage_id: str
    terminal_stage_id: str
    request_codec_version: str
    codec_binding_sha256: str

    def __post_init__(self) -> None:
        _sha(self.adapter_spec_sha256, "adapter_spec_sha256")
        _asset(self.target_script_asset_id)
        _safe_id(self.wire_stage_id, "wire_stage_id")
        _safe_id(self.terminal_stage_id, "terminal_stage_id")
        _text(self.request_codec_version, "request_codec_version")
        expected_version = (
            "2"
            if self.wire_stage_id in {"segmentation", "primary_classifier", "measurements"}
            else "1"
        )
        if self.request_codec_version != expected_version:
            raise ProjectStageCodecError(
                f"request codec version must be exactly {expected_version}"
            )
        if _ASSET_WIRE_STAGE.get(self.target_script_asset_id) != self.wire_stage_id:
            raise ProjectStageCodecError(
                "codec wire stage differs from the registered asset mapping"
            )
        expected = _digest(
            {
                item.name: getattr(self, item.name)
                for item in fields(self)
                if item.name != "codec_binding_sha256"
            }
        )
        if _sha(self.codec_binding_sha256, "codec_binding_sha256") != expected:
            raise ProjectStageCodecError("codec binding digest differs")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageCodecBinding":
        material = dict(values)
        return cls(**material, codec_binding_sha256=_digest(material))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageTargetIntent:
    project_entry_id: str
    sample_id: str
    slide_id: str
    pipeline_run_id: str
    image_role: Literal["primary_mif"]
    analysis_domain_bindings: tuple[object, ...]
    prerequisite_artifact_ref_sha256s: tuple[str, ...]
    saved_state_before_sha256: str
    resolved_sample: ResolvedBatchSample

    def __post_init__(self) -> None:
        _safe_id(self.project_entry_id, "project_entry_id")
        _safe_id(self.sample_id, "sample_id")
        _text(self.slide_id, "slide_id")
        _safe_id(self.pipeline_run_id, "pipeline_run_id")
        if self.image_role != "primary_mif":
            raise ProjectStageCodecError("project-stage target must be primary_mif")
        _domains(
            self.analysis_domain_bindings,
            entry=self.project_entry_id,
            sample=self.sample_id,
            slide=self.slide_id,
        )
        _sorted_unique_sha(
            self.prerequisite_artifact_ref_sha256s,
            "prerequisite_artifact_ref_sha256s",
        )
        _sha(self.saved_state_before_sha256, "saved_state_before_sha256")
        sample = self.resolved_sample
        _canonical_config(sample)
        if (
            sample.sample_id != self.sample_id
            or sample.slide_id != self.slide_id
            or not sample.enabled
        ):
            raise ProjectStageCodecError("resolved sample differs from target intent")


@dataclass(frozen=True, slots=True)
class ProjectStageRunIntent:
    authority_layout_sha256: str
    batch_id: str
    run_plan_id: str
    stage_id: str
    stage_version: str
    project_uri: str
    project_identity_sha256: str
    expected_project_authority_head_sha256: str
    project_catalogue_sha256: str
    launch_mode: ProjectStageLaunchMode
    target_script_asset_id: str
    guard_script_asset_id: str
    targets: tuple[ProjectStageTargetIntent, ...]
    excluded_project_entry_ids: tuple[str, ...]
    shared_artifact_ref_sha256s: tuple[str, ...]
    launch_record_wait_millis: int

    def __post_init__(self) -> None:
        """Validate the ProjectStageRunIntent fields when the record is constructed.

        The checks cover ``authority_layout_sha256``, ``stage_version``, ``project_uri``,
        ``launch_mode``, ``target_script_asset_id``, ``guard_script_asset_id``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _sha(self.authority_layout_sha256, "authority_layout_sha256")
        for name in ("batch_id", "run_plan_id", "stage_id"):
            _safe_id(getattr(self, name), name)
        _text(self.stage_version, "stage_version")
        _project_uri(self.project_uri)
        for name in (
            "project_identity_sha256",
            "expected_project_authority_head_sha256",
            "project_catalogue_sha256",
        ):
            _sha(getattr(self, name), name)
        ProjectStageLaunchMode(self.launch_mode)
        _asset(self.target_script_asset_id)
        _asset(self.guard_script_asset_id, guard=True)
        if (
            type(self.targets) is not tuple
            or not self.targets
            or not all(type(item) is ProjectStageTargetIntent for item in self.targets)
            or self.targets != tuple(sorted(self.targets, key=lambda item: item.project_entry_id))
            or len({item.project_entry_id for item in self.targets}) != len(self.targets)
        ):
            raise ProjectStageCodecError("run intent targets must be canonical and unique")
        excluded = _sorted_unique_text(
            self.excluded_project_entry_ids, "excluded_project_entry_ids"
        )
        if set(excluded) & {item.project_entry_id for item in self.targets}:
            raise ProjectStageCodecError("eligible and excluded entries overlap")
        _sorted_unique_sha(
            self.shared_artifact_ref_sha256s,
            "shared_artifact_ref_sha256s",
        )
        if (
            type(self.launch_record_wait_millis) is not int
            or not 1 <= self.launch_record_wait_millis <= 600_000
        ):
            raise ProjectStageCodecError("launch wait is outside its closed bound")


class ProjectStageScientificRequestCapability:
    __slots__ = (
        "node_id",
        "attempt_id",
        "attempt_authorization_sha256",
        "input_artifact_set_sha256",
        "adapter_spec_sha256",
        "target_request_sha256",
        "request_codec_id",
        "request_codec_version",
        "scientific_request_body",
        "scientific_request_body_sha256",
        "scientific_request_body_byte_count",
        "capability_sha256",
        "_codec_issuer",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProjectStageCodecError("scientific request capabilities are issuer-only")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("scientific request capability is immutable")

    @classmethod
    def _issue(
        cls,
        *,
        context: BatchNodeExecutionContext,
        target: ProjectStageTargetRequest,
        scientific_request_body: Mapping[str, JsonValue],
        scientific_request_body_sha256: str,
        scientific_request_body_byte_count: int,
        codec_issuer: object,
        _issuer: object,
    ) -> "ProjectStageScientificRequestCapability":
        if _issuer is not _CODEC_ISSUER:
            raise ProjectStageCodecError("scientific request capability issuer differs")
        capability = object.__new__(cls)
        authorization = context.authorization
        values = {
            "node_id": context.node_id,
            "attempt_id": authorization.attempt_id,
            "attempt_authorization_sha256": authorization.authorization_sha256,
            "input_artifact_set_sha256": authorization.input_artifact_set_sha256,
            "adapter_spec_sha256": context.adapter_spec.spec_sha256,
            "target_request_sha256": target.target_request_sha256,
            "request_codec_id": target.request_codec_id,
            "request_codec_version": target.request_codec_version,
            "scientific_request_body": scientific_request_body,
            "scientific_request_body_sha256": scientific_request_body_sha256,
            "scientific_request_body_byte_count": scientific_request_body_byte_count,
        }
        digest = _digest({key: _export_json(item) for key, item in values.items()})
        for name, value in {**values, "capability_sha256": digest}.items():
            object.__setattr__(capability, name, value)
        object.__setattr__(capability, "_codec_issuer", codec_issuer)
        return capability


@dataclass(frozen=True, slots=True)
class _ProjectStageRequestProjection:
    """Carry a live owner's authenticated routing alongside its scientific body."""

    scientific_request_body: Mapping[str, object]
    producer_output_dirs: Mapping[str, Path]


class ProjectStagePreparedTarget:
    __slots__ = (
        "target",
        "context",
        "scientific_request",
        "resolved_config",
        "producer_output_dirs",
        "_codec",
        "_registry_issuer",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProjectStageCodecError("prepared targets are registry-issued")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared target is immutable")

    @classmethod
    def _issue(
        cls,
        *,
        target: ProjectStageTargetRequest,
        context: BatchNodeExecutionContext,
        scientific_request: ProjectStageScientificRequestCapability,
        resolved_config: Mapping[str, JsonValue],
        producer_output_dirs: Mapping[str, str] | None,
        codec: "ProjectStageCodec",
        registry_issuer: object,
        _issuer: object,
    ) -> "ProjectStagePreparedTarget":
        if _issuer is not _REGISTRY_ISSUER:
            raise ProjectStageCodecError("prepared target issuer differs")
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "target", target)
        object.__setattr__(prepared, "context", context)
        object.__setattr__(prepared, "scientific_request", scientific_request)
        object.__setattr__(prepared, "resolved_config", resolved_config)
        object.__setattr__(prepared, "producer_output_dirs", producer_output_dirs)
        object.__setattr__(prepared, "_codec", codec)
        object.__setattr__(prepared, "_registry_issuer", registry_issuer)
        return prepared


@runtime_checkable
class ProjectStageTargetCodec(Protocol):
    @property
    def binding(self) -> ProjectStageCodecBinding: ...

    def prepare_target(
        self,
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
    ) -> ProjectStagePreparedTarget: ...

    def validate_target(
        self, evidence: object
    ) -> BatchNodeExecutionResult | ProjectStageCodecValidationResult: ...


@runtime_checkable
class ExchangeReopeningProjector(Protocol):
    def __call__(
        self,
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener,
    ) -> Mapping[str, object]: ...


class ProjectStageCodec:
    """One immutable stage-owner request projection and result validator."""

    __slots__ = (
        "_binding",
        "_request_projector",
        "_result_validator",
        "_consumes_resolved_config",
        "_issuer",
        "_registry_issuer",
        "_stage_owner_issuer",
        "_stage_owner_binding_key",
        "_adapter_spec",
        "_exchange_reopening",
        "_sealed",
    )

    def __init__(
        self,
        *,
        binding: ProjectStageCodecBinding,
        request_projector: Callable[
            [BatchNodeExecutionContext, ProjectStageTargetIntent],
            Mapping[str, object] | _ProjectStageRequestProjection,
        ],
        result_validator: Callable[
            [object], BatchNodeExecutionResult | ProjectStageCodecValidationResult
        ],
        consumes_resolved_config: bool,
        _stage_owner_issuer: object | None = None,
        _adapter_spec: StageAdapterSpec | None = None,
        _exchange_reopening: bool = False,
    ) -> None:
        if type(binding) is not ProjectStageCodecBinding:
            raise ProjectStageCodecError("codec requires one exact binding")
        if not callable(request_projector) or not callable(result_validator):
            raise ProjectStageCodecError("codec request and result owners must be callable")
        if type(consumes_resolved_config) is not bool:
            raise ProjectStageCodecError("codec config-consumer flag must be boolean")
        if type(_exchange_reopening) is not bool or (
            _exchange_reopening and _stage_owner_issuer is not _STAGE_OWNER_CODEC_ISSUER
        ):
            raise ProjectStageCodecError("exchange reopening is reserved for an exact stage owner")
        expected_config_consumer = _asset_consumes_resolved_config(binding.target_script_asset_id)
        if consumes_resolved_config is not expected_config_consumer:
            raise ProjectStageCodecError(
                "codec config-consumer policy differs from the registered asset"
            )
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_request_projector", request_projector)
        object.__setattr__(self, "_result_validator", result_validator)
        object.__setattr__(self, "_consumes_resolved_config", consumes_resolved_config)
        object.__setattr__(self, "_issuer", object())
        object.__setattr__(self, "_registry_issuer", None)
        object.__setattr__(self, "_stage_owner_issuer", _stage_owner_issuer)
        object.__setattr__(self, "_adapter_spec", _adapter_spec)
        object.__setattr__(self, "_exchange_reopening", _exchange_reopening)
        object.__setattr__(
            self,
            "_stage_owner_binding_key",
            (
                binding.adapter_spec_sha256,
                binding.target_script_asset_id,
                binding.wire_stage_id,
                binding.terminal_stage_id,
                binding.request_codec_version,
            ),
        )
        object.__setattr__(self, "_sealed", True)

    @classmethod
    def for_stage_owner(
        cls,
        *,
        binding: ProjectStageCodecBinding,
        spec: StageAdapterSpec,
        request_projector: Callable[
            [BatchNodeExecutionContext, ProjectStageTargetIntent],
            Mapping[str, object] | _ProjectStageRequestProjection,
        ],
        result_validator: Callable[
            [object], BatchNodeExecutionResult | ProjectStageCodecValidationResult
        ],
    ) -> "ProjectStageCodec":
        """Issue one codec from the closed scientific-stage owner seam."""

        if type(binding) is not ProjectStageCodecBinding:
            raise ProjectStageCodecError("stage-owner codec requires one exact binding")
        if type(spec) is not StageAdapterSpec:
            raise ProjectStageCodecError(
                "stage-owner codec requires one exact adapter specification"
            )
        if binding.target_script_asset_id not in _ASSET_OWNER_REQUEST_FIELDS:
            raise ProjectStageCodecError("stage-owner codec asset is not registered")
        if (
            spec.spec_sha256 != binding.adapter_spec_sha256
            or spec.node_kind != binding.terminal_stage_id
            or spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
            or spec.resource_kind not in {"qupath_image_read", "qupath_image_write"}
        ):
            raise ProjectStageCodecError(
                "stage-owner codec binding differs from adapter specification"
            )
        return cls(
            binding=binding,
            request_projector=request_projector,
            result_validator=result_validator,
            consumes_resolved_config=_asset_consumes_resolved_config(
                binding.target_script_asset_id
            ),
            _stage_owner_issuer=_STAGE_OWNER_CODEC_ISSUER,
            _adapter_spec=spec,
        )

    @classmethod
    def for_exchange_reopening_stage_owner(
        cls,
        *,
        binding: ProjectStageCodecBinding,
        spec: StageAdapterSpec,
        request_projector: ExchangeReopeningProjector,
        result_validator: Callable[
            [object], BatchNodeExecutionResult | ProjectStageCodecValidationResult
        ],
    ) -> "ProjectStageCodec":
        """Issue one stage-owner codec with invocation-scoped exchange reopen."""

        codec = cls.for_stage_owner(
            binding=binding,
            spec=spec,
            request_projector=request_projector,  # type: ignore[arg-type]
            result_validator=result_validator,
        )
        object.__setattr__(codec, "_exchange_reopening", True)
        return codec

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("project-stage codec is immutable")
        object.__setattr__(self, name, value)

    @property
    def binding(self) -> ProjectStageCodecBinding:
        return self._binding

    @property
    def consumes_resolved_config(self) -> bool:
        return self._consumes_resolved_config

    def _bind_registry(self, issuer: object) -> None:
        if self._registry_issuer is not None and self._registry_issuer is not issuer:
            raise ProjectStageCodecError("codec already belongs to another registry")
        object.__setattr__(self, "_registry_issuer", issuer)

    def prepare_target(
        self,
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener | None = None,
    ) -> ProjectStagePreparedTarget:
        if self._registry_issuer is None:
            raise ProjectStageCodecError("codec is not owned by a closed registry")
        binding = self.binding
        try:
            if self._exchange_reopening:
                if type(committed_exchange_reopener) is not ProjectStageCommittedExchangeReopener:
                    raise ProjectStageCodecError(
                        "exchange-reopening codec requires one runtime-issued reopener"
                    )
                body_value = self._request_projector(
                    context,
                    intent,
                    committed_exchange_reopener=committed_exchange_reopener,
                )
            else:
                body_value = self._request_projector(context, intent)
        except ProjectStageCodecError:
            raise
        except Exception as exc:
            stage = binding.terminal_stage_id.replace("_", " ")
            raise ProjectStageCodecError(
                f"The application could not prepare the {stage} request: {exc}"
            ) from exc
        producer_output_dirs = None
        if type(body_value) is _ProjectStageRequestProjection:
            if self._stage_owner_issuer is not _STAGE_OWNER_CODEC_ISSUER:
                raise ProjectStageCodecError("producer directories require a stage-owner projection")
            if any(type(attempt) is not str or not attempt or not isinstance(root, Path)
                   for attempt, root in body_value.producer_output_dirs.items()):
                raise ProjectStageCodecError("stage-owner producer directories are invalid")
            producer_output_dirs = MappingProxyType({
                attempt: _project_stage_operation_output_dir({"operational_output_dir": str(root)}, {})
                for attempt, root in body_value.producer_output_dirs.items()
            })
            body_value = body_value.scientific_request_body
        body, body_bytes = _canonical_frozen_mapping(body_value, field="scientific_request_body")
        if self._stage_owner_issuer is _STAGE_OWNER_CODEC_ISSUER:
            missing = _ASSET_OWNER_REQUEST_FIELDS[binding.target_script_asset_id].difference(body)
            if body.get("schema_version") != "roi-workflow-invocation/v1" or missing:
                details = ", ".join(sorted(missing)) or "schema_version"
                raise ProjectStageCodecError(
                    f"stage-owner request body is missing its {details} contract"
                )
            if "slide_id" in body and body["slide_id"] != intent.slide_id:
                raise ProjectStageCodecError("stage-owner request slide identity differs")
            if "run_id" in body and body["run_id"] != intent.pipeline_run_id:
                raise ProjectStageCodecError("stage-owner request run identity differs")
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()
        target = ProjectStageTargetRequest.create(
            node_id=context.node_id,
            project_entry_id=intent.project_entry_id,
            sample_id=intent.sample_id,
            slide_id=intent.slide_id,
            pipeline_run_id=intent.pipeline_run_id,
            image_role=intent.image_role,
            wire_stage_id=binding.wire_stage_id,
            terminal_stage_id=binding.terminal_stage_id,
            analysis_domain_bindings=intent.analysis_domain_bindings,
            resolved_config_sha256=intent.resolved_sample.resolved_config_sha256,
            prerequisite_artifact_ref_sha256s=(intent.prerequisite_artifact_ref_sha256s),
            saved_state_before_sha256=intent.saved_state_before_sha256,
            adapter_spec_sha256=binding.adapter_spec_sha256,
            request_codec_id=binding.target_script_asset_id,
            request_codec_version=binding.request_codec_version,
            scientific_request_body_sha256=body_sha256,
            scientific_request_body_byte_count=len(body_bytes),
        )
        capability = ProjectStageScientificRequestCapability._issue(
            context=context,
            target=target,
            scientific_request_body=body,
            scientific_request_body_sha256=body_sha256,
            scientific_request_body_byte_count=len(body_bytes),
            codec_issuer=self._issuer,
            _issuer=_CODEC_ISSUER,
        )
        config, _ = _canonical_config(intent.resolved_sample)
        return ProjectStagePreparedTarget._issue(
            target=target,
            context=context,
            scientific_request=capability,
            resolved_config=config,
            producer_output_dirs=producer_output_dirs,
            codec=self,
            registry_issuer=self._registry_issuer,
            _issuer=_REGISTRY_ISSUER,
        )

    def validate_target(
        self, evidence: object
    ) -> BatchNodeExecutionResult | ProjectStageCodecValidationResult:
        return self._result_validator(evidence)


class PreparedProjectStageRunRequest:
    __slots__ = ("request", "prepared_targets", "_registry", "_registry_issuer")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProjectStageCodecError("prepared project-stage requests are registry-issued")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared project-stage request is immutable")

    @classmethod
    def _issue(
        cls,
        *,
        request: ProjectStageRunRequest,
        prepared_targets: tuple[ProjectStagePreparedTarget, ...],
        registry: "ProjectStageCodecRegistry",
        registry_issuer: object,
        _issuer: object,
    ) -> "PreparedProjectStageRunRequest":
        if _issuer is not _REGISTRY_ISSUER:
            raise ProjectStageCodecError("prepared request issuer differs")
        value = object.__new__(cls)
        object.__setattr__(value, "request", request)
        object.__setattr__(value, "prepared_targets", prepared_targets)
        object.__setattr__(value, "_registry", registry)
        object.__setattr__(value, "_registry_issuer", registry_issuer)
        return value


class ProjectStageCodecRegistry:
    """Immutable exact-key registry retained by one project-stage runtime."""

    __slots__ = ("_codecs", "_issuer", "_application_composition", "_sealed")

    def __init__(self, codecs: tuple[ProjectStageCodec, ...]) -> None:
        if type(codecs) is not tuple or not all(
            type(codec) is ProjectStageCodec for codec in codecs
        ):
            raise ProjectStageCodecError("codec registry requires one exact tuple")
        keyed: dict[tuple[str, str, str, str, str], ProjectStageCodec] = {}
        object.__setattr__(self, "_issuer", object())
        for codec in codecs:
            binding = codec.binding
            key = (
                binding.adapter_spec_sha256,
                binding.target_script_asset_id,
                binding.wire_stage_id,
                binding.terminal_stage_id,
                binding.request_codec_version,
            )
            if key in keyed:
                raise ProjectStageCodecError("codec registry has a duplicate exact binding")
            codec._bind_registry(self._issuer)
            keyed[key] = codec
        object.__setattr__(self, "_codecs", MappingProxyType(keyed))
        object.__setattr__(self, "_application_composition", False)
        object.__setattr__(self, "_sealed", True)

    @classmethod
    def for_application_composition(
        cls, codecs: tuple[ProjectStageCodec, ...]
    ) -> "ProjectStageCodecRegistry":
        """Close the complete registered stage-owner codec set."""

        registry = cls(codecs)
        registry._validate_application_composition()
        object.__setattr__(registry, "_application_composition", True)
        return registry

    def _validate_application_composition(self) -> None:
        if len(self._codecs) != len(_APPLICATION_CODEC_BINDING_IDENTITIES):
            raise ProjectStageCodecError(
                "application composition requires the complete stage-owner codec set"
            )
        codec_count_by_asset = {
            asset_id: 0 for asset_id in _APPLICATION_CODEC_MULTIPLICITY_BY_ASSET
        }
        binding_identities: set[tuple[str, str, str, str]] = set()
        for key, codec in self._codecs.items():
            binding = codec.binding
            if (
                codec._stage_owner_issuer is not _STAGE_OWNER_CODEC_ISSUER
                or codec._stage_owner_binding_key != key
                or type(codec._adapter_spec) is not StageAdapterSpec
                or codec._adapter_spec.spec_sha256 != key[0]
                or codec._adapter_spec.node_kind != key[3]
                or codec._adapter_spec.dispatch_mode
                is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
                or codec._adapter_spec.resource_kind
                not in {"qupath_image_read", "qupath_image_write"}
                or key[1] not in codec_count_by_asset
                or _ASSET_WIRE_STAGE.get(key[1]) != key[2]
                or key[4]
                != (
                    "2" if key[2] in {"segmentation", "primary_classifier", "measurements"} else "1"
                )
                or codec.consumes_resolved_config is not _asset_consumes_resolved_config(key[1])
                or binding.target_script_asset_id != key[1]
                or binding.wire_stage_id != key[2]
                or binding.terminal_stage_id != key[3]
                or binding.request_codec_version != key[4]
            ):
                raise ProjectStageCodecError(
                    "application composition contains a foreign or generic codec"
                )
            codec_count_by_asset[key[1]] += 1
            binding_identities.add(
                (
                    codec._adapter_spec.node_kind,
                    key[1],
                    key[2],
                    key[3],
                )
            )
        if codec_count_by_asset != dict(_APPLICATION_CODEC_MULTIPLICITY_BY_ASSET):
            raise ProjectStageCodecError(
                "application composition does not cover the exact registered binding inventory"
            )
        if binding_identities != _APPLICATION_CODEC_BINDING_IDENTITIES:
            raise ProjectStageCodecError(
                "application composition does not cover the exact owner binding inventory"
            )

    def require_application_composition(self) -> "ProjectStageCodecRegistry":
        """Reauthenticate the registry before installing it in a live runtime."""

        if not self._application_composition:
            raise ProjectStageCodecError(
                "project-stage codec registry is not an application composition"
            )
        self._validate_application_composition()
        return self

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("project-stage codec registry is immutable")
        object.__setattr__(self, name, value)

    def _resolve(
        self,
        *,
        adapter_spec_sha256: str,
        target_script_asset_id: str,
        wire_stage_id: str,
        terminal_stage_id: str,
        request_codec_version: str = "1",
    ) -> ProjectStageCodec:
        key = (
            adapter_spec_sha256,
            target_script_asset_id,
            wire_stage_id,
            terminal_stage_id,
            request_codec_version,
        )
        codec = self._codecs.get(key)
        if codec is None:
            raise ProjectStageCodecError("no exact project-stage codec is registered")
        return codec

    def application_codecs(self) -> tuple[ProjectStageCodec, ...]:
        """Return a fresh exact tuple without exposing the registry backing map."""

        self.require_application_composition()
        return tuple(self._codecs.values())

    def _resolve_adapter_spec(
        self,
        *,
        adapter_spec_sha256: str,
        target_script_asset_id: str,
        wire_stage_id: str,
        terminal_stage_id: str,
        request_codec_version: str,
    ) -> StageAdapterSpec:
        """Resolve the exact retained spec from a complete binding key."""
        self.require_application_composition()
        codec = self._resolve(
            adapter_spec_sha256=adapter_spec_sha256,
            target_script_asset_id=target_script_asset_id,
            wire_stage_id=wire_stage_id,
            terminal_stage_id=terminal_stage_id,
            request_codec_version=request_codec_version,
        )
        self._validate_application_composition()
        spec = codec._adapter_spec
        if (
            type(spec) is not StageAdapterSpec
            or spec.spec_sha256 != adapter_spec_sha256
            or spec.node_kind != terminal_stage_id
            or spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
            or spec.resource_kind not in {"qupath_image_read", "qupath_image_write"}
        ):
            raise ProjectStageCodecError("retained adapter specification differs from binding")
        return spec

    def prepare_target(
        self,
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        target_script_asset_id: str,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener | None = None,
    ) -> ProjectStagePreparedTarget:
        if (
            type(context) is not BatchNodeExecutionContext
            or type(intent) is not ProjectStageTargetIntent
        ):
            raise ProjectStageCodecError("codec preparation requires typed context and intent")
        binding_entry_ids = context.authorization.dispatch_binding.project_entry_ids
        participant_ids = context.authorization.dispatch_binding.participant_ids
        if (
            context.adapter_spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
            or binding_entry_ids != (intent.project_entry_id,)
            or participant_ids != (intent.sample_id,)
            or len(binding_entry_ids) != 1
        ):
            raise ProjectStageCodecError(
                "direct project-stage targets must own exactly one project entry"
            )
        candidates = tuple(
            codec
            for key, codec in self._codecs.items()
            if key[0] == context.adapter_spec.spec_sha256
            and key[1] == target_script_asset_id
            and key[3] == context.adapter_spec.node_kind
        )
        if len(candidates) != 1:
            raise ProjectStageCodecError("target does not resolve one exact codec")
        codec = candidates[0]
        prepared = (
            codec.prepare_target(
                context,
                intent,
                committed_exchange_reopener=committed_exchange_reopener,
            )
            if codec._exchange_reopening
            else codec.prepare_target(context, intent)
        )
        self._authenticate_prepared_target(prepared)
        return prepared

    def prepare_run(
        self,
        intent: ProjectStageRunIntent,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener | None = None,
    ) -> PreparedProjectStageRunRequest:
        if type(intent) is not ProjectStageRunIntent or type(contexts) is not tuple:
            raise ProjectStageCodecError("run preparation requires typed intent and contexts")
        by_entry: dict[str, BatchNodeExecutionContext] = {}
        for context in contexts:
            if type(context) is not BatchNodeExecutionContext:
                raise ProjectStageCodecError("run contexts must be typed")
            entry_ids = context.authorization.dispatch_binding.project_entry_ids
            if len(entry_ids) != 1 or entry_ids[0] in by_entry:
                raise ProjectStageCodecError("run contexts must own unique project entries")
            by_entry[entry_ids[0]] = context
        if set(by_entry) != {item.project_entry_id for item in intent.targets}:
            raise ProjectStageCodecError("run targets and contexts differ")
        prepared_targets = tuple(
            self.prepare_target(
                by_entry[target.project_entry_id],
                target,
                target_script_asset_id=intent.target_script_asset_id,
                committed_exchange_reopener=committed_exchange_reopener,
            )
            for target in intent.targets
        )
        request = ProjectStageRunRequest.create(
            authority_layout_sha256=intent.authority_layout_sha256,
            batch_id=intent.batch_id,
            run_plan_id=intent.run_plan_id,
            stage_id=intent.stage_id,
            stage_version=intent.stage_version,
            project_uri=intent.project_uri,
            project_identity_sha256=intent.project_identity_sha256,
            expected_project_authority_head_sha256=(intent.expected_project_authority_head_sha256),
            project_catalogue_sha256=intent.project_catalogue_sha256,
            launch_mode=intent.launch_mode,
            target_script_asset_id=intent.target_script_asset_id,
            guard_script_asset_id=intent.guard_script_asset_id,
            targets=tuple(item.target for item in prepared_targets),
            excluded_project_entry_ids=intent.excluded_project_entry_ids,
            shared_artifact_ref_sha256s=intent.shared_artifact_ref_sha256s,
            launch_record_wait_millis=intent.launch_record_wait_millis,
        )
        if any(
            item.target.terminal_stage_id != intent.stage_id
            or item.context.adapter_spec.node_kind != intent.stage_id
            or item.context.adapter_spec.node_version != intent.stage_version
            for item in prepared_targets
        ):
            raise ProjectStageCodecError("run intent differs from terminal adapter identity")
        return PreparedProjectStageRunRequest._issue(
            request=request,
            prepared_targets=prepared_targets,
            registry=self,
            registry_issuer=self._issuer,
            _issuer=_REGISTRY_ISSUER,
        )

    def _authenticate_prepared_target(self, prepared: ProjectStagePreparedTarget) -> None:
        if (
            type(prepared) is not ProjectStagePreparedTarget
            or prepared._registry_issuer is not self._issuer
            or prepared._codec not in self._codecs.values()
            or type(prepared.scientific_request) is not ProjectStageScientificRequestCapability
            or prepared.scientific_request._codec_issuer is not prepared._codec._issuer
        ):
            raise ProjectStageCodecError("prepared target has a foreign issuer")
        target = prepared.target
        context = prepared.context
        capability = prepared.scientific_request
        # i) Our codec freezes and hashes the request/config when it issues this
        # capability. ii) Current dispatch checks its issuer and binding without
        # serializing the same immutable scientific body again. External resume
        # still authenticates retained request/config bytes at its decoder.
        if (
            context.node_id != target.node_id
            or context.adapter_spec.spec_sha256 != target.adapter_spec_sha256
            or context.adapter_spec.node_kind != target.terminal_stage_id
            or capability.node_id != target.node_id
            or capability.attempt_id != context.authorization.attempt_id
            or capability.attempt_authorization_sha256 != context.authorization.authorization_sha256
            or capability.input_artifact_set_sha256
            != context.authorization.input_artifact_set_sha256
            or capability.adapter_spec_sha256 != target.adapter_spec_sha256
            or capability.target_request_sha256 != target.target_request_sha256
            or capability.request_codec_id != target.request_codec_id
            or capability.request_codec_version != target.request_codec_version
            or capability.scientific_request_body_sha256 != target.scientific_request_body_sha256
            or capability.scientific_request_body_byte_count
            != target.scientific_request_body_byte_count
        ):
            raise ProjectStageCodecError("prepared target authority differs")

    def authenticate_prepared_run(self, prepared: PreparedProjectStageRunRequest) -> None:
        if (
            type(prepared) is not PreparedProjectStageRunRequest
            or prepared._registry is not self
            or prepared._registry_issuer is not self._issuer
            or tuple(item.target for item in prepared.prepared_targets) != prepared.request.targets
        ):
            raise ProjectStageCodecError("prepared run has a foreign issuer")
        for target in prepared.prepared_targets:
            self._authenticate_prepared_target(target)

    def request_envelope_bytes(
        self,
        prepared: ProjectStagePreparedTarget,
        *,
        project_file: str | Path,
        canonical_image_uri: str,
        canonical_image_sha256: str,
        script_bundle_sha256: str,
        script_asset_path: str,
        script_asset_sha256: str,
        operational_output_dir: Path,
        producer_output_dirs: Mapping[str, str],
    ) -> bytes:
        self._authenticate_prepared_target(prepared)
        target = prepared.target
        capability = prepared.scientific_request
        project_text = str(project_file)
        if not project_text or "\x00" in project_text or not Path(project_text).is_absolute():
            raise ProjectStageCodecError("project_file must be one absolute native path")
        if (
            not isinstance(operational_output_dir, Path)
            or not operational_output_dir.is_absolute()
            or operational_output_dir != operational_output_dir.resolve(strict=False)
        ):
            raise ProjectStageCodecError("operational output directory is not canonical")
        try:
            canonical_image_uri = validate_canonical_image_uri(canonical_image_uri)
        except ExchangeContractError as exc:
            raise ProjectStageCodecError("canonical_image_uri is invalid") from exc
        _sha(canonical_image_sha256, "canonical_image_sha256")
        _sha(script_bundle_sha256, "script_bundle_sha256")
        _sha(script_asset_sha256, "script_asset_sha256")
        expected_asset_path = target.request_codec_id.removeprefix("qupath/scripts/")
        if (
            script_asset_path != expected_asset_path
            or Path(script_asset_path).is_absolute()
            or "\\" in script_asset_path
            or ".." in Path(script_asset_path).parts
        ):
            raise ProjectStageCodecError("script_asset_path differs from registry")
        request = dict(_export_json(capability.scientific_request_body))
        request.update(
            {
                "project_file": project_text,
                "project_entry_id": target.project_entry_id,
                "canonical_image_uri": canonical_image_uri,
                "canonical_image_sha256": canonical_image_sha256,
                "script_bundle_sha256": script_bundle_sha256,
                "script_asset_path": script_asset_path,
                "script_asset_sha256": script_asset_sha256,
                "exchange_version": "roi-stage-request/v2",
                "operational_output_dir": str(operational_output_dir),
                "producer_output_dirs": dict(producer_output_dirs),
            }
        )
        if prepared._codec.consumes_resolved_config:
            config = _export_json(prepared.resolved_config)
            config_hash = hashlib.sha256(canonical_json_bytes(config)).hexdigest()
            if config_hash != target.resolved_config_sha256:
                raise ProjectStageCodecError("resolved configuration authority differs")
            request["resolved_config"] = config
            request["config_hash"] = config_hash
        envelope = {
            "schema_version": "roi-stage-request/v2",
            "invocation_id": capability.attempt_id,
            "run_id": target.pipeline_run_id,
            "stage": target.wire_stage_id,
            "slide_id": target.slide_id,
            "request": request,
        }
        payload = canonical_json_bytes(envelope)
        if not payload or len(payload) > _MAX_EXCHANGE_BYTES:
            raise ProjectStageCodecError("scientific request envelope exceeds its bound")
        return payload


def prepare_project_stage_run_request(
    intent: ProjectStageRunIntent,
    contexts: tuple[BatchNodeExecutionContext, ...],
    runtime: object,
    *,
    workspace_lease: BatchWorkspaceLease,
) -> PreparedProjectStageRunRequest:
    """Prepare one run without exposing its issuer-bound target capabilities."""

    from .project_stage_plan import ProjectStageRuntime

    if type(runtime) is not ProjectStageRuntime or type(workspace_lease) is not BatchWorkspaceLease:
        raise ProjectStageCodecError("project-stage runtime authority differs")
    prepared = runtime.prepare_request(intent, contexts, workspace_lease=workspace_lease)
    if type(prepared) is not PreparedProjectStageRunRequest:
        raise ProjectStageCodecError("project-stage runtime returned a foreign request")
    return prepared


__all__ = [
    "PreparedProjectStageRunRequest",
    "ExchangeReopeningProjector",
    "ProjectStageCodec",
    "ProjectStageCodecBinding",
    "ProjectStageCodecError",
    "ProjectStageCodecValidationResult",
    "ProjectStageCodecRegistry",
    "ProjectStagePreparedTarget",
    "ProjectStageRunIntent",
    "ProjectStageScientificRequestCapability",
    "ProjectStageTargetCodec",
    "ProjectStageTargetIntent",
    "prepare_project_stage_run_request",
]
