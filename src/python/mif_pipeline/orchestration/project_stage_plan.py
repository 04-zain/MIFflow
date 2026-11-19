"""Strict project-stage intent, plan, and prepared-run value contracts.

The `PreparedProjectStageRunDescriptor` record carries `schema_name`, `schema_version`,
`authority_layout_sha256`, `runtime_composition_binding_sha256`,
`workspace_storage_scope_sha256` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from threading import Condition, Event, RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterator, Literal, Mapping
from urllib.parse import urlparse

from mif_pipeline.pipeline_core.authority_layout import AuthorityMemberRefV2
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.stage_result import StageResult, StageStatus
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeCompositionBinding,
    RuntimeLifecycleBorrower,
    RuntimeLifecycleError,
)
from mif_pipeline.qupath_runtime.scripts import (
    ProjectStageScriptBundleAuthority,
    QuPathScriptAsset,
)
from mif_pipeline.qupath_runtime.runner import (
    _ProjectStageRuntimeIdentity,
    format_qupath_process_diagnostics,
)
from mif_pipeline.qupath_runtime.exchange import ParsedStageResultExchange
from mif_pipeline.qupath_runtime.project_stage import (
    ProjectStageMutationEvidenceRecord,
    ProjectStageTargetObservation,
)

from .batch_artifacts import (
    AnalysisDomainBinding,
    BatchAttemptAuthorization,
    BatchNodeExecutionContext,
    DispatchMode,
    StageAdapterSpec,
)

if TYPE_CHECKING:
    from .batch_project import BatchProjectEntry
    from .batch_state import (
        BatchNodeAttempt,
        BatchStateHead,
        BatchStateStore,
        BatchRawNodeOutcome,
        BatchTerminalCommitter,
        BatchWorkspaceLease,
        ScheduledNodeResult,
    )
    from .project_stage_codecs import (
        PreparedProjectStageRunRequest,
        ProjectStageCodecValidationResult,
    )
    from mif_pipeline.qupath_runtime.catalogue import (
        ProjectMutationLease,
        ProjectMutationLeaseRecord,
        QuPathProjectSnapshot,
    )
    from mif_pipeline.qupath_runtime.project_stage import (
        ProjectStageProcessCompletion,
    )


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_MAX_TEXT_BYTES = 4096
_PROJECT_STAGE_RUNTIME_ISSUER = object()
_PROJECT_STAGE_COMMITTED_EXCHANGE_REOPENER_ISSUER = object()
_PROJECT_STAGE_REOPENED_EXCHANGE_ISSUER = object()
_PROJECT_STAGE_RETRY_RESUME_AUTHORITY_ISSUER = object()
_PROJECT_STAGE_TARGET_SCRIPT_ASSETS = frozenset(
    {
        QuPathScriptAsset.SETUP_TISSUE_DETECTOR,
        QuPathScriptAsset.DETECT_TISSUE_SECTIONS,
        QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS,
        QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS,
        QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER,
        QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS,
        QuPathScriptAsset.RETIRE_SAVED_RESULTS,
        QuPathScriptAsset.CORRECT_KNOWN_CELL,
        QuPathScriptAsset.RUN_INSTANSEG,
        QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS,
        QuPathScriptAsset.SNAPSHOT_SUPERVISED_TRAINING_EVIDENCE,
        QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS,
        QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES,
        QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS,
        QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS,
        QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE,
        QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS,
    }
)


# ==============================================================================
#                               Project stage plan
# ==============================================================================


class ProjectStageRecordError(ValueError):
    """A project-stage value is not the current closed contract."""


class ProjectStageLaunchMode(str, Enum):
    INTERACTIVE_RUN_FOR_PROJECT = "interactive_run_for_project"
    HEADLESS_PROJECT_SCRIPT = "headless_project_script"


def _wire(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (AuthorityMemberRefV2, AnalysisDomainBinding)):
        return value.to_mapping()
    if hasattr(value, "to_mapping") and callable(value.to_mapping):
        return value.to_mapping()
    if type(value) is tuple:
        return [_wire(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    return value


def _mapping(value: object, *, omit: str | None = None) -> dict[str, object]:
    return {
        item.name: _wire(getattr(value, item.name))
        for item in fields(value)  # type: ignore[arg-type]
        if item.name != omit and not item.name.startswith("_")
    }


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(_wire(value))).hexdigest()
    except Exception as exc:
        raise ProjectStageRecordError("project-stage value is not canonical JSON") from exc


def _payload_facts(record: object) -> tuple[str, int]:
    try:
        payload = canonical_json_bytes(record.to_mapping())
    except Exception as exc:
        raise ProjectStageRecordError("project-stage payload is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _closed(value: object, expected: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or frozenset(value) != expected
    ):
        raise ProjectStageRecordError(f"{label} fields are not the current closed schema")
    return value


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ProjectStageRecordError(f"{label} must be one lowercase SHA-256")
    return value


def _text(value: object, label: str) -> str:
    try:
        length = len(value.encode("utf-8", errors="strict")) if type(value) is str else 0
    except UnicodeError as exc:
        raise ProjectStageRecordError(f"{label} is invalid text") from exc
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or length > _MAX_TEXT_BYTES
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectStageRecordError(f"{label} must be bounded canonical text")
    return value


def _safe_id(value: object, label: str) -> str:
    text = _text(value, label)
    if _SAFE_ID.fullmatch(text) is None or text in {".", ".."}:
        raise ProjectStageRecordError(f"{label} must be one safe identifier")
    return text


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProjectStageRecordError(f"{label} must be a positive integer")
    return value


def _project_uri(value: object) -> str:
    uri = _text(value, "project_uri")
    parsed = urlparse(uri)
    if (
        parsed.scheme != "file"
        or not parsed.path.startswith("/")
        or parsed.fragment
        or parsed.query
    ):
        raise ProjectStageRecordError("project_uri must be one absolute file URI")
    return uri


def _tuple(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise ProjectStageRecordError(f"{label} must be a tuple")
    return value


def _from_array(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not list:
        raise ProjectStageRecordError(f"{label} must be an array")
    return tuple(value)


def _sorted_unique_text(values: object, label: str, *, safe: bool = True) -> tuple[str, ...]:
    items = _tuple(values, label)
    validator = _safe_id if safe else _text
    normalized = tuple(validator(item, label) for item in items)
    if normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
        raise ProjectStageRecordError(f"{label} must be sorted and unique")
    return normalized


def _sorted_unique_sha(values: object, label: str) -> tuple[str, ...]:
    items = _tuple(values, label)
    normalized = tuple(_sha(item, label) for item in items)
    if normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
        raise ProjectStageRecordError(f"{label} must be sorted and unique")
    return normalized


def _ref(value: object, *, layout: str, family: str, label: str) -> AuthorityMemberRefV2:
    try:
        result = (
            value
            if type(value) is AuthorityMemberRefV2
            else AuthorityMemberRefV2.from_mapping(value, expected_layout_sha256=layout)
        )
    except Exception as exc:
        raise ProjectStageRecordError(f"{label} is invalid") from exc
    if result.authority_layout_sha256 != layout or result.family != family:
        raise ProjectStageRecordError(f"{label} uses the wrong retained authority")
    return result


def _domains(
    value: object, *, entry: str, sample: str, slide: str
) -> tuple[AnalysisDomainBinding, ...]:
    values = _tuple(value, "analysis_domain_bindings")
    if not all(type(item) is AnalysisDomainBinding for item in values):
        raise ProjectStageRecordError("analysis_domain_bindings must be typed")
    key = lambda item: (
        item.analysis_domain_id,
        item.analysis_domain_revision,
        item.project_entry_id,
    )
    if values != tuple(sorted(values, key=key)) or len({key(item) for item in values}) != len(
        values
    ):
        raise ProjectStageRecordError("analysis_domain_bindings must be sorted and unique")
    if any(
        item.project_entry_id != entry or item.sample_id != sample or item.slide_id != slide
        for item in values
    ):
        raise ProjectStageRecordError("analysis-domain binding differs from target identity")
    return values  # type: ignore[return-value]


def _asset(value: object, *, guard: bool = False) -> str:
    asset = _text(value, "script asset ID")
    try:
        registered = QuPathScriptAsset(asset)
    except ValueError as exc:
        raise ProjectStageRecordError("script asset ID is not registered") from exc
    if guard:
        if registered is not QuPathScriptAsset.BATCH_STAGE_GUARD:
            raise ProjectStageRecordError("project-stage guard asset is not the batch-stage guard")
    elif registered not in _PROJECT_STAGE_TARGET_SCRIPT_ASSETS:
        raise ProjectStageRecordError("project-stage target asset is not bootstrapped")
    return asset


def _require_typed_record(value: object, expected_type: type, label: str) -> object:
    """We accept an already constructed current record without serializing it again."""
    if type(value) is not expected_type:
        raise ProjectStageRecordError(f"{label} must be typed")
    return value


def _validate_schema(
    name: object, version: object, expected: str, *, current_version: int = 1
) -> None:
    if name != expected or type(version) is not int or version != current_version:
        raise ProjectStageRecordError(f"{expected} schema is unsupported")


def _validate_digest(instance: object, field_name: str) -> None:
    actual = _sha(getattr(instance, field_name), field_name)
    if actual != _digest(_mapping(instance, omit=field_name)):
        raise ProjectStageRecordError(f"{field_name} does not match the complete record")


def _create(cls, values: Mapping[str, object], digest_field: str):
    digest = _digest({key: _wire(item) for key, item in values.items()})
    return cls(**dict(values), **{digest_field: digest})


@dataclass(frozen=True, slots=True)
class ProjectStageTargetRequest:
    node_id: str
    project_entry_id: str
    sample_id: str
    slide_id: str
    pipeline_run_id: str
    image_role: Literal["primary_mif"]
    wire_stage_id: str
    terminal_stage_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    resolved_config_sha256: str
    prerequisite_artifact_ref_sha256s: tuple[str, ...]
    saved_state_before_sha256: str
    adapter_spec_sha256: str
    request_codec_id: str
    request_codec_version: str
    scientific_request_body_sha256: str
    scientific_request_body_byte_count: int
    target_request_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "node_id",
            "project_entry_id",
            "sample_id",
            "pipeline_run_id",
            "wire_stage_id",
            "terminal_stage_id",
        ):
            _safe_id(getattr(self, name), name)
        _text(self.slide_id, "slide_id")
        if self.image_role != "primary_mif":
            raise ProjectStageRecordError("project-stage target must be primary_mif")
        _domains(
            self.analysis_domain_bindings,
            entry=self.project_entry_id,
            sample=self.sample_id,
            slide=self.slide_id,
        )
        _sha(self.resolved_config_sha256, "resolved_config_sha256")
        _sorted_unique_sha(
            self.prerequisite_artifact_ref_sha256s, "prerequisite_artifact_ref_sha256s"
        )
        _sha(self.saved_state_before_sha256, "saved_state_before_sha256")
        _sha(self.adapter_spec_sha256, "adapter_spec_sha256")
        _asset(self.request_codec_id)
        _text(self.request_codec_version, "request_codec_version")
        _sha(
            self.scientific_request_body_sha256,
            "scientific_request_body_sha256",
        )
        _positive(
            self.scientific_request_body_byte_count,
            "scientific_request_body_byte_count",
        )
        _validate_digest(self, "target_request_sha256")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageTargetRequest":
        return _create(cls, values, "target_request_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageTargetRequest":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "target request"))
        raw["analysis_domain_bindings"] = tuple(
            AnalysisDomainBinding.from_mapping(item)
            for item in _from_array(raw["analysis_domain_bindings"], "analysis_domain_bindings")
        )
        raw["prerequisite_artifact_ref_sha256s"] = _from_array(
            raw["prerequisite_artifact_ref_sha256s"], "prerequisite_artifact_ref_sha256s"
        )
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageExchangeAttestation:
    authority_layout_sha256: str
    request_identity_sha256: str
    target_request_sha256: str
    request_ref: AuthorityMemberRefV2
    request_envelope_sha256: str
    request_envelope_byte_count: int
    result_ref: AuthorityMemberRefV2
    generation_manifest_ref: AuthorityMemberRefV2
    exchange_sha256: str

    def __post_init__(self) -> None:
        layout = _sha(self.authority_layout_sha256, "authority_layout_sha256")
        for name in (
            "request_identity_sha256",
            "target_request_sha256",
            "request_envelope_sha256",
        ):
            _sha(getattr(self, name), name)
        _positive(self.request_envelope_byte_count, "request_envelope_byte_count")
        refs = tuple(
            _ref(getattr(self, name), layout=layout, family="runtime_exchange", label=name)
            for name in ("request_ref", "result_ref", "generation_manifest_ref")
        )
        if len({item.key_sha256 for item in refs}) != 3:
            raise ProjectStageRecordError("exchange refs must be distinct")
        _validate_digest(self, "exchange_sha256")

    @classmethod
    def create(
        cls,
        *,
        active_mutation_run_sha256: str,
        target_request: ProjectStageTargetRequest,
        request_envelope_sha256: str,
        request_ref: AuthorityMemberRefV2,
        request_envelope_byte_count: int,
        result_ref: AuthorityMemberRefV2,
        generation_manifest_ref: AuthorityMemberRefV2,
    ) -> "ProjectStageExchangeAttestation":
        if type(target_request) is not ProjectStageTargetRequest:
            raise ProjectStageRecordError("target_request must be typed")
        active = _sha(active_mutation_run_sha256, "active_mutation_run_sha256")
        envelope_sha256 = _sha(request_envelope_sha256, "request_envelope_sha256")
        identity = _digest(
            {
                "active_mutation_run_sha256": active,
                "kind": "project_stage_target_exchange/v2",
                "target_request_sha256": target_request.target_request_sha256,
                "request_envelope_sha256": envelope_sha256,
            }
        )
        values = {
            "authority_layout_sha256": request_ref.authority_layout_sha256,
            "request_identity_sha256": identity,
            "target_request_sha256": target_request.target_request_sha256,
            "request_ref": request_ref,
            "request_envelope_sha256": envelope_sha256,
            "request_envelope_byte_count": request_envelope_byte_count,
            "result_ref": result_ref,
            "generation_manifest_ref": generation_manifest_ref,
        }
        return _create(cls, values, "exchange_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageExchangeAttestation":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "exchange attestation"))
        layout = _sha(raw["authority_layout_sha256"], "authority_layout_sha256")
        for name in ("request_ref", "result_ref", "generation_manifest_ref"):
            raw[name] = _ref(raw[name], layout=layout, family="runtime_exchange", label=name)
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageRunRequest:
    schema_name: Literal["mif-project-stage-run-request"]
    schema_version: Literal[2]
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
    targets: tuple[ProjectStageTargetRequest, ...]
    target_request_set_sha256: str
    excluded_project_entry_ids: tuple[str, ...]
    excluded_entry_set_sha256: str
    shared_artifact_ref_sha256s: tuple[str, ...]
    shared_artifact_set_sha256: str
    launch_record_wait_millis: int
    request_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectStageRunRequest fields when the record is constructed.

        The checks cover ``schema_name``, ``schema_version``, ``authority_layout_sha256``,
        ``stage_version``, ``project_uri``, ``target_script_asset_id``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _validate_schema(
            self.schema_name,
            self.schema_version,
            "mif-project-stage-run-request",
            current_version=2,
        )
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
        try:
            launch_mode = ProjectStageLaunchMode(self.launch_mode)
        except (TypeError, ValueError) as exc:
            raise ProjectStageRecordError("launch_mode is invalid") from exc
        object.__setattr__(self, "launch_mode", launch_mode)
        _asset(self.target_script_asset_id)
        _asset(self.guard_script_asset_id, guard=True)
        targets = _tuple(self.targets, "targets")
        if not targets or not all(type(item) is ProjectStageTargetRequest for item in targets):
            raise ProjectStageRecordError("targets must be a non-empty typed tuple")
        if targets != tuple(sorted(targets, key=lambda item: item.project_entry_id)):
            raise ProjectStageRecordError("targets must be canonically ordered")
        target_entries = [item.project_entry_id for item in targets]
        if len(set(target_entries)) != len(target_entries):
            raise ProjectStageRecordError("targets must be unique")
        expected = _digest(
            {"target_request_sha256s": [item.target_request_sha256 for item in targets]}
        )
        if _sha(self.target_request_set_sha256, "target_request_set_sha256") != expected:
            raise ProjectStageRecordError("target request set digest is invalid")
        excluded = _sorted_unique_text(
            self.excluded_project_entry_ids, "excluded_project_entry_ids"
        )
        if set(excluded) & {item.project_entry_id for item in targets}:
            raise ProjectStageRecordError("eligible and excluded entries overlap")
        if _sha(self.excluded_entry_set_sha256, "excluded_entry_set_sha256") != _digest(
            {"excluded_project_entry_ids": list(excluded)}
        ):
            raise ProjectStageRecordError("excluded entry set digest is invalid")
        shared = _sorted_unique_sha(self.shared_artifact_ref_sha256s, "shared_artifact_ref_sha256s")
        if _sha(self.shared_artifact_set_sha256, "shared_artifact_set_sha256") != _digest(
            {"artifact_ref_sha256s": list(shared)}
        ):
            raise ProjectStageRecordError("shared artifact set digest is invalid")
        if (
            type(self.launch_record_wait_millis) is not int
            or not 1 <= self.launch_record_wait_millis <= 600000
        ):
            raise ProjectStageRecordError("launch_record_wait_millis is outside its closed bound")
        _validate_digest(self, "request_sha256")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageRunRequest":
        targets = values.get("targets")
        excluded = values.get("excluded_project_entry_ids")
        shared = values.get("shared_artifact_ref_sha256s")
        if type(targets) is not tuple or type(excluded) is not tuple or type(shared) is not tuple:
            raise ProjectStageRecordError("request aggregate fields must be tuples")
        complete = {
            "schema_name": "mif-project-stage-run-request",
            "schema_version": 2,
            **values,
            "target_request_set_sha256": _digest({"target_request_sha256s": [item.target_request_sha256 for item in targets]}),  # type: ignore[union-attr]
            "excluded_entry_set_sha256": _digest({"excluded_project_entry_ids": list(excluded)}),
            "shared_artifact_set_sha256": _digest({"artifact_ref_sha256s": list(shared)}),
        }
        return _create(cls, complete, "request_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageRunRequest":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "run request"))
        raw["launch_mode"] = ProjectStageLaunchMode(raw["launch_mode"])
        raw["targets"] = tuple(
            ProjectStageTargetRequest.from_mapping(item)
            for item in _from_array(raw["targets"], "targets")
        )
        for name in ("excluded_project_entry_ids", "shared_artifact_ref_sha256s"):
            raw[name] = _from_array(raw[name], name)
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProjectStageAuthorizedEntry:
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    input_artifact_set_sha256: str
    target_request_sha256: str
    project_entry_id: str
    sample_id: str
    slide_id: str
    pipeline_run_id: str
    image_role: Literal["primary_mif"]
    wire_stage_id: str
    terminal_stage_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    resolved_config_sha256: str
    prerequisite_artifact_ref_sha256s: tuple[str, ...]
    adapter_spec_sha256: str
    request_codec_id: str
    request_codec_version: str
    scientific_request_body_sha256: str
    scientific_request_body_byte_count: int
    exchange: ProjectStageExchangeAttestation
    saved_state_before_sha256: str
    authorized_entry_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "node_id",
            "attempt_id",
            "project_entry_id",
            "sample_id",
            "pipeline_run_id",
            "wire_stage_id",
            "terminal_stage_id",
        ):
            _safe_id(getattr(self, name), name)
        for name in (
            "attempt_authorization_sha256",
            "input_artifact_set_sha256",
            "target_request_sha256",
            "resolved_config_sha256",
            "saved_state_before_sha256",
            "adapter_spec_sha256",
            "scientific_request_body_sha256",
        ):
            _sha(getattr(self, name), name)
        _text(self.slide_id, "slide_id")
        if self.image_role != "primary_mif":
            raise ProjectStageRecordError("authorized target must be primary_mif")
        _domains(
            self.analysis_domain_bindings,
            entry=self.project_entry_id,
            sample=self.sample_id,
            slide=self.slide_id,
        )
        _sorted_unique_sha(
            self.prerequisite_artifact_ref_sha256s, "prerequisite_artifact_ref_sha256s"
        )
        _asset(self.request_codec_id)
        _text(self.request_codec_version, "request_codec_version")
        _positive(
            self.scientific_request_body_byte_count,
            "scientific_request_body_byte_count",
        )
        if (
            type(self.exchange) is not ProjectStageExchangeAttestation
            or self.exchange.target_request_sha256 != self.target_request_sha256
        ):
            raise ProjectStageRecordError("exchange differs from target request")
        _validate_digest(self, "authorized_entry_sha256")

    @classmethod
    def create(
        cls,
        *,
        target_request: ProjectStageTargetRequest,
        attempt_id: str,
        attempt_authorization_sha256: str,
        input_artifact_set_sha256: str,
        exchange: ProjectStageExchangeAttestation,
    ) -> "ProjectStageAuthorizedEntry":
        if type(target_request) is not ProjectStageTargetRequest:
            raise ProjectStageRecordError("target_request must be typed")
        values = {
            "node_id": target_request.node_id,
            "attempt_id": attempt_id,
            "attempt_authorization_sha256": attempt_authorization_sha256,
            "input_artifact_set_sha256": input_artifact_set_sha256,
            "target_request_sha256": target_request.target_request_sha256,
            "project_entry_id": target_request.project_entry_id,
            "sample_id": target_request.sample_id,
            "slide_id": target_request.slide_id,
            "pipeline_run_id": target_request.pipeline_run_id,
            "image_role": target_request.image_role,
            "wire_stage_id": target_request.wire_stage_id,
            "terminal_stage_id": target_request.terminal_stage_id,
            "analysis_domain_bindings": target_request.analysis_domain_bindings,
            "resolved_config_sha256": target_request.resolved_config_sha256,
            "prerequisite_artifact_ref_sha256s": target_request.prerequisite_artifact_ref_sha256s,
            "adapter_spec_sha256": target_request.adapter_spec_sha256,
            "request_codec_id": target_request.request_codec_id,
            "request_codec_version": target_request.request_codec_version,
            "scientific_request_body_sha256": (target_request.scientific_request_body_sha256),
            "scientific_request_body_byte_count": (
                target_request.scientific_request_body_byte_count
            ),
            "exchange": exchange,
            "saved_state_before_sha256": target_request.saved_state_before_sha256,
        }
        return _create(cls, values, "authorized_entry_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageAuthorizedEntry":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "authorized entry"))
        raw["analysis_domain_bindings"] = tuple(
            AnalysisDomainBinding.from_mapping(item)
            for item in _from_array(raw["analysis_domain_bindings"], "analysis_domain_bindings")
        )
        raw["prerequisite_artifact_ref_sha256s"] = _from_array(
            raw["prerequisite_artifact_ref_sha256s"], "prerequisite_artifact_ref_sha256s"
        )
        raw["exchange"] = ProjectStageExchangeAttestation.from_mapping(raw["exchange"])
        return cls(**raw)  # type: ignore[arg-type]


_PROJECT_STAGE_TARGET_EXECUTION_EVIDENCE_ISSUER = object()


def _retained_project_stage_json(value: object) -> object:
    """Convert arrays in a saved JSON request to the immutable request form."""
    if isinstance(value, dict):
        return {str(key): _retained_project_stage_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return tuple(_retained_project_stage_json(item) for item in value)
    return value


def _freeze_project_stage_validation_json(
    value: object,
    *,
    label: str,
) -> object:
    """Copy one passive JSON value into a recursively immutable form."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ProjectStageRecordError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ProjectStageRecordError(f"{label} must have string keys")
        return MappingProxyType(
            {
                key: _freeze_project_stage_validation_json(value[key], label=f"{label}.{key}")
                for key in sorted(value)
            }
        )
    if type(value) is tuple:
        return tuple(_freeze_project_stage_validation_json(item, label=label) for item in value)
    raise ProjectStageRecordError(f"{label} must contain only immutable finite JSON values")


def _frozen_project_stage_validation_mapping(
    value: object,
    *,
    label: str,
) -> tuple[Mapping[str, object], bytes]:
    frozen = _freeze_project_stage_validation_json(value, label=label)
    if not isinstance(frozen, Mapping):
        raise ProjectStageRecordError(f"{label} must be one JSON object")
    try:
        payload = canonical_json_bytes(_wire(frozen))
    except Exception as exc:
        raise ProjectStageRecordError(f"{label} is not canonical JSON") from exc
    return frozen, payload


_PROJECT_STAGE_VALIDATION_INPUT_ISSUER = object()


def _project_stage_operation_output_dir(
    request: Mapping[str, object], resolved_config: Mapping[str, object],
) -> str:
    """Read the publication location from an authenticated original request.

    Earlier operations recorded their location in resolved_config. New operations
    may bind a current workspace location separately, preserving that original
    scientific configuration. A present invalid override is never replaced by
    the config value. Actual directory/file ancestry is retained by each owner.
    """
    value = (request["operational_output_dir"] if "operational_output_dir" in request
             else resolved_config.get("output_dir"))
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or "\x00" in value or not Path(value).is_absolute()
        or str(Path(value)) != value or ".." in Path(value).parts
    ):
        raise ProjectStageRecordError("recorded operational output directory is invalid")
    return value


def _project_stage_producer_output_dirs(
    body, *, sample_id, input_refs, reopen_authorities, reopener, cache, producer_output_dirs=None,
):
    """Bind each referenced relative ROI table to its producing request directory.

    A live owner may already have opened these exact tables. Reuse its frozen
    routing after matching each table to this target's current inputs. Otherwise
    reopen only selected producers, sharing their directories within issuance.
    """
    selected = {}
    def visit(value):
        if isinstance(value, Mapping):
            if value.get("producer_attempt_id") is not None and value.get("roi_inventory") is not None:
                attempt = value["producer_attempt_id"]
                inventory = value["roi_inventory"]
                if attempt in selected and selected[attempt] != inventory:
                    raise ProjectStageRecordError("ROI producer has conflicting table selections")
                selected[attempt] = inventory
            for child in value.values():
                visit(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                visit(child)
    visit(body)
    if producer_output_dirs is not None and set(producer_output_dirs) != set(selected):
        raise ProjectStageRecordError("live ROI producer directories differ from the selected tables")
    # Measurement tables can be current ancestors of the direct segmentation
    # input. Their owner already selects that ancestry; retain current-state
    # matching when no live owner supplied its checked routing.
    if producer_output_dirs is None:
        authorities = reopen_authorities() if selected else ()
        candidates = tuple((item.artifact_ref, item) for item in authorities)
    else:
        candidates = tuple((ref, None) for ref in input_refs)
    roots = {}
    for attempt, inventory in selected.items():
        matches = tuple((ref, owner) for ref, owner in candidates
                        if ref.scope.sample_id == sample_id
                        and ref.producer_attempt_id == attempt
                        and ref.artifact_kind in {"primary_inference_result", "secondary_inference_result"}
                        and _wire(ref.owning_identity["roi_inventory"]) == _wire(inventory))
        if len(matches) != 1:
            raise ProjectStageRecordError("ROI table lacks its exact current producer")
        ref, authority = matches[0]
        if producer_output_dirs is not None:
            roots[attempt] = producer_output_dirs[attempt]
        else:
            if ref.artifact_ref_sha256 not in cache:
                cache[ref.artifact_ref_sha256] = str(reopener.classifier_output_dir(authority))
            roots[attempt] = cache[ref.artifact_ref_sha256]
    return roots


@dataclass(frozen=True, slots=True, init=False)
class ProjectStageValidationInputAuthority:
    """Issuer-authenticated passive preimages available to a stage owner."""

    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    input_artifact_set_sha256: str
    adapter_spec_sha256: str
    target_request_sha256: str
    request_codec_id: str
    request_codec_version: str
    scientific_request_body: Mapping[str, object]
    scientific_request_body_sha256: str
    scientific_request_body_byte_count: int
    resolved_config: Mapping[str, object]
    resolved_config_sha256: str
    operational_output_dir: str
    request_capability_sha256: str
    authority_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ProjectStageValidationInputAuthority is issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("ProjectStageValidationInputAuthority cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ProjectStageValidationInputAuthority cannot be serialized")

    @classmethod
    def _issue(
        cls,
        *,
        context: BatchNodeExecutionContext,
        authorized: ProjectStageAuthorizedEntry,
        scientific_request_body: object,
        scientific_request_body_sha256: object,
        scientific_request_body_byte_count: object,
        resolved_config: object,
        operational_output_dir: str,
        request_capability_sha256: object,
        _issuer: object,
    ) -> "ProjectStageValidationInputAuthority":
        """We issue immutable validation inputs for one current target.

        i) We reuse codec-owned frozen preimages, or freeze externally reopened
        JSON once. ii) We compare accepted digests and current attempt, input and
        adapter identities. iii) We retain a compact identity digest and expose
        the same mappings; a foreign issuer or changed binding raises an error.
        """
        if (
            _issuer is not _PROJECT_STAGE_VALIDATION_INPUT_ISSUER
            or type(context) is not BatchNodeExecutionContext
            or type(authorized) is not ProjectStageAuthorizedEntry
        ):
            raise ProjectStageRecordError("project-stage validation input issuer differs")
        # i) Reuse codec-owned immutable preimages; reopened external JSON is frozen once.
        if type(scientific_request_body) is MappingProxyType and type(resolved_config) is MappingProxyType:
            body, config = scientific_request_body, resolved_config
            body_sha256 = _sha(scientific_request_body_sha256, "scientific_request_body_sha256")
            body_byte_count = _positive(scientific_request_body_byte_count, "scientific_request_body_byte_count")
            config_sha256 = authorized.resolved_config_sha256
        else:
            body, body_payload = _frozen_project_stage_validation_mapping(scientific_request_body, label="scientific_request_body")
            config, config_payload = _frozen_project_stage_validation_mapping(resolved_config, label="resolved_config")
            body_sha256 = hashlib.sha256(body_payload).hexdigest()
            body_byte_count = len(body_payload)
            config_sha256 = hashlib.sha256(config_payload).hexdigest()
        capability_sha256 = _sha(
            request_capability_sha256,
            "request_capability_sha256",
        )
        authorization = context.authorization
        if (
            body_sha256
            != _sha(
                scientific_request_body_sha256,
                "scientific_request_body_sha256",
            )
            or body_byte_count
            != _positive(
                scientific_request_body_byte_count,
                "scientific_request_body_byte_count",
            )
            or body_sha256 != authorized.scientific_request_body_sha256
            or body_byte_count != authorized.scientific_request_body_byte_count
            or config_sha256 != authorized.resolved_config_sha256
            or context.node_id != authorized.node_id
            or authorization.attempt_id != authorized.attempt_id
            or authorization.authorization_sha256 != authorized.attempt_authorization_sha256
            or authorization.input_artifact_set_sha256 != authorized.input_artifact_set_sha256
            or context.adapter_spec.spec_sha256 != authorized.adapter_spec_sha256
        ):
            raise ProjectStageRecordError("project-stage validation input authority does not match")
        values = {
            "node_id": context.node_id,
            "attempt_id": authorization.attempt_id,
            "attempt_authorization_sha256": (authorization.authorization_sha256),
            "input_artifact_set_sha256": (authorization.input_artifact_set_sha256),
            "adapter_spec_sha256": context.adapter_spec.spec_sha256,
            "target_request_sha256": authorized.target_request_sha256,
            "request_codec_id": authorized.request_codec_id,
            "request_codec_version": authorized.request_codec_version,
            "scientific_request_body": body,
            "scientific_request_body_sha256": body_sha256,
            "scientific_request_body_byte_count": body_byte_count,
            "resolved_config": config,
            "resolved_config_sha256": config_sha256,
            "operational_output_dir": operational_output_dir,
            "request_capability_sha256": capability_sha256,
        }
        # The operation directory is already bound by authorized.exchange's
        # exact request digest; it does not change this scientific-input stamp.
        authority_sha256 = _digest({
            name: value for name, value in values.items()
            if name not in {"scientific_request_body", "resolved_config", "operational_output_dir"}
        })
        instance = object.__new__(cls)
        for name, item in {
            **values,
            "authority_sha256": authority_sha256,
            "_issuer": _issuer,
        }.items():
            object.__setattr__(instance, name, item)
        return instance

    def _reopen(
        self,
        *,
        context: BatchNodeExecutionContext,
        authorized: ProjectStageAuthorizedEntry,
    ) -> "ProjectStageValidationInputAuthority":
        """We return retained immutable inputs bound to this current attempt and target.

        The issuer established JSON shape and digests at preparation or external
        reopening. This access checks the current identity without rebuilding
        those preimages or the capability.
        """
        if type(self) is not ProjectStageValidationInputAuthority or self._issuer is not _PROJECT_STAGE_VALIDATION_INPUT_ISSUER:
            raise ProjectStageRecordError("project-stage validation input capability is foreign")
        if (
            self.node_id != context.node_id
            or self.attempt_id != context.authorization.attempt_id
            or self.attempt_authorization_sha256 != context.authorization.authorization_sha256
            or self.input_artifact_set_sha256 != context.authorization.input_artifact_set_sha256
            or self.adapter_spec_sha256 != context.adapter_spec.spec_sha256
            or self.target_request_sha256 != authorized.target_request_sha256
            or self.scientific_request_body_sha256 != authorized.scientific_request_body_sha256
            or self.scientific_request_body_byte_count != authorized.scientific_request_body_byte_count
            or self.resolved_config_sha256 != authorized.resolved_config_sha256
        ):
            raise ProjectStageRecordError("project-stage validation inputs belong to a different current target")
        return self


def _issue_project_stage_validation_input_authority(
    *, prepared_target: object, context: BatchNodeExecutionContext,
    authorized: ProjectStageAuthorizedEntry,
    operational_output_dir: str,
) -> ProjectStageValidationInputAuthority:
    """We bind the codec's immutable prepared inputs to the current accepted target.

    Preparation established their scientific/configuration digests once. We
    retain issuer, attempt, input and adapter identity checks before exposing the
    same frozen values to the stage owner; a foreign retained target is rejected.
    """
    from .project_stage_codecs import ProjectStageScientificRequestCapability
    target = getattr(prepared_target, "target", None)
    scientific_request = getattr(prepared_target, "scientific_request", None)
    codec = getattr(prepared_target, "_codec", None)
    if (
        type(target) is not ProjectStageTargetRequest
        or type(scientific_request) is not ProjectStageScientificRequestCapability
        or getattr(prepared_target, "context", None) is not context
        or codec is None
        or getattr(prepared_target, "_registry_issuer", None) is None
        or prepared_target._registry_issuer is not getattr(codec, "_registry_issuer", None)
        or scientific_request._codec_issuer is not getattr(codec, "_issuer", None)
        or any(getattr(target, name) != getattr(authorized, name) for name in ProjectStageTargetRequest.__dataclass_fields__)
        or scientific_request.node_id != context.node_id
        or scientific_request.attempt_id != context.authorization.attempt_id
        or scientific_request.attempt_authorization_sha256 != context.authorization.authorization_sha256
        or scientific_request.input_artifact_set_sha256 != context.authorization.input_artifact_set_sha256
        or scientific_request.adapter_spec_sha256 != context.adapter_spec.spec_sha256
        or scientific_request.target_request_sha256 != authorized.target_request_sha256
        or scientific_request.request_codec_id != authorized.request_codec_id
        or scientific_request.request_codec_version != authorized.request_codec_version
        or scientific_request.scientific_request_body_sha256 != authorized.scientific_request_body_sha256
        or scientific_request.scientific_request_body_byte_count != authorized.scientific_request_body_byte_count
    ):
        raise ProjectStageRecordError("project-stage retained validation input authority differs")
    return ProjectStageValidationInputAuthority._issue(
        context=context, authorized=authorized,
        scientific_request_body=scientific_request.scientific_request_body,
        scientific_request_body_sha256=scientific_request.scientific_request_body_sha256,
        scientific_request_body_byte_count=scientific_request.scientific_request_body_byte_count,
        resolved_config=prepared_target.resolved_config,
        operational_output_dir=operational_output_dir,
        request_capability_sha256=scientific_request.capability_sha256,
        _issuer=_PROJECT_STAGE_VALIDATION_INPUT_ISSUER,
    )


def _retained_output_free_failure(process_evidence, drain, mutations, plan, store):
    """A recorded nonzero exit is retryable only after unchanged-image inspection."""
    if (process_evidence is None or drain is None or mutations
            or drain.termination_reason != "nonzero_exit" or drain.exit_status in (None, 0)
            or drain.containment_drained is not True
            or plan.launch_mode is not ProjectStageLaunchMode.HEADLESS_PROJECT_SCRIPT):
        return False
    family = store._authority_layout.layout.family("runtime_exchange")
    return all(store._read_ref(ref, max_bytes=family.max_member_bytes,
        label="failed retained project-stage output", optional=True) is None
        for target in plan.targets for ref in (
            target.exchange.result_ref, target.exchange.generation_manifest_ref))


def _require_native_completion_binding(
    parsed: ParsedStageResultExchange,
    authorized: ProjectStageAuthorizedEntry,
    process_evidence: object,
) -> None:
    """Bind the native saved-state receipt to its original target and process.

    Current bytes are compared separately using the ordinary project inspection.
    Here we require the exact request selected for this entry within the grouped
    launch, so another target's valid receipt cannot authorize these outputs.
    """
    receipt = parsed.saved_project_state
    if (
        receipt.project_entry_id != authorized.project_entry_id
        or receipt.request_envelope_sha256 != authorized.exchange.request_envelope_sha256
        or receipt.prepared_descriptor_sha256
        != process_evidence.attempt.prepared_descriptor_sha256
        or receipt.process_launch_sha256 != process_evidence.launch.process_launch_sha256
    ):
        raise ProjectStageRecordError("native saved-state receipt differs from its original launch")


def _validate_project_stage_execution_evidence_inputs(
    *,
    context: BatchNodeExecutionContext,
    authorized: ProjectStageAuthorizedEntry,
    observation: ProjectStageTargetObservation,
    parsed_exchange: ParsedStageResultExchange,
    execution_exchange_sha256: str,
) -> tuple[str, StageResult, tuple[object, ...]]:
    """Validate project stage execution evidence inputs for the current operation.

    We use current typed records from the external parsing and preparation owners.
    The comparisons bind their attempt, input scope, saved state and exact exchange
    members before the caller can use the scientific result."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(authorized) is not ProjectStageAuthorizedEntry
        or type(observation) is not ProjectStageTargetObservation
        or type(parsed_exchange) is not ParsedStageResultExchange
    ):
        raise ProjectStageRecordError("target execution evidence inputs are invalid")
    exchange_sha256 = _sha(
        execution_exchange_sha256,
        "execution_exchange_sha256",
    )
    result = parsed_exchange.result
    if type(result) is not StageResult:
        raise ProjectStageRecordError("target execution evidence result must be typed")
    context_authority_facts = _project_stage_context_facts(context=context, authorized=authorized, observation=observation)
    common_identity = (
        context.node_id == authorized.node_id == observation.node_id
        and context.authorization.attempt_id == authorized.attempt_id == observation.attempt_id
        and context.authorization.authorization_sha256
        == authorized.attempt_authorization_sha256
        == observation.attempt_authorization_sha256
        and authorized.authorized_entry_sha256 == observation.authorized_entry_sha256
        and authorized.project_entry_id == observation.project_entry_id
        and authorized.sample_id == observation.sample_id
        and authorized.slide_id == observation.slide_id
        and authorized.saved_state_before_sha256 == observation.saved_state_before_sha256
        and authorized.exchange.result_ref == observation.result_ref
        and authorized.exchange.generation_manifest_ref == observation.generation_manifest_ref
        and context.adapter_spec.spec_sha256 == authorized.adapter_spec_sha256
        and context.adapter_spec.node_kind == authorized.terminal_stage_id
        and exchange_sha256 == authorized.exchange.exchange_sha256
    )
    parsed_member_facts_match = (
        observation.result_payload_sha256 == parsed_exchange.result_payload_sha256
        and observation.result_byte_count == parsed_exchange.result_byte_count
        and observation.generation_manifest_payload_sha256
        == parsed_exchange.generation_manifest_payload_sha256
        and observation.generation_manifest_byte_count
        == parsed_exchange.generation_manifest_byte_count
    )
    if (
        not common_identity
        or not parsed_member_facts_match
        or observation.saved_state_disposition == "unattestable"
        or observation.saved_state_after_sha256 is None
        or result.stage != authorized.wire_stage_id
        or result.slide_id != authorized.slide_id
        or parsed_exchange.saved_project_state.project_entry_id != authorized.project_entry_id
        or parsed_exchange.saved_project_state.saved_state_sha256
        != observation.saved_state_after_sha256
        or parsed_exchange.saved_project_state.request_envelope_sha256
        != authorized.exchange.request_envelope_sha256
    ):
        raise ProjectStageRecordError("target execution evidence authority does not match")
    return exchange_sha256, result, context_authority_facts


@dataclass(frozen=True, slots=True, init=False)
class ProjectStageTargetExecutionEvidence:
    """Exact non-wire authority presented only to the retained owner codec."""

    context: BatchNodeExecutionContext
    authorized: ProjectStageAuthorizedEntry
    observation: ProjectStageTargetObservation
    wire_stage_result: StageResult
    generation_manifest: Mapping[str, object] | None
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    _validation_inputs: ProjectStageValidationInputAuthority
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ProjectStageTargetExecutionEvidence is issuer-only")

    @classmethod
    def _issue(
        cls,
        *,
        context: BatchNodeExecutionContext,
        authorized: ProjectStageAuthorizedEntry,
        observation: ProjectStageTargetObservation,
        parsed_exchange: ParsedStageResultExchange,
        execution_exchange_sha256: str,
        validation_inputs: ProjectStageValidationInputAuthority,
        _issuer: object,
    ) -> "ProjectStageTargetExecutionEvidence":
        if (
            _issuer is not _PROJECT_STAGE_TARGET_EXECUTION_EVIDENCE_ISSUER
            or type(context) is not BatchNodeExecutionContext
            or type(authorized) is not ProjectStageAuthorizedEntry
            or type(observation) is not ProjectStageTargetObservation
            or type(parsed_exchange) is not ParsedStageResultExchange
            or type(validation_inputs) is not ProjectStageValidationInputAuthority
        ):
            raise ProjectStageRecordError("target execution evidence issuer or inputs are invalid")
        exchange_sha256, result, context_authority_facts = (
            _validate_project_stage_execution_evidence_inputs(
                context=context,
                authorized=authorized,
                observation=observation,
                parsed_exchange=parsed_exchange,
                execution_exchange_sha256=execution_exchange_sha256,
            )
        )
        validation_inputs._reopen(context=context, authorized=authorized)
        validation_material = {
            "context_authority": list(context_authority_facts),
            "authorized_entry_sha256": authorized.authorized_entry_sha256,
            "target_observation_sha256": observation.observation_sha256,
            "result_member": {
                "sha256": parsed_exchange.result_payload_sha256,
                "byte_count": parsed_exchange.result_byte_count,
            },
            "generation_manifest_member": {
                "sha256": parsed_exchange.generation_manifest_payload_sha256,
                "byte_count": parsed_exchange.generation_manifest_byte_count,
            },
            "execution_exchange_sha256": exchange_sha256,
            "validation_input_authority_sha256": (validation_inputs.authority_sha256),
        }
        values = {
            "context": context,
            "authorized": authorized,
            "observation": observation,
            "wire_stage_result": result,
            "generation_manifest": parsed_exchange.generation_manifest,
            "execution_exchange_sha256": exchange_sha256,
            "validated_exchange_sha256": _digest(validation_material),
            "_validation_inputs": validation_inputs,
            "_issuer": _issuer,
        }
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance

    def reopen_validation_inputs(self) -> ProjectStageValidationInputAuthority:
        """We return retained immutable preimages bound to this current execution."""

        if (
            type(self) is not ProjectStageTargetExecutionEvidence
            or self._issuer is not _PROJECT_STAGE_TARGET_EXECUTION_EVIDENCE_ISSUER
            or type(self._validation_inputs) is not ProjectStageValidationInputAuthority
        ):
            raise ProjectStageRecordError("target execution evidence validation inputs are foreign")
        return self._validation_inputs._reopen(
            context=self.context,
            authorized=self.authorized,
        )


def _issue_project_stage_target_execution_evidence(
    *,
    context: BatchNodeExecutionContext,
    authorized: ProjectStageAuthorizedEntry,
    observation: ProjectStageTargetObservation,
    parsed_exchange: ParsedStageResultExchange,
    execution_exchange_sha256: str,
    validation_inputs: ProjectStageValidationInputAuthority,
) -> ProjectStageTargetExecutionEvidence:
    return ProjectStageTargetExecutionEvidence._issue(
        context=context,
        authorized=authorized,
        observation=observation,
        parsed_exchange=parsed_exchange,
        execution_exchange_sha256=execution_exchange_sha256,
        validation_inputs=validation_inputs,
        _issuer=_PROJECT_STAGE_TARGET_EXECUTION_EVIDENCE_ISSUER,
    )


@dataclass(frozen=True, slots=True)
class ProjectStageRunPlan:
    schema_name: Literal["mif-project-stage-run-plan"]
    schema_version: Literal[2]
    authority_layout_sha256: str
    request_sha256: str
    run_plan_id: str
    batch_id: str
    stage_id: str
    stage_version: str
    launch_mode: ProjectStageLaunchMode
    project_uri: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_catalogue_sha256: str
    script_bundle_sha256: str
    script_bundle_manifest_ref: AuthorityMemberRefV2
    script_bundle_manifest_sha256: str
    script_bundle_manifest_byte_count: int
    target_script_asset_id: str
    target_script_ref: AuthorityMemberRefV2
    target_script_sha256: str
    target_script_byte_count: int
    guard_script_asset_id: str
    guard_script_ref: AuthorityMemberRefV2
    guard_script_sha256: str
    guard_script_byte_count: int
    targets: tuple[ProjectStageAuthorizedEntry, ...]
    target_authorization_set_sha256: str
    request_member_set_sha256: str
    eligible_entry_set_sha256: str
    excluded_project_entry_ids: tuple[str, ...]
    excluded_entry_set_sha256: str
    shared_artifact_ref_sha256s: tuple[str, ...]
    shared_artifact_set_sha256: str
    plan_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectStageRunPlan fields when the record is constructed.

        The checks cover ``schema_name``, ``schema_version``, ``authority_layout_sha256``,
        ``stage_version``, ``project_uri``, ``script_bundle_sha256``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        _validate_schema(
            self.schema_name,
            self.schema_version,
            "mif-project-stage-run-plan",
            current_version=2,
        )
        layout = _sha(self.authority_layout_sha256, "authority_layout_sha256")
        for name in (
            "request_sha256",
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_catalogue_sha256",
            "script_bundle_sha256",
            "script_bundle_manifest_sha256",
            "target_script_sha256",
            "guard_script_sha256",
        ):
            _sha(getattr(self, name), name)
        for name in ("run_plan_id", "batch_id", "stage_id"):
            _safe_id(getattr(self, name), name)
        _text(self.stage_version, "stage_version")
        try:
            launch_mode = ProjectStageLaunchMode(self.launch_mode)
        except (TypeError, ValueError) as exc:
            raise ProjectStageRecordError("launch_mode is invalid") from exc
        object.__setattr__(self, "launch_mode", launch_mode)
        _project_uri(self.project_uri)
        if self.script_bundle_sha256 != self.script_bundle_manifest_sha256:
            raise ProjectStageRecordError("script bundle and manifest digest differ")
        _ref(
            self.script_bundle_manifest_ref,
            layout=layout,
            family="batch_project_runtime_evidence",
            label="script_bundle_manifest_ref",
        )
        _positive(self.script_bundle_manifest_byte_count, "script_bundle_manifest_byte_count")
        _asset(self.target_script_asset_id)
        _asset(self.guard_script_asset_id, guard=True)
        target_ref = _ref(
            self.target_script_ref, layout=layout, family="runtime_asset", label="target_script_ref"
        )
        guard_ref = _ref(
            self.guard_script_ref, layout=layout, family="runtime_asset", label="guard_script_ref"
        )
        if target_ref == guard_ref or self.target_script_asset_id == self.guard_script_asset_id:
            raise ProjectStageRecordError("target and guard assets must be distinct")
        _positive(self.target_script_byte_count, "target_script_byte_count")
        _positive(self.guard_script_byte_count, "guard_script_byte_count")
        targets = _tuple(self.targets, "targets")
        if (
            not targets
            or not all(type(item) is ProjectStageAuthorizedEntry for item in targets)
            or targets != tuple(sorted(targets, key=lambda item: item.project_entry_id))
        ):
            raise ProjectStageRecordError("authorized targets must be a non-empty canonical tuple")
        if len({item.project_entry_id for item in targets}) != len(targets):
            raise ProjectStageRecordError("authorized targets must be unique")
        if any(item.exchange.authority_layout_sha256 != layout for item in targets):
            raise ProjectStageRecordError(
                "authorized target exchange uses a different authority layout"
            )
        auth = _digest(
            {"authorized_entry_sha256s": [item.authorized_entry_sha256 for item in targets]}
        )
        if _sha(self.target_authorization_set_sha256, "target_authorization_set_sha256") != auth:
            raise ProjectStageRecordError("target authorization set digest is invalid")
        request_members = [
            {
                "request_identity_sha256": item.exchange.request_identity_sha256,
                "request_ref": item.exchange.request_ref.to_mapping(),
                "request_envelope_sha256": item.exchange.request_envelope_sha256,
                "request_envelope_byte_count": (item.exchange.request_envelope_byte_count),
            }
            for item in targets
        ]
        if _sha(self.request_member_set_sha256, "request_member_set_sha256") != _digest(
            {"request_members": request_members}
        ):
            raise ProjectStageRecordError("request member set digest is invalid")
        eligible = [item.project_entry_id for item in targets]
        if len(set(eligible)) != len(eligible) or _sha(
            self.eligible_entry_set_sha256, "eligible_entry_set_sha256"
        ) != _digest({"eligible_project_entry_ids": eligible}):
            raise ProjectStageRecordError("eligible entry set digest is invalid")
        excluded = _sorted_unique_text(
            self.excluded_project_entry_ids, "excluded_project_entry_ids"
        )
        if set(excluded) & set(eligible):
            raise ProjectStageRecordError("eligible and excluded entries overlap")
        if _sha(self.excluded_entry_set_sha256, "excluded_entry_set_sha256") != _digest(
            {"excluded_project_entry_ids": list(excluded)}
        ):
            raise ProjectStageRecordError("excluded entry set digest is invalid")
        shared = _sorted_unique_sha(self.shared_artifact_ref_sha256s, "shared_artifact_ref_sha256s")
        if _sha(self.shared_artifact_set_sha256, "shared_artifact_set_sha256") != _digest(
            {"artifact_ref_sha256s": list(shared)}
        ):
            raise ProjectStageRecordError("shared artifact set digest is invalid")
        _validate_digest(self, "plan_sha256")

    @classmethod
    def create(cls, **values: object) -> "ProjectStageRunPlan":
        targets = values.get("targets")
        excluded = values.get("excluded_project_entry_ids")
        shared = values.get("shared_artifact_ref_sha256s")
        if type(targets) is not tuple or type(excluded) is not tuple or type(shared) is not tuple:
            raise ProjectStageRecordError("plan aggregate fields must be tuples")
        request_members = [
            {
                "request_identity_sha256": item.exchange.request_identity_sha256,
                "request_ref": item.exchange.request_ref.to_mapping(),
                "request_envelope_sha256": item.exchange.request_envelope_sha256,
                "request_envelope_byte_count": (item.exchange.request_envelope_byte_count),
            }
            for item in targets
        ]  # type: ignore[union-attr]
        complete = {
            "schema_name": "mif-project-stage-run-plan",
            "schema_version": 2,
            **values,
            "target_authorization_set_sha256": _digest({"authorized_entry_sha256s": [item.authorized_entry_sha256 for item in targets]}),  # type: ignore[union-attr]
            "request_member_set_sha256": _digest({"request_members": request_members}),
            "eligible_entry_set_sha256": _digest({"eligible_project_entry_ids": [item.project_entry_id for item in targets]}),  # type: ignore[union-attr]
            "excluded_entry_set_sha256": _digest({"excluded_project_entry_ids": list(excluded)}),
            "shared_artifact_set_sha256": _digest({"artifact_ref_sha256s": list(shared)}),
        }
        return _create(cls, complete, "plan_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectStageRunPlan":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "run plan"))
        raw["launch_mode"] = ProjectStageLaunchMode(raw["launch_mode"])
        layout = _sha(raw["authority_layout_sha256"], "authority_layout_sha256")
        for name, family in (
            ("script_bundle_manifest_ref", "batch_project_runtime_evidence"),
            ("target_script_ref", "runtime_asset"),
            ("guard_script_ref", "runtime_asset"),
        ):
            raw[name] = _ref(raw[name], layout=layout, family=family, label=name)
        raw["targets"] = tuple(
            ProjectStageAuthorizedEntry.from_mapping(item)
            for item in _from_array(raw["targets"], "targets")
        )
        for name in ("excluded_project_entry_ids", "shared_artifact_ref_sha256s"):
            raw[name] = _from_array(raw[name], name)
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PublishedProjectStageRunPlan:
    plan: ProjectStageRunPlan
    plan_ref: AuthorityMemberRefV2
    payload_sha256: str
    byte_count: int
    target_authorization_set_sha256: str

    def __post_init__(self) -> None:
        _require_typed_record(self.plan, ProjectStageRunPlan, "published plan")
        _ref(
            self.plan_ref,
            layout=self.plan.authority_layout_sha256,
            family="batch_project_runtime_evidence",
            label="plan_ref",
        )
        payload = _sha(self.payload_sha256, "payload_sha256")
        count = _positive(self.byte_count, "byte_count")
        expected_payload, expected_count = _payload_facts(self.plan)
        if payload != expected_payload or count != expected_count:
            raise ProjectStageRecordError("published plan payload facts differ")
        if self.target_authorization_set_sha256 != self.plan.target_authorization_set_sha256:
            raise ProjectStageRecordError("published target authorization differs")


class _ProjectStageUnattestableTargetError(ProjectStageRecordError):
    """Private Slice-7/8 control seam for unresolved saved-state identity."""

    def __init__(self, authorized: ProjectStageAuthorizedEntry, observation: object):
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageTargetObservation,
        )

        if (
            type(authorized) is not ProjectStageAuthorizedEntry
            or type(observation) is not ProjectStageTargetObservation
            or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
            or observation.saved_state_disposition != "unattestable"
        ):
            raise TypeError("unattestable target error requires its exact authorized observation")
        self.authorized_entry_sha256 = authorized.authorized_entry_sha256
        self.observation = observation
        self.reason_code: Literal["project_stage_saved_state_unattestable"] = (
            "project_stage_saved_state_unattestable"
        )
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class PreparedProjectStageRunDescriptor:
    schema_name: Literal["mif-project-stage-prepared-run"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    runtime_composition_binding_sha256: str
    workspace_storage_scope_sha256: str
    project_uri: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    active_mutation_run_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    run_plan_ref: AuthorityMemberRefV2
    run_plan_sha256: str
    run_plan_byte_count: int
    target_authorization_set_sha256: str
    request_member_set_sha256: str
    script_bundle_sha256: str
    script_bundle_manifest_ref: AuthorityMemberRefV2
    script_bundle_manifest_sha256: str
    script_bundle_manifest_byte_count: int
    target_script_asset_id: str
    target_script_ref: AuthorityMemberRefV2
    target_script_sha256: str
    target_script_byte_count: int
    guard_script_asset_id: str
    guard_script_ref: AuthorityMemberRefV2
    guard_script_sha256: str
    guard_script_byte_count: int
    fencing_token_sha256: str
    launch_nonce_sha256: str
    launch_request_identity_sha256: str
    process_launch_ref: AuthorityMemberRefV2
    stdout_log_ref: AuthorityMemberRefV2
    stderr_log_ref: AuthorityMemberRefV2
    prepared_descriptor_sha256: str

    def __post_init__(self) -> None:
        _validate_schema(self.schema_name, self.schema_version, "mif-project-stage-prepared-run")
        layout = _sha(self.authority_layout_sha256, "authority_layout_sha256")
        for item in fields(self):
            if item.name.endswith("_sha256"):
                _sha(getattr(self, item.name), item.name)
        _project_uri(self.project_uri)
        for name in (
            "run_plan_byte_count",
            "script_bundle_manifest_byte_count",
            "target_script_byte_count",
            "guard_script_byte_count",
        ):
            _positive(getattr(self, name), name)
        expected_composition = _digest(
            {
                "kind": "project_stage_runtime_composition/v1",
                "authority_layout_sha256": layout,
                "workspace_storage_scope_sha256": self.workspace_storage_scope_sha256,
            }
        )
        if self.runtime_composition_binding_sha256 != expected_composition:
            raise ProjectStageRecordError("runtime composition binding digest is invalid")
        expected_launch = _digest(
            {
                "active_mutation_run_sha256": self.active_mutation_run_sha256,
                "launch_nonce_sha256": self.launch_nonce_sha256,
                "run_plan_sha256": self.run_plan_sha256,
            }
        )
        if self.launch_request_identity_sha256 != expected_launch:
            raise ProjectStageRecordError("launch request identity digest is invalid")
        for name, family in (
            ("run_plan_ref", "batch_project_runtime_evidence"),
            ("script_bundle_manifest_ref", "batch_project_runtime_evidence"),
            ("target_script_ref", "runtime_asset"),
            ("guard_script_ref", "runtime_asset"),
            ("process_launch_ref", "runtime_exchange"),
            ("stdout_log_ref", "runtime_log"),
            ("stderr_log_ref", "runtime_log"),
        ):
            _ref(getattr(self, name), layout=layout, family=family, label=name)
        if self.script_bundle_sha256 != self.script_bundle_manifest_sha256:
            raise ProjectStageRecordError("script bundle and manifest digest differ")
        _asset(self.target_script_asset_id)
        _asset(self.guard_script_asset_id, guard=True)
        if (
            self.target_script_ref == self.guard_script_ref
            or self.stdout_log_ref == self.stderr_log_ref
        ):
            raise ProjectStageRecordError("prepared descriptor refs must be distinct")
        _validate_digest(self, "prepared_descriptor_sha256")

    @classmethod
    def create(cls, **values: object) -> "PreparedProjectStageRunDescriptor":
        layout = values.get("authority_layout_sha256")
        workspace = values.get("workspace_storage_scope_sha256")
        active = values.get("active_mutation_run_sha256")
        nonce = values.get("launch_nonce_sha256")
        plan = values.get("run_plan_sha256")
        complete = {
            "schema_name": "mif-project-stage-prepared-run",
            "schema_version": 1,
            **values,
            "runtime_composition_binding_sha256": _digest(
                {
                    "kind": "project_stage_runtime_composition/v1",
                    "authority_layout_sha256": layout,
                    "workspace_storage_scope_sha256": workspace,
                }
            ),
            "launch_request_identity_sha256": _digest(
                {
                    "active_mutation_run_sha256": active,
                    "launch_nonce_sha256": nonce,
                    "run_plan_sha256": plan,
                }
            ),
        }
        return _create(cls, complete, "prepared_descriptor_sha256")

    def to_mapping(self) -> dict[str, object]:
        return _mapping(self)

    @classmethod
    def from_mapping(cls, value: object) -> "PreparedProjectStageRunDescriptor":
        raw = dict(_closed(value, frozenset(cls.__dataclass_fields__), "prepared descriptor"))
        layout = _sha(raw["authority_layout_sha256"], "authority_layout_sha256")
        for name, family in (
            ("run_plan_ref", "batch_project_runtime_evidence"),
            ("script_bundle_manifest_ref", "batch_project_runtime_evidence"),
            ("target_script_ref", "runtime_asset"),
            ("guard_script_ref", "runtime_asset"),
            ("process_launch_ref", "runtime_exchange"),
            ("stdout_log_ref", "runtime_log"),
            ("stderr_log_ref", "runtime_log"),
        ):
            raw[name] = _ref(raw[name], layout=layout, family=family, label=name)
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PublishedPreparedProjectStageRunDescriptor:
    descriptor: PreparedProjectStageRunDescriptor
    descriptor_ref: AuthorityMemberRefV2
    payload_sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _require_typed_record(
            self.descriptor,
            PreparedProjectStageRunDescriptor,
            "published descriptor",
        )
        _ref(
            self.descriptor_ref,
            layout=self.descriptor.authority_layout_sha256,
            family="batch_project_runtime_evidence",
            label="descriptor_ref",
        )
        payload = _sha(self.payload_sha256, "payload_sha256")
        count = _positive(self.byte_count, "byte_count")
        expected_payload, expected_count = _payload_facts(self.descriptor)
        if payload != expected_payload or count != expected_count:
            raise ProjectStageRecordError("published descriptor payload facts differ")


@dataclass(frozen=True, slots=True)
class QuPathProjectLaunchDescriptor:
    mode: ProjectStageLaunchMode
    project_file: Path
    target_script_file: Path
    target_script_asset_id: str
    eligible_project_entry_ids: tuple[str, ...]
    excluded_project_entry_ids: tuple[str, ...]
    run_plan_sha256: str

    def __post_init__(self) -> None:
        try:
            mode = ProjectStageLaunchMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ProjectStageRecordError("launch mode is invalid") from exc
        object.__setattr__(self, "mode", mode)
        for name in ("project_file", "target_script_file"):
            path = getattr(self, name)
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise ProjectStageRecordError(f"{name} must be one absolute project-owned path")
        _asset(self.target_script_asset_id)
        eligible = _sorted_unique_text(
            self.eligible_project_entry_ids, "eligible_project_entry_ids"
        )
        excluded = _sorted_unique_text(
            self.excluded_project_entry_ids, "excluded_project_entry_ids"
        )
        if not eligible or set(eligible) & set(excluded):
            raise ProjectStageRecordError("eligible/excluded entry sets are invalid")
        _sha(self.run_plan_sha256, "run_plan_sha256")


@dataclass(frozen=True, slots=True)
class ProjectStageManualReview:
    reason_code: str
    project_uri: str
    project_identity_sha256: str
    run_plan_sha256: str
    prepared_descriptor_sha256: str
    retained_record_refs: tuple[AuthorityMemberRefV2, ...]
    in_process_authority_released: bool
    operator_message: str

    def __post_init__(self) -> None:
        _safe_id(self.reason_code, "reason_code")
        _project_uri(self.project_uri)
        for name in ("project_identity_sha256", "run_plan_sha256", "prepared_descriptor_sha256"):
            _sha(getattr(self, name), name)
        refs = _tuple(self.retained_record_refs, "retained_record_refs")
        if not refs or not all(type(item) is AuthorityMemberRefV2 for item in refs):
            raise ProjectStageRecordError("retained_record_refs must be typed")
        if refs != tuple(sorted(refs, key=lambda item: (item.family, item.key_sha256))) or len(
            set(refs)
        ) != len(refs):
            raise ProjectStageRecordError("retained_record_refs must be sorted and unique")
        if type(self.in_process_authority_released) is not bool:
            raise ProjectStageRecordError("in_process_authority_released must be boolean")
        _text(self.operator_message, "operator_message")


class ProjectStageManualReviewRequired(RuntimeError):
    def __init__(self, review: ProjectStageManualReview):
        if type(review) is not ProjectStageManualReview:
            raise TypeError("review must be ProjectStageManualReview")
        super().__init__(review.operator_message)
        self.review = review
        self.add_note("Retained project-stage evidence:\n" + json.dumps({
            "reason_code": review.reason_code,
            "project_uri": review.project_uri,
            "project_identity_sha256": review.project_identity_sha256,
            "run_plan_sha256": review.run_plan_sha256,
            "prepared_descriptor_sha256": review.prepared_descriptor_sha256,
            "retained_records": [ref.physical_name for ref in review.retained_record_refs],
            "current_python_authority_released": review.in_process_authority_released,
        }, indent=2))


class ProjectStageCleanupError(RuntimeError):
    def __init__(
        self,
        review: ProjectStageManualReview,
        *,
        failed_step: str,
        safe_cleanup: Literal[False] = False,
    ):
        if type(review) is not ProjectStageManualReview or safe_cleanup is not False:
            raise TypeError("cleanup error requires an unsafe typed manual review")
        self.review = review
        self.failed_step = _safe_id(failed_step, "failed_step")
        self.safe_cleanup = False
        super().__init__(f"project-stage cleanup failed at {self.failed_step}")

    @property
    def retry_session(self) -> "ProjectStageRunSession | None":
        """Return the exact session accepted by the public abort retry path."""

        session = getattr(self, "_retry_session", None)
        if session is not None and type(session) is not ProjectStageRunSession:
            raise ProjectStageLifecycleError("project-stage cleanup retry session is invalid")
        return session


class _ProjectStageUnresolvedLifecycle:
    """Runtime-issued exact ownership for validation and unresolved cleanup."""

    __slots__ = (
        "_runtime",
        "_publication",
        "_prepared_request",
        "_completion",
        "_project_lease",
        "_workspace_lease",
        "_terminal_committer",
        "_workspace_operation",
        "_workspace_use",
        "_runtime_token",
        "_review",
        "_next",
        "_state",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProjectStageLifecycleError(
            "unresolved lifecycles are issued only by ProjectStageRuntime"
        )

    @classmethod
    def _issue(
        cls,
        *,
        runtime: object,
        publication: object,
        prepared_request: object,
        completion: object,
        project_lease: object,
        workspace_lease: object,
        terminal_committer: object,
        _issuer: object,
    ) -> "_ProjectStageUnresolvedLifecycle":
        from .batch_state import (
            BatchTerminalCommitter,
            BatchWorkspaceLease,
            BatchWorkspaceLeaseUse,
        )
        from mif_pipeline.pipeline_core.runtime_lifecycle import RuntimeUseToken
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLease
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageProcessCompletion,
        )

        if (
            _issuer is not _PROJECT_STAGE_RUNTIME_ISSUER
            or type(runtime) is not ProjectStageRuntime
            or type(publication) is not PublishedProjectStageRunPlan
            or type(completion) is not ProjectStageProcessCompletion
            or not isinstance(project_lease, ProjectMutationLease)
            or not isinstance(workspace_lease, BatchWorkspaceLease)
            or not isinstance(terminal_committer, BatchTerminalCommitter)
        ):
            raise ProjectStageLifecycleError("unresolved lifecycle authority is invalid")
        try:
            runtime_token = runtime._runtime_lifecycle.acquire("batch-state-operation")
            workspace_operation = runtime._state_store._lease_operation(workspace_lease)
            workspace_use = workspace_operation.__enter__()
        except BaseException:
            if "runtime_token" in locals():
                runtime_token.release()
            raise
        if (
            type(runtime_token) is not RuntimeUseToken
            or type(workspace_use) is not BatchWorkspaceLeaseUse
            or not runtime_token.is_live
            or not workspace_use.is_live
        ):
            workspace_operation.__exit__(None, None, None)
            runtime_token.release()
            raise ProjectStageLifecycleError(
                "unresolved lifecycle could not retain exact resources"
            )
        value = object.__new__(cls)
        value._runtime = runtime
        value._publication = publication
        value._prepared_request = prepared_request
        value._completion = completion
        value._project_lease = project_lease
        value._workspace_lease = workspace_lease
        value._terminal_committer = terminal_committer
        value._workspace_operation = workspace_operation
        value._workspace_use = workspace_use
        value._runtime_token = runtime_token
        value._review = None
        value._next = 0
        value._state = "validating"
        return value

    @property
    def state(self) -> str:
        return self._state

    def _authenticate_registration(self) -> None:
        with self._runtime._lock:
            registered = self._runtime._validation_lifecycles.get(id(self._completion))
        if registered is not self:
            raise ProjectStageLifecycleError(
                "unresolved lifecycle registration is stale or foreign"
            )

    def close_validation(self) -> None:
        """Release validation-only guards without closing project authority."""

        self._authenticate_registration()
        if self._state != "validating":
            raise ProjectStageLifecycleError("validation lifecycle is not open")
        self._release_workspace_pin()
        if self._workspace_use.is_live:
            raise ProjectStageLifecycleError("validation workspace pin remains live")
        self._release_runtime_borrow()
        if self._runtime_token.is_live:
            raise ProjectStageLifecycleError("validation runtime borrow remains live")
        self._state = "validation_closed"
        self._runtime._forget_result_validation_lifecycle(self)

    def prepare_unresolved(self, unresolved: _ProjectStageUnattestableTargetError) -> None:
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageProcessEvidenceCapability,
            ProjectStageTargetObservation,
        )

        self._authenticate_registration()
        if (
            self._state != "validating"
            or type(unresolved) is not _ProjectStageUnattestableTargetError
        ):
            raise ProjectStageLifecycleError(
                "unresolved transition requires the exact validation signal"
            )
        with self._runtime._lock:
            completed = self._runtime._completed_processes.get(id(self._completion))
        if completed is None:
            raise ProjectStageLifecycleError("unresolved completion authority is unavailable")
        published_descriptor = completed[1]
        descriptor = completed[2]
        plan = self._publication.plan
        process_evidence = self._completion.process_evidence
        observation = unresolved.observation
        targets = tuple(
            item
            for item in plan.targets
            if item.authorized_entry_sha256 == unresolved.authorized_entry_sha256
        )
        if (
            completed[0] is not self._completion
            or completed[3] is not self._project_lease
            or completed[4] is not self._workspace_lease
            or self._completion.disposition != "released_child"
            or type(process_evidence) is not ProjectStageProcessEvidenceCapability
            or type(observation) is not ProjectStageTargetObservation
            or len(targets) != 1
            or published_descriptor.descriptor != descriptor
            or descriptor.project_uri != plan.project_uri
            or descriptor.project_identity_sha256 != plan.project_identity_sha256
            or descriptor.run_plan_sha256 != plan.plan_sha256
            or process_evidence.attempt.run_plan_sha256 != plan.plan_sha256
            or process_evidence.attempt.prepared_descriptor_sha256
            != descriptor.prepared_descriptor_sha256
            or process_evidence.attempt.project_identity_sha256 != plan.project_identity_sha256
            or observation.process_evidence_sha256 != process_evidence.evidence_set_sha256
        ):
            raise ProjectStageRecordError(
                "unattestable cleanup authority is stale or foreign"
            ) from unresolved
        target = targets[0]
        if (
            observation.node_id != target.node_id
            or observation.attempt_id != target.attempt_id
            or observation.attempt_authorization_sha256 != target.attempt_authorization_sha256
            or observation.project_entry_id != target.project_entry_id
            or observation.sample_id != target.sample_id
            or observation.slide_id != target.slide_id
            or observation.result_ref != target.exchange.result_ref
            or observation.generation_manifest_ref != target.exchange.generation_manifest_ref
            or observation.saved_state_before_sha256 != target.saved_state_before_sha256
            or observation.saved_state_disposition != "unattestable"
            or observation.saved_state_after_sha256 is not None
        ):
            raise ProjectStageRecordError(
                "unattestable cleanup target authority is stale or foreign"
            ) from unresolved
        refs = (
            published_descriptor.descriptor_ref,
            descriptor.run_plan_ref,
            process_evidence.attempt_ref,
            process_evidence.launch_ref,
            process_evidence.drain_ref,
            process_evidence.drain.stdout_log_ref,
            process_evidence.drain.stderr_log_ref,
            observation.result_ref,
            observation.generation_manifest_ref,
        )
        retained_refs = tuple(
            sorted(
                {(item.family, item.key_sha256): item for item in refs}.values(),
                key=lambda item: (item.family, item.key_sha256),
            )
        )
        self._review = ProjectStageManualReview(
            reason_code=unresolved.reason_code,
            project_uri=plan.project_uri,
            project_identity_sha256=plan.project_identity_sha256,
            run_plan_sha256=plan.plan_sha256,
            prepared_descriptor_sha256=descriptor.prepared_descriptor_sha256,
            retained_record_refs=retained_refs,
            in_process_authority_released=False,
            operator_message=(
                "The released QuPath child changed project state that could not "
                "be authenticated. Review the retained project-stage evidence."
            ),
        )
        self._state = "unresolved"

    def _seal_committer(self) -> None:
        self._terminal_committer.seal()

    def _close_project_acquisition(self) -> None:
        self._runtime._close_unresolved_project_acquisition(self)

    def _release_workspace_pin(self) -> None:
        if self._workspace_use.is_live:
            self._workspace_operation.__exit__(None, None, None)

    def _release_runtime_borrow(self) -> None:
        self._runtime_token.release()

    def _project_acquisition_is_closed(self) -> bool:
        lease = self._project_lease
        with self._runtime._lock:
            active = self._runtime._active_project_leases.get(id(lease))
        return (
            not lease.is_live
            and active is None
            and all(lock.stream is None for lock in lease._held_locks)
            and all(
                pin._parent_descriptor is None and pin._win32_parent is None
                for pin in lease._held_path_pins
            )
        )

    def _step_postcondition(self, name: str) -> bool:
        if name == "seal_committer":
            return self._terminal_committer._is_unused_sealed
        if name == "close_project_acquisition":
            return self._project_acquisition_is_closed()
        if name == "release_workspace_pin":
            return not self._workspace_use.is_live
        if name == "release_runtime_borrow":
            return not self._runtime_token.is_live
        raise ProjectStageLifecycleError("project-stage cleanup postcondition is unknown")

    def close(self) -> None:
        self._authenticate_registration()
        if self._state == "unresolved_closed":
            assert self._review is not None
            raise ProjectStageManualReviewRequired(self._review)
        if self._review is None or self._state not in {
            "unresolved",
            "cleanup_failed(seal_committer)",
            "cleanup_failed(close_project_acquisition)",
            "cleanup_failed(release_workspace_pin)",
            "cleanup_failed(release_runtime_borrow)",
        }:
            raise ProjectStageLifecycleError("unresolved lifecycle is not prepared for cleanup")
        steps = (
            ("seal_committer", self._seal_committer),
            ("close_project_acquisition", self._close_project_acquisition),
            ("release_workspace_pin", self._release_workspace_pin),
            ("release_runtime_borrow", self._release_runtime_borrow),
        )
        while self._next < len(steps):
            name, operation = steps[self._next]
            try:
                operation()
                if not self._step_postcondition(name):
                    raise ProjectStageLifecycleError(
                        f"project-stage cleanup postcondition failed: {name}"
                    )
            except BaseException as exc:
                self._state = f"cleanup_failed({name})"
                failure = ProjectStageCleanupError(
                    self._review, failed_step=name, safe_cleanup=False
                )
                failure._unresolved_cleanup = self
                raise failure from exc
            self._next += 1
        if not all(self._step_postcondition(name) for name, _operation in steps):
            raise ProjectStageLifecycleError("project-stage cleanup authority remains live")
        self._review = replace(self._review, in_process_authority_released=True)
        self._state = "unresolved_closed"
        self._runtime._finish_unresolved_result_lifecycle(self)
        raise ProjectStageManualReviewRequired(self._review)


@dataclass(frozen=True, slots=True)
class _ValidatedProjectStageResultSet:
    publication: object
    prepared_request: object
    completion: object
    outcomes: tuple[object, ...]
    project_lease: object
    workspace_lease: object
    plan: object
    snapshot: object
    catalogue: object
    observations: tuple[ProjectStageTargetObservation, ...]
    expected_state_head_sha256: str


class ProjectStageLifecycleError(RuntimeError):
    """The one-shot project-stage session state transition is invalid."""

    @property
    def retry_session(self) -> "ProjectStageRunSession | None":
        """Return the exact session accepted by the public abort retry path."""

        session = getattr(self, "_retry_session", None)
        if session is not None and type(session) is not ProjectStageRunSession:
            raise ProjectStageLifecycleError("project-stage lifecycle retry session is invalid")
        return session


class _ProjectStageOuterCleanupFailure(ProjectStageLifecycleError):
    """Private signal retaining the first unfinished outer cleanup step."""

    def __init__(self, failed_step: str):
        self.failed_step = _safe_id(failed_step, "failed_step")
        super().__init__(f"project-stage outer cleanup failed at {self.failed_step}")


@dataclass(frozen=True, slots=True)
class _RetainedProjectStageCompletion:
    """Original native completion reopened for a still-pending owned operation."""

    active: object
    publication: PublishedProjectStageRunPlan
    published_descriptor: PublishedPreparedProjectStageRunDescriptor
    completion: ProjectStageProcessCompletion
    request_envelopes: tuple[bytes, ...]
    mutation: ProjectStageMutationEvidenceRecord | None


class _ProjectStageAcquisitionLifecycle:
    """Inspectable step-indexed ownership for one outer project acquisition."""

    __slots__ = (
        "_runtime",
        "_session",
        "_workspace_lease",
        "_runtime_token",
        "_recovery",
        "_unlaunched",
        "_workspace_operation",
        "_workspace_use",
        "_project_operation",
        "_project_lease",
        "_project_operation_closed",
        "_workspace_operation_closed",
        "_next",
        "_state",
    )

    def __init__(
        self,
        runtime: "ProjectStageRuntime",
        session: object,
        workspace_lease: object,
        recovery: _RetainedProjectStageCompletion | None = None,
        unlaunched: ProjectMutationLeaseRecord | None = None,
    ) -> None:
        self._runtime = runtime
        self._recovery = recovery
        self._unlaunched = unlaunched
        self._session = session
        self._workspace_lease = workspace_lease
        self._runtime_token = None
        self._workspace_operation = None
        self._workspace_use = None
        self._project_operation = None
        self._project_lease = None
        self._project_operation_closed = False
        self._workspace_operation_closed = False
        self._next = 0
        self._state = "new"

    def __enter__(self) -> object:
        if self._state != "new":
            raise ProjectStageLifecycleError("project-stage acquisition lifecycle is one-shot")
        runtime = self._runtime
        with runtime._lock:
            issued = runtime._sessions.get(id(self._session))
        if (
            issued is None
            or issued[0] is not self._session
            or issued[4] is not self._workspace_lease
        ):
            raise ProjectStageLifecycleError("project-stage acquisition session is foreign")
        self._state = "entering"
        try:
            self._runtime_token = runtime._runtime_lifecycle.acquire("batch-state-operation")
            self._workspace_operation = runtime._state_store._lease_operation(self._workspace_lease)
            self._workspace_use = self._workspace_operation.__enter__()
            self._project_operation = (
                runtime._provider.acquire(self._session)
                if self._recovery is None and self._unlaunched is None
                else runtime._provider._recover_project_stage(
                    self._session,
                    stale_active_record_sha256=(
                        self._unlaunched.lease_record_sha256 if self._unlaunched is not None
                        else self._recovery.active.lease_record_sha256
                    ),
                    runtime=runtime,
                    unlaunched=self._unlaunched,
                )
            )
            project_lease = self._project_operation.__enter__()
            self._project_lease = project_lease
            if (
                project_lease.operation != "project_stage"
                or project_lease._provider is not runtime._provider
            ):
                raise ProjectStageLifecycleError("project-stage project lease is foreign")
            with runtime._lock:
                runtime._active_project_leases[id(project_lease)] = (
                    project_lease,
                    self._session,
                    issued[1],
                    issued[3],
                    self._workspace_lease,
                )
            self._state = "open"
            return project_lease
        except BaseException as primary:
            self._close_after_primary(primary)
            raise

    def _close_after_primary(self, primary: BaseException) -> None:
        first_cleanup: BaseException | None = None
        unfinished_cleanup: BaseException | None = None
        while not self.is_closed:
            try:
                self.close()
            except BaseException as cleanup:
                if first_cleanup is None:
                    first_cleanup = cleanup
                if type(cleanup) is not _ProjectStageOuterCleanupFailure or not self._postcondition(
                    cleanup.failed_step
                ):
                    unfinished_cleanup = cleanup
                    break
            else:
                break
        governing_cleanup = unfinished_cleanup or first_cleanup
        if governing_cleanup is not None:
            if first_cleanup is not None and first_cleanup is not governing_cleanup:
                earlier = (
                    first_cleanup.__cause__
                    if first_cleanup.__cause__ is not None
                    else first_cleanup
                )
                try:
                    governing_cleanup.add_note(
                        "earlier project-stage cleanup also reported: "
                        f"{type(earlier).__name__}: "
                        f"{str(earlier)[:240]}"
                    )
                except BaseException:
                    pass
            if type(governing_cleanup) is _ProjectStageOuterCleanupFailure:
                governing_cleanup._preparation_failure = primary
            try:
                governing_cleanup.add_note(
                    "project-stage preparation also failed: "
                    f"{type(primary).__name__}: {str(primary)[:240]}"
                )
            except BaseException:
                pass
            raise governing_cleanup

    def _project_is_closed(self) -> bool:
        lease = self._project_lease
        if lease is None:
            return self._project_operation is None or self._project_operation_closed
        return (
            self._project_operation_closed
            and not lease.is_live
            and all(lock.stream is None for lock in lease._held_locks)
            and all(
                pin._parent_descriptor is None and pin._win32_parent is None
                for pin in lease._held_path_pins
            )
        )

    def _workspace_is_released(self) -> bool:
        use = self._workspace_use
        return use is None or (self._workspace_operation_closed and not use.is_live)

    def _runtime_is_released(self) -> bool:
        token = self._runtime_token
        return token is None or not token.is_live

    def _registration_is_released(self) -> bool:
        lease = self._project_lease
        if lease is None:
            return True
        with self._runtime._lock:
            return self._runtime._active_project_leases.get(id(lease)) is None

    def _close_project(self) -> None:
        operation = self._project_operation
        if operation is None or self._project_operation_closed:
            return
        try:
            operation.__exit__(None, None, None)
        except BaseException:
            lease = self._project_lease
            if lease is not None and (
                not lease.is_live
                and all(lock.stream is None for lock in lease._held_locks)
                and all(
                    pin._parent_descriptor is None and pin._win32_parent is None
                    for pin in lease._held_path_pins
                )
            ):
                self._project_operation_closed = True
            raise
        self._project_operation_closed = True

    def _release_workspace(self) -> None:
        operation = self._workspace_operation
        use = self._workspace_use
        if operation is None or self._workspace_operation_closed:
            return
        try:
            operation.__exit__(None, None, None)
        except BaseException:
            if use is not None and not use.is_live:
                self._workspace_operation_closed = True
            raise
        self._workspace_operation_closed = True

    def _release_runtime(self) -> None:
        token = self._runtime_token
        if token is not None and token.is_live:
            token.release()

    def _release_registration(self) -> None:
        lease = self._project_lease
        if lease is None:
            return
        with self._runtime._lock:
            active = self._runtime._active_project_leases.get(id(lease))
            if active is not None:
                if active[0] is not lease or active[1] is not self._session:
                    raise ProjectStageLifecycleError(
                        "project-stage acquisition registration changed"
                    )
                self._runtime._active_project_leases.pop(id(lease))

    def _postcondition(self, step: str) -> bool:
        return {
            "close_outer_project_acquisition": self._project_is_closed,
            "release_outer_workspace_pin": self._workspace_is_released,
            "release_outer_runtime_borrow": self._runtime_is_released,
            "release_outer_facade_registration": self._registration_is_released,
        }[step]()

    @property
    def is_closed(self) -> bool:
        return all(
            self._postcondition(step)
            for step in (
                "close_outer_project_acquisition",
                "release_outer_workspace_pin",
                "release_outer_runtime_borrow",
                "release_outer_facade_registration",
            )
        )

    @property
    def is_ready_for_session_finish(self) -> bool:
        return all(
            self._postcondition(step)
            for step in (
                "release_outer_facade_registration",
                "close_outer_project_acquisition",
                "release_outer_workspace_pin",
            )
        )

    def _steps(self):
        return (
            ("release_outer_facade_registration", self._release_registration),
            ("close_outer_project_acquisition", self._close_project),
            ("release_outer_workspace_pin", self._release_workspace),
            ("release_outer_runtime_borrow", self._release_runtime),
        )

    def _close_through(self, limit: int) -> None:
        steps = self._steps()
        while self._next < limit:
            name, operation = steps[self._next]
            try:
                operation()
                if not self._postcondition(name):
                    raise ProjectStageLifecycleError(
                        f"project-stage outer cleanup postcondition failed: {name}"
                    )
            except BaseException as exc:
                if self._postcondition(name):
                    self._next += 1
                self._state = f"cleanup_failed({name})"
                raise _ProjectStageOuterCleanupFailure(name) from exc
            self._next += 1

    def close_for_session_finish(self) -> None:
        self._close_through(3)
        if not self.is_ready_for_session_finish:
            raise ProjectStageLifecycleError(
                "project-stage outer acquisition is not ready for session finish"
            )
        self._state = "awaiting_session_finish"

    def release_runtime_for_session_finish(self) -> None:
        if not self.is_ready_for_session_finish:
            raise ProjectStageLifecycleError(
                "project-stage runtime release preceded facade deregistration"
            )
        self._close_through(4)
        if not self.is_closed:
            raise ProjectStageLifecycleError("project-stage outer acquisition remains live")
        self._state = "closed"

    def close(self) -> None:
        self._close_through(4)
        if not self.is_closed:
            raise ProjectStageLifecycleError("project-stage outer acquisition remains live")
        self._state = "closed"

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, traceback
        if exc is None:
            self.close()
            return
        self._close_after_primary(exc)


class ProjectStageRunSession:
    """Non-serializable owner of one live project-stage run."""

    __slots__ = (
        "_runtime",
        "_prepared_request",
        "_publication",
        "_published_descriptor",
        "_launch_descriptor",
        "_acquisition_context",
        "_acquisition_lifecycle",
        "_acquisition_session",
        "_project_lease",
        "_launch",
        "_workspace_lease",
        "_terminal_committer",
        "_cancellation_event",
        "_cancellation_reason",
        "_completion",
        "_results",
        "_unresolved_cleanup",
        "_manual_review",
        "_acquisition_closed",
        "_terminalization_started",
        "_failure",
        "_failure_phase",
        "_failure_cleanup_next",
        "_cleanup_retry_in_progress",
        "_pending_terminal_results",
        "_project_completion_confirmed",
        "_state",
        "_condition",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProjectStageLifecycleError(
            "project-stage run sessions are issued only by ProjectStageRuntime"
        )

    @classmethod
    def _issue(
        cls,
        *,
        runtime: ProjectStageRuntime,
        prepared_request: object,
        publication: PublishedProjectStageRunPlan | None,
        published_descriptor: PublishedPreparedProjectStageRunDescriptor | None,
        launch_descriptor: QuPathProjectLaunchDescriptor | None,
        acquisition_context: object,
        acquisition_session: object,
        project_lease: object,
        launch: object,
        workspace_lease: object,
        terminal_committer: object,
        _preparation_cleanup: bool = False,
        _preparation_failure: BaseException | None = None,
        _preparation_cleanup_step: str | None = None,
        _issuer: object,
    ) -> "ProjectStageRunSession":
        if _issuer is not _PROJECT_STAGE_RUNTIME_ISSUER:
            raise ProjectStageLifecycleError("project-stage run session issuer is invalid")
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationAcquisitionSession,
            ProjectMutationLease,
            ProjectMutationRecoveryLease,
        )
        from mif_pipeline.qupath_runtime.runner import QuPathMutationLaunchBinding

        from .batch_state import BatchTerminalCommitter, BatchWorkspaceLease
        from .project_stage_codecs import PreparedProjectStageRunRequest

        issued_acquisition = None
        published_request = None
        active_registration = None
        if type(runtime) is ProjectStageRuntime:
            with runtime._lock:
                issued_acquisition = runtime._sessions.get(id(acquisition_session))
                published_request = runtime._published_prepared_requests.get(id(prepared_request))
                if project_lease is not None:
                    active_registration = runtime._active_project_leases.get(id(project_lease))
        common_shape = (
            type(runtime) is ProjectStageRuntime
            and type(prepared_request) is PreparedProjectStageRunRequest
            and type(acquisition_context) is _ProjectStageAcquisitionLifecycle
            and type(acquisition_session) is ProjectMutationAcquisitionSession
            and type(workspace_lease) is BatchWorkspaceLease
            and type(terminal_committer) is BatchTerminalCommitter
            and acquisition_context._runtime is runtime
            and acquisition_context._session is acquisition_session
            and acquisition_context._workspace_lease is workspace_lease
            and issued_acquisition is not None
            and issued_acquisition[0] is acquisition_session
            and issued_acquisition[1] is prepared_request
            and issued_acquisition[3] == prepared_request.request
            and issued_acquisition[4] is workspace_lease
            and published_request is not None
            and published_request[0] is prepared_request
            and published_request[2] is workspace_lease
            and published_request[3] is terminal_committer
        )
        prepared_shape = (
            _preparation_cleanup is False
            and _preparation_failure is None
            and _preparation_cleanup_step is None
            and type(publication) is PublishedProjectStageRunPlan
            and type(published_descriptor) is PublishedPreparedProjectStageRunDescriptor
            and type(launch_descriptor) is QuPathProjectLaunchDescriptor
            and (
                type(project_lease) is ProjectMutationLease
                or (
                    type(project_lease) is ProjectMutationRecoveryLease
                    and project_lease._project_stage_resumes_preparation
                    and acquisition_context._unlaunched is not None
                    and acquisition_context._unlaunched.lease_record_sha256
                    == project_lease.stale_active_record_sha256
                )
            )
            and type(launch) is QuPathMutationLaunchBinding
            and acquisition_context._project_lease is project_lease
            and acquisition_context._state == "open"
            and launch._project_lease is project_lease
            and active_registration is not None
            and active_registration[0] is project_lease
            and active_registration[1] is acquisition_session
            and active_registration[2] is prepared_request
            and active_registration[3] == prepared_request.request
            and active_registration[4] is workspace_lease
            and publication.plan.request_sha256 == prepared_request.request.request_sha256
            and published_descriptor.descriptor.run_plan_ref == publication.plan_ref
            and published_descriptor.descriptor.run_plan_sha256 == publication.plan.plan_sha256
            and published_descriptor.descriptor.run_plan_byte_count == publication.byte_count
            and published_descriptor.descriptor.target_authorization_set_sha256
            == publication.plan.target_authorization_set_sha256
            and published_descriptor.descriptor.request_member_set_sha256
            == publication.plan.request_member_set_sha256
            and published_descriptor.descriptor.acquisition_record_sha256
            == acquisition_session.record.acquisition_record_sha256
            and published_descriptor.descriptor.acquisition_session_challenge_sha256
            == acquisition_session.acquisition_session_challenge_sha256
            and launch_descriptor.run_plan_sha256 == publication.plan.plan_sha256
            and launch_descriptor.target_script_asset_id == publication.plan.target_script_asset_id
            and launch_descriptor.eligible_project_entry_ids
            == tuple(sorted(item.project_entry_id for item in publication.plan.targets))
            and launch_descriptor.excluded_project_entry_ids
            == publication.plan.excluded_project_entry_ids
        )
        cleanup_shape = (
            _preparation_cleanup is True
            and isinstance(_preparation_failure, BaseException)
            and type(_preparation_cleanup_step) is str
            and publication is None
            and published_descriptor is None
            and launch_descriptor is None
            and launch is None
            and project_lease is acquisition_context._project_lease
            and not acquisition_context.is_closed
            and acquisition_context._state == f"cleanup_failed({_preparation_cleanup_step})"
        )
        if (
            type(_preparation_cleanup) is not bool
            or not common_shape
            or not (prepared_shape or cleanup_shape)
        ):
            raise ProjectStageLifecycleError("project-stage run session authority shape is invalid")
        value = object.__new__(cls)
        value._runtime = runtime
        value._prepared_request = prepared_request
        value._publication = publication
        value._published_descriptor = published_descriptor
        value._launch_descriptor = launch_descriptor
        value._acquisition_context = acquisition_context
        value._acquisition_lifecycle = acquisition_context
        value._acquisition_session = acquisition_session
        value._project_lease = project_lease
        value._launch = launch
        value._workspace_lease = workspace_lease
        value._terminal_committer = terminal_committer
        value._cancellation_event = Event()
        value._cancellation_reason = None
        value._completion = None
        value._results = None
        value._unresolved_cleanup = None
        value._manual_review = None
        value._acquisition_closed = False
        value._terminalization_started = False
        value._failure = _preparation_failure
        value._failure_phase = "preparation" if _preparation_cleanup else None
        value._failure_cleanup_next = 0
        value._cleanup_retry_in_progress = False
        value._pending_terminal_results = None
        value._project_completion_confirmed = False
        value._state = (
            f"cleanup_failed({_preparation_cleanup_step})" if _preparation_cleanup else "prepared"
        )
        value._condition = Condition(RLock())
        return value

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def launch_descriptor(self) -> QuPathProjectLaunchDescriptor:
        descriptor = self._launch_descriptor
        if type(descriptor) is not QuPathProjectLaunchDescriptor:
            raise ProjectStageLifecycleError(
                "project-stage launch descriptor is unavailable during cleanup"
            )
        return descriptor

    @property
    def completion(self) -> ProjectStageProcessCompletion | None:
        with self._condition:
            return self._completion

    @property
    def cancellation_reason(self) -> str | None:
        with self._condition:
            return self._cancellation_reason


def _retained_project_stage_process_records(
    store: object,
    *,
    active_mutation_run_sha256: str | None = None,
) -> tuple[tuple[AuthorityMemberRefV2, object], ...]:
    """Read this analysis's retained native process records.

    We select the runtime-record filename family before decoding its JSON. The
    stored workspace scope then selects records belonging to the current analysis.
    Each selected record retains its name, content and schema checks. Other
    analyses share the physical directory and resolve their own scoped records.
    """

    return _read_retained_project_stage_process_records(
        retained=store._authority_layout,
        workspace_storage_scope_sha256=store.workspace_storage_scope_sha256,
        runtime_evidence_ref=store.runtime_evidence_ref,
        active_mutation_run_sha256=active_mutation_run_sha256,
    )


def _read_retained_project_stage_process_records(
    *, retained, workspace_storage_scope_sha256, runtime_evidence_ref,
    active_mutation_run_sha256=None,
):
    """Share immutable process discovery with read-only review presentation."""
    from mif_pipeline.qupath_runtime.project_stage import (
        ProjectStageMutationEvidenceRecord, ProjectStageProcessAttemptRecord,
        ProjectStageProcessDrainRecord,
    )
    if retained is None:
        raise ProjectStageLifecycleError(
            "project-stage retained discovery authority is unavailable"
        )
    family = retained.layout.family("batch_project_runtime_evidence")
    bucket = retained.bucket(family.physical_bucket)
    authority = bucket._authority
    members = getattr(authority, "members", None)
    if type(members) is dict:
        names = tuple(sorted(members))
    else:
        path = getattr(authority, "path", None)
        if not isinstance(path, Path):
            raise ProjectStageLifecycleError(
                "project-stage retained discovery cannot enumerate evidence"
            )
        bucket.validate()
        try:
            entries = tuple(path.iterdir())
        except OSError as exc:
            raise ProjectStageRecordError(
                "project-stage retained evidence cannot be enumerated"
            ) from exc
        if any(
            item.is_symlink() or not item.is_file() or item.name != item.name.strip()
            for item in entries
        ):
            raise ProjectStageRecordError("project-stage retained evidence inventory is unsafe")
        names = tuple(sorted(item.name for item in entries))
        bucket.validate()
    names = tuple(
        name for name in names
        if name.startswith("batch_project_runtime_evidence-") and name.endswith(".json")
    )
    physical_families = tuple(
        item for item in retained.layout.families if item.physical_bucket == family.physical_bucket
    )
    maximum_members = sum(item.max_members_per_scope for item in physical_families)
    if len(names) > maximum_members:
        raise ProjectStageRecordError("project-stage retained evidence inventory is oversized")
    maximum_bytes = max(item.max_member_bytes for item in physical_families)
    record_types = {
        "mif-project-stage-prepared-run": PreparedProjectStageRunDescriptor,
        "mif-project-stage-mutation-evidence": ProjectStageMutationEvidenceRecord,
        "mif-project-stage-process-attempt": ProjectStageProcessAttemptRecord,
        "mif-project-stage-process-drain": ProjectStageProcessDrainRecord,
    }
    reopened: list[tuple[AuthorityMemberRefV2, object]] = []
    for name in names:
        try:
            payload = authority.read_bytes(
                name,
                max_bytes=maximum_bytes,
                label="retained project-stage discovery member",
                optional=False,
            )
        except Exception as exc:
            raise ProjectStageRecordError(
                "project-stage retained evidence member cannot be read"
            ) from exc
        assert payload is not None
        try:
            mapping = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            if payload.lstrip().startswith(b"{"):
                raise ProjectStageRecordError(
                    "project-stage retained JSON evidence is malformed"
                ) from exc
            continue
        if not isinstance(mapping, Mapping):
            continue
        if mapping.get("workspace_storage_scope_sha256") != workspace_storage_scope_sha256:
            continue
        schema_name = mapping.get("schema_name")
        record_type = record_types.get(schema_name)
        if record_type is None:
            if (
                active_mutation_run_sha256 is not None
                and type(schema_name) is str
                and schema_name.startswith("mif-project-stage-")
                and mapping.get("active_mutation_run_sha256") == active_mutation_run_sha256
            ):
                raise ProjectStageRecordError(
                    "project-stage retained evidence schema is unsupported"
                )
            continue
        if canonical_json_bytes(mapping) != payload:
            raise ProjectStageRecordError("project-stage retained evidence is not canonical")
        try:
            record = record_type.from_mapping(mapping)
        except (TypeError, ValueError) as exc:
            raise ProjectStageRecordError("project-stage retained evidence is invalid") from exc
        digest_field = {
            PreparedProjectStageRunDescriptor: "prepared_descriptor_sha256",
            ProjectStageMutationEvidenceRecord: "mutation_evidence_sha256",
            ProjectStageProcessAttemptRecord: "attempt_sha256",
            ProjectStageProcessDrainRecord: "process_drain_sha256",
        }[record_type]
        ref = runtime_evidence_ref(getattr(record, digest_field))
        if ref.physical_name != name or bucket.read_member(ref) != payload:
            raise ProjectStageRecordError("project-stage retained evidence name or bytes differ")
        reopened.append((ref, record))
    return tuple(reopened)


def _reopen_exact_retained_project_stage_mutation(
    store: object,
    mutation: object,
) -> tuple[tuple[AuthorityMemberRefV2, object], ...]:
    """Re-enumerate one selected run to reject hidden or changed evidence."""

    records = _retained_project_stage_process_records(
        store, active_mutation_run_sha256=mutation.active_mutation_run_sha256
    )
    if sum(item == mutation for _ref, item in records) != 1:
        raise ProjectStageRecordError("retained project-stage mutation changed")
    return records


def _reopen_retained_project_stage_context(
    *,
    state: "BatchStateStore",
    head: "BatchStateHead",
    terminal: "BatchNodeAttempt",
    authorization: "BatchAttemptAuthorization",
    spec: StageAdapterSpec,
    node_id: str,
) -> BatchNodeExecutionContext:
    """Rebuild one historical context from the exact retained input refs."""

    from .batch_artifacts import BatchArtifactRef, artifact_ref_set_sha256
    from .batch_state import _execution_context_sha256

    binding = terminal.execution_context_binding
    input_digests = binding.input_artifact_ref_sha256s
    outputs: dict[str, BatchArtifactRef] = {}
    try:
        for terminal_digest in head.terminal_attempt_sha256_by_attempt_id.values():
            candidate = state.load_attempt(terminal_digest)
            for output in candidate.output_artifacts:
                outputs[output.artifact_ref_sha256] = output
    except Exception as exc:
        raise ProjectStageRecordError(
            "retained project-stage input producer cannot be reopened"
        ) from exc

    missing = tuple(item for item in input_digests if item not in outputs)
    if missing:
        try:
            state._load_plan_snapshot(head.active_plan_sha256)
            plan_payload = (state.layout.plans / f"{head.active_plan_sha256}.json").read_bytes()
            plan_mapping = json.loads(plan_payload.decode("utf-8", errors="strict"))
            external_rows = tuple(plan_mapping["external_inputs"])
            requirements = tuple((*spec.required_inputs, *spec.optional_inputs))
            for digest in missing:
                rows = tuple(row for row in external_rows if _digest(row) == digest)
                if len(rows) != 1:
                    raise ValueError("external input is missing or ambiguous")
                row = rows[0]
                contracts = tuple(
                    requirement.identity_contract
                    for requirement in requirements
                    if requirement.artifact_kind == row.get("artifact_kind")
                    and requirement.schema_name == row.get("schema_name")
                    and requirement.schema_version == row.get("schema_version")
                    and requirement.identity_contract.contract_sha256
                    == row.get("owning_identity_contract_sha256")
                )
                if len(contracts) != 1:
                    raise ValueError("external input contract is missing or ambiguous")
                external = BatchArtifactRef.from_mapping(row, identity_contract=contracts[0])
                if external.artifact_ref_sha256 != digest:
                    raise ValueError("external input digest differs")
                outputs[digest] = external
        except Exception as exc:
            raise ProjectStageRecordError(
                "retained project-stage external context input is unavailable"
            ) from exc

    try:
        inputs = tuple(outputs[item] for item in input_digests)
        if artifact_ref_set_sha256(inputs) != authorization.input_artifact_set_sha256:
            raise ValueError("authorized input set differs")
        context = BatchNodeExecutionContext(
            authorization=authorization,
            node_id=node_id,
            adapter_spec=spec,
            inputs=inputs,
            projection_target_sha256=binding.projection_target_sha256,
            current_image_identity_sha256=binding.current_image_identity_sha256,
            script_sha256=binding.script_sha256,
            project_identity_sha256=binding.project_identity_sha256,
            project_authority_head_sha256=binding.project_authority_head_sha256,
            project_qpproj_snapshot_sha256=binding.project_qpproj_snapshot_sha256,
            project_entry_projection_sha256=binding.project_entry_projection_sha256,
            compatibility_signature_sha256=binding.compatibility_signature_sha256,
        )
        if _execution_context_sha256(context) != terminal.execution_context_sha256:
            raise ValueError("execution context digest differs")
    except Exception as exc:
        raise ProjectStageRecordError("retained project-stage execution context differs") from exc
    return context


@dataclass(frozen=True, slots=True, init=False)
class _ReopenedProjectStageExchange:
    """Exact retained members plus the reissued synchronous owner evidence."""

    request_bytes: bytes
    result_bytes: bytes
    generation_manifest_bytes: bytes
    evidence: ProjectStageTargetExecutionEvidence
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("reopened project-stage exchanges are issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("reopened project-stage exchanges are nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("reopened project-stage exchanges are nonserializable")


def _project_stage_failure_can_retry(node_kind: str, result) -> bool:
    """Select supported failures for a fresh attempt with current owner control.

    Ordinary image stages require an absent exchange and unchanged saved image.
    Reference snapshots, segmentation and measurements also admit a completed
    codec-rejected exchange after exact reopening. Segmentation reuses its checked
    population; measurements recalculate from it. Marker application can accept
    its completed component without another native run. Region selection restores
    its saved candidate review or accepted regions without repeating the snapshot.
    Saved-result retirement accepts its completed selection changes without another write.
    The caller authenticates the process drain, outputs and current image; rejected
    scientific results remain unpublished.
    """
    snapshot = node_kind in {
        "primary_training_evidence_snapshot", "secondary_training_evidence_snapshot",
        "supervised_training_evidence_snapshot",
    }
    from .batch_stage_adapters import PROJECT_CURRENT_IMAGE_ASSET_BY_NODE

    if (not snapshot and node_kind not in PROJECT_CURRENT_IMAGE_ASSET_BY_NODE) or (
        result.error_code != "qupath_project_stage_incomplete"
    ):
        return False
    details = result.error_context
    if not isinstance(details, Mapping):
        return False
    reason = details.get("process_termination_code")
    saved = details.get("saved_state_disposition_code")
    code = details.get("transport_validation_code")
    if code == "result_payload_invalid" and (snapshot or node_kind == "supervised_phenotype_inference"):
        return reason == "normal_exit" and saved == "authenticated_unchanged"
    if node_kind in {"semantic_raw_inventory_snapshot", "semantic_evidence", "phenotype_handoff"}:
        return saved == "authenticated_unchanged" and (
            (code == "target_codec_rejected" and reason == "normal_exit")
            or (code == "exchange_members_missing"
                and reason in {"normal_exit", "nonzero_exit", "timeout", "cancelled"})
        )
    if node_kind in {
        "segmentation", "measurements", "marker_rule_application", "analysis_domain_selection",
        "saved_result_retirement", "segmentation_correction",
    } and code == "target_codec_rejected":
        return reason == "normal_exit" and saved in {
            "authenticated_changed", "authenticated_unchanged",
        }
    if not snapshot:
        return (
            code == "exchange_members_missing"
            and saved == "authenticated_unchanged"
            and reason in {"normal_exit", "nonzero_exit", "timeout", "cancelled"}
        )
    if code == "target_codec_rejected":
        return reason == "normal_exit" and saved in {
            "authenticated_changed", "authenticated_unchanged",
        }
    if code != "exchange_members_missing":
        return False
    return ((reason == "normal_exit" and saved in {"authenticated_changed", "authenticated_unchanged"})
            or (reason in {"nonzero_exit", "timeout", "cancelled"} and saved == "authenticated_unchanged"))


@dataclass(frozen=True, slots=True, init=False)
class _ReopenedProjectStageFailure:
    """Historical correspondence; current saved state is a state projection.

    This value grants no retry authority or physical `.qpdata` attestation.
    """

    context: BatchNodeExecutionContext
    authorized: ProjectStageAuthorizedEntry
    run_plan: ProjectStageRunPlan
    prepared_descriptor: PreparedProjectStageRunDescriptor
    mutation_evidence: ProjectStageMutationEvidenceRecord
    target_observation: ProjectStageTargetObservation
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    completed_evidence: ProjectStageTargetExecutionEvidence | None
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("reopened project-stage failures are issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("reopened project-stage failures are nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("reopened project-stage failures are nonserializable")


@dataclass(frozen=True, slots=True, init=False)
class _ProjectStageRetryResumeAuthority:
    """Invocation-local authority joining one retry to its predecessor exchange."""

    current_context: BatchNodeExecutionContext
    accepted_action: str
    action_context: Mapping[str, object]
    predecessor_evidence: ProjectStageTargetExecutionEvidence
    authority_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ProjectStageRetryResumeAuthority is issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("ProjectStageRetryResumeAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ProjectStageRetryResumeAuthority is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("ProjectStageRetryResumeAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("ProjectStageRetryResumeAuthority is noncopyable")

    @classmethod
    def _issue(
        cls,
        *,
        current_context: BatchNodeExecutionContext,
        accepted_action: str,
        action_context: Mapping[str, object],
        predecessor_evidence: ProjectStageTargetExecutionEvidence,
        _issuer: object,
    ) -> "_ProjectStageRetryResumeAuthority":
        if (
            _issuer is not _PROJECT_STAGE_RETRY_RESUME_AUTHORITY_ISSUER
            or type(current_context) is not BatchNodeExecutionContext
            or type(predecessor_evidence) is not ProjectStageTargetExecutionEvidence
            or type(accepted_action) is not str
            or not accepted_action
            or not isinstance(action_context, Mapping)
            or frozenset(action_context) != {"service_context", "response"}
            or not isinstance(action_context["service_context"], Mapping)
            or not isinstance(action_context["response"], Mapping)
        ):
            raise ProjectStageRecordError("retry resume authority issuer or projection is invalid")
        authorization = current_context.authorization
        if (
            authorization.retry_ordinal <= 0
            or authorization.interaction_response_sha256 is None
            or authorization.interaction_predecessor_terminal_record_sha256 is None
            or predecessor_evidence.context.node_id != current_context.node_id
            or predecessor_evidence.authorized.project_entry_id
            not in authorization.dispatch_binding.project_entry_ids
        ):
            raise ProjectStageRecordError("retry resume authority lineage or target differs")
        authority_sha256 = _digest(
            {
                "current_attempt_authorization_sha256": (authorization.authorization_sha256),
                "interaction_response_sha256": (authorization.interaction_response_sha256),
                "predecessor_terminal_record_sha256": (
                    authorization.interaction_predecessor_terminal_record_sha256
                ),
                "predecessor_validated_exchange_sha256": (
                    predecessor_evidence.validated_exchange_sha256
                ),
                "accepted_action": accepted_action,
                "action_context": action_context,
            }
        )
        value = object.__new__(cls)
        for name, item in {
            "current_context": current_context,
            "accepted_action": accepted_action,
            "action_context": MappingProxyType(dict(action_context)),
            "predecessor_evidence": predecessor_evidence,
            "authority_sha256": authority_sha256,
            "_issuer": _issuer,
        }.items():
            object.__setattr__(value, name, item)
        return value


@dataclass(frozen=True, slots=True, init=False)
class ProjectStageCommittedExchangeReopener:
    """Process-local capability for exact committed Task-10 exchange recovery."""

    _runtime: "ProjectStageRuntime"
    _workspace_lease: object
    _state_head_sha256: str
    _active_plan_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ProjectStageCommittedExchangeReopener is issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("ProjectStageCommittedExchangeReopener is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ProjectStageCommittedExchangeReopener is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("ProjectStageCommittedExchangeReopener is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("ProjectStageCommittedExchangeReopener is noncopyable")

    @classmethod
    def _issue(
        cls,
        *,
        runtime: "ProjectStageRuntime",
        workspace_lease: object,
        state_head_sha256: str,
        active_plan_sha256: str,
        _issuer: object,
    ) -> "ProjectStageCommittedExchangeReopener":
        if (
            _issuer is not _PROJECT_STAGE_COMMITTED_EXCHANGE_REOPENER_ISSUER
            or type(runtime) is not ProjectStageRuntime
        ):
            raise ProjectStageLifecycleError("committed exchange reopener issuer is invalid")
        value = object.__new__(cls)
        for name, item in {
            "_runtime": runtime,
            "_workspace_lease": workspace_lease,
            "_state_head_sha256": _sha(state_head_sha256, "state_head_sha256"),
            "_active_plan_sha256": _sha(active_plan_sha256, "active_plan_sha256"),
            "_issuer": _issuer,
        }.items():
            object.__setattr__(value, name, item)
        return value

    def _reopen_for_owner(
        self,
        authority: object,
        *,
        producer_artifact_ref_sha256: str,
        producer_node_id: str,
        producer_attempt_id: str,
        producer_attempt_authorization_sha256: str,
        terminal_record_sha256: str,
        execution_exchange_sha256: str,
        expected_binding: tuple[str, ...],
    ) -> _ReopenedProjectStageExchange:
        """Reissue one exact artifact-backed exchange to its synchronous owner."""

        reopened = self._reopen_exact_exchange_for_owner(
            authority=authority,
            retry_context=None,
            producer_artifact_ref_sha256=producer_artifact_ref_sha256,
            producer_node_id=producer_node_id,
            producer_attempt_id=producer_attempt_id,
            producer_attempt_authorization_sha256=(producer_attempt_authorization_sha256),
            terminal_record_sha256=terminal_record_sha256,
            execution_exchange_sha256=execution_exchange_sha256,
            expected_binding=expected_binding,
        )
        if type(reopened) is not _ReopenedProjectStageExchange:
            raise ProjectStageRecordError(
                "committed artifact exchange reopen returned foreign authority"
            )
        return reopened

    def retain_pending_segmentation_request(self, context, current):
        """Reopen an interrupted attempt's exact model observation receipt.

        Older segmentation requests included the fresh probe challenge digest.
        Rechecking the unchanged model produces a different receipt. Recover the
        saved receipt through its active acquisition, descriptor and envelope;
        the complete scientific body must still match the retained target. This
        grants no mutation authority and leaves normal recovery guards intact.
        """
        from .batch_artifacts import BatchNodeExecutionContext
        from .batch_segmentation_setup import _retain_matching_segmentation_request

        if (type(self) is not ProjectStageCommittedExchangeReopener
                or self._issuer is not _PROJECT_STAGE_COMMITTED_EXCHANGE_REOPENER_ISSUER
                or type(context) is not BatchNodeExecutionContext):
            raise ProjectStageRecordError("pending segmentation reopen authority is invalid")
        if current.get("segmentation_model_evidence") is None:
            return current
        runtime = self._runtime
        with runtime._operation(), runtime._state_store._lease_operation(self._workspace_lease):
            head = runtime._state_store.load_head()
            if (head.sha256 != self._state_head_sha256
                    or head.active_plan_sha256 != self._active_plan_sha256):
                raise ProjectStageRecordError("pending segmentation state changed")
            candidates = runtime._provider._unresolved_active_records_for_project(
                project_uri=Path(head.project_path).resolve().as_uri(),
                project_identity_sha256=head.project_identity_sha256)
            if not candidates:
                return current
            if len(candidates) != 1:
                raise ProjectStageRecordError("pending segmentation acquisition is ambiguous")
            active = candidates[0][1]
            if (active.operation != "project_stage"
                    or active.authority_layout_sha256 != runtime._state_store.authority_layout_sha256
                    or active.predecessor_authority_head_sha256 != head.project_authority_head_sha256):
                raise ProjectStageRecordError("pending segmentation acquisition is foreign or stale")
            store = runtime._catalogue_store(self._workspace_lease)
            retained = store.reopen_project_stage_request_value(
                active.mutation_request_sha256, workspace_lease=self._workspace_lease)
            targets = tuple(item for item in retained.targets if item.node_id == context.node_id)
            if not targets:
                return current
            if (len(targets) != 1 or targets[0].terminal_stage_id != "segmentation"
                    or retained.project_identity_sha256 != head.project_identity_sha256
                    or retained.project_uri != active.project_uri
                    or retained.project_catalogue_sha256 != head.project_catalogue_sha256
                    or retained.expected_project_authority_head_sha256 != head.project_authority_head_sha256
                    or head.pending_authorization_sha256_by_node.get(context.node_id)
                    != context.authorization.authorization_sha256
                    or runtime._state_store.load_authorization(context.authorization.authorization_sha256)
                    != context.authorization):
                raise ProjectStageRecordError("pending segmentation target authority differs")
            target = targets[0]
            active_sha256 = runtime._provider._active_mutation_run_sha256(active)
            records = _retained_project_stage_process_records(store,
                active_mutation_run_sha256=active_sha256)
            descriptors = tuple(record for _, record in records
                if type(record) is PreparedProjectStageRunDescriptor
                and record.active_mutation_run_sha256 == active_sha256)
            if not descriptors:
                # New requests have stable model identity even when interrupted
                # before preparation publishes an envelope.
                return current
            if len({item.run_plan_sha256 for item in descriptors}) != 1:
                raise ProjectStageRecordError("pending segmentation preparation is ambiguous")
            descriptor = descriptors[0]
            if (descriptor.active_mutation_run_sha256 != active_sha256
                    or descriptor.acquisition_record_sha256 != active.acquisition_record_sha256
                    or descriptor.mutation_request_sha256 != retained.request_sha256
                    or descriptor.mutation_authorization_sha256 != active.mutation_authorization_sha256):
                raise ProjectStageRecordError("pending segmentation descriptor authority differs")
            plan = store._reopen_project_stage_runtime_record(
                descriptor.run_plan_ref, record_type=ProjectStageRunPlan,
                digest_field="plan_sha256", expected_digest=descriptor.run_plan_sha256,
                label="pending segmentation run plan")
            entries = tuple(item for item in plan.targets
                if item.target_request_sha256 == target.target_request_sha256)
            if (plan.request_sha256 != retained.request_sha256 or len(entries) != 1
                    or entries[0].attempt_id != context.authorization.attempt_id
                    or entries[0].attempt_authorization_sha256 != context.authorization.authorization_sha256):
                raise ProjectStageRecordError("pending segmentation plan authority differs")
            exchange = entries[0].exchange
            payload = store._read_ref(exchange.request_ref,
                max_bytes=store._authority_layout.layout.family("runtime_exchange").max_member_bytes,
                label="pending segmentation request", optional=False)
            if (payload is None or hashlib.sha256(payload).hexdigest() != exchange.request_envelope_sha256
                    or len(payload) != exchange.request_envelope_byte_count):
                raise ProjectStageRecordError("pending segmentation request bytes differ")
            envelope = store._validate_project_stage_request_envelope(target, payload)
            if envelope["invocation_id"] != context.authorization.attempt_id:
                raise ProjectStageRecordError("pending segmentation invocation differs")
            evidence = envelope["request"].get("segmentation_model_evidence")
            if not isinstance(evidence, Mapping):
                raise ProjectStageRecordError("pending segmentation model evidence is absent")
            return _retain_matching_segmentation_request(current,
                retained_evidence_sha256=evidence.get("evidence_sha256"),
                scientific_request_body_sha256=target.scientific_request_body_sha256,
                scientific_request_body_byte_count=target.scientific_request_body_byte_count)

    def classifier_output_dir(self, authority: object) -> Path:
        """Reopen the directory from this classifier's exact producing request.

        The current committed reference identifies its original attempt and
        exchange. Historical requests use their saved configuration; imported
        later requests retain their separately issued publication directory.
        """
        from .batch_artifacts import BatchCommittedArtifactAuthority

        if type(authority) is not BatchCommittedArtifactAuthority:
            raise ProjectStageRecordError("classifier output requires committed authority")
        ref = authority.artifact_ref
        stages = {"primary_inference_result": ("primary_classifier", "primary_classifier_inference"),
                  "secondary_inference_result": ("secondary_classifier", "secondary_classifier_inference")}
        if ref.artifact_kind not in stages:
            raise ProjectStageRecordError("producer is not a classifier ROI publication")
        wire, terminal = stages[ref.artifact_kind]
        identity = ref.owning_identity
        reopened = self._reopen_for_owner(
            authority,
            producer_artifact_ref_sha256=ref.artifact_ref_sha256,
            producer_node_id=ref.producer_node_id,
            producer_attempt_id=ref.producer_attempt_id,
            producer_attempt_authorization_sha256=ref.producer_attempt_authorization_sha256,
            terminal_record_sha256=authority.terminal_record_sha256,
            execution_exchange_sha256=authority.execution_exchange_sha256,
            expected_binding=(identity["adapter_spec_sha256"], identity["request_codec_id"],
                              wire, terminal, identity["request_codec_version"]),
        )
        return Path(reopened.evidence.reopen_validation_inputs().operational_output_dir)

    def reopen_settled_review_predecessor_for_owner(
        self, current_context, *, execution, expected_binding,
    ) -> _ReopenedProjectStageExchange | None:
        """Reopen the existing native exchange selected by current review lineage."""
        reopened = self._reopen_exact_exchange_for_owner(
            authority=None, retry_context=None, review_context=current_context,
            review_execution=execution, producer_artifact_ref_sha256=None,
            producer_node_id=None, producer_attempt_id=None,
            producer_attempt_authorization_sha256=None, terminal_record_sha256=None,
            execution_exchange_sha256=None, expected_binding=expected_binding)
        if reopened is not None and type(reopened) is not _ReopenedProjectStageExchange:
            raise ProjectStageRecordError("settled review predecessor returned foreign authority")
        return reopened

    def reopen_retry_predecessor_for_owner(
        self,
        current_context: BatchNodeExecutionContext,
        *,
        expected_binding: tuple[str, ...],
    ) -> _ProjectStageRetryResumeAuthority:
        """Reissue the exact interactive predecessor for one current retry."""

        reopened = self._reopen_exact_exchange_for_owner(
            authority=None,
            retry_context=current_context,
            producer_artifact_ref_sha256=None,
            producer_node_id=None,
            producer_attempt_id=None,
            producer_attempt_authorization_sha256=None,
            terminal_record_sha256=None,
            execution_exchange_sha256=None,
            expected_binding=expected_binding,
        )
        if type(reopened) is not _ProjectStageRetryResumeAuthority:
            raise ProjectStageRecordError("retry predecessor reopen returned foreign authority")
        return reopened

    def reopen_failed_target_for_owner(
        self,
        *,
        node_id: str,
        terminal_record_sha256: str,
        expected_binding: tuple[str, ...],
    ) -> _ReopenedProjectStageFailure:
        """Reopen one state-selected failed target without issuing retry authority.

        We bind retained exchange bytes to their original observation before
        admitting a fresh attempt. Physical `.qpdata` reinspection belongs to
        later admission/dispatch; rejected outputs remain unpublished.
        """

        from .batch_manifest import revalidate_retained_manifest
        from .project_stage_codecs import ProjectStageCodecError
        from mif_pipeline.qupath_runtime.exchange import (
            ExchangeContractError,
            parse_stage_result_exchange_bytes,
        )
        from mif_pipeline.pipeline_core.stage_result import StageStatus
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceRecord,
            ProjectStageProcessAttemptRecord,
            ProjectStageProcessDrainRecord,
            ProjectStageProcessLaunchRecord,
            ProjectStageProcessSummary,
            validate_project_stage_process_evidence,
        )

        if (
            type(self) is not ProjectStageCommittedExchangeReopener
            or self._issuer is not _PROJECT_STAGE_COMMITTED_EXCHANGE_REOPENER_ISSUER
            or type(expected_binding) is not tuple
            or len(expected_binding) != 5
            or any(type(item) is not str for item in expected_binding)
        ):
            raise ProjectStageRecordError("failed target reopen authority is invalid")
        node_id = _safe_id(node_id, "node_id")
        terminal_sha256 = _sha(terminal_record_sha256, "terminal_record_sha256")
        runtime = self._runtime
        state = runtime._state_store
        with runtime._operation():
            with state._lease_operation(self._workspace_lease):
                head = state.load_head()
                attempt_ids = head.attempt_ids_by_node.get(node_id, ())
                if (
                    head.sha256 != self._state_head_sha256
                    or head.active_plan_sha256 != self._active_plan_sha256
                    or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
                    or node_id in head.invalidated_node_ids
                    or not attempt_ids
                    or head.terminal_attempt_sha256_by_attempt_id.get(attempt_ids[-1])
                    != terminal_sha256
                ):
                    raise ProjectStageRecordError("failed target is not the current terminal")
                try:
                    terminal = state.load_attempt(terminal_sha256)
                    authorization = state.load_authorization(terminal.authorization_sha256)
                    current_plan = state._load_plan_snapshot(head.active_plan_sha256)
                    node = current_plan.node_by_id[node_id]
                    current_inputs = state._current_input_ref_sha256s(current_plan, head, node_id)
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target state authority cannot be reopened"
                    ) from exc
                error = terminal.stage_result
                failure = error.error_context
                binding = terminal.execution_context_binding
                if (
                    terminal.node_id != node_id
                    or terminal.attempt_id != attempt_ids[-1]
                    or authorization.node_id != node_id
                    or authorization.attempt_id != terminal.attempt_id
                    or terminal.output_artifacts
                    or terminal.execution_exchange_sha256 is None
                    or error.status is not StageStatus.FAILED
                    or not _project_stage_failure_can_retry(node["kind"], error)
                    or binding.adapter_spec_sha256 != expected_binding[0]
                    or node["adapter_spec_sha256"] != expected_binding[0]
                    or node["kind"] != expected_binding[3]
                    or tuple(node["project_entry_ids"])
                    != authorization.dispatch_binding.project_entry_ids
                    or tuple(current_inputs) != tuple(binding.input_artifact_ref_sha256s)
                ):
                    raise ProjectStageRecordError("failed target terminal or current inputs differ")
                try:
                    spec = runtime._codec_registry._resolve_adapter_spec(
                        adapter_spec_sha256=expected_binding[0],
                        target_script_asset_id=expected_binding[1],
                        wire_stage_id=expected_binding[2],
                        terminal_stage_id=expected_binding[3],
                        request_codec_version=expected_binding[4],
                    )
                except (ProjectStageCodecError, TypeError, ValueError) as exc:
                    raise ProjectStageRecordError(
                        "failed target lacks its exact registered binding"
                    ) from exc
                if spec.node_kind != expected_binding[3]:
                    raise ProjectStageRecordError("failed target adapter kind differs")

                store = runtime._catalogue_store(self._workspace_lease)
                records = _retained_project_stage_process_records(store)
                candidates = tuple(
                    (ref, item)
                    for ref, item in records
                    if type(item) is ProjectStageMutationEvidenceRecord
                    and item.project_identity_sha256 == binding.project_identity_sha256
                    and item.termination_reason == failure["process_termination_code"]
                    and any(
                        obs.node_id == node_id
                        and obs.attempt_id == terminal.attempt_id
                        and obs.attempt_authorization_sha256 == terminal.authorization_sha256
                        for obs in item.target_observations
                    )
                )
                if len(candidates) != 1:
                    raise ProjectStageRecordError(
                        "failed target retained evidence is missing or ambiguous"
                    )
                mutation_ref, mutation = candidates[0]
                records = _reopen_exact_retained_project_stage_mutation(store, mutation)
                observations = tuple(
                    item
                    for item in mutation.target_observations
                    if item.node_id == node_id
                    and item.attempt_id == terminal.attempt_id
                    and item.attempt_authorization_sha256 == terminal.authorization_sha256
                )
                descriptors = tuple(
                    (ref, item)
                    for ref, item in records
                    if type(item) is PreparedProjectStageRunDescriptor
                    and item.active_mutation_run_sha256 == mutation.active_mutation_run_sha256
                    and item.prepared_descriptor_sha256 == mutation.prepared_descriptor_sha256
                )
                attempts = tuple(
                    (ref, item)
                    for ref, item in records
                    if type(item) is ProjectStageProcessAttemptRecord
                    and item.attempt_sha256 == mutation.attempt_sha256
                )
                drains = tuple(
                    (ref, item)
                    for ref, item in records
                    if type(item) is ProjectStageProcessDrainRecord
                    and item.process_drain_sha256 == mutation.process_drain_sha256
                )
                if any(len(items) != 1 for items in (observations, descriptors, attempts, drains)):
                    raise ProjectStageRecordError("failed target process chain is ambiguous")
                observation = observations[0]
                descriptor_ref, descriptor = descriptors[0]
                attempt_ref, attempt = attempts[0]
                drain_ref, drain = drains[0]
                try:
                    for ref, record in (
                        (mutation_ref, mutation),
                        (attempt_ref, attempt),
                        (drain_ref, drain),
                    ):
                        store.reopen_project_stage_process_record(
                            ref,
                            expected_record=record,
                            workspace_lease=self._workspace_lease,
                        )
                    if (
                        store._reopen_project_stage_runtime_record(
                            descriptor_ref,
                            record_type=PreparedProjectStageRunDescriptor,
                            digest_field="prepared_descriptor_sha256",
                            expected_digest=descriptor.prepared_descriptor_sha256,
                            label="failed target prepared descriptor",
                        )
                        != descriptor
                    ):
                        raise ProjectStageRecordError("failed target prepared descriptor differs")
                    plan = store._reopen_project_stage_runtime_record(
                        descriptor.run_plan_ref,
                        record_type=ProjectStageRunPlan,
                        digest_field="plan_sha256",
                        expected_digest=descriptor.run_plan_sha256,
                        label="failed target original run plan",
                    )
                    launch_family = store._authority_layout.layout.family("runtime_exchange")
                    launch_bytes = store._read_ref(
                        descriptor.process_launch_ref,
                        max_bytes=launch_family.max_member_bytes,
                        label="failed target process launch",
                        optional=False,
                    )
                    launch = ProjectStageProcessLaunchRecord.from_mapping(
                        json.loads(launch_bytes.decode("utf-8", errors="strict"))
                    )
                    store.reopen_project_stage_process_launch(
                        descriptor.process_launch_ref,
                        expected_record=launch,
                        workspace_lease=self._workspace_lease,
                    )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target original records cannot be reopened"
                    ) from exc
                targets = tuple(item for item in plan.targets if item.node_id == node_id)
                if len(targets) != 1:
                    raise ProjectStageRecordError("failed target original plan target differs")
                authorized = targets[0]
                if (
                    plan.launch_mode is not ProjectStageLaunchMode.HEADLESS_PROJECT_SCRIPT
                    or plan.target_script_asset_id != expected_binding[1]
                    or plan.project_identity_sha256 != binding.project_identity_sha256
                    or plan.request_sha256 != mutation.mutation_request_sha256
                    or descriptor.target_script_asset_id != expected_binding[1]
                    or descriptor.script_bundle_sha256 != plan.script_bundle_sha256
                    or descriptor.script_bundle_manifest_ref != plan.script_bundle_manifest_ref
                    or descriptor.target_script_ref != plan.target_script_ref
                    or descriptor.target_script_sha256 != plan.target_script_sha256
                    or descriptor.guard_script_ref != plan.guard_script_ref
                    or descriptor.guard_script_sha256 != plan.guard_script_sha256
                    or descriptor.project_identity_sha256 != plan.project_identity_sha256
                    or descriptor.project_authority_head_sha256
                    != plan.project_authority_head_sha256
                    or descriptor.run_plan_sha256 != mutation.run_plan_sha256
                    or descriptor.prepared_descriptor_sha256 != attempt.prepared_descriptor_sha256
                    or authorized.attempt_id != terminal.attempt_id
                    or authorized.attempt_authorization_sha256 != terminal.authorization_sha256
                    or authorized.exchange.exchange_sha256 != terminal.execution_exchange_sha256
                    or (
                        authorized.adapter_spec_sha256,
                        authorized.request_codec_id,
                        authorized.wire_stage_id,
                        authorized.terminal_stage_id,
                        authorized.request_codec_version,
                    )
                    != expected_binding
                    or authorized.authorized_entry_sha256 != failure.get("authorized_entry_sha256")
                    or authorized.project_entry_id != observation.project_entry_id
                    or authorized.authorized_entry_sha256 != observation.authorized_entry_sha256
                    or authorized.saved_state_before_sha256 != observation.saved_state_before_sha256
                    or observation.saved_state_disposition != failure["saved_state_disposition_code"]
                    or observation.saved_state_after_sha256 is None
                    or (observation.saved_state_disposition == "authenticated_unchanged"
                        and observation.saved_state_after_sha256 != authorized.saved_state_before_sha256)
                    or observation.result_ref != authorized.exchange.result_ref
                    or observation.generation_manifest_ref
                    != authorized.exchange.generation_manifest_ref
                    or observation.observation_sha256 != failure.get("target_observation_sha256")
                    or plan.plan_sha256 != failure.get("run_plan_sha256")
                    or drain.termination_reason != failure.get("process_termination_code")
                    or mutation.termination_reason != drain.termination_reason
                    or mutation.attempt_sha256 != attempt.attempt_sha256
                    or mutation.process_launch_sha256 != launch.process_launch_sha256
                    or drain.process_launch_sha256 != launch.process_launch_sha256
                    or drain.attempt_sha256 != attempt.attempt_sha256
                    or launch.attempt_sha256 != attempt.attempt_sha256
                    or launch.process_launch_ref != descriptor.process_launch_ref
                    or mutation.stdout_log_ref != drain.stdout_log_ref
                    or mutation.stderr_log_ref != drain.stderr_log_ref
                    or drain.containment_drained is not True
                    or attempt.run_plan_sha256 != plan.plan_sha256
                    or launch.run_plan_sha256 != plan.plan_sha256
                    or drain.run_plan_sha256 != plan.plan_sha256
                    or attempt.project_identity_sha256 != plan.project_identity_sha256
                    or launch.project_identity_sha256 != plan.project_identity_sha256
                    or drain.project_identity_sha256 != plan.project_identity_sha256
                    or attempt.active_mutation_run_sha256 != mutation.active_mutation_run_sha256
                    or launch.active_mutation_run_sha256 != mutation.active_mutation_run_sha256
                    or drain.active_mutation_run_sha256 != mutation.active_mutation_run_sha256
                    or attempt.launch_nonce_sha256 != descriptor.launch_nonce_sha256
                    or launch.launch_nonce_sha256 != descriptor.launch_nonce_sha256
                    or drain.launch_nonce_sha256 != descriptor.launch_nonce_sha256
                    or launch.process_start_token != drain.process_start_token
                    or descriptor.stdout_log_ref != drain.stdout_log_ref
                    or descriptor.stderr_log_ref != drain.stderr_log_ref
                ):
                    raise ProjectStageRecordError("failed target original authority differs")

                family = store._authority_layout.layout.family("runtime_exchange")
                log_family = store._authority_layout.layout.family("runtime_log")
                asset_family = store._authority_layout.layout.family("runtime_asset")
                evidence_family = store._authority_layout.layout.family(
                    "batch_project_runtime_evidence"
                )
                try:
                    request_bytes = store._read_ref(
                        authorized.exchange.request_ref,
                        max_bytes=family.max_member_bytes,
                        label="failed target request",
                        optional=False,
                    )
                    result_bytes = store._read_ref(
                        authorized.exchange.result_ref,
                        max_bytes=family.max_member_bytes,
                        label="failed target result",
                        optional=True,
                    )
                    manifest_bytes = store._read_ref(
                        authorized.exchange.generation_manifest_ref,
                        max_bytes=family.max_member_bytes,
                        label="failed target manifest",
                        optional=True,
                    )
                    stdout = store._read_ref(
                        drain.stdout_log_ref,
                        max_bytes=log_family.max_member_bytes,
                        label="failed target stdout",
                        optional=False,
                    )
                    stderr = store._read_ref(
                        drain.stderr_log_ref,
                        max_bytes=log_family.max_member_bytes,
                        label="failed target stderr",
                        optional=False,
                    )
                    bundle = store._read_ref(
                        plan.script_bundle_manifest_ref,
                        max_bytes=evidence_family.max_member_bytes,
                        label="failed target script bundle",
                        optional=False,
                    )
                    target_script = store._read_ref(
                        plan.target_script_ref,
                        max_bytes=asset_family.max_member_bytes,
                        label="failed target script asset",
                        optional=False,
                    )
                    guard_script = store._read_ref(
                        plan.guard_script_ref,
                        max_bytes=asset_family.max_member_bytes,
                        label="failed target guard asset",
                        optional=False,
                    )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target named exchange or log changed"
                    ) from exc
                if (
                    hashlib.sha256(request_bytes).hexdigest()
                    != authorized.exchange.request_envelope_sha256
                    or len(request_bytes) != authorized.exchange.request_envelope_byte_count
                    or hashlib.sha256(stdout).hexdigest() != mutation.stdout_payload_sha256
                    or len(stdout) != mutation.stdout_byte_count
                    or hashlib.sha256(stderr).hexdigest() != mutation.stderr_payload_sha256
                    or len(stderr) != mutation.stderr_byte_count
                    or hashlib.sha256(bundle).hexdigest() != plan.script_bundle_manifest_sha256
                    or len(bundle) != plan.script_bundle_manifest_byte_count
                    or hashlib.sha256(target_script).hexdigest() != plan.target_script_sha256
                    or len(target_script) != plan.target_script_byte_count
                    or hashlib.sha256(guard_script).hexdigest() != plan.guard_script_sha256
                    or len(guard_script) != plan.guard_script_byte_count
                ):
                    raise ProjectStageRecordError("failed target exchange or logs differ")

                complete_exchange = failure["transport_validation_code"] == "target_codec_rejected"
                invalid_exchange = failure["transport_validation_code"] == "result_payload_invalid"
                if invalid_exchange:
                    # Preserve the rejected producer bytes as failure provenance, never as scientific output.
                    for payload, digest, size in (
                        (result_bytes, observation.result_payload_sha256, observation.result_byte_count),
                        (manifest_bytes, observation.generation_manifest_payload_sha256,
                         observation.generation_manifest_byte_count),
                    ):
                        if (payload is None or hashlib.sha256(payload).hexdigest() != digest
                                or len(payload) != size):
                            raise ProjectStageRecordError("failed target rejected exchange differs")
                elif complete_exchange:
                    if result_bytes is None or manifest_bytes is None:
                        raise ProjectStageRecordError("failed target complete exchange is missing")
                    try:
                        parsed = parse_stage_result_exchange_bytes(
                            result_bytes,
                            manifest_bytes,
                            invocation_id=authorized.attempt_id,
                            run_id=authorized.pipeline_run_id,
                            stage=authorized.wire_stage_id,
                            slide_id=authorized.slide_id,
                            generation_manifest_name=(
                                authorized.exchange.generation_manifest_ref.physical_name
                            ),
                        )
                    except ExchangeContractError as exc:
                        raise ProjectStageRecordError(
                            "failed target complete exchange is invalid"
                        ) from exc
                    if (
                        parsed.result.status is not StageStatus.COMPLETED
                        or parsed.result_payload_sha256 != observation.result_payload_sha256
                        or parsed.result_byte_count != observation.result_byte_count
                        or parsed.generation_manifest_payload_sha256
                        != observation.generation_manifest_payload_sha256
                        or parsed.generation_manifest_byte_count
                        != observation.generation_manifest_byte_count
                    ):
                        raise ProjectStageRecordError("failed target recorded snapshot differs")
                elif any(value is not None for value in (
                    result_bytes,
                    manifest_bytes,
                    observation.result_payload_sha256,
                    observation.result_byte_count,
                    observation.generation_manifest_payload_sha256,
                    observation.generation_manifest_byte_count,
                )):
                    raise ProjectStageRecordError("failed target absent exchange differs")

                common_names = (
                    set(mutation.__dataclass_fields__) & set(attempt.__dataclass_fields__)
                ) - {"schema_name", "schema_version"}
                if any(getattr(mutation, name) != getattr(attempt, name) for name in common_names):
                    raise ProjectStageRecordError(
                        "failed target mutation and process attempt differ"
                    )
                attempt_payload = canonical_json_bytes(attempt.to_mapping())
                drain_payload = canonical_json_bytes(drain.to_mapping())
                try:
                    process_evidence_sha256 = validate_project_stage_process_evidence(
                        attempt=attempt,
                        attempt_ref=attempt_ref,
                        attempt_payload_sha256=hashlib.sha256(attempt_payload).hexdigest(),
                        attempt_byte_count=len(attempt_payload),
                        launch=launch,
                        launch_ref=descriptor.process_launch_ref,
                        launch_payload_sha256=hashlib.sha256(launch_bytes).hexdigest(),
                        launch_byte_count=len(launch_bytes),
                        drain=drain,
                        drain_ref=drain_ref,
                        drain_payload_sha256=hashlib.sha256(drain_payload).hexdigest(),
                        drain_byte_count=len(drain_payload),
                    )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target native process chain differs"
                    ) from exc
                try:
                    summary = ProjectStageProcessSummary.create(
                        process_start_token=drain.process_start_token,
                        process_evidence_sha256=process_evidence_sha256,
                        termination_reason=drain.termination_reason,
                        exit_status=drain.exit_status,
                        stdout_log_ref=drain.stdout_log_ref,
                        stdout_payload_sha256=mutation.stdout_payload_sha256,
                        stdout_byte_count=mutation.stdout_byte_count,
                        stderr_log_ref=drain.stderr_log_ref,
                        stderr_payload_sha256=mutation.stderr_payload_sha256,
                        stderr_byte_count=mutation.stderr_byte_count,
                        containment_drained=True,
                    )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target process summary is invalid"
                    ) from exc
                completion_sha256 = _digest(
                    {
                        "disposition": "released_child",
                        "evidence_set_sha256": process_evidence_sha256,
                        "proof_sha256": None,
                        "summary_sha256": summary.summary_sha256,
                    }
                )
                if (
                    observation.process_evidence_sha256 != process_evidence_sha256
                    or completion_sha256 != failure.get("process_completion_sha256")
                ):
                    raise ProjectStageRecordError("failed target completion evidence differs")

                try:
                    current_saved = state.current_project_entry_saved_state_sha256_by_id(
                        expected_head_sha256=head.sha256,
                        lease=self._workspace_lease,
                    )
                    catalogue = store.load_current()
                    entry = catalogue.resolve(authorized.project_entry_id)
                    current_qpproj_sha256 = hashlib.sha256(
                        Path(head.project_path).read_bytes()
                    ).hexdigest()
                    provider = state._project_mutation_provider
                    cursor = provider.load_authority_head(head.project_identity_sha256)
                    visited: set[str] = set()
                    mutation_in_current_lineage = False
                    while cursor.authority_head_sha256 not in visited:
                        visited.add(cursor.authority_head_sha256)
                        attestation_sha256 = cursor.latest_mutation_attestation_sha256
                        if attestation_sha256 is None:
                            break
                        attestation = provider.load_mutation_attestation(
                            head.project_identity_sha256, attestation_sha256
                        )
                        if (
                            attestation.mutation_kind == "project_stage"
                            and attestation.operation_evidence_sha256
                            == mutation.mutation_evidence_sha256
                        ):
                            mutation_in_current_lineage = True
                            break
                        predecessor = cursor.predecessor_authority_head_sha256
                        if predecessor is None:
                            break
                        cursor = provider._load_head_version(
                            head.project_identity_sha256, predecessor
                        )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target current project cannot be authenticated"
                    ) from exc
                if (
                    not mutation_in_current_lineage
                    or mutation.predecessor_project_authority_head_sha256
                    != attestation.predecessor_authority_head_sha256
                    or catalogue.catalogue_sha256 != head.project_catalogue_sha256
                    or entry.entry_projection_sha256 != binding.project_entry_projection_sha256
                    or entry.sample_id != authorized.sample_id
                    or entry.slide_id != authorized.slide_id
                    or entry.image_sha256 != binding.current_image_identity_sha256
                    or head.sample_project_entry_id_by_sample[authorized.sample_id]
                    != authorized.project_entry_id
                    or head.sample_project_entry_projection_sha256_by_sample[authorized.sample_id]
                    != entry.entry_projection_sha256
                    or catalogue.project_uri != plan.project_uri
                    or head.project_identity_sha256 != plan.project_identity_sha256
                    or current_qpproj_sha256 != head.project_qpproj_snapshot_sha256
                ):
                    raise ProjectStageRecordError("failed target current project or entry differs")

                context = _reopen_retained_project_stage_context(
                    state=state,
                    head=head,
                    terminal=terminal,
                    authorization=authorization,
                    spec=spec,
                    node_id=node_id,
                )
                if failure.get("attempt_authorization_sha256") != (
                    authorization.authorization_sha256
                ):
                    raise ProjectStageRecordError("failed target scheduler context differs")
                try:
                    manifest = state.retained_manifest(head.normalized_manifest_sha256)
                    sample = tuple(
                        item for item in manifest.samples if item.sample_id == authorized.sample_id
                    )
                    if (
                        len(sample) != 1
                        or sample[0].slide_id != authorized.slide_id
                        or sample[0].resolved_config_sha256 != authorized.resolved_config_sha256
                        or head.resolved_config_sha256_by_sample[authorized.sample_id]
                        != authorized.resolved_config_sha256
                    ):
                        raise ValueError("source or configuration changed")
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "failed target source or configuration differs"
                    ) from exc
                try:
                    if state.load_head() != head:
                        raise ProjectStageRecordError("failed target state head changed")
                    for ref in (() if complete_exchange or invalid_exchange else (
                        authorized.exchange.result_ref,
                        authorized.exchange.generation_manifest_ref,
                    )):
                        if (
                            store._read_ref(
                                ref,
                                max_bytes=family.max_member_bytes,
                                label="failed target absent exchange",
                                optional=True,
                            )
                            is not None
                        ):
                            raise ProjectStageRecordError("failed target exchange appeared")
                    if invalid_exchange:
                        for ref, original in ((authorized.exchange.result_ref, result_bytes),
                                              (authorized.exchange.generation_manifest_ref, manifest_bytes)):
                            if store._read_ref(ref, max_bytes=family.max_member_bytes,
                                    label="failed target rejected exchange", optional=False) != original:
                                raise ProjectStageRecordError("failed target rejected exchange changed")
                    current_project_head = provider.load_authority_head(
                        head.project_identity_sha256
                    )
                    if (
                        current_project_head.authority_head_sha256
                        != head.project_authority_head_sha256
                        or hashlib.sha256(Path(head.project_path).read_bytes()).hexdigest()
                        != head.project_qpproj_snapshot_sha256
                    ):
                        raise ProjectStageRecordError("failed target current project changed")
                except ProjectStageRecordError:
                    raise
                except Exception as exc:
                    raise ProjectStageRecordError("failed target named authority changed") from exc
                completed_evidence = None
                if complete_exchange and node["kind"] in {
                    "marker_rule_application", "analysis_domain_selection",
                    "saved_result_retirement", "segmentation_correction",
                }:
                    envelope = json.loads(request_bytes.decode("utf-8"))
                    request = envelope["request"]
                    injected = {"project_file", "project_entry_id", "canonical_image_uri",
                        "canonical_image_sha256", "script_bundle_sha256", "script_asset_path",
                        "script_asset_sha256", "exchange_version", "resolved_config", "config_hash",
                        "operational_output_dir", "producer_output_dirs"}
                    scientific_body = _retained_project_stage_json(
                        {key: value for key, value in request.items() if key not in injected})
                    validation_inputs = ProjectStageValidationInputAuthority._issue(
                        context=context, authorized=authorized,
                        scientific_request_body=scientific_body,
                        scientific_request_body_sha256=authorized.scientific_request_body_sha256,
                        scientific_request_body_byte_count=authorized.scientific_request_body_byte_count,
                        resolved_config=_retained_project_stage_json(sample[0].config.to_mapping()),
                        operational_output_dir=_project_stage_operation_output_dir(
                            request, sample[0].config.to_mapping()),
                        request_capability_sha256=_digest({"failed_terminal": terminal_sha256,
                            "target_request": authorized.target_request_sha256}),
                        _issuer=_PROJECT_STAGE_VALIDATION_INPUT_ISSUER,
                    )
                    completed_evidence = _issue_project_stage_target_execution_evidence(
                        context=context, authorized=authorized, observation=observation,
                        parsed_exchange=parsed,
                        execution_exchange_sha256=authorized.exchange.exchange_sha256,
                        validation_inputs=validation_inputs,
                    )
                value = object.__new__(_ReopenedProjectStageFailure)
                for name, item in {
                    "context": context,
                    "authorized": authorized,
                    "run_plan": plan,
                    "prepared_descriptor": descriptor,
                    "mutation_evidence": mutation,
                    "target_observation": observation,
                    "state_head_sha256": head.sha256,
                    "active_plan_sha256": head.active_plan_sha256,
                    "terminal_record_sha256": terminal_sha256,
                    "completed_evidence": completed_evidence,
                    "_issuer": _PROJECT_STAGE_REOPENED_EXCHANGE_ISSUER,
                }.items():
                    object.__setattr__(value, name, item)
                return value

    def _reopen_exact_exchange_for_owner(
        self,
        *,
        authority: object | None,
        retry_context: BatchNodeExecutionContext | None,
        producer_artifact_ref_sha256: str | None,
        producer_node_id: str | None,
        producer_attempt_id: str | None,
        producer_attempt_authorization_sha256: str | None,
        terminal_record_sha256: str | None,
        execution_exchange_sha256: str | None,
        expected_binding: tuple[str, ...],
        review_context: BatchNodeExecutionContext | None = None,
        review_execution: object | None = None,
    ) -> _ReopenedProjectStageExchange | _ProjectStageRetryResumeAuthority | None:
        """Reissue one exact retained exchange to its synchronous stage owner."""

        from .batch_artifacts import (
            BatchCommittedArtifactAuthority,
            BatchNodeExecutionContext,
            validate_committed_artifact_authority_set,
        )
        from .batch_manifest import revalidate_retained_manifest
        from .project_stage_codecs import ProjectStageCodecError
        from mif_pipeline.qupath_runtime.exchange import (
            ExchangeContractError,
            parse_stage_result_exchange_bytes,
        )
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceRecord,
            ProjectStageProcessAttemptRecord,
            ProjectStageProcessDrainRecord,
        )

        review_backed = review_context is not None
        artifact_backed = retry_context is None and not review_backed
        if (
            type(self) is not ProjectStageCommittedExchangeReopener
            or self._issuer is not _PROJECT_STAGE_COMMITTED_EXCHANGE_REOPENER_ISSUER
            or type(expected_binding) is not tuple
            or len(expected_binding) not in {4, 5}
            or (artifact_backed and type(authority) is not BatchCommittedArtifactAuthority)
            or (review_backed and (authority is not None or retry_context is not None
                or type(review_context) is not BatchNodeExecutionContext))
            or (
                not artifact_backed and not review_backed
                and (authority is not None or type(retry_context) is not BatchNodeExecutionContext)
            )
        ):
            raise ProjectStageRecordError("committed exchange reopen authority is invalid")
        artifact_digest: str | None = None
        if artifact_backed:
            artifact_digest = _sha(producer_artifact_ref_sha256, "producer_artifact_ref_sha256")
            node_id = _safe_id(producer_node_id, "producer_node_id")
            attempt_id = _safe_id(producer_attempt_id, "producer_attempt_id")
            authorization_sha256 = _sha(
                producer_attempt_authorization_sha256,
                "producer_attempt_authorization_sha256",
            )
            terminal_sha256 = _sha(terminal_record_sha256, "terminal_record_sha256")
            exchange_sha256 = _sha(execution_exchange_sha256, "execution_exchange_sha256")
        binding = tuple(expected_binding)
        if any(type(item) is not str for item in binding):
            raise ProjectStageRecordError("committed exchange codec binding is invalid")

        runtime = self._runtime
        with runtime._operation():
            with runtime._state_store._lease_operation(self._workspace_lease):
                if artifact_backed:
                    assert type(authority) is BatchCommittedArtifactAuthority
                    validate_committed_artifact_authority_set(
                        (authority,),
                        expected_active_plan_sha256=self._active_plan_sha256,
                    )
                head = runtime._state_store.load_head()
                if (
                    head.sha256 != self._state_head_sha256
                    or head.active_plan_sha256 != self._active_plan_sha256
                ):
                    raise ProjectStageRecordError("committed exchange state head or plan changed")
                accepted_response: Mapping[str, object] | None = None
                current_execution = None
                if artifact_backed:
                    assert type(authority) is BatchCommittedArtifactAuthority
                    assert artifact_digest is not None
                    try:
                        reopened_authority = runtime._state_store.authenticate_available_artifact(
                            authority.artifact_ref
                        )
                    except Exception as exc:
                        raise ProjectStageRecordError(
                            "committed artifact authority cannot be reauthenticated"
                        ) from exc
                    if reopened_authority != authority:
                        raise ProjectStageRecordError(
                            "committed artifact authority changed after restart"
                        )
                    try:
                        terminal = runtime._state_store.load_attempt(terminal_sha256)
                        authorization = runtime._state_store.load_authorization(
                            authorization_sha256
                        )
                    except Exception as exc:
                        raise ProjectStageRecordError(
                            "committed producer attempt cannot be reopened"
                        ) from exc
                    matching_outputs = tuple(
                        item
                        for item in terminal.output_artifacts
                        if item.artifact_ref_sha256 == artifact_digest
                    )
                    if (
                        terminal.terminal_record_sha256 != terminal_sha256
                        or terminal.node_id != node_id
                        or terminal.attempt_id != attempt_id
                        or terminal.authorization_sha256 != authorization_sha256
                        or terminal.execution_exchange_sha256 != exchange_sha256
                        or authorization.node_id != node_id
                        or authorization.attempt_id != attempt_id
                        or len(matching_outputs) != 1
                    ):
                        raise ProjectStageRecordError("committed producer attempt differs")
                    target_project_entry_id = matching_outputs[0].scope.project_entry_id
                elif review_backed:
                    try:
                        current_execution = review_execution
                        selected = runtime._state_store.settled_review_predecessor(
                            review_context, execution=current_execution)
                    except (TypeError, ValueError) as exc:
                        raise ProjectStageRecordError("settled review predecessor cannot be admitted") from exc
                    if selected is None or selected[0].stage_result.status is StageStatus.FAILED:
                        return None
                    terminal, authorization = selected
                    if (len(terminal.output_artifacts) != 1
                            or len(current_execution.project_entry_ids) != 1
                            or terminal.output_artifacts[0].scope.project_entry_id != current_execution.project_entry_ids[0]
                            or terminal.execution_context_binding.project_identity_sha256 != review_context.project_identity_sha256):
                        raise ProjectStageRecordError("settled review native producer scope differs")
                    source = terminal.output_artifacts[0]
                    if (current_execution.node_kind == "analysis_domain_selection"
                            and source.artifact_kind == "accepted_analysis_domain"
                            and source.owning_identity["selection_mode"] in {
                                "whole_tissue_direct", "whole_tissue_after_retirement"}):
                        # Whole-section decisions have no active selected-ROI
                        # manifest to reuse. The exact typed producer is enough.
                        return None
                    if terminal.execution_exchange_sha256 is None:
                        raise ProjectStageRecordError("settled review predecessor lacks its native exchange")
                    node_id, attempt_id = terminal.node_id, terminal.attempt_id
                    authorization_sha256 = authorization.authorization_sha256
                    terminal_sha256 = terminal.terminal_record_sha256
                    exchange_sha256 = terminal.execution_exchange_sha256
                    target_project_entry_id = current_execution.project_entry_ids[0]
                else:
                    assert type(retry_context) is BatchNodeExecutionContext
                    retry_authorization = retry_context.authorization
                    predecessor_sha256 = (
                        retry_authorization.interaction_predecessor_terminal_record_sha256
                    )
                    if (
                        retry_authorization.retry_ordinal <= 0
                        or retry_authorization.interaction_response_sha256 is None
                        or predecessor_sha256 is None
                    ):
                        raise ProjectStageRecordError("retry predecessor lineage is incomplete")
                    try:
                        current_execution = (
                            runtime._state_store.authenticate_current_execution_context(
                                retry_context
                            )
                        )
                        accepted_response = (
                            runtime._state_store.load_validated_interaction_response(
                                retry_authorization
                            )
                        )
                        terminal = runtime._state_store._validate_retry_predecessor(
                            head,
                            retry_context.node_id,
                            predecessor_sha256,
                            retry_authorization.retry_ordinal,
                        )
                        authorization = runtime._state_store.load_authorization(
                            terminal.authorization_sha256
                        )
                    except Exception as exc:
                        raise ProjectStageRecordError(
                            "retry predecessor cannot be authenticated"
                        ) from exc
                    if (
                        accepted_response is None
                        or terminal.execution_exchange_sha256 is None
                        or terminal.output_artifacts
                        or authorization.node_id != retry_context.node_id
                        or authorization.attempt_id != terminal.attempt_id
                        or retry_context.project_identity_sha256
                        != terminal.execution_context_binding.project_identity_sha256
                        or current_execution.node_kind != retry_context.adapter_spec.node_kind
                        or current_execution.project_entry_ids
                        != retry_authorization.dispatch_binding.project_entry_ids
                        or len(current_execution.project_entry_ids) != 1
                    ):
                        raise ProjectStageRecordError("retry predecessor execution scope differs")
                    node_id = terminal.node_id
                    attempt_id = terminal.attempt_id
                    authorization_sha256 = terminal.authorization_sha256
                    terminal_sha256 = terminal.terminal_record_sha256
                    exchange_sha256 = _sha(
                        terminal.execution_exchange_sha256,
                        "retry predecessor execution_exchange_sha256",
                    )
                    target_project_entry_id = current_execution.project_entry_ids[0]

                if ((artifact_backed or review_backed)
                        and binding[-2] == "analysis_domain_selection"
                        and authorization.failed_retry is not None):
                    # The accepted artifact belongs to the fresh Retry. Its
                    # native exchange belongs to the exact failed predecessor,
                    # even if that predecessor itself followed an earlier retry.
                    # Follow one recorded link; the native chain below must
                    # authenticate that immediate attempt, never an older one.
                    source_terminal = runtime._state_store.load_attempt(
                        authorization.failed_retry.terminal_record_sha256)
                    # An ordinary Retry may have launched its own native work.
                    # Only a reused exchange selects the original failed attempt;
                    # a new exchange keeps the current native producer below.
                    if source_terminal.execution_exchange_sha256 == exchange_sha256:
                        source_authorization = runtime._state_store.load_authorization(
                            source_terminal.authorization_sha256)
                        source_binding = source_terminal.execution_context_binding
                        current_binding = terminal.execution_context_binding
                        if (
                            terminal.stage_result.status is not StageStatus.COMPLETED
                            or source_terminal.stage_result.status is not StageStatus.FAILED
                            or source_terminal.output_artifacts
                            or not _project_stage_failure_can_retry(
                                "analysis_domain_selection", source_terminal.stage_result)
                            or source_terminal.stage_result.error_context.get("process_termination_code") != "normal_exit"
                            or source_terminal.stage_result.error_context.get("transport_validation_code") != "target_codec_rejected"
                            or source_terminal.node_id != node_id
                            or source_authorization.node_id != node_id
                            or source_authorization.attempt_id != source_terminal.attempt_id
                            or source_authorization.retry_ordinal + 1 != authorization.retry_ordinal
                            or head.attempt_ids_by_node.get(node_id, ())[
                                authorization.retry_ordinal - 1:authorization.retry_ordinal]
                            != (source_terminal.attempt_id,)
                            or head.terminal_attempt_sha256_by_attempt_id.get(source_terminal.attempt_id)
                            != authorization.failed_retry.terminal_record_sha256
                            or source_authorization.node_material_sha256 != authorization.node_material_sha256
                            or source_authorization.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
                            or source_authorization.dispatch_binding.project_entry_ids != (target_project_entry_id,)
                            or any(getattr(source_binding, field) != getattr(current_binding, field)
                                for field in ("adapter_spec_sha256", "current_image_identity_sha256",
                                              "project_entry_projection_sha256", "project_identity_sha256"))
                        ):
                            raise ProjectStageRecordError("accepted region Retry differs from its native predecessor")
                        terminal, authorization = source_terminal, source_authorization
                        attempt_id = terminal.attempt_id
                        authorization_sha256 = authorization.authorization_sha256
                        terminal_sha256 = terminal.terminal_record_sha256

                if len(binding) == 5:
                    adapter_sha, asset_id, wire_stage, terminal_stage, codec_version = binding
                else:
                    asset_id, wire_stage, terminal_stage, codec_version = binding
                    adapter_sha = terminal.execution_context_binding.adapter_spec_sha256
                complete_binding = (
                    adapter_sha,
                    asset_id,
                    wire_stage,
                    terminal_stage,
                    codec_version,
                )
                try:
                    spec = runtime._codec_registry._resolve_adapter_spec(
                        adapter_spec_sha256=adapter_sha,
                        target_script_asset_id=asset_id,
                        wire_stage_id=wire_stage,
                        terminal_stage_id=terminal_stage,
                        request_codec_version=codec_version,
                    )
                except (ProjectStageCodecError, TypeError, ValueError) as exc:
                    raise ProjectStageRecordError(
                        "committed exchange lacks its exact registered binding"
                    ) from exc
                if (
                    spec.spec_sha256 != terminal.execution_context_binding.adapter_spec_sha256
                    or spec.node_kind != terminal_stage
                ):
                    raise ProjectStageRecordError(
                        "committed exchange adapter specification differs"
                    )

                store = runtime._catalogue_store(self._workspace_lease)
                records = _retained_project_stage_process_records(store)
                mutations = tuple(
                    item
                    for _ref, item in records
                    if type(item) is ProjectStageMutationEvidenceRecord
                    and item.project_identity_sha256
                    == terminal.execution_context_binding.project_identity_sha256
                    and item.termination_reason == "normal_exit"
                    and any(
                        observation.project_entry_id == target_project_entry_id
                        and observation.node_id == node_id
                        and observation.attempt_id == attempt_id
                        and observation.attempt_authorization_sha256 == authorization_sha256
                        for observation in item.target_observations
                    )
                )
                if len(mutations) != 1:
                    raise ProjectStageRecordError(
                        "committed retained owner target is missing or ambiguous"
                    )
                mutation = mutations[0]
                # Re-enumerate under the selected active-run scope so a linked
                # unsupported current/future record cannot hide beside an
                # otherwise valid candidate.
                records = _reopen_exact_retained_project_stage_mutation(store, mutation)
                observations = tuple(
                    item
                    for item in mutation.target_observations
                    if item.project_entry_id == target_project_entry_id
                    and item.node_id == node_id
                    and item.attempt_id == attempt_id
                    and item.attempt_authorization_sha256 == authorization_sha256
                )
                if len(observations) != 1:
                    raise ProjectStageRecordError(
                        "committed retained observation is missing or ambiguous"
                    )
                observation = observations[0]
                descriptors = tuple(
                    item
                    for _ref, item in records
                    if type(item) is PreparedProjectStageRunDescriptor
                    and item.active_mutation_run_sha256 == mutation.active_mutation_run_sha256
                    and item.mutation_request_sha256 == mutation.mutation_request_sha256
                    and item.run_plan_sha256 == mutation.run_plan_sha256
                    and item.project_identity_sha256 == mutation.project_identity_sha256
                    and item.target_script_asset_id == asset_id
                )
                if len(descriptors) != 1:
                    raise ProjectStageRecordError(
                        "committed retained descriptor is missing or ambiguous"
                    )
                descriptor = descriptors[0]
                attempts = tuple(
                    item
                    for _ref, item in records
                    if type(item) is ProjectStageProcessAttemptRecord
                    and item.active_mutation_run_sha256 == mutation.active_mutation_run_sha256
                    and item.run_plan_sha256 == descriptor.run_plan_sha256
                    and item.prepared_descriptor_sha256 == descriptor.prepared_descriptor_sha256
                )
                drains = tuple(
                    item
                    for _ref, item in records
                    if type(item) is ProjectStageProcessDrainRecord
                    and item.active_mutation_run_sha256 == mutation.active_mutation_run_sha256
                    and item.run_plan_sha256 == descriptor.run_plan_sha256
                    and item.prepared_descriptor_sha256 == descriptor.prepared_descriptor_sha256
                    and item.process_drain_sha256 == mutation.process_drain_sha256
                )
                if (
                    len(attempts) != 1
                    or len(drains) != 1
                    or attempts[0].attempt_sha256 != mutation.attempt_sha256
                    or drains[0].attempt_sha256 != attempts[0].attempt_sha256
                    or drains[0].process_launch_sha256 != mutation.process_launch_sha256
                ):
                    raise ProjectStageRecordError(
                        "committed retained process chain is missing or ambiguous"
                    )
                try:
                    plan = store._reopen_project_stage_runtime_record(
                        descriptor.run_plan_ref,
                        record_type=ProjectStageRunPlan,
                        digest_field="plan_sha256",
                        expected_digest=descriptor.run_plan_sha256,
                        label="committed project-stage run plan",
                    )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "committed retained run plan cannot be reopened"
                    ) from exc
                targets = tuple(
                    item
                    for item in plan.targets
                    if item.node_id == node_id
                    and item.attempt_id == attempt_id
                    and item.project_entry_id == observation.project_entry_id
                    and item.authorized_entry_sha256 == observation.authorized_entry_sha256
                    and item.exchange.exchange_sha256 == exchange_sha256
                    and (
                        item.adapter_spec_sha256,
                        item.request_codec_id,
                        item.wire_stage_id,
                        item.terminal_stage_id,
                        item.request_codec_version,
                    )
                    == complete_binding
                )
                if len(targets) != 1:
                    raise ProjectStageRecordError(
                        "committed run plan target is missing or ambiguous"
                    )
                authorized = targets[0]
                if (
                    authorized.exchange.result_ref != observation.result_ref
                    or authorized.exchange.generation_manifest_ref
                    != observation.generation_manifest_ref
                    or plan.request_sha256 != mutation.mutation_request_sha256
                ):
                    raise ProjectStageRecordError("committed retained target members differ")
                family = store._authority_layout.layout.family("runtime_exchange")
                try:
                    request_bytes = store._read_ref(
                        authorized.exchange.request_ref,
                        max_bytes=family.max_member_bytes,
                        label="committed project-stage request",
                        optional=False,
                    )
                    result_bytes = store._read_ref(
                        authorized.exchange.result_ref,
                        max_bytes=family.max_member_bytes,
                        label="committed project-stage result",
                        optional=False,
                    )
                    manifest_bytes = store._read_ref(
                        authorized.exchange.generation_manifest_ref,
                        max_bytes=family.max_member_bytes,
                        label="committed project-stage generation manifest",
                        optional=False,
                    )
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "committed retained exchange member is unavailable"
                    ) from exc
                assert request_bytes is not None
                assert result_bytes is not None
                assert manifest_bytes is not None
                if (
                    hashlib.sha256(request_bytes).hexdigest()
                    != authorized.exchange.request_envelope_sha256
                    or len(request_bytes) != authorized.exchange.request_envelope_byte_count
                    or hashlib.sha256(result_bytes).hexdigest() != observation.result_payload_sha256
                    or len(result_bytes) != observation.result_byte_count
                    or hashlib.sha256(manifest_bytes).hexdigest()
                    != observation.generation_manifest_payload_sha256
                    or len(manifest_bytes) != observation.generation_manifest_byte_count
                ):
                    raise ProjectStageRecordError("committed retained exchange member changed")
                try:
                    envelope = json.loads(request_bytes.decode("utf-8", errors="strict"))
                    if canonical_json_bytes(envelope) != request_bytes:
                        raise ValueError("request envelope is not canonical")
                    request = envelope["request"]
                    if not isinstance(request, Mapping):
                        raise ValueError("request body is not a mapping")
                    injected = {
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
                        "operational_output_dir",
                        "producer_output_dirs",
                    }

                    scientific_body = _retained_project_stage_json(
                        {key: value for key, value in request.items() if key not in injected}
                    )
                    scientific_payload = canonical_json_bytes(scientific_body)
                    if (
                        envelope.get("schema_version") != "roi-stage-request/v2"
                        or envelope.get("invocation_id") != attempt_id
                        or envelope.get("run_id") != authorized.pipeline_run_id
                        or envelope.get("stage") != wire_stage
                        or envelope.get("slide_id") != authorized.slide_id
                        or request.get("project_entry_id") != authorized.project_entry_id
                        or request.get("exchange_version") != "roi-stage-request/v2"
                        or hashlib.sha256(scientific_payload).hexdigest()
                        != authorized.scientific_request_body_sha256
                        or len(scientific_payload) != authorized.scientific_request_body_byte_count
                    ):
                        raise ValueError("request envelope authority differs")
                    parsed = parse_stage_result_exchange_bytes(
                        result_bytes,
                        manifest_bytes,
                        invocation_id=attempt_id,
                        run_id=authorized.pipeline_run_id,
                        stage=wire_stage,
                        slide_id=authorized.slide_id,
                        generation_manifest_name=(
                            authorized.exchange.generation_manifest_ref.physical_name
                        ),
                        _manifest_required=lambda _result: True,
                    )
                except (
                    KeyError,
                    TypeError,
                    UnicodeError,
                    ValueError,
                    json.JSONDecodeError,
                    ExchangeContractError,
                ) as exc:
                    raise ProjectStageRecordError("committed retained exchange is invalid") from exc

                context = _reopen_retained_project_stage_context(
                    state=runtime._state_store,
                    head=head,
                    terminal=terminal,
                    authorization=authorization,
                    spec=spec,
                    node_id=node_id,
                )
                try:
                    retained_manifest = runtime._state_store.retained_manifest(head.normalized_manifest_sha256)
                    samples = tuple(
                        item
                        for item in retained_manifest.samples
                        if item.sample_id == authorized.sample_id
                    )
                    if len(samples) != 1:
                        raise ValueError("resolved sample is ambiguous")
                    resolved_config = _retained_project_stage_json(samples[0].config.to_mapping())
                    if samples[0].resolved_config_sha256 != authorized.resolved_config_sha256:
                        raise ValueError("resolved sample digest differs")
                    if not artifact_backed and (
                        current_execution is None
                        or type(review_context if review_backed else retry_context) is not BatchNodeExecutionContext
                        or authorized.sample_id not in current_execution.participant_ids
                        or samples[0].sample_id != authorized.sample_id
                        or samples[0].slide_id != authorized.slide_id
                        or authorized.project_entry_id != target_project_entry_id
                        or (review_context if review_backed else retry_context).adapter_spec.spec_sha256 != adapter_sha
                    ):
                        raise ValueError("retry predecessor target differs")
                except Exception as exc:
                    raise ProjectStageRecordError(
                        "committed retained resolved configuration differs"
                    ) from exc
                request_capability_sha256 = _digest(
                    {
                        "node_id": node_id,
                        "attempt_id": attempt_id,
                        "attempt_authorization_sha256": authorization_sha256,
                        "input_artifact_set_sha256": authorization.input_artifact_set_sha256,
                        "adapter_spec_sha256": adapter_sha,
                        "target_request_sha256": authorized.target_request_sha256,
                        "request_codec_id": asset_id,
                        "request_codec_version": codec_version,
                        "scientific_request_body": scientific_body,
                        "scientific_request_body_sha256": authorized.scientific_request_body_sha256,
                        "scientific_request_body_byte_count": authorized.scientific_request_body_byte_count,
                    }
                )
                validation_inputs = ProjectStageValidationInputAuthority._issue(
                    context=context,
                    authorized=authorized,
                    scientific_request_body=scientific_body,
                    scientific_request_body_sha256=authorized.scientific_request_body_sha256,
                    scientific_request_body_byte_count=authorized.scientific_request_body_byte_count,
                    resolved_config=resolved_config,
                    operational_output_dir=_project_stage_operation_output_dir(
                        request, resolved_config),
                    request_capability_sha256=request_capability_sha256,
                    _issuer=_PROJECT_STAGE_VALIDATION_INPUT_ISSUER,
                )
                evidence = _issue_project_stage_target_execution_evidence(
                    context=context,
                    authorized=authorized,
                    observation=observation,
                    parsed_exchange=parsed,
                    execution_exchange_sha256=exchange_sha256,
                    validation_inputs=validation_inputs,
                )
                if not artifact_backed and not review_backed:
                    from .batch_state import batch_interaction_request_id

                    assert type(retry_context) is BatchNodeExecutionContext
                    assert accepted_response is not None
                    action = accepted_response.get("action")
                    validated_resume = accepted_response.get("response")
                    service_context = terminal.stage_result.resume_context
                    interaction_context_sha256 = _digest(service_context)
                    interaction_request_id = batch_interaction_request_id(
                        node_id=node_id,
                        attempt_id=attempt_id,
                        attempt_authorization_sha256=authorization_sha256,
                        interaction_context_sha256=interaction_context_sha256,
                    )
                    try:
                        persisted_request = runtime._state_store.load_interaction_request(
                            interaction_request_id
                        )
                    except Exception as exc:
                        raise ProjectStageRecordError(
                            "retry predecessor interaction request is unavailable"
                        ) from exc
                    if (
                        type(action) is not str
                        or not isinstance(validated_resume, Mapping)
                        or validated_resume.get("action") != action
                        or not isinstance(validated_resume.get("response"), Mapping)
                        or not isinstance(service_context, Mapping)
                        or any(
                            validated_resume.get(key) != item
                            for key, item in service_context.items()
                        )
                        or accepted_response.get("interaction_request_id") != interaction_request_id
                        or accepted_response.get("interaction_context_sha256")
                        != interaction_context_sha256
                        or persisted_request != terminal.stage_result
                    ):
                        raise ProjectStageRecordError(
                            "retry predecessor response or compact context differs"
                        )
                    return _ProjectStageRetryResumeAuthority._issue(
                        current_context=retry_context,
                        accepted_action=action,
                        action_context=MappingProxyType(
                            {
                                "service_context": service_context,
                                "response": validated_resume["response"],
                            }
                        ),
                        predecessor_evidence=evidence,
                        _issuer=_PROJECT_STAGE_RETRY_RESUME_AUTHORITY_ISSUER,
                    )
                value = object.__new__(_ReopenedProjectStageExchange)
                for name, item in {
                    "request_bytes": bytes(request_bytes),
                    "result_bytes": bytes(result_bytes),
                    "generation_manifest_bytes": bytes(manifest_bytes),
                    "evidence": evidence,
                    "_issuer": _PROJECT_STAGE_REOPENED_EXCHANGE_ISSUER,
                }.items():
                    object.__setattr__(value, name, item)
                return value


def _containment_request_matches_partial_evidence(request: object, evidence: object) -> bool:
    """Authenticate every request/evidence identity present at a partial boundary."""

    provider = getattr(request, "provider", None)
    if provider != getattr(evidence, "provider", None):
        return False
    if provider == "windows-kill-on-close-job/v1":
        return getattr(request, "windows_job_name", None) == getattr(evidence, "job_name", None)
    if provider == "linux-systemd-scope-broker/v1":
        try:
            receipt = json.loads(evidence.attachment_receipt_json_utf8)
        except (AttributeError, TypeError, json.JSONDecodeError):
            return False
        return isinstance(receipt, Mapping) and receipt.get(
            "executable_registration_id"
        ) == getattr(request, "registration_id", None)
    return (
        provider == "linux-local-process-group/v1"
        and getattr(request, "registration_id", None) is None
        and getattr(request, "windows_job_name", None) is None
    )


class ProjectStageRuntime(_ProjectStageRuntimeIdentity):
    """Runtime-issued, path-free facade over one exact composed authority set."""

    __slots__ = (
        "_issuer",
        "_state_store",
        "_provider",
        "_runner",
        "_project_operations",
        "_runtime_binding",
        "_process_runner_binding",
        "_runtime_lifecycle",
        "_codec_registry",
        "_published_prepared_requests",
        "_sessions",
        "_active_project_leases",
        "_catalogue_stores",
        "_completed_processes",
        "_recovered_completions",
        "_validated_result_sets",
        "_validation_lifecycles",
        "_run_sessions",
        "_condition",
        "_lock",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProjectStageLifecycleError(
            "project-stage runtimes are issued only by BatchStateStore"
        )

    @classmethod
    def _issue(
        cls,
        *,
        state_store: object,
        provider: object,
        runner: object,
        project_operations: object,
        runtime_binding: RuntimeCompositionBinding,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        codec_registry: object,
        _issuer: object,
    ) -> "ProjectStageRuntime":
        if _issuer is not _PROJECT_STAGE_RUNTIME_ISSUER:
            raise ProjectStageLifecycleError("project-stage runtime issuer is invalid")
        runtime = object.__new__(cls)
        runtime._issuer = _issuer
        runtime._state_store = state_store
        runtime._provider = provider
        runtime._runner = runner
        runtime._project_operations = project_operations
        runtime._runtime_binding = runtime_binding
        from mif_pipeline.qupath_runtime.runner import QuPathRunner

        runtime._process_runner_binding = (
            runner._bind_project_stage_runtime(
                runtime=runtime,
                composition_binding=runtime_binding,
                project_operations=project_operations,
            )
            if isinstance(runner, QuPathRunner)
            else None
        )
        runtime._runtime_lifecycle = runtime_lifecycle
        from .project_stage_codecs import ProjectStageCodecRegistry

        if type(codec_registry) is not ProjectStageCodecRegistry:
            raise ProjectStageLifecycleError(
                "project-stage runtime requires one closed codec registry"
            )
        runtime._codec_registry = codec_registry
        runtime._published_prepared_requests = {}
        runtime._sessions = {}
        runtime._active_project_leases = {}
        runtime._catalogue_stores = {}
        runtime._completed_processes = {}
        runtime._recovered_completions = {}
        runtime._validated_result_sets = {}
        runtime._validation_lifecycles = {}
        runtime._run_sessions = {}
        runtime._lock = RLock()
        runtime._condition = Condition(runtime._lock)
        return runtime

    def _authenticates(
        self,
        *,
        state_store: object,
        provider: object,
        runner: object,
        project_operations: object,
        runtime_binding: RuntimeCompositionBinding,
        codec_registry: object,
    ) -> bool:
        return (
            self._issuer is _PROJECT_STAGE_RUNTIME_ISSUER
            and self._state_store is state_store
            and self._provider is provider
            and self._runner is runner
            and self._project_operations is project_operations
            and self._runtime_binding is runtime_binding
            and self._codec_registry is codec_registry
        )

    def require_composition_coherence(
        self,
        *,
        state_store: object,
        project_provider: object,
        runner: object,
        project_operations: object,
        expected_adapter_specs: tuple[StageAdapterSpec, ...],
    ) -> "ProjectStageRuntime":
        """Require this issued runtime to share owners and a closed spec subset."""

        if (
            type(self) is not ProjectStageRuntime
            or getattr(self, "_issuer", None) is not _PROJECT_STAGE_RUNTIME_ISSUER
            or getattr(self, "_state_store", None) is not state_store
            or getattr(self, "_provider", None) is not project_provider
            or getattr(self, "_runner", None) is not runner
            or getattr(self, "_project_operations", None) is not project_operations
        ):
            raise ProjectStageLifecycleError("project-stage runtime owners differ")
        if (
            type(expected_adapter_specs) is not tuple
            or not expected_adapter_specs
            or not all(
                type(spec) is StageAdapterSpec
                and spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
                for spec in expected_adapter_specs
            )
        ):
            raise ProjectStageLifecycleError(
                "project-stage expected adapter specifications are invalid"
            )
        expected = {(spec.node_kind, spec.spec_sha256) for spec in expected_adapter_specs}
        if len(expected) != len(expected_adapter_specs):
            raise ProjectStageLifecycleError(
                "project-stage expected adapter specifications are not closed"
            )
        try:
            registered = tuple(
                (
                    codec.binding.terminal_stage_id,
                    codec.binding.adapter_spec_sha256,
                )
                for codec in self._codec_registry._codecs.values()
            )
        except (AttributeError, TypeError) as exc:
            raise ProjectStageLifecycleError("project-stage codec registry is invalid") from exc
        expected_kinds = {node_kind for node_kind, _digest in expected}
        actual_subset = tuple(identity for identity in registered if identity[0] in expected_kinds)
        if len(actual_subset) != len(expected) or set(actual_subset) != expected:
            raise ProjectStageLifecycleError("project-stage adapter specification subset differs")
        return self

    @contextmanager
    def _operation(self) -> Iterator[None]:
        try:
            token = self._runtime_lifecycle.acquire("batch-state-operation")
        except RuntimeLifecycleError as exc:
            raise ProjectStageLifecycleError("project-stage runtime lifecycle is not open") from exc
        try:
            yield
        finally:
            token.release()

    def _catalogue_store(self, workspace_lease: object):
        with self._lock:
            issued = self._catalogue_stores.get(id(workspace_lease))
            if issued is not None:
                if issued[0] is not workspace_lease:
                    raise ProjectStageLifecycleError(
                        "project-stage workspace lease identity is ambiguous"
                    )
                return issued[1]
            store = self._state_store.project_catalogue_store(workspace_lease)
            self._catalogue_stores[id(workspace_lease)] = (workspace_lease, store)
            return store

    def committed_exchange_reopener(
        self, *, workspace_lease: object
    ) -> ProjectStageCommittedExchangeReopener:
        """Issue a process-local exact retained-exchange recovery capability."""

        with self._operation():
            with self._state_store._lease_operation(workspace_lease):
                # Authenticate the lease against the same catalogue authority
                # used for retained members before sealing the head.
                self._catalogue_store(workspace_lease)
                head = self._state_store.load_head()
                return ProjectStageCommittedExchangeReopener._issue(
                    runtime=self,
                    workspace_lease=workspace_lease,
                    state_head_sha256=head.sha256,
                    active_plan_sha256=head.active_plan_sha256,
                    _issuer=_PROJECT_STAGE_COMMITTED_EXCHANGE_REOPENER_ISSUER,
                )

    def _begin_result_validation_lifecycle(
        self,
        publication: object,
        prepared_request: object,
        completion: object,
        *,
        project_lease: object,
        workspace_lease: object,
    ) -> _ProjectStageUnresolvedLifecycle:
        with self._lock:
            published = self._published_prepared_requests.get(id(prepared_request))
            active = self._active_project_leases.get(id(project_lease))
            completed = self._completed_processes.get(id(completion))
            existing = self._validation_lifecycles.get(id(completion))
        if (
            published is None
            or published[0] is not prepared_request
            or published[2] is not workspace_lease
        ):
            raise ProjectStageRecordError(
                "project-stage prepared result authority is stale or foreign"
            )
        if (
            active is None
            or active[0] is not project_lease
            or active[2] is not prepared_request
            or active[4] is not workspace_lease
            or completed is None
            or completed[0] is not completion
            or completed[3] is not project_lease
            or completed[4] is not workspace_lease
            or type(publication) is not PublishedProjectStageRunPlan
            or completed[1].descriptor.run_plan_sha256 != publication.plan.plan_sha256
            or existing is not None
        ):
            raise ProjectStageLifecycleError(
                "project-stage result lifecycle authority is stale or foreign"
            )
        lifecycle = _ProjectStageUnresolvedLifecycle._issue(
            runtime=self,
            publication=publication,
            prepared_request=prepared_request,
            completion=completion,
            project_lease=project_lease,
            workspace_lease=workspace_lease,
            terminal_committer=published[3],
            _issuer=_PROJECT_STAGE_RUNTIME_ISSUER,
        )
        with self._lock:
            if id(completion) in self._validation_lifecycles:
                lifecycle._release_workspace_pin()
                lifecycle._release_runtime_borrow()
                raise ProjectStageLifecycleError("project-stage result lifecycle became ambiguous")
            self._validation_lifecycles[id(completion)] = lifecycle
        return lifecycle

    def _forget_result_validation_lifecycle(
        self, lifecycle: _ProjectStageUnresolvedLifecycle
    ) -> None:
        with self._lock:
            current = self._validation_lifecycles.get(id(lifecycle._completion))
            if current is not lifecycle:
                raise ProjectStageLifecycleError(
                    "project-stage result lifecycle registration changed"
                )
            self._validation_lifecycles.pop(id(lifecycle._completion))

    def _close_unresolved_project_acquisition(
        self, lifecycle: _ProjectStageUnresolvedLifecycle
    ) -> None:
        if type(lifecycle) is not _ProjectStageUnresolvedLifecycle:
            raise ProjectStageLifecycleError("unresolved acquisition close authority is invalid")
        lifecycle._authenticate_registration()
        lease = lifecycle._project_lease
        with self._lock:
            active = self._active_project_leases.get(id(lease))
        if (
            active is None
            or active[0] is not lease
            or active[2] is not lifecycle._prepared_request
            or active[4] is not lifecycle._workspace_lease
        ):
            raise ProjectStageLifecycleError("unresolved project acquisition is stale or foreign")
        lease._close()
        with self._lock:
            if self._active_project_leases.get(id(lease)) is not active:
                raise ProjectStageLifecycleError(
                    "unresolved project acquisition registration changed"
                )
            self._active_project_leases.pop(id(lease))

    def _finish_unresolved_result_lifecycle(
        self, lifecycle: _ProjectStageUnresolvedLifecycle
    ) -> None:
        lifecycle._authenticate_registration()
        if (
            lifecycle.state != "unresolved_closed"
            or lifecycle._review is None
            or not lifecycle._review.in_process_authority_released
        ):
            raise ProjectStageLifecycleError("unresolved lifecycle is not safely closed")
        with self._lock:
            self._validation_lifecycles.pop(id(lifecycle._completion))
            self._completed_processes.pop(id(lifecycle._completion), None)
            self._validated_result_sets.pop(id(lifecycle._completion), None)

    def prepare_request(
        self,
        intent: object,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        workspace_lease: BatchWorkspaceLease,
    ) -> object:
        """Atomically prepare one capability-bound request through the registry."""

        with self._operation():
            reopener = self.committed_exchange_reopener(workspace_lease=workspace_lease)
            return self._codec_registry.prepare_run(
                intent,
                contexts,
                committed_exchange_reopener=reopener,
            )

    def _authenticate_prepared(self, prepared: object) -> object:
        self._codec_registry.authenticate_prepared_run(prepared)
        return prepared

    def publish_request(
        self,
        prepared: object,
        *,
        workspace_lease: object,
        terminal_committer: object,
    ) -> object:
        with self._operation():
            prepared = self._authenticate_prepared(prepared)
            request = prepared.request
            contexts = tuple(item.context for item in prepared.prepared_targets)
            _require_typed_record(request, ProjectStageRunRequest, "project-stage request")
            store = self._catalogue_store(workspace_lease)
            capability = self._state_store._publish_project_stage_request(
                store,
                request=request,
                contexts=contexts,
                lease=workspace_lease,
                terminal_committer=terminal_committer,
            )
            with self._lock:
                existing = self._published_prepared_requests.get(id(prepared))
                value = (
                    prepared,
                    capability,
                    workspace_lease,
                    terminal_committer,
                )
                if existing is not None and existing != value:
                    raise ProjectStageLifecycleError(
                        "prepared request publication authority differs"
                    )
                self._published_prepared_requests[id(prepared)] = value
            return capability

    def reopen_request(
        self,
        prepared: object,
        *,
        workspace_lease: object,
        terminal_committer: object,
    ) -> ProjectStageRunRequest:
        with self._operation():
            prepared = self._authenticate_prepared(prepared)
            with self._lock:
                published = self._published_prepared_requests.get(id(prepared))
            if (
                published is None
                or published[0] is not prepared
                or published[2] is not workspace_lease
                or published[3] is not terminal_committer
            ):
                raise ProjectStageLifecycleError(
                    "prepared request has no matching retained publication"
                )
            contexts = tuple(item.context for item in prepared.prepared_targets)
            store = self._catalogue_store(workspace_lease)
            request, capability = self._state_store._reopen_project_stage_request(
                store,
                prepared.request.request_sha256,
                contexts=contexts,
                lease=workspace_lease,
                terminal_committer=terminal_committer,
            )
            if (
                request != prepared.request
                or capability.mutation_request_sha256 != published[1].mutation_request_sha256
                or capability.mutation_authorization_sha256
                != published[1].mutation_authorization_sha256
            ):
                raise ProjectStageLifecycleError(
                    "reopened request authority differs from its prepared request"
                )
            return request

    def prepare_acquisition(
        self,
        prepared: object,
        *,
        static_targets: tuple[object, ...],
        dynamic_targets: tuple[object, ...],
        workspace_lease: object,
        unlaunched: ProjectMutationLeaseRecord | None = None,
    ) -> object:
        with self._operation():
            prepared = self._authenticate_prepared(prepared)
            with self._lock:
                published = self._published_prepared_requests.get(id(prepared))
            if (
                published is None
                or published[0] is not prepared
                or published[2] is not workspace_lease
            ):
                raise ProjectStageLifecycleError(
                    "prepared request has no matching retained publication"
                )
            request_capability = published[1]
            store = self._catalogue_store(workspace_lease)
            store._authenticate_request_capability(
                request_capability, expected_kind="project_stage"
            )
            request = store.reopen_project_stage_request_value(
                request_capability.mutation_request_sha256,
                workspace_lease=workspace_lease,
            )
            _require_typed_record(request, ProjectStageRunRequest, "project-stage request")
            inventory_value = self._state_store.prepare_project_mutation_artifact_inventory(
                request=request_capability,
                expected_project_identity_sha256=(request.project_identity_sha256),
                static_targets=static_targets,
                dynamic_targets=dynamic_targets,
                lease=workspace_lease,
            )
            inventory = self._state_store.publish_project_mutation_artifact_inventory(
                inventory_value, lease=workspace_lease
            )
            if unlaunched is not None:
                if inventory.inventory.artifact_inventory_sha256 != unlaunched.artifact_inventory_sha256:
                    raise ProjectStageRecordError("unlaunched project-stage inventory changed")
                acquisition = self._state_store.reopen_project_mutation_acquisition_record(
                    unlaunched.acquisition_record_sha256, lease=workspace_lease,
                )
            else:
                acquisition = self._state_store.publish_project_mutation_acquisition_record(
                    request=request_capability,
                    inventory=inventory,
                    operation="project_stage",
                    intent="open_existing",
                    project_uri=request.project_uri,
                    expected_project_identity_sha256=request.project_identity_sha256,
                    expected_authority_head_sha256=(request.expected_project_authority_head_sha256),
                    recovery_predecessor_acquisition_record_sha256=None,
                    lease=workspace_lease,
                )
            durability = self._state_store.project_mutation_durability_authority(workspace_lease)
            session = self._provider.issue_acquisition_session(
                acquisition,
                request=request_capability,
                inventory=inventory,
                durability_authority=durability,
            )
            with self._lock:
                self._sessions[id(session)] = (
                    session,
                    prepared,
                    request_capability,
                    request,
                    workspace_lease,
                )
            return session

    def acquire_project(
        self, session: object, *, workspace_lease: object
    ) -> _ProjectStageAcquisitionLifecycle:
        return _ProjectStageAcquisitionLifecycle(self, session, workspace_lease)

    def _active_run(
        self, project_lease: object, workspace_lease: object, *,
        validate_project_target: bool = True,
    ) -> tuple[str, object]:
        """Resolve the live operation and its prepared request.

        Ordinary calls check the retained project file. Result collection checks
        lease ownership first, then adopts the saved file after the drained child,
        project identity and observed snapshot have been verified. QuPath's atomic
        save can replace the file inode while retaining that same project identity.
        """
        with self._lock:
            issued = self._active_project_leases.get(id(project_lease))
        if (
            issued is None
            or issued[0] is not project_lease
            or issued[4] is not workspace_lease
            or not project_lease.is_live
            or project_lease._provider is not self._provider
        ):
            raise ProjectStageLifecycleError(
                "project-stage retained authority requires its acquired project lease"
            )
        active = self._provider._validate_live_lease_authority(
            project_lease, require_project=False,
            validate_project_target=validate_project_target,
        )
        return self._provider._active_mutation_run_sha256(active), issued[2]

    def publish_run_plan(
        self,
        prepared: object,
        *,
        project_lease: object,
        workspace_lease: object,
        terminal_committer: object,
    ) -> PublishedProjectStageRunPlan:
        with self._operation():
            prepared = self._authenticate_prepared(prepared)
            active_run_sha256, acquired_prepared = self._active_run(project_lease, workspace_lease)
            acquired_request = acquired_prepared.request
            values = tuple(item.context for item in prepared.prepared_targets)
            if (
                type(values) is not tuple
                or not values
                or not all(isinstance(context, BatchNodeExecutionContext) for context in values)
            ):
                raise ProjectStageRecordError(
                    "project-stage plan contexts must be a non-empty typed tuple"
                )
            with self._lock:
                acquired = self._active_project_leases.get(id(project_lease))
                session = None if acquired is None else acquired[1]
                issued = None if session is None else self._sessions.get(id(session))
            if (
                issued is None
                or issued[0] is not session
                or issued[1] is not prepared
                or issued[1] is not acquired_prepared
                or issued[3] != acquired_request
                or issued[4] is not workspace_lease
            ):
                raise ProjectStageLifecycleError(
                    "project-stage plan requires its acquired request capability"
                )

            request_capability = issued[2]
            store = self._catalogue_store(workspace_lease)
            store._authenticate_request_capability(
                request_capability, expected_kind="project_stage"
            )
            request = store.reopen_project_stage_request_value(
                request_capability.mutation_request_sha256,
                workspace_lease=workspace_lease,
            )
            _require_typed_record(request, ProjectStageRunRequest, "project-stage request")
            expected_authorization = self._state_store._project_stage_mutation_authorization(
                request=request,
                contexts=values,
                lease=workspace_lease,
                terminal_committer=terminal_committer,
            )
            if (
                request != acquired_request
                or request.request_sha256 != request_capability.mutation_request_sha256
                or expected_authorization != request_capability.mutation_authorization_sha256
            ):
                raise ProjectStageRecordError("project-stage plan request authority differs")

            head = self._state_store.load_head()
            catalogue = store.load_current()
            if (
                catalogue is None
                or head.batch_id != request.batch_id
                or head.project_identity_sha256 != request.project_identity_sha256
                or head.project_authority_head_sha256
                != request.expected_project_authority_head_sha256
                or head.project_catalogue_sha256 != request.project_catalogue_sha256
                or Path(head.project_path).resolve().as_uri() != request.project_uri
                or catalogue.batch_id != request.batch_id
                or catalogue.project_uri != request.project_uri
                or catalogue.project_identity_sha256 != request.project_identity_sha256
                or catalogue.catalogue_sha256 != request.project_catalogue_sha256
            ):
                raise ProjectStageRecordError("project-stage plan project authority is stale")
            current_saved_state_by_entry_id = (
                self._state_store.current_project_entry_saved_state_sha256_by_id(
                    expected_head_sha256=head.sha256,
                    lease=workspace_lease,
                )
            )

            by_node = {context.node_id: context for context in values}
            if len(by_node) != len(values) or set(by_node) != {
                target.node_id for target in request.targets
            }:
                raise ProjectStageRecordError(
                    "project-stage targets require exact scheduler contexts"
                )
            target_entry_ids = {target.project_entry_id for target in request.targets}
            excluded_entry_ids = set(request.excluded_project_entry_ids)
            catalogue_entry_ids = {entry.project_entry_id for entry in catalogue.entries}
            if (
                target_entry_ids & excluded_entry_ids
                or catalogue_entry_ids != target_entry_ids | excluded_entry_ids
            ):
                raise ProjectStageRecordError(
                    "project-stage catalogue contains unknown or unplanned entries"
                )

            validated_targets: list[
                tuple[
                    ProjectStageTargetRequest,
                    BatchNodeExecutionContext,
                    BatchProjectEntry,
                ]
            ] = []
            input_sets: list[set[str]] = []
            for target in request.targets:
                context = by_node[target.node_id]
                authorization = context.authorization
                try:
                    entry = catalogue.resolve(target.project_entry_id)
                except KeyError as exc:
                    raise ProjectStageRecordError(
                        "project-stage target is absent from the catalogue"
                    ) from exc
                input_refs = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
                input_sets.append(set(input_refs))
                if (
                    entry.image_role != "primary_mif"
                    or entry.sample_id != target.sample_id
                    or entry.slide_id != target.slide_id
                    or current_saved_state_by_entry_id.get(target.project_entry_id)
                    != target.saved_state_before_sha256
                    or context.current_image_identity_sha256 != entry.image_sha256
                    or context.project_entry_projection_sha256 != entry.entry_projection_sha256
                    or context.node_id != target.node_id
                    or context.adapter_spec.node_kind != target.terminal_stage_id
                    or context.adapter_spec.spec_sha256 != target.adapter_spec_sha256
                    or context.adapter_spec.node_version != request.stage_version
                    or target.terminal_stage_id != request.stage_id
                    or context.project_identity_sha256 != request.project_identity_sha256
                    or authorization.dispatch_binding.project_entry_ids
                    != (target.project_entry_id,)
                    or authorization.attempt_id == ""
                    or tuple(target.prerequisite_artifact_ref_sha256s) != input_refs
                    or head.sample_project_entry_id_by_sample.get(target.sample_id)
                    != target.project_entry_id
                    or head.resolved_config_sha256_by_sample.get(target.sample_id)
                    != target.resolved_config_sha256
                ):
                    raise ProjectStageRecordError(
                        "project-stage target, catalogue, and context differ"
                    )
                validated_targets.append((target, context, entry))
            shared = set.intersection(*input_sets) if input_sets else set()
            if tuple(sorted(shared)) != request.shared_artifact_ref_sha256s:
                raise ProjectStageRecordError("project-stage shared artifact authority differs")

            try:
                target_asset = QuPathScriptAsset(request.target_script_asset_id)
                guard_asset = QuPathScriptAsset(request.guard_script_asset_id)
            except ValueError as exc:
                raise ProjectStageRecordError(
                    "project-stage registered script asset differs"
                ) from exc
            bundle = self._project_operations.materialize_project_stage_script_bundle(
                lease=project_lease,
                target_asset=target_asset,
                guard_asset=guard_asset,
            )
            if (
                type(bundle) is not ProjectStageScriptBundleAuthority
                or bundle.authority_layout_sha256 != request.authority_layout_sha256
                or bundle.workspace_storage_scope_sha256 != store.workspace_storage_scope_sha256
                or bundle.active_mutation_run_sha256 != active_run_sha256
                or bundle.project_uri != request.project_uri
                or bundle.project_identity_sha256 != request.project_identity_sha256
                or bundle.target_script_asset_id != request.target_script_asset_id
                or bundle.guard_script_asset_id != request.guard_script_asset_id
                or hashlib.sha256(bundle.script_bundle_manifest_bytes).hexdigest()
                != bundle.script_bundle_manifest_sha256
                or len(bundle.script_bundle_manifest_bytes)
                != bundle.script_bundle_manifest_byte_count
                or hashlib.sha256(bundle.target_script_bytes).hexdigest()
                != bundle.target_script_sha256
                or len(bundle.target_script_bytes) != bundle.target_script_byte_count
                or hashlib.sha256(bundle.guard_script_bytes).hexdigest()
                != bundle.guard_script_sha256
                or len(bundle.guard_script_bytes) != bundle.guard_script_byte_count
            ):
                raise ProjectStageRecordError("project-stage script bundle authority differs")

            authorized_entries: list[ProjectStageAuthorizedEntry] = []
            from .batch_manifest import operational_sample_output_root

            resolved_samples = {
                sample.sample_id: sample
                for sample in self._state_store.retained_manifest(
                    head.normalized_manifest_sha256
                ).samples
            }
            prepared_by_sha256 = {
                item.target.target_request_sha256: item for item in prepared.prepared_targets
            }
            producer_reopener = self.committed_exchange_reopener(workspace_lease=workspace_lease)
            producer_root_cache: dict[str, str] = {}
            for target, context, entry in validated_targets:
                authorization = context.authorization
                prepared_target = prepared_by_sha256.get(target.target_request_sha256)
                if prepared_target is None:
                    raise ProjectStageRecordError("project-stage prepared target authority differs")
                request_envelope = self._codec_registry.request_envelope_bytes(
                    prepared_target,
                    project_file=project_lease.designation.project_path,
                    canonical_image_uri=entry.canonical_uri,
                    canonical_image_sha256=entry.image_sha256,
                    script_bundle_sha256=bundle.script_bundle_sha256,
                    script_asset_path=target.request_codec_id.removeprefix("qupath/scripts/"),
                    script_asset_sha256=bundle.target_script_sha256,
                    operational_output_dir=operational_sample_output_root(
                        resolved_samples[target.sample_id], self._state_store.layout.root
                    ) / "analysis",
                    producer_output_dirs=_project_stage_producer_output_dirs(
                        prepared_target.scientific_request.scientific_request_body,
                        sample_id=target.sample_id, input_refs=context.inputs,
                        reopen_authorities=lambda: self._state_store.reopen_execution_snapshot(
                            expected_head_sha256=head.sha256).committed_artifact_authorities,
                        reopener=producer_reopener, cache=producer_root_cache,
                        producer_output_dirs=prepared_target.producer_output_dirs,
                    ),
                )
                exchange = store._publish_project_stage_target_exchange(
                    target,
                    active_mutation_run_sha256=active_run_sha256,
                    request_envelope_bytes=request_envelope,
                )
                authorized_entries.append(
                    ProjectStageAuthorizedEntry.create(
                        target_request=target,
                        attempt_id=authorization.attempt_id,
                        attempt_authorization_sha256=(authorization.authorization_sha256),
                        input_artifact_set_sha256=(authorization.input_artifact_set_sha256),
                        exchange=exchange,
                    )
                )

            plan = ProjectStageRunPlan.create(
                authority_layout_sha256=request.authority_layout_sha256,
                request_sha256=request.request_sha256,
                run_plan_id=request.run_plan_id,
                batch_id=request.batch_id,
                stage_id=request.stage_id,
                stage_version=request.stage_version,
                launch_mode=request.launch_mode,
                project_uri=request.project_uri,
                project_identity_sha256=request.project_identity_sha256,
                project_authority_head_sha256=(request.expected_project_authority_head_sha256),
                project_catalogue_sha256=request.project_catalogue_sha256,
                script_bundle_sha256=bundle.script_bundle_sha256,
                script_bundle_manifest_ref=bundle.script_bundle_manifest_ref,
                script_bundle_manifest_sha256=(bundle.script_bundle_manifest_sha256),
                script_bundle_manifest_byte_count=(bundle.script_bundle_manifest_byte_count),
                target_script_asset_id=bundle.target_script_asset_id,
                target_script_ref=bundle.target_script_ref,
                target_script_sha256=bundle.target_script_sha256,
                target_script_byte_count=bundle.target_script_byte_count,
                guard_script_asset_id=bundle.guard_script_asset_id,
                guard_script_ref=bundle.guard_script_ref,
                guard_script_sha256=bundle.guard_script_sha256,
                guard_script_byte_count=bundle.guard_script_byte_count,
                targets=tuple(authorized_entries),
                excluded_project_entry_ids=(request.excluded_project_entry_ids),
                shared_artifact_ref_sha256s=(request.shared_artifact_ref_sha256s),
            )
            return self._state_store._publish_project_stage_run_plan(
                store, plan, lease=workspace_lease
            )

    def reopen_run_plan(
        self,
        published: PublishedProjectStageRunPlan,
        *,
        workspace_lease: object,
    ) -> ProjectStageRunPlan:
        with self._operation():
            return self._catalogue_store(workspace_lease).reopen_project_stage_run_plan(
                published, workspace_lease=workspace_lease
            )

    def _reopen_result_validation_inputs(
        self,
        publication: PublishedProjectStageRunPlan,
        prepared_request: object,
        completion: object,
        *,
        project_lease: object,
        workspace_lease: object,
    ) -> tuple[
        object,
        object,
        object,
        tuple[tuple[bytes, bytes | None, bytes | None], ...],
        tuple[object, ...],
    ]:
        """Reopen original request and output bytes at the completion boundary.

        The exact request binds the operation's publication location, including
        interrupted completed runs. It must not be inferred from a later
        workspace or configuration when accepting those retained outputs.
        """

        from mif_pipeline.qupath_runtime.catalogue import (
            QuPathProjectInspection,
            QuPathProjectSnapshot,
        )
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageProcessCompletion,
            ProjectStageProcessEvidenceCapability,
            ProjectStageProcessSummary,
        )

        with self._operation():
            with self._state_store._lease_operation(workspace_lease):
                active_run_sha256, active_prepared = self._active_run(
                    project_lease, workspace_lease, validate_project_target=False
                )
                prepared = self._authenticate_prepared(prepared_request)
                if prepared is not active_prepared:
                    raise ProjectStageRecordError(
                        "project-stage prepared request authority differs"
                    )
                request = prepared.request
                store = self._catalogue_store(workspace_lease)
                plan = store.reopen_project_stage_run_plan(
                    publication, workspace_lease=workspace_lease
                )
                if (
                    type(plan) is not ProjectStageRunPlan
                    or plan.request_sha256 != request.request_sha256
                    or plan.project_uri != project_lease.designation.project_uri
                    or plan.project_identity_sha256
                    != project_lease.designation.identity.project_identity_sha256
                    or plan.project_authority_head_sha256
                    != project_lease.current_authority_head.authority_head_sha256
                ):
                    raise ProjectStageRecordError("project-stage validation plan authority differs")

                if (
                    type(completion) is not ProjectStageProcessCompletion
                    or completion.disposition != "released_child"
                    or completion.no_mutation_proof is not None
                    or type(completion.process_evidence)
                    is not ProjectStageProcessEvidenceCapability
                    or type(completion.process_summary) is not ProjectStageProcessSummary
                ):
                    raise ProjectStageRecordError(
                        "project-stage validation requires one released child"
                    )
                evidence = completion.process_evidence
                summary = completion.process_summary
                assert evidence is not None and summary is not None
                try:
                    canonical_completion = ProjectStageProcessCompletion.create(
                        disposition=completion.disposition,
                        process_evidence=evidence,
                        no_mutation_proof=None,
                        process_summary=summary,
                    )
                    canonical_summary = ProjectStageProcessSummary.create(
                        process_start_token=evidence.drain.process_start_token,
                        process_evidence_sha256=summary.process_evidence_sha256,
                        termination_reason=summary.termination_reason,
                        exit_status=summary.exit_status,
                        stdout_log_ref=summary.stdout_log_ref,
                        stdout_payload_sha256=summary.stdout_payload_sha256,
                        stdout_byte_count=summary.stdout_byte_count,
                        stderr_log_ref=summary.stderr_log_ref,
                        stderr_payload_sha256=summary.stderr_payload_sha256,
                        stderr_byte_count=summary.stderr_byte_count,
                        containment_drained=summary.containment_drained,
                    )
                except (TypeError, ValueError) as exc:
                    raise ProjectStageRecordError(
                        "project-stage process completion is not canonical"
                    ) from exc
                if (
                    canonical_completion != completion
                    or canonical_summary != summary
                    or summary.containment_drained is not True
                ):
                    raise ProjectStageRecordError(
                        "project-stage process completion authority differs"
                    )

                records = (
                    (
                        "attempt",
                        evidence.attempt,
                        evidence.attempt_ref,
                        evidence.attempt_payload_sha256,
                        evidence.attempt_byte_count,
                    ),
                    (
                        "launch",
                        evidence.launch,
                        evidence.launch_ref,
                        evidence.launch_payload_sha256,
                        evidence.launch_byte_count,
                    ),
                    (
                        "drain",
                        evidence.drain,
                        evidence.drain_ref,
                        evidence.drain_payload_sha256,
                        evidence.drain_byte_count,
                    ),
                )
                evidence_projection: list[dict[str, object]] = []
                for role, record, ref, payload_sha256, byte_count in records:
                    if role == "launch":
                        reopened = store.reopen_project_stage_process_launch(
                            ref,
                            expected_record=record,
                            workspace_lease=workspace_lease,
                        )
                    else:
                        reopened = store.reopen_project_stage_process_record(
                            ref,
                            expected_record=record,
                            workspace_lease=workspace_lease,
                        )
                    expected_payload_sha256, expected_byte_count = _payload_facts(reopened)
                    if (
                        reopened != record
                        or payload_sha256 != expected_payload_sha256
                        or byte_count != expected_byte_count
                    ):
                        raise ProjectStageRecordError(f"project-stage {role} evidence facts differ")
                    evidence_projection.append(
                        {
                            "role": role,
                            "record_ref": ref.to_mapping(),
                            "payload_sha256": payload_sha256,
                            "byte_count": byte_count,
                        }
                    )
                if evidence.evidence_set_sha256 != _digest({"evidence": evidence_projection}):
                    raise ProjectStageRecordError("project-stage process evidence set differs")

                descriptor_ref = store.runtime_evidence_ref(
                    evidence.attempt.prepared_descriptor_sha256
                )
                descriptor = store._reopen_project_stage_runtime_record(
                    descriptor_ref,
                    record_type=PreparedProjectStageRunDescriptor,
                    digest_field="prepared_descriptor_sha256",
                    expected_digest=(evidence.attempt.prepared_descriptor_sha256),
                    label="project-stage prepared descriptor",
                )
                with self._lock:
                    active_lease = self._active_project_leases.get(id(project_lease))
                session = None if active_lease is None else active_lease[1]
                session_record = None if session is None else session.record
                common = (
                    "authority_layout_sha256",
                    "workspace_storage_scope_sha256",
                    "project_identity_sha256",
                    "active_mutation_run_sha256",
                    "acquisition_record_sha256",
                    "acquisition_session_challenge_sha256",
                    "artifact_inventory_sha256",
                    "mutation_request_sha256",
                    "mutation_authorization_sha256",
                    "fencing_token_sha256",
                    "run_plan_sha256",
                    "prepared_descriptor_sha256",
                    "application_request_sha256",
                    "launch_nonce_sha256",
                )
                if (
                    type(descriptor) is not PreparedProjectStageRunDescriptor
                    or session_record is None
                    or any(
                        getattr(evidence.attempt, name) != getattr(evidence.launch, name)
                        or getattr(evidence.attempt, name) != getattr(evidence.drain, name)
                        for name in common
                    )
                    or evidence.attempt.active_mutation_run_sha256 != active_run_sha256
                    or evidence.attempt.run_plan_sha256 != plan.plan_sha256
                    or evidence.attempt.project_identity_sha256 != plan.project_identity_sha256
                    or evidence.attempt.mutation_request_sha256 != plan.request_sha256
                    or evidence.attempt.acquisition_record_sha256
                    != session_record.acquisition_record_sha256
                    or evidence.attempt.acquisition_session_challenge_sha256
                    != self._process_acquisition_challenge(project_lease, session)
                    or evidence.attempt.artifact_inventory_sha256
                    != session_record.artifact_inventory_sha256
                    or evidence.attempt.mutation_authorization_sha256
                    != session_record.mutation_authorization_sha256
                    or evidence.attempt.fencing_token_sha256 != project_lease.fencing_token_sha256
                    or descriptor.prepared_descriptor_sha256
                    != evidence.attempt.prepared_descriptor_sha256
                    or evidence.attempt.application_request_sha256
                    != descriptor.prepared_descriptor_sha256
                    or descriptor.active_mutation_run_sha256 != active_run_sha256
                    or descriptor.run_plan_ref != publication.plan_ref
                    or descriptor.run_plan_sha256 != plan.plan_sha256
                    or descriptor.run_plan_byte_count != publication.byte_count
                    or descriptor.project_uri != plan.project_uri
                    or descriptor.project_identity_sha256 != plan.project_identity_sha256
                    or descriptor.project_authority_head_sha256
                    != plan.project_authority_head_sha256
                    or descriptor.target_authorization_set_sha256
                    != plan.target_authorization_set_sha256
                    or descriptor.request_member_set_sha256 != plan.request_member_set_sha256
                    or descriptor.process_launch_ref != evidence.launch_ref
                    or descriptor.stdout_log_ref != evidence.drain.stdout_log_ref
                    or descriptor.stderr_log_ref != evidence.drain.stderr_log_ref
                ):
                    raise ProjectStageRecordError(
                        "project-stage process evidence differs from its live plan"
                    )

                inspection = self._project_operations.inspect_project(
                    project_lease.designation.project_path,
                    require_opened_file_identity=True,
                )
                if type(inspection) is not QuPathProjectInspection:
                    raise ProjectStageRecordError(
                        "project-stage inspection lacks opened-file identity"
                    )
                snapshot = inspection.snapshot
                if type(snapshot) is not QuPathProjectSnapshot:
                    raise ProjectStageRecordError("project-stage inspection snapshot must be typed")
                if (
                    snapshot.project_path != project_lease.designation.project_path
                    or snapshot.project_uri != plan.project_uri
                    or snapshot.identity_record != project_lease.designation.identity
                    or snapshot.identity_record is None
                    or snapshot.identity_record.project_identity_sha256
                    != plan.project_identity_sha256
                ):
                    raise ProjectStageRecordError(
                        "project-stage project snapshot authority differs"
                    )

                family = store._authority_layout.layout.family("runtime_exchange")
                members = tuple(
                    (
                        store._read_ref(
                            target.exchange.request_ref,
                            max_bytes=family.max_member_bytes,
                            label="project-stage original target request",
                        ),
                        store._read_ref(
                            target.exchange.result_ref,
                            max_bytes=family.max_member_bytes,
                            label="project-stage target result",
                            optional=True,
                        ),
                        store._read_ref(
                            target.exchange.generation_manifest_ref,
                            max_bytes=family.max_member_bytes,
                            label="project-stage generation manifest",
                            optional=True,
                        ),
                    )
                    for target in plan.targets
                )
                catalogue = store.load_current()
                if (
                    catalogue is None
                    or catalogue.catalogue_sha256 != plan.project_catalogue_sha256
                    or len(prepared.prepared_targets) != len(plan.targets)
                    or any(
                        item.target.target_request_sha256 != target.target_request_sha256
                        for item, target in zip(
                            prepared.prepared_targets,
                            plan.targets,
                            strict=True,
                        )
                    )
                ):
                    raise ProjectStageRecordError("project-stage catalogue authority differs")
                # The completed child may atomically replace project.qpproj.
                # We retain its verified snapshot as the new file identity before
                # the ordinary result/durability readers reopen the live lease.
                pin = project_lease._project_path_pin
                pin._accept_authenticated_successor(
                    predecessor_identity=pin._successor_predecessor_identity(),
                    expected_sha256=snapshot.qpproj_sha256,
                )
                project_lease.validate_project_target(require_exists=True)
                return (
                    plan,
                    inspection,
                    catalogue,
                    members,
                    prepared.prepared_targets,
                )

    def _record_validated_project_stage_results(
        self,
        publication: object,
        prepared_request: object,
        completion: object,
        outcomes: tuple[object, ...],
        *,
        project_lease: object,
        workspace_lease: object,
        plan: object,
        snapshot: object,
        catalogue: object,
        observations: tuple[ProjectStageTargetObservation, ...],
    ) -> None:
        """Retain the exact effect-free Slice-7 result set for durability."""

        with self._operation():
            with self._state_store._lease_operation(workspace_lease):
                head = self._state_store.load_head()
                with self._lock:
                    completed = self._completed_processes.get(id(completion))
                if (
                    completed is None
                    or completed[0] is not completion
                    or completed[3] is not project_lease
                    or completed[4] is not workspace_lease
                    or completed[1].descriptor.run_plan_sha256 != plan.plan_sha256
                    or head.project_authority_head_sha256 != plan.project_authority_head_sha256
                    or head.project_catalogue_sha256 != catalogue.catalogue_sha256
                ):
                    raise ProjectStageRecordError(
                        "project-stage validated result authority is stale"
                    )
                capability = _ValidatedProjectStageResultSet(
                    publication=publication,
                    prepared_request=prepared_request,
                    completion=completion,
                    outcomes=outcomes,
                    project_lease=project_lease,
                    workspace_lease=workspace_lease,
                    plan=plan,
                    snapshot=snapshot,
                    catalogue=catalogue,
                    observations=observations,
                    expected_state_head_sha256=head.sha256,
                )
                with self._lock:
                    self._validated_result_sets[id(completion)] = capability

    def _prepare_project_stage_material(
        self,
        publication: object,
        prepared_request: object,
        completion: object,
        outcomes: tuple[object, ...],
        *,
        project_lease: object,
        workspace_lease: object,
    ) -> object:
        """Bridge exact observed project authority into grouped batch material."""

        from .batch_artifacts import BatchArtifactError, StageAdapterRegistry
        from .batch_state import BatchRawNodeOutcome
        from mif_pipeline.pipeline_core import StageResult, StageStatus
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceCapability,
            ProjectStageMutationEvidenceRecord,
            ProjectStageNoMutationProof,
            ProjectStageProcessCompletion,
        )

        if (
            type(completion) is not ProjectStageProcessCompletion
            or type(outcomes) is not tuple
            or not outcomes
            or not all(type(item) is BatchRawNodeOutcome for item in outcomes)
        ):
            raise ProjectStageRecordError("project-stage durability inputs are invalid")
        with self._operation():
            with self._state_store._lease_operation(workspace_lease):
                active_run_sha256, active_prepared = self._active_run(
                    project_lease, workspace_lease
                )
                prepared = self._authenticate_prepared(prepared_request)
                if prepared is not active_prepared:
                    raise ProjectStageRecordError(
                        "project-stage durability request authority differs"
                    )
                store = self._catalogue_store(workspace_lease)
                plan = store.reopen_project_stage_run_plan(
                    publication, workspace_lease=workspace_lease
                )
                contexts = tuple(item.context for item in prepared.prepared_targets)
                if (
                    plan.request_sha256 != prepared.request.request_sha256
                    or tuple(item.context for item in outcomes) != contexts
                    or len({item.node_id for item in contexts}) != len(contexts)
                ):
                    raise ProjectStageRecordError(
                        "project-stage durability target authority differs"
                    )
                head, _ = self._state_store._read_canonical_state_head()
                catalogue = store.load_current()
                if (
                    catalogue is None
                    or catalogue.catalogue_sha256 != plan.project_catalogue_sha256
                    or head.project_catalogue_sha256 != catalogue.catalogue_sha256
                ):
                    raise ProjectStageRecordError("project-stage catalogue authority changed")
                prepared_results = tuple(
                    self._state_store.prepare_project_mutation_node_result(
                        context,
                        outcome.stage_result,
                        outcome.execution_result,
                        execution_exchange_sha256=(outcome.execution_exchange_sha256),
                        project_lease=project_lease,
                        expected_head_sha256=head.sha256,
                        lease=workspace_lease,
                    )
                    for context, outcome in zip(contexts, outcomes, strict=True)
                )
                for context, prepared_result in zip(contexts, prepared_results, strict=True):
                    if prepared_result.stage_result.status is StageStatus.COMPLETED:
                        try:
                            StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
                                context.adapter_spec,
                                prepared_result.execution_result.outputs,
                                authorization=context.authorization,
                            )
                        except BatchArtifactError as exc:
                            raise ProjectStageRecordError(
                                "project-stage terminal output authority is invalid"
                            ) from exc
                    elif prepared_result.execution_result.outputs:
                        raise ProjectStageRecordError(
                            "non-completed project-stage result has outputs"
                        )

                if completion.disposition == "released_child":
                    with self._lock:
                        validated = self._validated_result_sets.get(id(completion))
                    if (
                        type(validated) is not _ValidatedProjectStageResultSet
                        or validated.publication is not publication
                        or validated.prepared_request is not prepared_request
                        or validated.completion is not completion
                        or validated.project_lease is not project_lease
                        or validated.workspace_lease is not workspace_lease
                        or len(validated.outcomes) != len(outcomes)
                        or any(
                            expected is not actual
                            for expected, actual in zip(validated.outcomes, outcomes, strict=True)
                        )
                        or validated.plan != plan
                        or validated.catalogue != catalogue
                        or head.sha256 != validated.expected_state_head_sha256
                    ):
                        raise ProjectStageRecordError(
                            "project-stage validated result capability is stale"
                        )
                    evidence = completion.process_evidence
                    summary = completion.process_summary
                    assert evidence is not None and summary is not None
                    attempt = evidence.attempt
                    mutation = ProjectStageMutationEvidenceRecord.create(
                        authority_layout_sha256=(attempt.authority_layout_sha256),
                        workspace_storage_scope_sha256=(attempt.workspace_storage_scope_sha256),
                        project_identity_sha256=(attempt.project_identity_sha256),
                        active_mutation_run_sha256=(attempt.active_mutation_run_sha256),
                        acquisition_record_sha256=(attempt.acquisition_record_sha256),
                        acquisition_session_challenge_sha256=(
                            attempt.acquisition_session_challenge_sha256
                        ),
                        artifact_inventory_sha256=(attempt.artifact_inventory_sha256),
                        mutation_request_sha256=(attempt.mutation_request_sha256),
                        mutation_authorization_sha256=(attempt.mutation_authorization_sha256),
                        fencing_token_sha256=attempt.fencing_token_sha256,
                        run_plan_sha256=attempt.run_plan_sha256,
                        prepared_descriptor_sha256=(attempt.prepared_descriptor_sha256),
                        application_request_sha256=(attempt.application_request_sha256),
                        launch_nonce_sha256=attempt.launch_nonce_sha256,
                        attempt_sha256=attempt.attempt_sha256,
                        process_launch_sha256=(evidence.launch.process_launch_sha256),
                        process_drain_sha256=(evidence.drain.process_drain_sha256),
                        termination_reason=summary.termination_reason,
                        predecessor_project_authority_head_sha256=(
                            plan.project_authority_head_sha256
                        ),
                        observed_project_snapshot_sha256=hashlib.sha256(
                            canonical_json_bytes(validated.snapshot.to_mapping())
                        ).hexdigest(),
                        target_observations=tuple(
                            sorted(
                                validated.observations,
                                key=lambda item: item.project_entry_id,
                            )
                        ),
                        stdout_log_ref=summary.stdout_log_ref,
                        stdout_payload_sha256=(summary.stdout_payload_sha256),
                        stdout_byte_count=summary.stdout_byte_count,
                        stderr_log_ref=summary.stderr_log_ref,
                        stderr_payload_sha256=(summary.stderr_payload_sha256),
                        stderr_byte_count=summary.stderr_byte_count,
                    )
                    with self._lock:
                        acquired = self._active_project_leases.get(id(project_lease))
                        recovery = (
                            None if acquired is None
                            else self._recovered_completions.get(id(acquired[1]))
                        )
                    retained_mutation = None if recovery is None else recovery[1].mutation
                    if retained_mutation is not None and mutation != retained_mutation:
                        raise ProjectStageRecordError(
                            "current observation differs from the retained project-stage mutation"
                        )
                    mutation_ref = store.publish_project_stage_process_record(
                        mutation, workspace_lease=workspace_lease
                    )
                    reopened = store.reopen_project_stage_process_record(
                        mutation_ref,
                        expected_record=mutation,
                        workspace_lease=workspace_lease,
                    )
                    payload_sha256, byte_count = _payload_facts(reopened)
                    mutation_capability = ProjectStageMutationEvidenceCapability._issue(
                        issuer=evidence._issuer,
                        mutation=mutation,
                        mutation_ref=mutation_ref,
                        mutation_payload_sha256=payload_sha256,
                        mutation_byte_count=byte_count,
                        process_evidence=evidence,
                    )
                    projections = tuple(
                        sorted(
                            catalogue.resolve(observation.project_entry_id).entry_projection_sha256
                            for observation in validated.observations
                        )
                    )
                    attestation = project_lease.commit_project_stage_authority(
                        mutation_capability,
                        workspace_catalogue_sha256=(catalogue.catalogue_sha256),
                        project_entry_projection_sha256s=projections,
                    )
                    if attestation.operation_evidence_sha256 != mutation.mutation_evidence_sha256:
                        raise ProjectStageRecordError("project-stage attestation evidence differs")
                    disposition = "observed_mutation"
                    evidence_sha256 = mutation.mutation_evidence_sha256
                    expected_head_sha256 = validated.expected_state_head_sha256
                else:
                    proof = completion.no_mutation_proof
                    with self._lock:
                        completed = self._completed_processes.get(id(completion))
                        active_lease = self._active_project_leases.get(id(project_lease))
                    descriptor = None if completed is None else completed[2]
                    session = None if active_lease is None else active_lease[1]
                    no_mutation_checks = (
                        ("proof_type", type(proof) is ProjectStageNoMutationProof),
                        ("completed", completed is not None),
                        (
                            "completion_identity",
                            completed is not None and completed[0] is completion,
                        ),
                        ("project_lease", completed is not None and completed[3] is project_lease),
                        (
                            "workspace_lease",
                            completed is not None and completed[4] is workspace_lease,
                        ),
                        (
                            "proof_issuer",
                            completed is not None
                            and proof is not None
                            and proof._issuer is completed[5],
                        ),
                        ("session", session is not None),
                        (
                            "run_plan",
                            descriptor is not None
                            and descriptor.run_plan_sha256 == plan.plan_sha256,
                        ),
                        (
                            "variant",
                            proof is not None
                            and proof.variant
                            in {"never_entered_child_creation", "contained_never_released"},
                        ),
                        (
                            "project_identity",
                            proof is not None
                            and proof.project_identity_sha256 == plan.project_identity_sha256,
                        ),
                        (
                            "active_run",
                            proof is not None
                            and proof.active_mutation_run_sha256 == active_run_sha256,
                        ),
                        (
                            "acquisition",
                            proof is not None
                            and session is not None
                            and proof.acquisition_record_sha256
                            == session.record.acquisition_record_sha256,
                        ),
                        (
                            "challenge",
                            proof is not None
                            and session is not None
                            and proof.acquisition_session_challenge_sha256
                            == session.acquisition_session_challenge_sha256,
                        ),
                        (
                            "descriptor",
                            proof is not None
                            and descriptor is not None
                            and proof.prepared_descriptor_sha256
                            == descriptor.prepared_descriptor_sha256,
                        ),
                        (
                            "project_head",
                            head.project_authority_head_sha256
                            == project_lease.current_authority_head.authority_head_sha256,
                        ),
                        (
                            "qpproj_head",
                            head.project_qpproj_snapshot_sha256
                            == project_lease.current_authority_head.qpproj_snapshot_sha256,
                        ),
                        (
                            "qpproj_bytes",
                            hashlib.sha256(project_lease._project_path_pin.read_bytes()).hexdigest()
                            == head.project_qpproj_snapshot_sha256,
                        ),
                    )
                    if not all(passed for _name, passed in no_mutation_checks):
                        failed = ",".join(name for name, passed in no_mutation_checks if not passed)
                        raise ProjectStageRecordError(
                            "project-stage no-mutation proof is stale or foreign: " f"{failed}"
                        )
                    failure_code, failure_message = {
                        "never_entered_child_creation": (
                            "project_stage_cancelled_before_launch",
                            ("The project-stage run was cancelled before " "child creation."),
                        ),
                        "contained_never_released": (
                            "project_stage_launch_not_released",
                            ("The contained project-stage child was drained " "before release."),
                        ),
                    }[proof.variant]
                    for target, prepared_result in zip(
                        prepared.prepared_targets,
                        prepared_results,
                        strict=True,
                    ):
                        expected_failure = StageResult.failed(
                            stage=target.context.adapter_spec.node_kind,
                            slide_id=target.target.slide_id,
                            error_code=failure_code,
                            error_message=failure_message,
                        )
                        if (
                            prepared_result.stage_result != expected_failure
                            or prepared_result.execution_result.outputs
                            or prepared_result.execution_exchange_sha256 is not None
                        ):
                            raise ProjectStageRecordError(
                                "project-stage no-mutation terminal is not the "
                                "exact proof-derived failure"
                            )
                    disposition = "unchanged_project"
                    evidence_sha256 = proof.proof_sha256
                    expected_head_sha256 = head.sha256

                return self._state_store.prepare_project_stage_material_successor(
                    contexts=contexts,
                    prepared_results=prepared_results,
                    project_lease=project_lease,
                    expected_head_sha256=expected_head_sha256,
                    disposition=disposition,
                    project_catalogue_sha256=catalogue.catalogue_sha256,
                    project_stage_evidence_sha256=evidence_sha256,
                    lease=workspace_lease,
                )

    def _reauthenticate_target_after_codec(
        self,
        publication: PublishedProjectStageRunPlan,
        authorized: ProjectStageAuthorizedEntry,
        prepared_target: object,
        result_bytes: bytes,
        manifest_bytes: bytes | None,
        saved_state_after_sha256: str,
        *,
        snapshot: QuPathProjectSnapshot,
        project_lease: object,
        workspace_lease: object,
    ) -> None:
        """Reopen callback bytes against the authenticated post-child snapshot.

        The codec reads saved artifacts. Literal project and target data checks
        guard edits during that read; the provider inspects independently before
        committing the resulting project authority.
        """

        from mif_pipeline.qupath_runtime.catalogue import (
            _ENTRY_HIERARCHY_MAX_BYTES,
            _bounded_registration_regular_file_digest,
        )

        with self._operation():
            with self._state_store._lease_operation(workspace_lease):
                _active_run_sha256, prepared = self._active_run(project_lease, workspace_lease)
                prepared = self._authenticate_prepared(prepared)
                if not any(
                    item is prepared_target
                    and item.target.target_request_sha256 == authorized.target_request_sha256
                    for item in prepared.prepared_targets
                ):
                    raise ProjectStageRecordError("project-stage retained codec authority differs")
                store = self._catalogue_store(workspace_lease)
                plan = store.reopen_project_stage_run_plan(
                    publication, workspace_lease=workspace_lease
                )
                if type(plan) is not ProjectStageRunPlan or authorized not in plan.targets:
                    raise ProjectStageRecordError("project-stage callback plan authority differs")
                family = store._authority_layout.layout.family("runtime_exchange")
                reopened_result = store._read_ref(
                    authorized.exchange.result_ref,
                    max_bytes=family.max_member_bytes,
                    label="project-stage target result",
                    optional=True,
                )
                reopened_manifest = store._read_ref(
                    authorized.exchange.generation_manifest_ref,
                    max_bytes=family.max_member_bytes,
                    label="project-stage generation manifest",
                    optional=True,
                )
                if reopened_result != result_bytes or reopened_manifest != manifest_bytes:
                    raise ProjectStageRecordError("project-stage callback exchange bytes changed")
                if (
                    hashlib.sha256(project_lease._project_path_pin.read_bytes()).hexdigest()
                    != snapshot.qpproj_sha256
                ):
                    raise ProjectStageRecordError("project-stage callback project bytes changed")
                entries = tuple(
                    item
                    for item in snapshot.entries
                    if item.project_entry_id == authorized.project_entry_id
                )
                if (
                    len(entries) != 1
                    or entries[0].saved_state_attestation_sha256 != saved_state_after_sha256
                ):
                    raise ProjectStageRecordError("project-stage callback saved state changed")
                data_root = project_lease.designation.project_path.parent / "data"
                data_path = entries[0].entry_path / "data.qpdata"
                try:
                    data_path.relative_to(data_root)
                except ValueError as exc:
                    raise ProjectStageRecordError(
                        "project-stage callback saved state escapes project data"
                    ) from exc
                saved_sha256, _saved_bytes = _bounded_registration_regular_file_digest(
                    data_path,
                    max_bytes=_ENTRY_HIERARCHY_MAX_BYTES,
                    label="project-stage callback saved state",
                )
                if saved_sha256 != saved_state_after_sha256:
                    raise ProjectStageRecordError("project-stage callback saved state changed")

    def publish_prepared_descriptor(
        self,
        descriptor: PreparedProjectStageRunDescriptor,
        *,
        workspace_lease: object,
    ) -> PublishedPreparedProjectStageRunDescriptor:
        with self._operation():
            _require_typed_record(
                descriptor,
                PreparedProjectStageRunDescriptor,
                "prepared descriptor",
            )
            return self._catalogue_store(workspace_lease).publish_project_stage_prepared_descriptor(
                descriptor, workspace_lease=workspace_lease
            )

    def reopen_prepared_descriptor(
        self,
        published: PublishedPreparedProjectStageRunDescriptor,
        *,
        workspace_lease: object,
    ) -> PreparedProjectStageRunDescriptor:
        with self._operation():
            return self._catalogue_store(workspace_lease).reopen_project_stage_prepared_descriptor(
                published, workspace_lease=workspace_lease
            )

    def run_process(
        self,
        published: PublishedPreparedProjectStageRunDescriptor,
        *,
        project_lease: object,
        launch: object,
        workspace_lease: object,
        cancellation_event: object | None = None,
    ) -> object:
        """Compose one path-confined contained run from retained authority."""

        from mif_pipeline.qupath_runtime._script_asset_registry import (
            QuPathScriptAsset,
            relative_script_path,
        )
        from mif_pipeline.qupath_runtime.runner import QuPathRunner

        with self._operation():
            with self._state_store._lease_operation(workspace_lease):
                active_sha256, prepared = self._active_run(project_lease, workspace_lease)
                request = prepared.request
                with self._lock:
                    acquired = self._active_project_leases.get(id(project_lease))
                if acquired is None:
                    raise ProjectStageLifecycleError(
                        "project-stage process requires its acquired request"
                    )
                session = acquired[1]
                store = self._catalogue_store(workspace_lease)
                descriptor = store.reopen_project_stage_prepared_descriptor(
                    published, workspace_lease=workspace_lease
                )
                if (
                    type(descriptor) is not PreparedProjectStageRunDescriptor
                    or descriptor.active_mutation_run_sha256 != active_sha256
                    or descriptor.acquisition_record_sha256
                    != session.record.acquisition_record_sha256
                    or descriptor.acquisition_session_challenge_sha256
                    != session.acquisition_session_challenge_sha256
                    or descriptor.artifact_inventory_sha256
                    != session.record.artifact_inventory_sha256
                    or descriptor.mutation_request_sha256 != session.record.mutation_request_sha256
                    or descriptor.mutation_authorization_sha256
                    != session.record.mutation_authorization_sha256
                    or descriptor.mutation_request_sha256 != request.request_sha256
                    or descriptor.runtime_composition_binding_sha256
                    != _digest(
                        {
                            "kind": "project_stage_runtime_composition/v1",
                            "authority_layout_sha256": (
                                self._runtime_binding.authority_layout_sha256
                            ),
                            "workspace_storage_scope_sha256": (
                                self._runtime_binding.workspace_storage_scope_sha256
                            ),
                        }
                    )
                ):
                    raise ProjectStageRecordError(
                        "project-stage prepared descriptor is foreign to its session"
                    )
                plan = store._reopen_project_stage_runtime_record(
                    descriptor.run_plan_ref,
                    record_type=ProjectStageRunPlan,
                    digest_field="plan_sha256",
                    expected_digest=descriptor.run_plan_sha256,
                    label="project-stage run plan",
                )
                if (
                    type(plan) is not ProjectStageRunPlan
                    or plan.request_sha256 != request.request_sha256
                    or plan.authority_layout_sha256 != descriptor.authority_layout_sha256
                    or plan.project_uri != descriptor.project_uri
                    or plan.project_identity_sha256 != descriptor.project_identity_sha256
                    or plan.project_authority_head_sha256
                    != descriptor.project_authority_head_sha256
                ):
                    raise ProjectStageRecordError(
                        "project-stage run plan differs from its acquired request"
                    )
                authority_layout = store._authority_layout
                if (
                    authority_layout is None
                    or not isinstance(self._runner, QuPathRunner)
                    or self._runtime_binding.authority_layout_sha256
                    != authority_layout.layout.layout_sha256
                    or self._runtime_binding.workspace_storage_scope_sha256
                    != store.workspace_storage_scope_sha256
                ):
                    raise ProjectStageLifecycleError(
                        "project-stage runtime authority is unavailable"
                    )
                self._project_operations.authenticate_runtime_binding(
                    self._runtime_binding, runner=self._runner
                )
                bundle = self._project_operations.materialize_project_stage_script_bundle(
                    lease=project_lease,
                    target_asset=QuPathScriptAsset(plan.target_script_asset_id),
                    guard_asset=QuPathScriptAsset(plan.guard_script_asset_id),
                )
                if (
                    bundle.authority_layout_sha256 != descriptor.authority_layout_sha256
                    or bundle.workspace_storage_scope_sha256
                    != descriptor.workspace_storage_scope_sha256
                    or bundle.active_mutation_run_sha256 != descriptor.active_mutation_run_sha256
                    or bundle.project_uri != descriptor.project_uri
                    or bundle.project_identity_sha256 != descriptor.project_identity_sha256
                    or bundle.script_bundle_sha256 != descriptor.script_bundle_sha256
                    or bundle.script_bundle_manifest_ref != descriptor.script_bundle_manifest_ref
                    or bundle.target_script_ref != descriptor.target_script_ref
                    or bundle.guard_script_ref != descriptor.guard_script_ref
                ):
                    raise ProjectStageRecordError(
                        "project-stage script bundle differs from its descriptor"
                    )
                immutable = authority_layout.bucket("immutable")
                exchange = authority_layout.bucket("exchange")
                retained_manifest = immutable.read_member(descriptor.script_bundle_manifest_ref)
                retained_target = immutable.read_member(descriptor.target_script_ref)
                retained_guard = immutable.read_member(descriptor.guard_script_ref)
                run_plan_bytes = immutable.read_member(descriptor.run_plan_ref)
                prepared_bytes = immutable.read_member(published.descriptor_ref)
                if (
                    retained_manifest != bundle.script_bundle_manifest_bytes
                    or retained_target != bundle.target_script_bytes
                    or retained_guard != bundle.guard_script_bytes
                    or run_plan_bytes != canonical_json_bytes(plan.to_mapping())
                    or prepared_bytes != canonical_json_bytes(descriptor.to_mapping())
                    or exchange.read_member(descriptor.process_launch_ref, optional=True)
                    is not None
                ):
                    raise ProjectStageRecordError("project-stage retained launch inputs changed")
                project_path = project_lease.designation.project_path
                bundle_root = (
                    project_path.parent / "scripts" / f"bundle-{descriptor.script_bundle_sha256}"
                )
                manifest_path = bundle_root / "qupath-script-bundle.v1.json"
                target_path = bundle_root / relative_script_path(
                    QuPathScriptAsset(descriptor.target_script_asset_id)
                )
                guard_path = bundle_root / relative_script_path(
                    QuPathScriptAsset(descriptor.guard_script_asset_id)
                )
                authority = self._runner._issue_project_stage_process_authority(
                    runtime=self,
                    runtime_binding=self._process_runner_binding,
                    project_lease=project_lease,
                    prepared_descriptor=descriptor,
                    authority_layout=authority_layout,
                    workspace_lease=workspace_lease,
                    run_plan=plan,
                    run_plan_path=immutable.member_path(descriptor.run_plan_ref),
                    run_plan_bytes=run_plan_bytes,
                    prepared_descriptor_path=immutable.member_path(published.descriptor_ref),
                    prepared_descriptor_bytes=prepared_bytes,
                    process_launch_path=exchange.member_path(descriptor.process_launch_ref),
                    bundle_manifest_path=manifest_path,
                    bundle_manifest_bytes=bundle.script_bundle_manifest_bytes,
                    guard_script_path=guard_path,
                    guard_script_bytes=bundle.guard_script_bytes,
                    target_script_path=target_path,
                    target_script_bytes=bundle.target_script_bytes,
                    publish_record=lambda record: store.publish_project_stage_process_record(
                        record, workspace_lease=workspace_lease
                    ),
                    reopen_record=lambda ref, record: store.reopen_project_stage_process_record(
                        ref,
                        expected_record=record,
                        workspace_lease=workspace_lease,
                    ),
                    publish_launch=lambda record: store.publish_project_stage_process_launch(
                        record, workspace_lease=workspace_lease
                    ),
                    reopen_launch=lambda ref, record: store.reopen_project_stage_process_launch(
                        ref,
                        expected_record=record,
                        workspace_lease=workspace_lease,
                    ),
                )
                completion = self._runner.run_project_stage_process(
                    project_lease=project_lease,
                    prepared_descriptor=descriptor,
                    launch=launch,
                    authority=authority,
                    cancellation_event=cancellation_event,
                )
                with self._lock:
                    self._completed_processes[id(completion)] = (
                        completion,
                        published,
                        descriptor,
                        project_lease,
                        workspace_lease,
                        authority._evidence_issuer._identity,
                    )
                return completion

    def publish_process_record(
        self, record: object, *, workspace_lease: object
    ) -> AuthorityMemberRefV2:
        with self._operation():
            return self._catalogue_store(workspace_lease).publish_project_stage_process_record(
                record, workspace_lease=workspace_lease
            )

    def reopen_process_record(
        self,
        ref: AuthorityMemberRefV2,
        *,
        expected_record: object,
        workspace_lease: object,
    ) -> object:
        with self._operation():
            return self._catalogue_store(workspace_lease).reopen_project_stage_process_record(
                ref,
                expected_record=expected_record,
                workspace_lease=workspace_lease,
            )

    def publish_process_launch(
        self, record: object, *, workspace_lease: object
    ) -> AuthorityMemberRefV2:
        with self._operation():
            return self._catalogue_store(workspace_lease).publish_project_stage_process_launch(
                record, workspace_lease=workspace_lease
            )

    def reopen_process_launch(
        self,
        ref: AuthorityMemberRefV2,
        *,
        expected_record: object,
        workspace_lease: object,
    ) -> object:
        with self._operation():
            return self._catalogue_store(workspace_lease).reopen_project_stage_process_launch(
                ref,
                expected_record=expected_record,
                workspace_lease=workspace_lease,
            )

    def reserve_process_authority(
        self,
        *,
        run_plan_sha256: str,
        launch_nonce_sha256: str,
        project_lease: object,
        workspace_lease: object,
    ) -> tuple[AuthorityMemberRefV2, AuthorityMemberRefV2, AuthorityMemberRefV2]:
        with self._operation():
            active, _request = self._active_run(project_lease, workspace_lease)
            launch_identity = _digest(
                {
                    "active_mutation_run_sha256": active,
                    "launch_nonce_sha256": _sha(launch_nonce_sha256, "launch_nonce_sha256"),
                    "run_plan_sha256": _sha(run_plan_sha256, "run_plan_sha256"),
                }
            )
            store = self._catalogue_store(workspace_lease)
            return (
                store._project_stage_exchange_ref(
                    active_mutation_run_sha256=active,
                    request_identity_sha256=launch_identity,
                    direction="result",
                ),
                store._project_stage_log_ref(
                    active_mutation_run_sha256=active,
                    launch_nonce_sha256=launch_nonce_sha256,
                    stream="stdout",
                ),
                store._project_stage_log_ref(
                    active_mutation_run_sha256=active,
                    launch_nonce_sha256=launch_nonce_sha256,
                    stream="stderr",
                ),
            )

    def _register_run_session(self, session: ProjectStageRunSession) -> None:
        if type(session) is not ProjectStageRunSession or session._runtime is not self:
            raise ProjectStageLifecycleError("project-stage run session authority is invalid")
        with self._condition:
            if id(session) in self._run_sessions:
                raise ProjectStageLifecycleError("project-stage run session is already registered")
            self._run_sessions[id(session)] = session
            self._condition.notify_all()

    def _authenticate_run_session(self, session: object) -> ProjectStageRunSession:
        if type(session) is not ProjectStageRunSession:
            raise ProjectStageLifecycleError("project-stage run session is invalid")
        with self._lock:
            registered = self._run_sessions.get(id(session))
        if registered is not session or session._runtime is not self:
            raise ProjectStageLifecycleError("project-stage run session is stale or foreign")
        return session

    @property
    def active_session_count(self) -> int:
        with self._lock:
            return len(self._run_sessions)

    @property
    def is_quiescent(self) -> bool:
        with self._lock:
            return not (
                self._run_sessions or self._active_project_leases or self._validation_lifecycles
            )

    def wait_for_quiescence(self, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise ProjectStageLifecycleError("project-stage quiescence timeout is invalid")
        with self._condition:
            return self._condition.wait_for(
                lambda: not (
                    self._run_sessions or self._active_project_leases or self._validation_lifecycles
                ),
                timeout=None if timeout_seconds is None else float(timeout_seconds),
            )

    def _discover_persisted_unproved_work(
        self,
        *,
        request: ProjectStageRunRequest,
        contexts: tuple[BatchNodeExecutionContext, ...],
        workspace_lease: object,
        terminal_committer: object,
        allow_completed: bool = False,
        _live_project_lease: object | None = None,
    ) -> _RetainedProjectStageCompletion | ProjectMutationLeaseRecord | None:
        """Identify an interrupted preparation or a completed pending run.

        No process can start before its prepared descriptor is persisted. An
        acquisition without process records can resume preparation under its
        recovered locks. Once process records exist, result recovery requires
        the original child to have exited normally and its result receipts to
        bind the saved entries. Both routes compare the current saved bytes.
        """

        from mif_pipeline.qupath_runtime.exchange import parse_stage_result_exchange_bytes
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceCapability,
            ProjectStageMutationEvidenceRecord,
            ProjectStageProcessEvidenceCapability,
            ProjectStageProcessAttemptRecord,
            ProjectStageProcessDrainRecord,
            ProjectStageProcessLaunchRecord,
        )

        try:
            authorization = self._state_store._project_stage_mutation_authorization(
                request=request,
                contexts=contexts,
                lease=workspace_lease,
                terminal_committer=terminal_committer,
            )
            candidates = self._provider._unresolved_active_records_for_project(
                project_uri=request.project_uri,
                project_identity_sha256=request.project_identity_sha256,
            )
        except Exception as exc:
            raise ProjectStageRecordError(
                "project-stage persisted acquisition cannot be authenticated"
            ) from exc
        if not candidates:
            return
        if len(candidates) != 1:
            raise ProjectStageRecordError("project-stage persisted acquisition is ambiguous")
        active = candidates[0][1]
        active_sha256 = self._provider._active_mutation_run_sha256(active)
        if (
            active.operation != "project_stage"
            or active.project_uri != request.project_uri
            or active.project_identity_sha256 != request.project_identity_sha256
            or active.authority_layout_sha256 != request.authority_layout_sha256
            or active.mutation_request_sha256 != request.request_sha256
            or active.mutation_authorization_sha256 != authorization
            or active.predecessor_authority_head_sha256
            != request.expected_project_authority_head_sha256
        ):
            raise ProjectStageRecordError("project-stage persisted acquisition is foreign or stale")
        with self._lock:
            live = self._active_project_leases.get(id(_live_project_lease))
            if _live_project_lease is None:
                if self._run_sessions or self._active_project_leases:
                    raise ProjectStageLifecycleError(
                        "project-stage persisted acquisition is still live in this runtime"
                    )
            elif (live is None or live[0] is not _live_project_lease
                    or live[4] is not workspace_lease
                    or live[1].record.acquisition_record_sha256 != active.acquisition_record_sha256):
                raise ProjectStageLifecycleError("prelaunch cleanup does not own the live acquisition")
        store = self._catalogue_store(workspace_lease)
        try:
            current_catalogue = store.load_current()
        except Exception as exc:
            raise ProjectStageRecordError(
                "project-stage persisted catalogue cannot be authenticated"
            ) from exc
        head = self._state_store.load_head()
        if (
            current_catalogue is None
            or head.batch_id != request.batch_id
            or head.project_identity_sha256 != request.project_identity_sha256
            or Path(head.project_path).resolve().as_uri() != request.project_uri
            or head.project_authority_head_sha256 != request.expected_project_authority_head_sha256
            or head.project_catalogue_sha256 != request.project_catalogue_sha256
            or current_catalogue.batch_id != request.batch_id
            or current_catalogue.project_uri != request.project_uri
            or current_catalogue.project_identity_sha256 != request.project_identity_sha256
            or current_catalogue.catalogue_sha256 != request.project_catalogue_sha256
        ):
            raise ProjectStageRecordError("project-stage persisted catalogue authority is stale")
        current_saved_state_by_entry_id = (
            self._state_store.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=head.sha256,
                lease=workspace_lease,
            )
        )
        records = _retained_project_stage_process_records(
            store, active_mutation_run_sha256=active_sha256
        )
        if not any(record.active_mutation_run_sha256 == active_sha256 for _ref, record in records):
            # The descriptor is persisted before the runner may spawn a child.
            # Preserve an interrupted preparation as its original acquisition;
            # current saved bytes are inspected again under recovered locks.
            if any(current_saved_state_by_entry_id.get(target.project_entry_id)
                   != target.saved_state_before_sha256 for target in request.targets):
                raise ProjectStageRecordError("unlaunched project-stage saved state changed")
            return active
        descriptors = tuple(
            (ref, record)
            for ref, record in records
            if type(record) is PreparedProjectStageRunDescriptor
            and record.active_mutation_run_sha256 == active_sha256
        )
        attempts = tuple(
            (ref, record)
            for ref, record in records
            if type(record) is ProjectStageProcessAttemptRecord
            and record.active_mutation_run_sha256 == active_sha256
        )
        if not descriptors or len(attempts) > 1:
            raise ProjectStageRecordError(
                "project-stage persisted prepared work is missing or ambiguous"
            )
        selected = (
            tuple(item for item in descriptors
                  if item[1].prepared_descriptor_sha256
                  == attempts[0][1].prepared_descriptor_sha256)
            if attempts else descriptors[:1]
        )
        if len(selected) != 1:
            raise ProjectStageRecordError("project-stage attempt lacks its exact descriptor")
        descriptor_ref, descriptor = selected[0]
        # A crash after preparation can leave a descriptor without any attempt.
        # Such descriptors remain immutable; only session/launch fields may
        # differ when the same acquisition resumes the same plan.
        preparation_fields = {
            "acquisition_session_challenge_sha256", "launch_nonce_sha256",
            "launch_request_identity_sha256", "process_launch_ref",
            "stdout_log_ref", "stderr_log_ref", "prepared_descriptor_sha256",
        }
        for retained_ref, retained_descriptor in descriptors:
            if (
                retained_ref != store.runtime_evidence_ref(
                    retained_descriptor.prepared_descriptor_sha256
                )
                or any(
                    getattr(retained_descriptor, field.name) != getattr(descriptor, field.name)
                    for field in fields(descriptor) if field.name not in preparation_fields
                )
                or retained_descriptor.authority_layout_sha256 != request.authority_layout_sha256
                or retained_descriptor.workspace_storage_scope_sha256
                != self._runtime_binding.workspace_storage_scope_sha256
                or retained_descriptor.project_uri != request.project_uri
                or retained_descriptor.project_identity_sha256 != request.project_identity_sha256
                or retained_descriptor.project_authority_head_sha256
                != request.expected_project_authority_head_sha256
                or retained_descriptor.acquisition_record_sha256 != active.acquisition_record_sha256
                or retained_descriptor.artifact_inventory_sha256 != active.artifact_inventory_sha256
                or retained_descriptor.mutation_request_sha256 != active.mutation_request_sha256
                or retained_descriptor.mutation_authorization_sha256 != active.mutation_authorization_sha256
                or retained_descriptor.fencing_token_sha256 != active.fencing_token_sha256
            ):
                raise ProjectStageRecordError("project-stage persisted descriptor authority differs")
            if retained_descriptor is not descriptor and store._read_ref(
                retained_descriptor.process_launch_ref,
                max_bytes=store._authority_layout.layout.family("runtime_exchange").max_member_bytes,
                label="unlaunched project-stage preparation receipt", optional=True,
            ) is not None:
                raise ProjectStageRecordError("abandoned preparation has a process launch receipt")
        try:
            plan = store._reopen_project_stage_runtime_record(
                descriptor.run_plan_ref,
                record_type=ProjectStageRunPlan,
                digest_field="plan_sha256",
                expected_digest=descriptor.run_plan_sha256,
                label="persisted project-stage run plan",
            )
        except Exception as exc:
            raise ProjectStageRecordError(
                "project-stage persisted run plan cannot be authenticated"
            ) from exc
        if (
            type(plan) is not ProjectStageRunPlan
            or plan.request_sha256 != request.request_sha256
            or plan.project_uri != request.project_uri
            or plan.project_identity_sha256 != request.project_identity_sha256
            or plan.project_authority_head_sha256 != request.expected_project_authority_head_sha256
            or plan.plan_sha256 != descriptor.run_plan_sha256
            or plan.target_authorization_set_sha256 != descriptor.target_authorization_set_sha256
        ):
            raise ProjectStageRecordError("project-stage persisted run plan authority differs")
        try:
            from .batch_artifacts import artifact_ref_set_sha256

            if len(request.targets) != len(plan.targets) or len(contexts) != len(plan.targets):
                raise ProjectStageRecordError("project-stage persisted plan targets differ")
            exchange_family = store._authority_layout.layout.family("runtime_exchange")
            authenticated_targets: list[
                tuple[
                    ProjectStageTargetRequest,
                    ProjectStageAuthorizedEntry,
                    BatchNodeExecutionContext,
                    ProjectStageExchangeAttestation,
                ]
            ] = []
            request_envelopes: list[bytes] = []
            for current_target, authorized, context in zip(
                request.targets, plan.targets, contexts, strict=True
            ):
                try:
                    entry = current_catalogue.resolve(current_target.project_entry_id)
                except KeyError as exc:
                    raise ProjectStageRecordError(
                        "project-stage persisted target is absent from the catalogue"
                    ) from exc
                scheduler_authorization = context.authorization
                dispatch = scheduler_authorization.dispatch_binding
                reopened_authorization = self._state_store.load_authorization(
                    scheduler_authorization.authorization_sha256
                )
                envelope_sha256 = authorized.exchange.request_envelope_sha256
                request_identity_sha256 = _digest(
                    {
                        "active_mutation_run_sha256": active_sha256,
                        "kind": "project_stage_target_exchange/v2",
                        "target_request_sha256": (current_target.target_request_sha256),
                        "request_envelope_sha256": envelope_sha256,
                    }
                )
                request_ref, result_ref, generation_manifest_ref = tuple(
                    store._project_stage_exchange_ref(
                        active_mutation_run_sha256=active_sha256,
                        request_identity_sha256=request_identity_sha256,
                        direction=direction,
                    )
                    for direction in (
                        "request",
                        "result",
                        "generation_manifest",
                    )
                )
                request_payload = store._read_ref(
                    request_ref,
                    max_bytes=exchange_family.max_member_bytes,
                    label="persisted project-stage target request",
                    optional=False,
                )
                if (
                    request_payload is None
                    or hashlib.sha256(request_payload).hexdigest() != envelope_sha256
                    or len(request_payload) != authorized.exchange.request_envelope_byte_count
                ):
                    raise ProjectStageRecordError("project-stage persisted target request differs")
                request_mapping = store._validate_project_stage_request_envelope(
                    current_target, request_payload
                )
                request_envelopes.append(request_payload)
                runtime_request = request_mapping["request"]
                try:
                    project_file_uri = Path(runtime_request["project_file"]).resolve().as_uri()
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    raise ProjectStageRecordError(
                        "project-stage persisted target request differs"
                    ) from exc
                if (
                    request_mapping["invocation_id"] != scheduler_authorization.attempt_id
                    or project_file_uri != request.project_uri
                    or entry.image_role != "primary_mif"
                    or entry.sample_id != current_target.sample_id
                    or entry.slide_id != current_target.slide_id
                    or current_saved_state_by_entry_id.get(current_target.project_entry_id)
                    != current_target.saved_state_before_sha256
                    or entry.entry_projection_sha256 != context.project_entry_projection_sha256
                    or entry.image_sha256 != context.current_image_identity_sha256
                    or runtime_request["canonical_image_uri"] != entry.canonical_uri
                    or runtime_request["canonical_image_sha256"] != entry.image_sha256
                    or runtime_request["script_bundle_sha256"] != plan.script_bundle_sha256
                    or runtime_request["script_asset_sha256"] != plan.target_script_sha256
                ):
                    raise ProjectStageRecordError(
                        "project-stage persisted target request authority differs"
                    )
                expected_exchange = ProjectStageExchangeAttestation.create(
                    active_mutation_run_sha256=active_sha256,
                    target_request=current_target,
                    request_envelope_sha256=envelope_sha256,
                    request_ref=request_ref,
                    request_envelope_byte_count=len(request_payload),
                    result_ref=result_ref,
                    generation_manifest_ref=generation_manifest_ref,
                )
                expected_authorized = ProjectStageAuthorizedEntry.create(
                    target_request=current_target,
                    attempt_id=scheduler_authorization.attempt_id,
                    attempt_authorization_sha256=(scheduler_authorization.authorization_sha256),
                    input_artifact_set_sha256=(scheduler_authorization.input_artifact_set_sha256),
                    exchange=expected_exchange,
                )
                if (
                    reopened_authorization != scheduler_authorization
                    or authorized != expected_authorized
                    or context.node_id != expected_authorized.node_id
                    or scheduler_authorization.node_id != expected_authorized.node_id
                    or scheduler_authorization.attempt_id != expected_authorized.attempt_id
                    or scheduler_authorization.authorization_sha256
                    != expected_authorized.attempt_authorization_sha256
                    or scheduler_authorization.input_artifact_set_sha256
                    != expected_authorized.input_artifact_set_sha256
                    or artifact_ref_set_sha256(context.inputs)
                    != expected_authorized.input_artifact_set_sha256
                    or tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
                    != expected_authorized.prerequisite_artifact_ref_sha256s
                    or context.adapter_spec.spec_sha256 != expected_authorized.adapter_spec_sha256
                    or context.adapter_spec.node_kind != expected_authorized.terminal_stage_id
                    or expected_authorized.terminal_stage_id != plan.stage_id
                    or context.adapter_spec.node_version != plan.stage_version
                    or dispatch.project_entry_ids != (expected_authorized.project_entry_id,)
                    or dispatch.participant_ids != (expected_authorized.sample_id,)
                    or context.project_identity_sha256 != plan.project_identity_sha256
                ):
                    raise ProjectStageRecordError(
                        "project-stage persisted plan target authority differs"
                    )
                authenticated_targets.append(
                    (
                        current_target,
                        expected_authorized,
                        context,
                        expected_exchange,
                    )
                )
        except ProjectStageRecordError:
            raise
        except Exception as exc:
            raise ProjectStageRecordError(
                "project-stage persisted plan target graph differs"
            ) from exc
        drains = tuple(
            (ref, record)
            for ref, record in records
            if type(record) is ProjectStageProcessDrainRecord
            and record.active_mutation_run_sha256 == active_sha256
        )
        mutations = tuple(
            (ref, record)
            for ref, record in records
            if type(record) is ProjectStageMutationEvidenceRecord
            and record.active_mutation_run_sha256 == active_sha256
        )
        if len(attempts) > 1 or len(drains) > 1 or len(mutations) > 1:
            raise ProjectStageRecordError("project-stage persisted process evidence is ambiguous")
        attempt = None if not attempts else attempts[0][1]
        drain = None if not drains else drains[0][1]
        launch_payload = store._read_ref(
            descriptor.process_launch_ref,
            max_bytes=store._authority_layout.layout.family("runtime_exchange").max_member_bytes,
            label="persisted project-stage process launch",
            optional=True,
        )
        launch = None
        if launch_payload is not None:
            try:
                launch_mapping = json.loads(launch_payload.decode("utf-8", errors="strict"))
                if canonical_json_bytes(launch_mapping) != launch_payload:
                    raise ValueError("launch is not canonical")
                launch = ProjectStageProcessLaunchRecord.from_mapping(launch_mapping)
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ProjectStageRecordError(
                    "project-stage persisted launch evidence is invalid"
                ) from exc
        if attempt is None and launch is None and not drains and not mutations:
            for target in plan.targets:
                for ref in (target.exchange.result_ref, target.exchange.generation_manifest_ref):
                    if store._read_ref(
                        ref, max_bytes=exchange_family.max_member_bytes,
                        label="unlaunched project-stage output", optional=True,
                    ) is not None:
                        raise ProjectStageRecordError("unlaunched project stage has saved output")
            return active
        evidence = tuple(item for item in (attempt, launch, drain) if item is not None)
        common = (
            "authority_layout_sha256",
            "workspace_storage_scope_sha256",
            "project_identity_sha256",
            "active_mutation_run_sha256",
            "acquisition_record_sha256",
            "artifact_inventory_sha256",
            "mutation_request_sha256",
            "mutation_authorization_sha256",
            "fencing_token_sha256",
            "run_plan_sha256",
            "launch_nonce_sha256",
        )
        if any(
            any(getattr(item, name) != getattr(descriptor, name) for name in common)
            or item.acquisition_session_challenge_sha256
            != descriptor.acquisition_session_challenge_sha256
            or item.prepared_descriptor_sha256 != descriptor.prepared_descriptor_sha256
            or item.application_request_sha256 != descriptor.prepared_descriptor_sha256
            for item in evidence
        ):
            raise ProjectStageRecordError(
                "project-stage persisted process evidence authority differs"
            )
        if (
            (launch is not None and attempt is None)
            or (drain is not None and attempt is None)
            or (
                attempt is not None
                and launch is not None
                and launch.attempt_sha256 != attempt.attempt_sha256
            )
            or (
                attempt is not None
                and drain is not None
                and drain.attempt_sha256 != attempt.attempt_sha256
            )
            or (
                drain is not None
                and drain.process_launch_sha256 is not None
                and (launch is None or drain.process_launch_sha256 != launch.process_launch_sha256)
            )
        ):
            raise ProjectStageRecordError("project-stage persisted process evidence chain differs")
        if attempt is not None:
            containment = attempt.containment_request
            binding = containment.authority
            expected_profile = (
                "headless-script"
                if plan.launch_mode is ProjectStageLaunchMode.HEADLESS_PROJECT_SCRIPT
                else "interactive-project"
            )
            if (
                binding.launch_nonce_sha256 != descriptor.launch_nonce_sha256
                or binding.application_request_sha256 != descriptor.prepared_descriptor_sha256
                or binding.project_identity_sha256 != descriptor.project_identity_sha256
                or binding.active_mutation_run_sha256 != descriptor.active_mutation_run_sha256
                or binding.containment_scope_sha256 != descriptor.project_identity_sha256
                or containment.command_profile != expected_profile
                or containment.stdout_log_name != descriptor.stdout_log_ref.physical_name
                or containment.stderr_log_name != descriptor.stderr_log_ref.physical_name
            ):
                raise ProjectStageRecordError(
                    "project-stage persisted containment authority differs"
                )
        if launch is not None and (
            launch.process_launch_ref != descriptor.process_launch_ref
            or attempt is None
            or not _containment_request_matches_partial_evidence(
                attempt.containment_request, launch.launch_evidence
            )
        ):
            raise ProjectStageRecordError("project-stage persisted launch authority differs")
        if drain is not None and (
            attempt is None
            or not _containment_request_matches_partial_evidence(
                attempt.containment_request, drain.drain_evidence
            )
            or drain.stdout_log_ref != descriptor.stdout_log_ref
            or drain.stderr_log_ref != descriptor.stderr_log_ref
        ):
            raise ProjectStageRecordError("project-stage persisted drain authority differs")
        process_evidence = None
        if attempt is not None and launch is not None and drain is not None:
            try:
                attempt_payload_sha256, attempt_byte_count = _payload_facts(attempt)
                launch_payload_sha256, launch_byte_count = _payload_facts(launch)
                drain_payload_sha256, drain_byte_count = _payload_facts(drain)
                process_evidence = ProjectStageProcessEvidenceCapability._issue(
                    issuer=_PROJECT_STAGE_RUNTIME_ISSUER,
                    attempt=attempt,
                    attempt_ref=attempts[0][0],
                    attempt_payload_sha256=attempt_payload_sha256,
                    attempt_byte_count=attempt_byte_count,
                    launch=launch,
                    launch_ref=descriptor.process_launch_ref,
                    launch_payload_sha256=launch_payload_sha256,
                    launch_byte_count=launch_byte_count,
                    drain=drain,
                    drain_ref=drains[0][0],
                    drain_payload_sha256=drain_payload_sha256,
                    drain_byte_count=drain_byte_count,
                )
            except Exception as exc:
                raise ProjectStageRecordError(
                    "project-stage persisted process evidence graph differs"
                ) from exc
        elif drain is not None and (
            launch is not None
            or drain.process_launch_sha256 is not None
            or drain.termination_reason != "launch_publication_failed"
        ):
            raise ProjectStageRecordError("project-stage persisted launch-failure evidence differs")
        if mutations:
            mutation_ref, mutation = mutations[0]
            if (
                process_evidence is None
                or mutation.predecessor_project_authority_head_sha256
                != descriptor.project_authority_head_sha256
            ):
                raise ProjectStageRecordError(
                    "project-stage persisted mutation evidence boundary differs"
                )
            try:
                observations = mutation.target_observations
                if len(observations) != len(authenticated_targets):
                    raise ProjectStageRecordError("project-stage persisted mutation targets differ")
                for authenticated, observation in zip(
                    authenticated_targets,
                    observations,
                    strict=True,
                ):
                    (
                        current_target,
                        expected_authorized,
                        context,
                        expected_exchange,
                    ) = authenticated
                    if (
                        observation.node_id != expected_authorized.node_id
                        or observation.attempt_id != expected_authorized.attempt_id
                        or observation.attempt_authorization_sha256
                        != expected_authorized.attempt_authorization_sha256
                        or observation.authorized_entry_sha256
                        != expected_authorized.authorized_entry_sha256
                        or observation.project_entry_id != expected_authorized.project_entry_id
                        or observation.sample_id != expected_authorized.sample_id
                        or observation.slide_id != expected_authorized.slide_id
                        or observation.process_evidence_sha256
                        != process_evidence.evidence_set_sha256
                        or observation.result_ref != expected_exchange.result_ref
                        or observation.generation_manifest_ref
                        != expected_exchange.generation_manifest_ref
                        or observation.saved_state_before_sha256
                        != expected_authorized.saved_state_before_sha256
                    ):
                        raise ProjectStageRecordError(
                            "project-stage persisted mutation target authority differs"
                        )
                    _project_stage_context_facts(
                        context=context,
                        authorized=expected_authorized,
                        observation=observation,
                    )
                    result_payload = store._read_ref(
                        expected_exchange.result_ref,
                        max_bytes=exchange_family.max_member_bytes,
                        label="persisted project-stage target result",
                        optional=True,
                    )
                    manifest_payload = store._read_ref(
                        expected_exchange.generation_manifest_ref,
                        max_bytes=exchange_family.max_member_bytes,
                        label="persisted project-stage generation manifest",
                        optional=True,
                    )
                    for payload, digest, byte_count, label in (
                        (
                            result_payload,
                            observation.result_payload_sha256,
                            observation.result_byte_count,
                            "result",
                        ),
                        (
                            manifest_payload,
                            observation.generation_manifest_payload_sha256,
                            observation.generation_manifest_byte_count,
                            "generation manifest",
                        ),
                    ):
                        if (
                            (payload is None) != (digest is None)
                            or (payload is None) != (byte_count is None)
                            or (
                                payload is not None
                                and (
                                    len(payload) != byte_count
                                    or hashlib.sha256(payload).hexdigest() != digest
                                )
                            )
                        ):
                            raise ProjectStageRecordError(
                                f"project-stage persisted {label} differs"
                            )
                    if result_payload is None:
                        if manifest_payload is not None:
                            raise ProjectStageRecordError(
                                "project-stage persisted exchange is incomplete"
                            )
                        continue
                    parsed_exchange = parse_stage_result_exchange_bytes(
                        result_payload,
                        manifest_payload,
                        invocation_id=expected_authorized.attempt_id,
                        run_id=current_target.pipeline_run_id,
                        stage=current_target.wire_stage_id,
                        slide_id=current_target.slide_id,
                        generation_manifest_name=(
                            expected_exchange.generation_manifest_ref.physical_name
                        ),
                        _manifest_required=lambda result, target=expected_authorized: (
                            _project_stage_result_requires_manifest(target, result)
                        ),
                    )
                    _validate_project_stage_execution_evidence_inputs(
                        context=context,
                        authorized=expected_authorized,
                        observation=observation,
                        parsed_exchange=parsed_exchange,
                        execution_exchange_sha256=(expected_exchange.exchange_sha256),
                    )
                mutation_payload_sha256, mutation_byte_count = _payload_facts(mutation)
                ProjectStageMutationEvidenceCapability._issue(
                    issuer=_PROJECT_STAGE_RUNTIME_ISSUER,
                    mutation=mutation,
                    mutation_ref=mutation_ref,
                    mutation_payload_sha256=mutation_payload_sha256,
                    mutation_byte_count=mutation_byte_count,
                    process_evidence=process_evidence,
                )
                log_family = store._authority_layout.layout.family("runtime_log")
                for ref, digest, byte_count, label in (
                    (
                        mutation.stdout_log_ref,
                        mutation.stdout_payload_sha256,
                        mutation.stdout_byte_count,
                        "stdout",
                    ),
                    (
                        mutation.stderr_log_ref,
                        mutation.stderr_payload_sha256,
                        mutation.stderr_byte_count,
                        "stderr",
                    ),
                ):
                    payload = store._read_ref(
                        ref,
                        max_bytes=log_family.max_member_bytes,
                        label=f"persisted project-stage {label} log",
                        optional=False,
                    )
                    if (
                        payload is None
                        or len(payload) != byte_count
                        or hashlib.sha256(payload).hexdigest() != digest
                    ):
                        raise ProjectStageRecordError(
                            f"project-stage persisted {label} log differs"
                        )
            except ProjectStageRecordError:
                raise
            except Exception as exc:
                raise ProjectStageRecordError(
                    "project-stage persisted mutation evidence graph differs"
                ) from exc
        failed_without_outputs = allow_completed and _retained_output_free_failure(
            process_evidence, drain, mutations, plan, store)
        if (
            allow_completed
            and process_evidence is not None
            and ((drain.termination_reason == "normal_exit" and drain.exit_status == 0)
                 or failed_without_outputs)
            and drain.containment_drained is True
            and plan.launch_mode is ProjectStageLaunchMode.HEADLESS_PROJECT_SCRIPT
        ):
            from mif_pipeline.qupath_runtime.project_stage import (
                ProjectStageProcessCompletion, ProjectStageProcessSummary,
            )

            # Captured receipt bytes are checked against original immutable
            # launch identities here. Current saved bytes are observed under
            # the recovered project lock by ordinary result validation.
            for target in plan.targets:
                if failed_without_outputs:
                    continue
                result_payload = store._read_ref(
                    target.exchange.result_ref, max_bytes=exchange_family.max_member_bytes,
                    label="completed project-stage result", optional=False,
                )
                manifest_payload = store._read_ref(
                    target.exchange.generation_manifest_ref,
                    max_bytes=exchange_family.max_member_bytes,
                    label="completed project-stage manifest", optional=True,
                )
                parsed = parse_stage_result_exchange_bytes(
                    result_payload, manifest_payload, invocation_id=target.attempt_id,
                    run_id=target.pipeline_run_id, stage=target.wire_stage_id,
                    slide_id=target.slide_id,
                    generation_manifest_name=target.exchange.generation_manifest_ref.physical_name,
                    _manifest_required=lambda result, target=target: (
                        _project_stage_result_requires_manifest(target, result)
                    ),
                )
                _require_native_completion_binding(parsed, target, process_evidence)
            log_family = store._authority_layout.layout.family("runtime_log")
            log_facts = {}
            for name, ref in (("stdout", drain.stdout_log_ref), ("stderr", drain.stderr_log_ref)):
                payload = store._read_ref(
                    ref, max_bytes=log_family.max_member_bytes,
                    label=f"completed project-stage {name}", optional=False,
                )
                log_facts[f"{name}_log_ref"] = ref
                log_facts[f"{name}_payload_sha256"] = hashlib.sha256(payload).hexdigest()
                log_facts[f"{name}_byte_count"] = len(payload)
            summary = ProjectStageProcessSummary.create(
                process_start_token=drain.process_start_token,
                process_evidence_sha256=process_evidence.evidence_set_sha256,
                termination_reason=drain.termination_reason, exit_status=drain.exit_status,
                containment_drained=True, **log_facts,
            )
            completion = ProjectStageProcessCompletion.create(
                disposition="released_child", process_evidence=process_evidence,
                no_mutation_proof=None, process_summary=summary,
            )
            plan_sha, plan_bytes = _payload_facts(plan)
            descriptor_sha, descriptor_bytes = _payload_facts(descriptor)
            return _RetainedProjectStageCompletion(
                active=active,
                publication=PublishedProjectStageRunPlan(
                    plan, descriptor.run_plan_ref, plan_sha, plan_bytes,
                    plan.target_authorization_set_sha256,
                ),
                published_descriptor=PublishedPreparedProjectStageRunDescriptor(
                    descriptor, descriptor_ref, descriptor_sha, descriptor_bytes,
                ),
                completion=completion,
                request_envelopes=tuple(request_envelopes),
                mutation=None if not mutations else mutations[0][1],
            )
        refs = [active.lease_record_ref, descriptor_ref, descriptor.run_plan_ref]
        refs.extend(ref for ref, _record in attempts)
        refs.extend(ref for ref, _record in drains)
        refs.extend(ref for ref, _record in mutations)
        if launch is not None:
            refs.append(descriptor.process_launch_ref)
        if drain is not None:
            refs.extend((drain.stdout_log_ref, drain.stderr_log_ref))
        retained_refs = tuple(sorted(set(refs), key=lambda item: (item.family, item.key_sha256)))
        missing_completion = launch is not None and drain is None
        required = ProjectStageManualReviewRequired(
            ProjectStageManualReview(
                reason_code=("project_stage_process_completion_missing" if missing_completion
                             else "project_stage_interrupted_unproved"),
                project_uri=request.project_uri,
                project_identity_sha256=request.project_identity_sha256,
                run_plan_sha256=plan.plan_sha256,
                prepared_descriptor_sha256=(descriptor.prepared_descriptor_sha256),
                retained_record_refs=retained_refs,
                in_process_authority_released=True,
                operator_message=(
                    "The saved QuPath run has no process-completion record. "
                    "Its exit status and completed process cleanup are unknown. "
                    "The application cannot accept the old result from these records. "
                    "For a supported local first segmentation, Restore accepted regions checks "
                    "the prior process and saved regions before a new attempt. Keep the analysis folder intact."
                    if missing_completion else
                    "A retained project-stage run has no proved grouped "
                    "completion. Review its immutable evidence before retrying."
                ),
            )
        )
        if missing_completion:
            required.add_note("Original native launch and assigned output locations "
                "(outputs may be incomplete; these references do not prove completion):\n"
                + json.dumps({
                    "process_start_token": launch.process_start_token.to_mapping(),
                    "containment_provider": launch.launch_evidence.provider,
                    "process_launch_sha256": launch.process_launch_sha256,
                    "exit_status": None,
                    "process_completion_record": "missing",
                    "targets": [{
                        "attempt_id": target.attempt_id,
                        "node_id": target.node_id,
                        "project_entry_id": target.project_entry_id,
                        "request": target.exchange.request_ref.physical_name,
                        "result": target.exchange.result_ref.physical_name,
                        "generation_manifest": target.exchange.generation_manifest_ref.physical_name,
                    } for target in plan.targets],
                    "stdout": descriptor.stdout_log_ref.physical_name,
                    "stderr": descriptor.stderr_log_ref.physical_name,
                }, indent=2))
        raise required

    def _check_unmodified_entries(self, prepared: object, project_lease: object,
                                  workspace_lease: object, targets: tuple[object, ...]) -> object:
        """Check the project and named saved hierarchies under their current lease."""
        head = self._state_store.load_head()
        request = prepared.request
        from mif_pipeline.qupath_runtime.catalogue import ProjectAuthorityError
        from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
        try:
            snapshot = self._provider.inspect_live_project(project_lease)
        except ProjectAuthorityError as exc:
            raise ProjectStageRecordError("project-stage retry live project inspection failed") from exc
        if (snapshot.identity_record is None
                or snapshot.identity_record.project_identity_sha256 != request.project_identity_sha256
                or snapshot.project_uri != request.project_uri
                or not same_project_snapshot(snapshot.project_path,
                    head.project_qpproj_snapshot_sha256, snapshot.qpproj_sha256)):
            raise ProjectStageRecordError("project-stage retry live project authority changed")
        entries = {item.project_entry_id: item for item in snapshot.entries}
        for target in targets:
            entry = entries.get(target.project_entry_id)
            if (entry is None or entry.saved_state_attestation_sha256 is None
                    or entry.saved_state_attestation_sha256 != target.saved_state_before_sha256):
                raise ProjectStageRecordError("project-stage retry saved image state changed")
        return head

    def _authenticate_unlaunched_abort(self, project_lease: object) -> object:
        """Prove no child could have started, then inspect unchanged saved bytes."""
        with self._lock:
            live = self._active_project_leases.get(id(project_lease))
        if live is None or live[0] is not project_lease:
            raise ProjectStageLifecycleError("prelaunch abort lacks its live runtime acquisition")
        workspace_lease = live[4]
        _active_run, prepared = self._active_run(project_lease, workspace_lease)
        with self._lock:
            published = self._published_prepared_requests.get(id(prepared))
        if published is None or published[0] is not prepared:
            raise ProjectStageLifecycleError("prelaunch abort request is not published")
        retained = self._discover_persisted_unproved_work(
            request=prepared.request,
            contexts=tuple(target.context for target in prepared.prepared_targets),
            workspace_lease=workspace_lease, terminal_committer=published[3],
            _live_project_lease=project_lease,
        )
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseRecord
        if (type(retained) is not ProjectMutationLeaseRecord
                or retained.acquisition_record_sha256 != project_lease.acquisition_record_sha256
                or self._provider._persisted_runtime_spawn_attempts(retained, project_lease._provider_inventory)
                or self._provider._persisted_runtime_process_launch_receipts(retained, project_lease._provider_inventory)):
            raise ProjectStageRecordError("prelaunch abort has process evidence")
        return self._check_unmodified_entries(prepared, project_lease, workspace_lease,
            tuple(target.target for target in prepared.prepared_targets))

    def _abort_prelaunch_if_unattempted(self, project_lease: object, workspace_lease: object) -> None:
        """Settle ordinary preparation errors; never infer safety after a launch."""
        if project_lease is None or not project_lease.is_live or project_lease.is_finalized:
            return
        active = self._provider._validate_live_lease_authority(project_lease, require_project=True)
        active_run = self._provider._active_mutation_run_sha256(active)
        records = tuple((ref, record) for ref, record in _retained_project_stage_process_records(
            self._catalogue_store(workspace_lease), active_mutation_run_sha256=active_run)
            if record.active_mutation_run_sha256 == active_run)
        if (any(type(record) is not PreparedProjectStageRunDescriptor for _ref, record in records)
                or self._provider._persisted_runtime_spawn_attempts(active, project_lease._provider_inventory)
                or self._provider._persisted_runtime_process_launch_receipts(active, project_lease._provider_inventory)):
            return
        self._provider._abort_unlaunched_project_stage(project_lease, runtime=self)

    def settle_unlaunched_preparation(self, prepared_request: object,
                                     workspace_lease: object, terminal_committer: object) -> None:
        """Release an unused acquisition before accepting a previous native result.

        This changes only the project mutation journal. The batch authorization,
        failed predecessor and completed result remain with their existing owners.
        """
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseRecord

        prepared = self._authenticate_prepared(prepared_request)
        active = self._discover_persisted_unproved_work(
            request=prepared.request,
            contexts=tuple(target.context for target in prepared.prepared_targets),
            workspace_lease=workspace_lease, terminal_committer=terminal_committer,
        )
        if active is None:
            return
        if type(active) is not ProjectMutationLeaseRecord:
            raise ProjectStageRecordError("launched project-stage work cannot be discarded")
        self.publish_request(prepared, workspace_lease=workspace_lease,
            terminal_committer=terminal_committer)
        inventory = self._state_store.reopen_project_mutation_artifact_inventory(
            active.artifact_inventory_sha256, lease=workspace_lease).inventory
        session = self.prepare_acquisition(prepared,
            static_targets=inventory.static_targets, dynamic_targets=inventory.dynamic_targets,
            workspace_lease=workspace_lease, unlaunched=active)
        try:
            with _ProjectStageAcquisitionLifecycle(self, session, workspace_lease,
                    unlaunched=active) as project_lease:
                self._provider._abort_unlaunched_project_stage(project_lease, runtime=self)
        finally:
            with self._lock:
                self._sessions.pop(id(session), None)
                self._published_prepared_requests.pop(id(prepared), None)

    def _authenticate_unlaunched_recovery(
        self, session: object, active: ProjectMutationLeaseRecord,
    ) -> None:
        """Bind a preparation interrupted before any persisted process evidence."""
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseRecord

        with self._lock:
            issued = self._sessions.get(id(session))
        if (type(active) is not ProjectMutationLeaseRecord or issued is None
            or issued[0] is not session or session.record.operation != "project_stage"
            or session.record.acquisition_record_sha256 != active.acquisition_record_sha256
            or issued[3].request_sha256 != active.mutation_request_sha256
            or issued[3].project_uri != active.project_uri
            or issued[3].project_identity_sha256 != active.project_identity_sha256):
            raise ProjectStageLifecycleError("unlaunched project-stage recovery is foreign")
        with self._lock:
            published = self._published_prepared_requests.get(id(issued[1]))
        if (published is None or published[0] is not issued[1]
            or published[2] is not issued[4]):
            raise ProjectStageLifecycleError("unlaunched project-stage request is not current")
        retained = self._discover_persisted_unproved_work(
            request=issued[3],
            contexts=tuple(item.context for item in issued[1].prepared_targets),
            workspace_lease=issued[4], terminal_committer=published[3],
        )
        inventory = session._artifact_inventory_capability.inventory
        if (retained != active
            or self._provider._persisted_runtime_spawn_attempts(active, inventory)
            or self._provider._persisted_runtime_process_launch_receipts(active, inventory)):
            raise ProjectStageRecordError("project-stage preparation has retained process evidence")

    def _authenticate_completed_recovery(
        self, session: object, stale_active_record_sha256: str
    ) -> _RetainedProjectStageCompletion:
        """Limit provider takeover to this runtime's reopened completed operation."""
        with self._lock:
            issued = self._sessions.get(id(session))
            recovered = self._recovered_completions.get(id(session))
        if (
            issued is None or issued[0] is not session
            or recovered is None or recovered[0] is not session
            or recovered[1].active.lease_record_sha256 != stale_active_record_sha256
            or recovered[1].active.acquisition_record_sha256
            != session.record.acquisition_record_sha256
            or session.record.operation != "project_stage"
        ):
            raise ProjectStageLifecycleError("completed project-stage recovery is not owned here")
        return recovered[1]

    def _process_acquisition_challenge(self, project_lease: object, session: object) -> str:
        """Keep the original process identity distinct from fresh recovery control."""
        with self._lock:
            recovered = self._recovered_completions.get(id(session))
        if recovered is None:
            return session.acquisition_session_challenge_sha256
        retained = self._authenticate_completed_recovery(
            session, project_lease.stale_active_record_sha256
        )
        original_process = project_lease._project_stage_process_evidence
        if original_process is not retained.completion.process_evidence:
            raise ProjectStageLifecycleError("recovered project-stage process binding changed")
        return original_process.attempt.acquisition_session_challenge_sha256

    def _recover_completed_run(
        self, prepared_request: object, workspace_lease: object, terminal_committer: object
    ) -> tuple[ScheduledNodeResult, ...] | None:
        """Complete a native run whose result survived its original Python owner.

        We reacquire the same project operation after its owner has exited and
        its original child has drained. The native receipt describes the bytes
        saved by that child; ordinary inspection checks current bytes, and the
        ordinary codec and commit owners publish the resulting scientific state.
        No native processing is repeated and original launch records are retained.
        If the earlier owner published its mutation observation before stopping,
        we require an identical current observation before reusing that record.
        """
        prepared = self._authenticate_prepared(prepared_request)
        request = prepared.request
        if not self._provider._unresolved_active_records_for_project(
            project_uri=request.project_uri,
            project_identity_sha256=request.project_identity_sha256,
        ):
            return None
        contexts = tuple(target.context for target in prepared.prepared_targets)
        retained = self._discover_persisted_unproved_work(
            request=request, contexts=contexts, workspace_lease=workspace_lease,
            terminal_committer=terminal_committer, allow_completed=True,
        )
        if not isinstance(retained, _RetainedProjectStageCompletion):
            return None
        self.publish_request(
            prepared, workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )
        with self._lock:
            request_capability = self._published_prepared_requests[id(prepared)][1]
        inventory = self._state_store.reopen_project_mutation_artifact_inventory(
            retained.active.artifact_inventory_sha256, lease=workspace_lease,
        )
        acquisition = self._state_store.reopen_project_mutation_acquisition_record(
            retained.active.acquisition_record_sha256, lease=workspace_lease,
        )
        session = self._provider.issue_acquisition_session(
            acquisition, request=request_capability, inventory=inventory,
            durability_authority=self._state_store.project_mutation_durability_authority(
                workspace_lease
            ),
        )
        with self._lock:
            self._sessions[id(session)] = (
                session, prepared, request_capability, request, workspace_lease,
            )
            self._recovered_completions[id(session)] = (session, retained)
        completion = retained.completion
        try:
            with _ProjectStageAcquisitionLifecycle(
                self, session, workspace_lease, recovery=retained,
            ) as project_lease:
                if completion.process_summary.exit_status != 0:
                    # A real retained nonzero exit can produce an ordinary failed
                    # terminal only when it wrote no exchange and changed no image.
                    self._check_unmodified_entries(prepared, project_lease,
                        workspace_lease, retained.publication.plan.targets)
                # The original process's catalogue capabilities were in memory.
                # We reissue them from the captured original request bytes and
                # original run identity, preserving all three exchange members.
                store = self._catalogue_store(workspace_lease)
                for target, authorized, payload in zip(
                    prepared.prepared_targets, retained.publication.plan.targets,
                    retained.request_envelopes, strict=True,
                ):
                    exchange = store._publish_project_stage_target_exchange(
                        target.target, request_envelope_bytes=payload,
                        active_mutation_run_sha256=(
                            retained.published_descriptor.descriptor.active_mutation_run_sha256
                        ),
                    )
                    if exchange != authorized.exchange:
                        raise ProjectStageRecordError(
                            "recovered target exchange differs from its original plan"
                        )
                with self._lock:
                    self._completed_processes[id(completion)] = (
                        completion, retained.published_descriptor,
                        retained.published_descriptor.descriptor,
                        project_lease, workspace_lease, completion.process_evidence._issuer,
                    )
                outcomes = validate_project_stage_results(
                    retained.publication, prepared, completion, self,
                    workspace_lease=workspace_lease, project_lease=project_lease,
                )
                scheduled, receipt = _commit_project_stage_outcomes(
                    self, retained.publication, prepared, completion, outcomes,
                    project_lease=project_lease, workspace_lease=workspace_lease,
                    terminal_committer=terminal_committer,
                )
                project_lease.complete(receipt)
                return tuple(scheduled)
        finally:
            with self._lock:
                self._sessions.pop(id(session), None)
                self._recovered_completions.pop(id(session), None)
                self._completed_processes.pop(id(completion), None)
                self._validated_result_sets.pop(id(completion), None)
                self._published_prepared_requests.pop(id(prepared), None)

    def _prepare_run_session(
        self,
        prepared_request: object,
        workspace_lease: object,
        terminal_committer: object,
    ) -> ProjectStageRunSession:
        from .batch_state import BatchTerminalCommitter, BatchWorkspaceLease
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactTarget,
            ProjectMutationLeaseRecord,
        )
        from mif_pipeline.qupath_runtime._script_asset_registry import (
            QuPathScriptAsset,
            relative_script_path,
        )

        if (
            type(self) is not ProjectStageRuntime
            or not isinstance(workspace_lease, BatchWorkspaceLease)
            or not isinstance(terminal_committer, BatchTerminalCommitter)
        ):
            raise ProjectStageLifecycleError(
                "project-stage session preparation authority is invalid"
            )
        prepared = self._authenticate_prepared(prepared_request)
        request = prepared.request
        contexts = tuple(item.context for item in prepared.prepared_targets)
        if not contexts or len(contexts) != len(request.targets):
            raise ProjectStageRecordError(
                "project-stage session requires one capability per target"
            )
        unlaunched = self._discover_persisted_unproved_work(
            request=request,
            contexts=contexts,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )
        if unlaunched is not None and type(unlaunched) is not ProjectMutationLeaseRecord:
            raise ProjectStageRecordError("completed project-stage work requires result recovery")
        head = self._state_store.load_head()
        project_path = Path(head.project_path)
        if (
            project_path.resolve().as_uri() != request.project_uri
            or head.project_identity_sha256 != request.project_identity_sha256
            or head.project_authority_head_sha256 != request.expected_project_authority_head_sha256
            or head.project_qpproj_snapshot_sha256 is None
            or any(
                item.project_qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                for item in contexts
            )
        ):
            raise ProjectStageRecordError("project-stage session project authority is stale")
        self.publish_request(
            prepared,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )
        reopened_request = self.reopen_request(
            prepared,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )
        if reopened_request != request:
            raise ProjectStageRecordError("project-stage session request changed after publication")
        qpproj_target = ProjectMutationArtifactTarget.create(
            target_kind="qpproj",
            resolution_kind="exact_uri",
            owner_root_uri=project_path.parent.resolve().as_uri(),
            relative_locator_or_metadata_key=project_path.name,
            predecessor_sha256=head.project_qpproj_snapshot_sha256,
        )
        acquisition_session = self.prepare_acquisition(
            prepared,
            static_targets=(qpproj_target,),
            dynamic_targets=(),
            workspace_lease=workspace_lease,
            unlaunched=unlaunched,
        )
        acquisition_context = _ProjectStageAcquisitionLifecycle(
            self, acquisition_session, workspace_lease, unlaunched=unlaunched,
        )
        project_lease = None

        def retain_failed_acquisition(
            cleanup: _ProjectStageOuterCleanupFailure,
            failure: BaseException,
        ) -> None:
            session = ProjectStageRunSession._issue(
                runtime=self,
                prepared_request=prepared,
                publication=None,
                published_descriptor=None,
                launch_descriptor=None,
                acquisition_context=acquisition_context,
                acquisition_session=acquisition_session,
                project_lease=project_lease,
                launch=None,
                workspace_lease=workspace_lease,
                terminal_committer=terminal_committer,
                _preparation_cleanup=True,
                _preparation_failure=failure,
                _preparation_cleanup_step=cleanup.failed_step,
                _issuer=_PROJECT_STAGE_RUNTIME_ISSUER,
            )
            self._register_run_session(session)
            cleanup._retry_session = session

        try:
            project_lease = acquisition_context.__enter__()
            retried_targets = tuple(
                item.target
                for item in prepared.prepared_targets
                if unlaunched is not None or (
                    item.context.authorization.retry_ordinal > 0
                    and item.context.authorization.interaction_response_sha256 is None
                )
            )
            if retried_targets:
                self._check_unmodified_entries(prepared, project_lease, workspace_lease,
                    retried_targets)
            publication = self.publish_run_plan(
                prepared,
                project_lease=project_lease,
                workspace_lease=workspace_lease,
                terminal_committer=terminal_committer,
            )
            plan = self.reopen_run_plan(publication, workspace_lease=workspace_lease)
            if plan != publication.plan:
                raise ProjectStageRecordError(
                    "project-stage session run plan changed after publication"
                )
            launch = self._runner.issue_project_mutation_launch(project_lease=project_lease)
            launch_ref, stdout_ref, stderr_ref = self.reserve_process_authority(
                run_plan_sha256=plan.plan_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                project_lease=project_lease,
                workspace_lease=workspace_lease,
            )
            active = project_lease._provider._validate_live_lease_authority(
                project_lease, require_project=False
            )
            active_sha256 = project_lease._provider._active_mutation_run_sha256(active)
            descriptor = PreparedProjectStageRunDescriptor.create(
                authority_layout_sha256=plan.authority_layout_sha256,
                workspace_storage_scope_sha256=(
                    self._runtime_binding.workspace_storage_scope_sha256
                ),
                project_uri=plan.project_uri,
                project_identity_sha256=plan.project_identity_sha256,
                project_authority_head_sha256=plan.project_authority_head_sha256,
                active_mutation_run_sha256=active_sha256,
                acquisition_record_sha256=(acquisition_session.record.acquisition_record_sha256),
                acquisition_session_challenge_sha256=(
                    acquisition_session.acquisition_session_challenge_sha256
                ),
                artifact_inventory_sha256=(acquisition_session.record.artifact_inventory_sha256),
                mutation_request_sha256=(acquisition_session.record.mutation_request_sha256),
                mutation_authorization_sha256=(
                    acquisition_session.record.mutation_authorization_sha256
                ),
                run_plan_ref=publication.plan_ref,
                run_plan_sha256=plan.plan_sha256,
                run_plan_byte_count=publication.byte_count,
                target_authorization_set_sha256=(plan.target_authorization_set_sha256),
                request_member_set_sha256=plan.request_member_set_sha256,
                script_bundle_sha256=plan.script_bundle_sha256,
                script_bundle_manifest_ref=plan.script_bundle_manifest_ref,
                script_bundle_manifest_sha256=(plan.script_bundle_manifest_sha256),
                script_bundle_manifest_byte_count=(plan.script_bundle_manifest_byte_count),
                target_script_asset_id=plan.target_script_asset_id,
                target_script_ref=plan.target_script_ref,
                target_script_sha256=plan.target_script_sha256,
                target_script_byte_count=plan.target_script_byte_count,
                guard_script_asset_id=plan.guard_script_asset_id,
                guard_script_ref=plan.guard_script_ref,
                guard_script_sha256=plan.guard_script_sha256,
                guard_script_byte_count=plan.guard_script_byte_count,
                fencing_token_sha256=project_lease.fencing_token_sha256,
                launch_nonce_sha256=launch.launch_nonce_sha256,
                process_launch_ref=launch_ref,
                stdout_log_ref=stdout_ref,
                stderr_log_ref=stderr_ref,
            )
            published_descriptor = self.publish_prepared_descriptor(
                descriptor, workspace_lease=workspace_lease
            )
            reopened_descriptor = self.reopen_prepared_descriptor(
                published_descriptor, workspace_lease=workspace_lease
            )
            if reopened_descriptor != descriptor:
                raise ProjectStageRecordError(
                    "project-stage prepared descriptor changed after reopen"
                )
            target_file = (
                project_path.parent
                / "scripts"
                / f"bundle-{plan.script_bundle_sha256}"
                / relative_script_path(QuPathScriptAsset(plan.target_script_asset_id))
            )
            launch_descriptor = QuPathProjectLaunchDescriptor(
                mode=plan.launch_mode,
                project_file=project_path.resolve(),
                target_script_file=target_file.resolve(),
                target_script_asset_id=plan.target_script_asset_id,
                eligible_project_entry_ids=tuple(
                    sorted(item.project_entry_id for item in plan.targets)
                ),
                excluded_project_entry_ids=plan.excluded_project_entry_ids,
                run_plan_sha256=plan.plan_sha256,
            )
            session = ProjectStageRunSession._issue(
                runtime=self,
                prepared_request=prepared,
                publication=publication,
                published_descriptor=published_descriptor,
                launch_descriptor=launch_descriptor,
                acquisition_context=acquisition_context,
                acquisition_session=acquisition_session,
                project_lease=project_lease,
                launch=launch,
                workspace_lease=workspace_lease,
                terminal_committer=terminal_committer,
                _issuer=_PROJECT_STAGE_RUNTIME_ISSUER,
            )
            self._register_run_session(session)
            return session
        except BaseException as primary:
            if type(primary) is _ProjectStageOuterCleanupFailure:
                if not acquisition_context.is_closed:
                    retain_failed_acquisition(
                        primary,
                        getattr(primary, "_preparation_failure", primary),
                    )
                raise
            try:
                self._abort_prelaunch_if_unattempted(project_lease, workspace_lease)
            finally:
                try:
                    acquisition_context.__exit__(*__import__("sys").exc_info())
                except _ProjectStageOuterCleanupFailure as cleanup:
                    if not acquisition_context.is_closed:
                        retain_failed_acquisition(cleanup, primary)
                    raise
            raise

    def _finish_run_session(
        self,
        session: ProjectStageRunSession,
        results: tuple[object, ...],
        *,
        state: Literal["terminalized", "unresolved_closed"],
    ) -> None:
        self._authenticate_run_session(session)
        completion = session._completion
        release_failure: BaseException | None = None
        with self._condition:
            run_session = self._run_sessions.pop(id(session), None)
            acquisition_session = self._sessions.pop(id(session._acquisition_session), None)
            try:
                session._acquisition_lifecycle.release_runtime_for_session_finish()
            except BaseException as exc:
                if not session._acquisition_lifecycle._runtime_is_released():
                    if run_session is not None:
                        self._run_sessions[id(session)] = run_session
                    if acquisition_session is not None:
                        self._sessions[id(session._acquisition_session)] = acquisition_session
                    raise
                session._acquisition_lifecycle._state = "closed"
                release_failure = exc
            if completion is not None:
                self._completed_processes.pop(id(completion), None)
                self._validated_result_sets.pop(id(completion), None)
                self._validation_lifecycles.pop(id(completion), None)
            self._published_prepared_requests.pop(id(session._prepared_request), None)
            if not any(
                item._workspace_lease is session._workspace_lease
                for item in self._run_sessions.values()
            ):
                self._catalogue_stores.pop(id(session._workspace_lease), None)
            with session._condition:
                session._results = results
                session._state = state
                session._cleanup_retry_in_progress = False
                session._condition.notify_all()
            self._condition.notify_all()
        if release_failure is not None:
            raise release_failure


_SUPERVISED_ANNOTATIONS_RESUME_FIELDS = frozenset(
    {
        "schema_version", "phase", "class_ids", "feature_schema",
        "current_validation_domain_id", "point_selection_radius_level0",
    }
)
_SUPERVISED_FEATURE_FIELDS = frozenset({"measurement_name", "semantic_role"})


def _nonblank_transport_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _positive_finite_transport_number(value: object) -> bool:
    if type(value) not in {int, float} or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _nonempty_transport_text_sequence(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and all(_nonblank_transport_text(item) for item in value)
    )


def _closed_transport_records(
    value: object,
    fields: frozenset[str],
) -> tuple[Mapping[str, object], ...] | None:
    if type(value) is not tuple:
        return None
    records: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or frozenset(item) != fields:
            return None
        records.append(item)
    return tuple(records)


def _valid_supervised_feature_schema(value: object) -> bool:
    records = _closed_transport_records(value, _SUPERVISED_FEATURE_FIELDS)
    return (
        records is not None
        and bool(records)
        and all(
            _nonblank_transport_text(record["measurement_name"])
            and _nonblank_transport_text(record["semantic_role"])
            for record in records
        )
    )


def _is_supervised_result_only_profile(
    authorized: ProjectStageAuthorizedEntry,
    result: StageResult,
) -> bool:
    from mif_pipeline.pipeline_core import StageStatus

    resume_context = result.resume_context
    if (
        authorized.request_codec_id != QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES.value
        or authorized.wire_stage_id != "supervised_phenotyping"
        or authorized.terminal_stage_id != "supervised_phenotype_training_review"
        or authorized.request_codec_version != "1"
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.generation_id is None
        or result.interaction_reason.value != "supervised_training_annotations_required"
        or result.allowed_actions != ("annotate_supervised_training", "abort")
        or not isinstance(resume_context, Mapping)
        or resume_context.get("schema_version") != "supervised-phenotyping-interaction/v1"
        or resume_context.get("phase") != "annotations_required"
        or frozenset(resume_context) != _SUPERVISED_ANNOTATIONS_RESUME_FIELDS
    ):
        return False
    radius = resume_context["point_selection_radius_level0"]
    return (
        _nonempty_transport_text_sequence(resume_context["class_ids"])
        and _valid_supervised_feature_schema(resume_context["feature_schema"])
        and _nonblank_transport_text(resume_context["current_validation_domain_id"])
        and _positive_finite_transport_number(radius)
    )


def _project_stage_result_requires_manifest(
    authorized: ProjectStageAuthorizedEntry,
    result: StageResult,
) -> bool:
    from mif_pipeline.pipeline_core import StageStatus

    # Detector selection saves a resource, not a biological object generation.
    # Its owner still validates the observation and authenticated saved state.
    if (
        authorized.request_codec_id == QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value
        and authorized.wire_stage_id == "tissue_setup"
        and authorized.terminal_stage_id == "tissue_setup"
        and authorized.request_codec_version == "1"
        and result.stage == "tissue_setup"
        and result.status is StageStatus.COMPLETED
        and result.generation_id is None
        and result.outputs is not None
        and set(result.outputs) == {"tissue_setup_observation"}
    ):
        return False
    return (
        result.status is StageStatus.COMPLETED or result.generation_id is not None
    ) and not _is_supervised_result_only_profile(authorized, result)


_PROJECT_STAGE_INTERNAL_TRANSPORT_CODE = {
    "generation_manifest_required": "generation_manifest_payload_invalid",
    "target_codec_authority_invalid": "result_target_authority_invalid",
}


def _canonical_project_stage_transport_code(code: str) -> str:
    """Map internal detection facts into the closed recovery vocabulary."""

    return _PROJECT_STAGE_INTERNAL_TRANSPORT_CODE.get(code, code)


def _project_stage_transport_validation_code(
    authorized: ProjectStageAuthorizedEntry,
    prepared_target: object,
    result_bytes: bytes | None,
    manifest_bytes: bytes | None,
) -> tuple[str | None, object | None]:
    """Return the first defect and immutable parsed producer facts."""

    from mif_pipeline.qupath_runtime.exchange import (
        ExchangeContractError,
        parse_stage_result_exchange_bytes,
    )

    if result_bytes is None and manifest_bytes is None:
        return "exchange_members_missing", None
    if result_bytes is None:
        return "exchange_members_incomplete", None
    try:
        parsed = parse_stage_result_exchange_bytes(
            result_bytes,
            manifest_bytes,
            invocation_id=authorized.attempt_id,
            run_id=authorized.pipeline_run_id,
            stage=authorized.wire_stage_id,
            slide_id=authorized.slide_id,
            generation_manifest_name=(authorized.exchange.generation_manifest_ref.physical_name),
            _manifest_required=lambda result: (
                _project_stage_result_requires_manifest(authorized, result)
            ),
        )
    except ExchangeContractError as exc:
        code = getattr(getattr(exc, "code", None), "value", None)
        if code not in {
            "result_payload_invalid",
            "generation_manifest_payload_invalid",
            "result_generation_binding_invalid",
            "result_target_authority_invalid",
        }:
            code = "result_payload_invalid"
        return code, None
    result = parsed.result
    supervised_result_only_profile = _is_supervised_result_only_profile(
        authorized,
        result,
    )
    manifest_required = _project_stage_result_requires_manifest(
        authorized,
        result,
    )
    if manifest_bytes is None and manifest_required and not supervised_result_only_profile:
        return _canonical_project_stage_transport_code("generation_manifest_required"), None

    codec = getattr(prepared_target, "_codec", None)
    binding = getattr(codec, "binding", None)
    target = getattr(prepared_target, "target", None)
    binding_fields = getattr(binding, "__dataclass_fields__", None)
    binding_values = (
        None
        if not isinstance(binding_fields, Mapping)
        else {name: getattr(binding, name) for name in binding_fields}
    )
    binding_key = (
        authorized.adapter_spec_sha256,
        authorized.request_codec_id,
        authorized.wire_stage_id,
        authorized.terminal_stage_id,
        authorized.request_codec_version,
    )
    if (
        binding_values is None
        or binding_values.get("codec_binding_sha256")
        != _digest(
            {
                name: value
                for name, value in binding_values.items()
                if name != "codec_binding_sha256"
            }
        )
        or getattr(codec, "_stage_owner_binding_key", None) != binding_key
        or (
            binding.adapter_spec_sha256,
            binding.target_script_asset_id,
            binding.wire_stage_id,
            binding.terminal_stage_id,
            binding.request_codec_version,
        )
        != binding_key
        or target is None
        or target.target_request_sha256 != authorized.target_request_sha256
    ):
        return _canonical_project_stage_transport_code("target_codec_authority_invalid"), None
    if manifest_bytes is not None:
        return None, parsed
    if supervised_result_only_profile:
        return None, parsed
    return None, parsed


def _project_stage_process_diagnostics(completion, *, catalogue_store) -> str:
    """Read the retained failure logs once and share the runtime formatter."""
    summary = completion.process_summary
    log_paths = tuple(
        catalogue_store._bucket_for_ref(ref).member_path(ref)
        for ref in (summary.stdout_log_ref, summary.stderr_log_ref)
    )
    excerpts, truncated, log_error = (None, None), (False, False), None
    try:
        stdout, stderr, stdout_truncated, stderr_truncated = (
            catalogue_store.recover_contained_process_logs(
                completion.process_evidence.attempt.containment_request,
            )
        )
    except (OSError, ValueError) as exc:
        log_error = str(exc)
    else:
        excerpts, truncated = (stdout, stderr), (stdout_truncated, stderr_truncated)
    return format_qupath_process_diagnostics(
        termination_reason=summary.termination_reason,
        exit_status=summary.exit_status,
        log_paths=log_paths,
        excerpts=excerpts,
        truncated=truncated,
        log_error=log_error,
    )


def _project_stage_incomplete_outcome(
    *,
    authorized: ProjectStageAuthorizedEntry,
    context: BatchNodeExecutionContext,
    plan: ProjectStageRunPlan,
    completion: object,
    observation: object,
    transport_validation_code: str,
    process_diagnostics: str,
    validation_error: str | None = None,
) -> "BatchRawNodeOutcome":
    from .batch_artifacts import BatchNodeExecutionResult
    from .batch_state import BatchRawNodeOutcome
    from mif_pipeline.pipeline_core import StageResult

    summary = completion.process_summary
    # Keep the rejected result's cause ahead of native logs within the saved limit.
    validation_detail = f"Result validation: {transport_validation_code}."
    if validation_error:
        validation_detail += " " + validation_error.replace("\x00", "")
    message = (
        "The expected QuPath project-stage result was absent or invalid.\n\n"
        + validation_detail + "\n\n" + process_diagnostics
    ).encode("utf-8")[:4096].decode("utf-8", errors="ignore")
    stage_result = StageResult.failed(
        stage=context.adapter_spec.node_kind,
        slide_id=authorized.slide_id,
        error_code="qupath_project_stage_incomplete",
        error_message=message,
        error_context={
            "run_plan_sha256": plan.plan_sha256,
            "authorized_entry_sha256": authorized.authorized_entry_sha256,
            "attempt_authorization_sha256": (authorized.attempt_authorization_sha256),
            "process_completion_sha256": completion.completion_sha256,
            "process_termination_code": summary.termination_reason,
            "transport_validation_code": transport_validation_code,
            "target_observation_sha256": observation.observation_sha256,
            "saved_state_disposition_code": observation.saved_state_disposition,
        },
    )
    return BatchRawNodeOutcome(
        context=context,
        stage_result=stage_result,
        execution_result=BatchNodeExecutionResult(
            authorized.attempt_authorization_sha256,
            (),
        ),
        execution_exchange_sha256=authorized.exchange.exchange_sha256,
    )


def _validate_project_stage_candidate_outputs(
    *,
    authorized: ProjectStageAuthorizedEntry,
    context: BatchNodeExecutionContext,
    validation_domain_id: str,
    outputs: object,
    completed: bool,
) -> None:
    """Require candidate artifacts to remain inside one target authority."""

    from .batch_artifacts import (
        BatchArtifactError,
        StageAdapterRegistry,
    )

    if type(outputs) is not tuple:
        raise BatchArtifactError("project-stage target outputs must be a tuple")
    if not completed:
        if outputs:
            raise BatchArtifactError("non-completed project-stage target outputs must be empty")
        return
    StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
        context.adapter_spec,
        outputs,
        authorization=context.authorization,
    )
    for output in outputs:
        scope = output.scope
        domain_bindings = scope.analysis_domain_bindings
        configuration_bindings = output.configuration_bindings
        introduces_analysis_domain = any(
            item not in authorized.analysis_domain_bindings for item in domain_bindings
        )
        is_declared_analysis_domain_output = (
            authorized.request_codec_id == QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value
            and authorized.wire_stage_id == "analysis_domain_selection"
            and output.artifact_kind == "accepted_analysis_domain"
            and output.artifact_kind in context.adapter_spec.declared_outputs
        )
        may_introduce_analysis_domain = (
            is_declared_analysis_domain_output
            and authorized.terminal_stage_id == "analysis_domain_selection"
            and context.adapter_spec.node_kind == "analysis_domain_selection"
            and scope.project_entry_id == authorized.project_entry_id
            and scope.sample_id == authorized.sample_id
            and scope.slide_id == authorized.slide_id
            and bool(domain_bindings)
            and all(
                item.project_entry_id == authorized.project_entry_id
                and item.sample_id == authorized.sample_id
                and item.slide_id == authorized.slide_id
                for item in domain_bindings
            )
            and len(configuration_bindings) == 1
            and configuration_bindings[0].sample_id == authorized.sample_id
            and configuration_bindings[0].resolved_config_sha256
            == authorized.resolved_config_sha256
        )
        introduced_analysis_domain_is_foreign = introduces_analysis_domain and (
            not may_introduce_analysis_domain
            or any(
                item.project_entry_id != authorized.project_entry_id
                or item.sample_id != authorized.sample_id
                or item.slide_id != authorized.slide_id
                for item in scope.analysis_domain_bindings
            )
        )
        if (
            scope.validation_domain_id != validation_domain_id
            or scope.sample_id != authorized.sample_id
            or scope.slide_id != authorized.slide_id
            or scope.project_entry_id not in {None, authorized.project_entry_id}
            or (is_declared_analysis_domain_output and not may_introduce_analysis_domain)
            or introduced_analysis_domain_is_foreign
            or len(configuration_bindings) != 1
            or configuration_bindings[0].sample_id != authorized.sample_id
            or configuration_bindings[0].resolved_config_sha256 != authorized.resolved_config_sha256
        ):
            raise BatchArtifactError("project-stage target output scope is foreign")


def _project_stage_context_facts(
    *, context: BatchNodeExecutionContext, authorized: ProjectStageAuthorizedEntry,
    observation: ProjectStageTargetObservation,
) -> tuple[object, ...]:
    """We capture current attempt, target, observation and image/input identities.

    These current typed records were constructed once at their owning boundary.
    Stored digests bind the accepted records; no JSON copies or reconstructed
    contexts are needed to expose their identity to the result validator.
    """
    if type(context) is not BatchNodeExecutionContext or type(authorized) is not ProjectStageAuthorizedEntry or type(observation) is not ProjectStageTargetObservation:
        raise ProjectStageRecordError("project-stage execution context must be typed")
    return (
        context.authorization.authorization_sha256, authorized.authorized_entry_sha256,
        observation.observation_sha256, context.node_id, context.adapter_spec.spec_sha256,
        context.adapter_spec.dispatch_mode.value, context.authorization.input_artifact_set_sha256,
        tuple(item.artifact_ref_sha256 for item in context.inputs),
        context.projection_target_sha256, context.current_image_identity_sha256,
        context.script_sha256, context.project_identity_sha256, context.project_authority_head_sha256,
        context.project_qpproj_snapshot_sha256, context.project_entry_projection_sha256,
        context.compatibility_signature_sha256,
    )


def validate_project_stage_results(
    publication: PublishedProjectStageRunPlan,
    prepared_request: PreparedProjectStageRunRequest,
    completion: ProjectStageProcessCompletion,
    runtime: ProjectStageRuntime,
    *,
    workspace_lease: BatchWorkspaceLease,
    project_lease: ProjectMutationLease,
) -> tuple["BatchRawNodeOutcome", ...]:
    """Validate each retained project-stage target without committing state.

    We use current typed records from the external parsing and preparation owners.
    The comparisons bind their attempt, input scope, saved state and exact exchange
    members before the caller can use the scientific result."""

    from .batch_artifacts import (
        BatchArtifactError,
        BatchNodeExecutionResult,
        artifact_ref_set_sha256,
    )
    from .project_stage_codecs import (
        PreparedProjectStageRunRequest,
        ProjectStageCodecValidationResult,
    )
    from .batch_state import BatchRawNodeOutcome
    from .batch_project import deterministic_project_entry_name
    from mif_pipeline.pipeline_core import ROIAnalysisError, StageResult
    from mif_pipeline.qupath_runtime.catalogue import (
        QuPathProjectEntryEvidence,
    )
    from mif_pipeline.qupath_runtime.project_stage import (
        ProjectStageProcessCompletion,
        ProjectStageTargetObservation,
    )

    if (
        type(publication) is not PublishedProjectStageRunPlan
        or type(prepared_request) is not PreparedProjectStageRunRequest
        or type(completion) is not ProjectStageProcessCompletion
        or type(runtime) is not ProjectStageRuntime
    ):
        raise ProjectStageRecordError("project-stage result validation inputs are invalid")
    lifecycle = runtime._begin_result_validation_lifecycle(
        publication,
        prepared_request,
        completion,
        project_lease=project_lease,
        workspace_lease=workspace_lease,
    )
    try:
        plan, inspection, catalogue, members, prepared_targets = (
            runtime._reopen_result_validation_inputs(
                publication,
                prepared_request,
                completion,
                project_lease=project_lease,
                workspace_lease=workspace_lease,
            )
        )
        snapshot = inspection.snapshot
        snapshot_sha256 = hashlib.sha256(canonical_json_bytes(snapshot.to_mapping())).hexdigest()
        contexts = tuple(item.context for item in prepared_targets)
        if not contexts or not all(type(item) is BatchNodeExecutionContext for item in contexts):
            raise ProjectStageRecordError("project-stage prepared scheduler contexts are invalid")
        by_node = {item.node_id: item for item in contexts}
        if len(by_node) != len(contexts) or set(by_node) != {item.node_id for item in plan.targets}:
            raise ProjectStageRecordError("project-stage targets require exact scheduler contexts")
        live_by_id = {item.project_entry_id: item for item in snapshot.entries}
        if len(live_by_id) != len(snapshot.entries):
            raise ProjectStageRecordError("project-stage project entries are ambiguous")
        uri_owners: dict[str, list[QuPathProjectEntryEvidence]] = {}
        for live in snapshot.entries:
            for source_uri in live.source_uris:
                uri_owners.setdefault(source_uri, []).append(live)
        current_head = runtime._state_store.load_head()
        current_saved_state_by_entry_id = (
            runtime._state_store.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=current_head.sha256,
                lease=workspace_lease,
            )
        )

        outcomes: list[BatchRawNodeOutcome] = []
        observations: list[ProjectStageTargetObservation] = []
        process_diagnostics = None
        for authorized, (request_bytes, result_bytes, manifest_bytes), prepared_target in zip(
            plan.targets, members, prepared_targets, strict=True
        ):
            if (
                len(request_bytes) != authorized.exchange.request_envelope_byte_count
                or hashlib.sha256(request_bytes).hexdigest()
                != authorized.exchange.request_envelope_sha256
            ):
                raise ProjectStageRecordError("original project-stage request bytes changed")
            try:
                retained_request = json.loads(request_bytes)["request"]
                if not isinstance(retained_request, Mapping):
                    raise ValueError("request body is not an object")
                operational_output_dir = _project_stage_operation_output_dir(
                    retained_request, prepared_target.resolved_config
                )
            except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                raise ProjectStageRecordError("original project-stage request is invalid") from exc
            context = by_node[authorized.node_id]
            authorization = context.authorization
            dispatch_binding = authorization.dispatch_binding
            reopened_authorization = runtime._state_store.load_authorization(
                authorization.authorization_sha256
            )
            input_refs = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
            if (
                reopened_authorization != authorization
                or authorization.node_id != authorized.node_id
                or authorization.attempt_id != authorized.attempt_id
                or authorization.authorization_sha256 != authorized.attempt_authorization_sha256
                or authorization.input_artifact_set_sha256 != authorized.input_artifact_set_sha256
                or artifact_ref_set_sha256(context.inputs) != authorized.input_artifact_set_sha256
                or input_refs != authorized.prerequisite_artifact_ref_sha256s
                or context.adapter_spec.node_kind != authorized.terminal_stage_id
                or authorized.terminal_stage_id != plan.stage_id
                or context.adapter_spec.node_version != plan.stage_version
                or dispatch_binding.node_id != context.node_id
                or dispatch_binding.adapter_spec_sha256 != context.adapter_spec.spec_sha256
                or dispatch_binding.dispatch_mode is not context.adapter_spec.dispatch_mode
                or dispatch_binding.projection_target_sha256 != context.projection_target_sha256
                or dispatch_binding.current_image_identity_sha256
                != context.current_image_identity_sha256
                or dispatch_binding.script_sha256 != context.script_sha256
                or dispatch_binding.project_identity_sha256 != context.project_identity_sha256
                or dispatch_binding.project_authority_head_sha256
                != context.project_authority_head_sha256
                or dispatch_binding.project_qpproj_snapshot_sha256
                != context.project_qpproj_snapshot_sha256
                or dispatch_binding.project_entry_projection_sha256
                != context.project_entry_projection_sha256
                or dispatch_binding.compatibility_signature_sha256
                != context.compatibility_signature_sha256
                or context.project_identity_sha256 != plan.project_identity_sha256
                or dispatch_binding.project_entry_ids != (authorized.project_entry_id,)
                or dispatch_binding.participant_ids != (authorized.sample_id,)
                or context.node_id != authorized.node_id
            ):
                raise ProjectStageRecordError("project-stage target and scheduler context differ")

            try:
                catalogue_entry = catalogue.resolve(authorized.project_entry_id)
            except KeyError as exc:
                raise ProjectStageRecordError(
                    "project-stage planned target is absent from the catalogue"
                ) from exc
            live = live_by_id.get(authorized.project_entry_id)
            expected_metadata = {
                "mif_pipeline.batch_id": plan.batch_id,
                "mif_pipeline.sample_id": authorized.sample_id,
                "mif_pipeline.slide_id": authorized.slide_id,
                "mif_pipeline.validation_domain_id": (catalogue_entry.validation_domain_id),
                "mif_pipeline.image_role": "primary_mif",
                "mif_pipeline.source_image_sha256": (catalogue_entry.image_sha256),
            }
            semantic_matches = tuple(
                item
                for item in snapshot.entries
                if all(
                    item.metadata.get(name) == value
                    for name, value in expected_metadata.items()
                    if name != "mif_pipeline.source_image_sha256"
                )
            )
            if (
                authorized.image_role != "primary_mif"
                or catalogue_entry.image_role != "primary_mif"
                or catalogue_entry.sample_id != authorized.sample_id
                or catalogue_entry.slide_id != authorized.slide_id
                or current_saved_state_by_entry_id.get(authorized.project_entry_id)
                != authorized.saved_state_before_sha256
                or context.project_entry_projection_sha256
                != catalogue_entry.entry_projection_sha256
                or live is None
                or live.entry_name
                != deterministic_project_entry_name(
                    authorized.sample_id,
                    authorized.slide_id,
                    "primary_mif",
                )
                or any(
                    live.metadata.get(name) != value for name, value in expected_metadata.items()
                )
                or len(semantic_matches) != 1
                or semantic_matches[0] is not live
                or len(uri_owners.get(catalogue_entry.canonical_uri, ())) != 1
                or uri_owners[catalogue_entry.canonical_uri][0] is not live
            ):
                raise ProjectStageRecordError("project-stage planned primary entry differs")

            saved_after = live.saved_state_attestation_sha256
            if saved_after is None:
                disposition = "unattestable"
            elif saved_after == authorized.saved_state_before_sha256:
                disposition = "authenticated_unchanged"
            else:
                disposition = "authenticated_changed"
            observation = ProjectStageTargetObservation.create(
                node_id=authorized.node_id,
                attempt_id=authorized.attempt_id,
                attempt_authorization_sha256=(authorized.attempt_authorization_sha256),
                authorized_entry_sha256=authorized.authorized_entry_sha256,
                project_entry_id=authorized.project_entry_id,
                sample_id=authorized.sample_id,
                slide_id=authorized.slide_id,
                process_evidence_sha256=(completion.process_evidence.evidence_set_sha256),
                project_snapshot_sha256=snapshot_sha256,
                result_ref=authorized.exchange.result_ref,
                result_payload_sha256=(
                    None if result_bytes is None else hashlib.sha256(result_bytes).hexdigest()
                ),
                result_byte_count=(None if result_bytes is None else len(result_bytes)),
                generation_manifest_ref=(authorized.exchange.generation_manifest_ref),
                generation_manifest_payload_sha256=(
                    None if manifest_bytes is None else hashlib.sha256(manifest_bytes).hexdigest()
                ),
                generation_manifest_byte_count=(
                    None if manifest_bytes is None else len(manifest_bytes)
                ),
                saved_state_before_sha256=authorized.saved_state_before_sha256,
                saved_state_disposition=disposition,
                saved_state_after_sha256=saved_after,
            )
            if disposition == "unattestable":
                raise _ProjectStageUnattestableTargetError(authorized, observation)
            observations.append(observation)
            (
                transport_code,
                parsed_exchange,
            ) = _project_stage_transport_validation_code(
                authorized,
                prepared_target,
                result_bytes,
                manifest_bytes,
            )
            candidate: BatchNodeExecutionResult | None = None
            validation_error: str | None = None
            terminal_stage_result: StageResult | None = None
            terminal_override: StageResult | None = None
            if transport_code is None:
                assert parsed_exchange is not None
                _require_native_completion_binding(
                    parsed_exchange, authorized, completion.process_evidence,
                )
                if parsed_exchange.saved_project_state.saved_state_sha256 != saved_after:
                    raise ProjectStageRecordError(
                        "current saved image differs from its native completion receipt"
                    )
                validation_inputs = _issue_project_stage_validation_input_authority(
                    prepared_target=prepared_target,
                    context=context,
                    authorized=authorized,
                    operational_output_dir=operational_output_dir,
                )
                evidence = _issue_project_stage_target_execution_evidence(
                    context=context,
                    authorized=authorized,
                    observation=observation,
                    parsed_exchange=parsed_exchange,
                    execution_exchange_sha256=(authorized.exchange.exchange_sha256),
                    validation_inputs=validation_inputs,
                )
                try:
                    candidate = prepared_target._codec.validate_target(evidence)
                except Exception as exc:
                    candidate = None
                    transport_code = "target_codec_rejected"
                    detail = f"{type(exc).__name__}: {exc}"
                    if exc.__cause__ is not None:
                        cause = exc.__cause__
                        detail += f"; {type(cause).__name__}: {cause}"
                    validation_error = detail.encode("utf-8")[:800].decode("utf-8", errors="ignore")
                if type(candidate) is ProjectStageCodecValidationResult:
                    terminal_override = candidate.terminal_stage_result
                    candidate = candidate.execution_result
                if candidate is not None and type(candidate) is not BatchNodeExecutionResult:
                    candidate = None
                    if transport_code is None:
                        transport_code = "target_codec_outcome_invalid"
                if (
                    candidate is not None
                    and candidate.authorization_sha256 != authorized.attempt_authorization_sha256
                ):
                    candidate = None
                    if transport_code is None:
                        transport_code = "target_codec_outcome_invalid"
                try:
                    if candidate is None:
                        raise ProjectStageRecordError(
                            "project-stage target codec returned an invalid type"
                        )
                    if terminal_override is not None:
                        if (
                            parsed_exchange.result.status.value != "completed"
                            or terminal_override.status.value != "needs_interaction"
                            or terminal_override.stage != authorized.terminal_stage_id
                            or terminal_override.slide_id != authorized.slide_id
                            or terminal_override.generation_id
                            != parsed_exchange.result.generation_id
                            or candidate.outputs
                        ):
                            raise ProjectStageRecordError(
                                "project-stage terminal override differs from authority"
                            )
                        terminal_stage_result = terminal_override
                    else:
                        terminal_stage_result = replace(
                            parsed_exchange.result,
                            stage=authorized.terminal_stage_id,
                            outputs=None,
                            # The supervised codec retains the complete ID set in saved prediction rows.
                            affected_object_ids=(() if authorized.terminal_stage_id == "supervised_phenotype_inference"
                                                 else parsed_exchange.result.affected_object_ids),
                        )
                    if (
                        terminal_stage_result.stage != context.adapter_spec.node_kind
                        or terminal_stage_result.slide_id != authorized.slide_id
                        or candidate.authorization_sha256 != authorized.attempt_authorization_sha256
                    ):
                        raise ProjectStageRecordError(
                            "project-stage target codec substituted authority"
                        )
                except (ROIAnalysisError, ProjectStageRecordError):
                    candidate = None
                    terminal_stage_result = None
                    if transport_code is None:
                        transport_code = "target_codec_outcome_invalid"
                if candidate is not None:
                    try:
                        _validate_project_stage_candidate_outputs(
                            authorized=authorized,
                            context=context,
                            validation_domain_id=(catalogue_entry.validation_domain_id),
                            outputs=candidate.outputs,
                            completed=terminal_stage_result.status is StageStatus.COMPLETED,
                        )
                    except (BatchArtifactError, AttributeError):
                        candidate = None
                        transport_code = "output_artifact_authority_invalid"
                try:
                    assert result_bytes is not None
                    assert saved_after is not None
                    runtime._reauthenticate_target_after_codec(
                        publication,
                        authorized,
                        prepared_target,
                        result_bytes,
                        manifest_bytes,
                        saved_after,
                        snapshot=snapshot,
                        project_lease=project_lease,
                        workspace_lease=workspace_lease,
                    )
                except Exception:
                    candidate = None
                    if transport_code is None:
                        transport_code = "target_codec_outcome_invalid"
            if candidate is not None and terminal_stage_result is not None:
                outcomes.append(
                    BatchRawNodeOutcome(
                        context=context,
                        stage_result=terminal_stage_result,
                        execution_result=candidate,
                        execution_exchange_sha256=(authorized.exchange.exchange_sha256),
                    )
                )
                continue
            assert transport_code is not None
            if process_diagnostics is None:
                process_diagnostics = _project_stage_process_diagnostics(
                    completion, catalogue_store=runtime._catalogue_store(workspace_lease),
                )
            outcomes.append(
                _project_stage_incomplete_outcome(
                    authorized=authorized,
                    context=context,
                    plan=plan,
                    completion=completion,
                    observation=observation,
                    transport_validation_code=transport_code,
                    process_diagnostics=process_diagnostics,
                    validation_error=validation_error,
                )
            )
        result = tuple(outcomes)
        runtime._record_validated_project_stage_results(
            publication,
            prepared_request,
            completion,
            result,
            project_lease=project_lease,
            workspace_lease=workspace_lease,
            plan=plan,
            snapshot=snapshot,
            catalogue=catalogue,
            observations=tuple(observations),
        )
        lifecycle.close_validation()
        return result
    except _ProjectStageUnattestableTargetError as unresolved:
        try:
            lifecycle.prepare_unresolved(unresolved)
        except Exception:
            lifecycle.close_validation()
            raise
        lifecycle.close()
        raise AssertionError("unreachable unresolved lifecycle return")
    except ProjectStageRecordError:
        lifecycle.close_validation()
        raise
    except Exception as exc:
        lifecycle.close_validation()
        raise ProjectStageRecordError(
            "project-stage result authority could not be authenticated"
        ) from exc


def prepare_project_stage_run_session(
    prepared_request: PreparedProjectStageRunRequest,
    runtime: ProjectStageRuntime,
    workspace_lease: BatchWorkspaceLease,
    terminal_committer: BatchTerminalCommitter,
) -> ProjectStageRunSession:
    if type(runtime) is not ProjectStageRuntime:
        raise ProjectStageLifecycleError("project-stage session requires the exact runtime facade")
    return runtime._prepare_run_session(prepared_request, workspace_lease, terminal_committer)


def _project_stage_session(value: object) -> ProjectStageRunSession:
    if type(value) is not ProjectStageRunSession:
        raise ProjectStageLifecycleError("project-stage run session is invalid")
    return value._runtime._authenticate_run_session(value)


def _execute_project_stage_run_session(
    session: ProjectStageRunSession, *, permit_preexecute_cancel: bool
) -> ProjectStageProcessCompletion:
    from mif_pipeline.qupath_runtime.project_stage import (
        ProjectStageProcessCompletion,
    )

    session = _project_stage_session(session)
    with session._condition:
        if session._state != "prepared":
            raise ProjectStageLifecycleError("project-stage session execute is one-shot")
        if session._cancellation_event.is_set() and not permit_preexecute_cancel:
            raise ProjectStageLifecycleError("cancelled project-stage session cannot execute")
        session._state = "executing"
        session._condition.notify_all()
    try:
        completion = session._runtime.run_process(
            session._published_descriptor,
            project_lease=session._project_lease,
            launch=session._launch,
            workspace_lease=session._workspace_lease,
            cancellation_event=session._cancellation_event,
        )
        if type(completion) is not ProjectStageProcessCompletion:
            raise ProjectStageLifecycleError("project-stage process returned no exact completion")
        with session._condition:
            if session._state != "executing" or session._completion is not None:
                raise ProjectStageLifecycleError(
                    "project-stage process completion installation conflicted"
                )
            session._completion = completion
            session._state = "executed"
            session._condition.notify_all()
    except BaseException as exc:
        with session._condition:
            if session._state == "executing":
                session._failure = exc
                session._failure_phase = "execution"
                session._condition.notify_all()
        session._runtime._abort_prelaunch_if_unattempted(
            session._project_lease, session._workspace_lease)
        raise
    return completion


def execute_project_stage_run_session(
    session: ProjectStageRunSession,
) -> ProjectStageProcessCompletion:
    return _execute_project_stage_run_session(session, permit_preexecute_cancel=False)


def request_project_stage_cancel(
    session: ProjectStageRunSession,
    reason: str,
) -> None:
    session = _project_stage_session(session)
    bounded_reason = _text(reason, "project-stage cancellation reason")
    with session._condition:
        if (
            session._state not in {"prepared", "executing"}
            or session._terminalization_started
            or session._failure_phase is not None
        ):
            raise ProjectStageLifecycleError("project-stage cancellation is no longer available")
        if session._cancellation_reason is None:
            session._cancellation_reason = bounded_reason
        session._cancellation_event.set()
        session._condition.notify_all()


def _project_stage_completion_is_normal(completion: object) -> bool:
    summary = getattr(completion, "process_summary", None)
    return (
        getattr(completion, "disposition", None) == "released_child"
        and summary is not None
        and summary.termination_reason == "normal_exit"
        and summary.exit_status == 0
    )


def _no_mutation_project_stage_outcomes(
    session: ProjectStageRunSession,
) -> tuple[object, ...]:
    from .batch_artifacts import BatchNodeExecutionResult
    from .batch_state import BatchRawNodeOutcome

    completion = session._completion
    proof = None if completion is None else completion.no_mutation_proof
    if proof is None or proof.variant not in {
        "never_entered_child_creation",
        "contained_never_released",
    }:
        raise ProjectStageLifecycleError("project-stage abort lacks an exact no-mutation proof")
    failure_code, failure_message = {
        "never_entered_child_creation": (
            "project_stage_cancelled_before_launch",
            "The project-stage run was cancelled before child creation.",
        ),
        "contained_never_released": (
            "project_stage_launch_not_released",
            "The contained project-stage child was drained before release.",
        ),
    }[proof.variant]
    return tuple(
        BatchRawNodeOutcome(
            context=target.context,
            stage_result=StageResult.failed(
                stage=target.context.adapter_spec.node_kind,
                slide_id=target.target.slide_id,
                error_code=failure_code,
                error_message=failure_message,
            ),
            execution_result=BatchNodeExecutionResult(
                target.context.authorization.authorization_sha256, ()
            ),
            execution_exchange_sha256=None,
        )
        for target in session._prepared_request.prepared_targets
    )


def _close_project_stage_acquisition(session: ProjectStageRunSession) -> None:
    if session._acquisition_closed:
        return
    session._acquisition_lifecycle.close_for_session_finish()
    with session._runtime._lock:
        active = session._runtime._active_project_leases.get(id(session._project_lease))
    if (
        (session._project_lease is not None and session._project_lease.is_live)
        or active is not None
        or not session._acquisition_lifecycle.is_ready_for_session_finish
    ):
        raise ProjectStageLifecycleError("project-stage acquisition remains live after close")
    session._acquisition_closed = True


def _commit_project_stage_outcomes(
    runtime: ProjectStageRuntime,
    publication: PublishedProjectStageRunPlan,
    prepared_request: object,
    completion: object,
    outcomes: tuple[object, ...],
    *,
    project_lease: object,
    workspace_lease: object,
    terminal_committer: object,
) -> tuple[tuple[ScheduledNodeResult, ...], object]:
    """Publish validated outputs through the shared material and terminal owners.

    Normal execution and recovery differ in how control is obtained. Both use
    this same scientific publication boundary after current saved state and the
    native result have been validated together.
    """
    from .batch_state import BatchProjectMutationDurabilityReceipt

    material = runtime._prepare_project_stage_material(
        publication, prepared_request, completion, outcomes,
        project_lease=project_lease, workspace_lease=workspace_lease,
    )
    scheduled, receipt = terminal_committer.commit_project_stage(outcomes, material)
    if (
        type(receipt) is not BatchProjectMutationDurabilityReceipt
        or receipt.durability_kind != "project_stage"
    ):
        raise ProjectStageLifecycleError(
            "project-stage terminal commit returned foreign durability"
        )
    return tuple(scheduled), receipt


def _commit_project_stage_session(
    session: ProjectStageRunSession,
    outcomes: tuple[object, ...],
) -> tuple[ScheduledNodeResult, ...]:
    scheduled, receipt = _commit_project_stage_outcomes(
        session._runtime, session._publication, session._prepared_request,
        session._completion, outcomes, project_lease=session._project_lease,
        workspace_lease=session._workspace_lease,
        terminal_committer=session._terminal_committer,
    )
    session._pending_terminal_results = tuple(scheduled)
    try:
        session._project_lease.complete(receipt)
    finally:
        if session._project_lease.is_finalized:
            session._project_completion_confirmed = True
    _close_project_stage_acquisition(session)
    session._runtime._finish_run_session(session, tuple(scheduled), state="terminalized")
    return tuple(scheduled)


def _record_project_stage_terminalization_failure(
    session: ProjectStageRunSession, failure: BaseException
) -> None:
    with session._condition:
        if session._state == "executed" and session._terminalization_started:
            session._failure = failure
            session._failure_phase = "terminalization"
            session._terminalization_started = False
            session._condition.notify_all()


def _terminal_committer_is_consumed(session: ProjectStageRunSession) -> bool:
    committer = session._terminal_committer
    condition = getattr(committer, "_condition", None)
    if condition is None:
        return False
    with condition:
        return getattr(committer, "_state", None) in {"sealed", "used"}


def _close_failed_project_stage_session(
    session: ProjectStageRunSession,
) -> tuple[ScheduledNodeResult, ...]:
    """Consume one claimed failure-cleanup retry from its exact next step."""

    steps = (
        (
            "seal_terminal_committer",
            session._terminal_committer.seal,
            lambda: _terminal_committer_is_consumed(session),
        ),
        (
            "close_outer_project_acquisition",
            lambda: _close_project_stage_acquisition(session),
            lambda: session._acquisition_closed,
        ),
    )
    while session._failure_cleanup_next < len(steps):
        name, operation, postcondition = steps[session._failure_cleanup_next]
        try:
            operation()
            if not postcondition():
                raise ProjectStageLifecycleError(
                    f"project-stage failure cleanup postcondition failed: {name}"
                )
        except BaseException as exc:
            if postcondition():
                session._failure_cleanup_next += 1
            failed_step = exc.failed_step if type(exc) is _ProjectStageOuterCleanupFailure else name
            with session._condition:
                session._state = f"cleanup_failed({failed_step})"
                session._failure_phase = "failure_cleanup"
                session._cleanup_retry_in_progress = False
                session._condition.notify_all()
            raise
        session._failure_cleanup_next += 1

    failure = session._failure
    results = session._pending_terminal_results
    try:
        if session._project_completion_confirmed and results is not None:
            session._runtime._finish_run_session(session, results, state="terminalized")
            return results
        session._runtime._finish_run_session(session, (), state="unresolved_closed")
    except BaseException as exc:
        with session._condition:
            terminal_handoff_completed = session._state in {
                "terminalized",
                "unresolved_closed",
            }
        if terminal_handoff_completed:
            raise
        failed_step = (
            exc.failed_step
            if type(exc) is _ProjectStageOuterCleanupFailure
            else "release_outer_runtime_borrow"
        )
        with session._condition:
            session._state = f"cleanup_failed({failed_step})"
            session._failure_phase = "failure_cleanup"
            session._cleanup_retry_in_progress = False
            session._condition.notify_all()
        raise
    rejected = ProjectStageLifecycleError(
        "project-stage failed session authority was safely released"
    )
    if failure is None:
        raise rejected
    raise rejected from failure


def _remember_project_stage_cleanup_failure(
    session: ProjectStageRunSession,
    failure: ProjectStageCleanupError,
) -> None:
    cleanup = getattr(failure, "_unresolved_cleanup", None)
    if (
        type(cleanup) is not _ProjectStageUnresolvedLifecycle
        or cleanup._runtime is not session._runtime
        or cleanup._prepared_request is not session._prepared_request
        or cleanup._completion is not session._completion
        or cleanup._project_lease is not session._project_lease
        or cleanup._workspace_lease is not session._workspace_lease
        or cleanup._terminal_committer is not session._terminal_committer
    ):
        raise ProjectStageRecordError(
            "project-stage unresolved cleanup authority is stale or foreign"
        ) from failure
    session._unresolved_cleanup = cleanup
    session._manual_review = failure.review
    with session._condition:
        session._state = f"cleanup_failed({failure.failed_step})"
        session._cleanup_retry_in_progress = False
        session._condition.notify_all()


def _finish_project_stage_manual_review(
    session: ProjectStageRunSession,
    required: ProjectStageManualReviewRequired,
) -> None:
    unsafe_review = replace(required.review, in_process_authority_released=False)
    session._manual_review = unsafe_review
    session._unresolved_cleanup = None
    try:
        _close_project_stage_acquisition(session)
    except BaseException as exc:
        failed_step = (
            exc.failed_step
            if type(exc) is _ProjectStageOuterCleanupFailure
            else "close_outer_project_acquisition"
        )
        cleanup_cause = (
            exc.__cause__
            if type(exc) is _ProjectStageOuterCleanupFailure and exc.__cause__ is not None
            else exc
        )
        with session._condition:
            session._state = f"cleanup_failed({failed_step})"
            session._cleanup_retry_in_progress = False
            session._condition.notify_all()
        raise ProjectStageCleanupError(
            unsafe_review,
            failed_step=failed_step,
            safe_cleanup=False,
        ) from cleanup_cause
    safe_review = replace(unsafe_review, in_process_authority_released=True)
    try:
        session._runtime._finish_run_session(session, (), state="unresolved_closed")
    except BaseException as exc:
        with session._condition:
            terminal_handoff_completed = session._state == "unresolved_closed"
            if terminal_handoff_completed:
                session._manual_review = safe_review
        if terminal_handoff_completed:
            raise
        failed_step = (
            exc.failed_step
            if type(exc) is _ProjectStageOuterCleanupFailure
            else "release_outer_runtime_borrow"
        )
        cleanup_cause = (
            exc.__cause__
            if type(exc) is _ProjectStageOuterCleanupFailure and exc.__cause__ is not None
            else exc
        )
        session._manual_review = unsafe_review
        with session._condition:
            session._state = f"cleanup_failed({failed_step})"
            session._cleanup_retry_in_progress = False
            session._condition.notify_all()
        raise ProjectStageCleanupError(
            unsafe_review,
            failed_step=failed_step,
            safe_cleanup=False,
        ) from cleanup_cause
    session._manual_review = safe_review
    raise ProjectStageManualReviewRequired(safe_review)


def _validate_project_stage_session_results(
    session: ProjectStageRunSession,
) -> tuple[object, ...]:
    assert session._completion is not None
    try:
        return validate_project_stage_results(
            session._publication,
            session._prepared_request,
            session._completion,
            session._runtime,
            workspace_lease=session._workspace_lease,
            project_lease=session._project_lease,
        )
    except ProjectStageCleanupError as failure:
        _remember_project_stage_cleanup_failure(session, failure)
        raise
    except ProjectStageManualReviewRequired as required:
        _finish_project_stage_manual_review(session, required)
        raise AssertionError("unreachable manual-review completion")


def finalize_project_stage_run_session(
    session: ProjectStageRunSession,
) -> tuple[ScheduledNodeResult, ...]:
    session = _project_stage_session(session)
    with session._condition:
        if (
            session._state != "executed"
            or session._completion is None
            or session._terminalization_started
            or session._failure_phase is not None
        ):
            raise ProjectStageLifecycleError(
                "project-stage finalize requires stored execution completion"
            )
        if not _project_stage_completion_is_normal(session._completion):
            raise ProjectStageLifecycleError(
                "project-stage finalize requires normal released-child completion"
            )
        session._terminalization_started = True
        session._condition.notify_all()
    try:
        outcomes = _validate_project_stage_session_results(session)
        return _commit_project_stage_session(session, outcomes)
    except ProjectStageCleanupError as cleanup:
        cleanup._retry_session = session
        raise
    except ProjectStageManualReviewRequired:
        raise
    except BaseException as exc:
        _record_project_stage_terminalization_failure(session, exc)
        raise


def abort_project_stage_run_session(
    session: ProjectStageRunSession,
    reason: str,
) -> tuple[ScheduledNodeResult, ...]:
    session = _project_stage_session(session)
    bounded_reason = _text(reason, "project-stage abort reason")
    with session._condition:
        state = session._state
        cleanup_failed = state.startswith("cleanup_failed(")
        failed_session = session._failure_phase in {
            "preparation",
            "execution",
            "terminalization",
            "failure_cleanup",
        }
        outer_cleanup_failed = state in {
            "cleanup_failed(close_outer_project_acquisition)",
            "cleanup_failed(release_outer_workspace_pin)",
            "cleanup_failed(release_outer_runtime_borrow)",
            "cleanup_failed(release_outer_facade_registration)",
        }
        if cleanup_failed and not failed_session:
            cleanup = session._unresolved_cleanup
            if not outer_cleanup_failed and type(cleanup) is not _ProjectStageUnresolvedLifecycle:
                raise ProjectStageLifecycleError(
                    "project-stage cleanup retry authority is unavailable"
                )
        elif failed_session:
            cleanup = None
        else:
            cleanup = None
        if cleanup_failed or failed_session:
            if session._cleanup_retry_in_progress:
                raise ProjectStageLifecycleError(
                    "project-stage cleanup retry is already in progress"
                )
            session._cleanup_retry_in_progress = True
        if state == "prepared":
            if session._cancellation_reason is None:
                session._cancellation_reason = bounded_reason
            session._cancellation_event.set()
        elif state == "executing" and not failed_session:
            raise ProjectStageLifecycleError("project-stage abort cannot race active execution")
        elif failed_session:
            pass
        elif (state != "executed" or session._terminalization_started) and not cleanup_failed:
            raise ProjectStageLifecycleError(
                "project-stage abort is unavailable in this lifecycle state"
            )
    if failed_session:
        return _close_failed_project_stage_session(session)
    if outer_cleanup_failed:
        review = session._manual_review
        if type(review) is not ProjectStageManualReview:
            with session._condition:
                session._cleanup_retry_in_progress = False
            raise ProjectStageLifecycleError(
                "project-stage outer cleanup review authority is unavailable"
            )
        _finish_project_stage_manual_review(session, ProjectStageManualReviewRequired(review))
        raise AssertionError("unreachable outer cleanup retry")
    if cleanup is not None:
        try:
            cleanup.close()
        except ProjectStageCleanupError as failure:
            if getattr(failure, "_unresolved_cleanup", None) is not cleanup:
                with session._condition:
                    session._cleanup_retry_in_progress = False
                raise ProjectStageRecordError(
                    "project-stage cleanup retry substituted authority"
                ) from failure
            _remember_project_stage_cleanup_failure(session, failure)
            raise
        except ProjectStageManualReviewRequired as required:
            _finish_project_stage_manual_review(session, required)
            raise AssertionError("unreachable manual-review retry")
        except BaseException:
            with session._condition:
                session._cleanup_retry_in_progress = False
            raise
        with session._condition:
            session._cleanup_retry_in_progress = False
        raise ProjectStageLifecycleError(
            "project-stage cleanup retry returned without a typed result"
        )
    if state == "prepared":
        _execute_project_stage_run_session(session, permit_preexecute_cancel=True)
    completion = session._completion
    assert completion is not None
    if _project_stage_completion_is_normal(completion):
        raise ProjectStageLifecycleError("normal project-stage completion must be finalized")
    with session._condition:
        if session._state != "executed" or session._terminalization_started:
            raise ProjectStageLifecycleError("project-stage abort lost its stored completion")
        session._terminalization_started = True
        session._condition.notify_all()
    try:
        if completion.disposition == "released_child":
            outcomes = _validate_project_stage_session_results(session)
        else:
            outcomes = _no_mutation_project_stage_outcomes(session)
        return _commit_project_stage_session(session, outcomes)
    except ProjectStageCleanupError as cleanup:
        cleanup._retry_session = session
        raise
    except ProjectStageManualReviewRequired:
        raise
    except BaseException as exc:
        _record_project_stage_terminalization_failure(session, exc)
        raise


def run_project_stage_headless(
    prepared_request: PreparedProjectStageRunRequest,
    runtime: ProjectStageRuntime,
    workspace_lease: BatchWorkspaceLease,
    terminal_committer: BatchTerminalCommitter,
) -> tuple[ScheduledNodeResult, ...]:
    recovered = runtime._recover_completed_run(
        prepared_request, workspace_lease, terminal_committer
    )
    if recovered is not None:
        return recovered
    session = prepare_project_stage_run_session(
        prepared_request, runtime, workspace_lease, terminal_committer
    )
    try:
        completion = execute_project_stage_run_session(session)
        if _project_stage_completion_is_normal(completion):
            return finalize_project_stage_run_session(session)
        return abort_project_stage_run_session(
            session, "project-stage process did not complete normally"
        )
    except ProjectStageCleanupError as cleanup:
        cleanup._retry_session = session
        raise
    except ProjectStageManualReviewRequired:
        raise
    except BaseException as primary:
        with session._condition:
            recoverable = session._failure_phase in {
                "execution",
                "terminalization",
                "failure_cleanup",
            }
        if not recoverable:
            raise
        try:
            recovered = abort_project_stage_run_session(session, "headless failure cleanup")
        except ProjectStageLifecycleError as cleanup:
            if session.state == "unresolved_closed":
                raise primary
            cleanup._retry_session = session
            raise cleanup from primary
        return recovered


__all__ = [
    "PreparedProjectStageRunDescriptor",
    "ProjectStageAuthorizedEntry",
    "ProjectStageCleanupError",
    "ProjectStageCommittedExchangeReopener",
    "ProjectStageExchangeAttestation",
    "ProjectStageLaunchMode",
    "ProjectStageLifecycleError",
    "ProjectStageManualReview",
    "ProjectStageManualReviewRequired",
    "ProjectStageRecordError",
    "ProjectStageRuntime",
    "ProjectStageRunSession",
    "ProjectStageRunPlan",
    "ProjectStageRunRequest",
    "ProjectStageTargetRequest",
    "ProjectStageTargetExecutionEvidence",
    "PublishedPreparedProjectStageRunDescriptor",
    "PublishedProjectStageRunPlan",
    "QuPathProjectLaunchDescriptor",
    "abort_project_stage_run_session",
    "execute_project_stage_run_session",
    "finalize_project_stage_run_session",
    "prepare_project_stage_run_session",
    "request_project_stage_cancel",
    "run_project_stage_headless",
    "validate_project_stage_results",
]
