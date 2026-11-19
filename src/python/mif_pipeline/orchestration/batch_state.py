"""Persist the current analysis state and the exact history of admitted stage attempts.

The state store keeps compact records for plans, input references, accepted responses,
process outcomes and published results. Updates hold the workspace writer lock and compare
the expected predecessor before replacing the current head. Stage payloads stay in their
owning image, model or result stores.

Current project checks compare scientific image inputs while preserving exact historical
attempt bindings. Graceful pause records a resumable boundary only after active operations
have reached their required completion and cleanup state. Interrupted or incompatible writes
remain explicit failures requiring review."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
from .supervised_cutoff_selection import SupervisedCutoffSelection, merge_selection

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
import errno
from enum import Enum
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from threading import Condition, Lock, RLock, get_ident
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Literal, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from .batch_project import BatchProjectCatalogue, BatchProjectCatalogueStore
    from .batch_phenotyping.review_authority import (
        AuthorizedClusterLabelReviewResponse,
    )
    from .batch_phenotyping.review_target import AggregateClusterReviewTarget
    from .batch_phenotyping.review_records import BatchClusterReviewEvidence

from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactProjectionTarget,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchAttemptAuthorization,
    BatchFailedRetry,
    BatchDispatchBinding,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    BatchPreparedDispatchAuthority,
    DispatchMode,
    StageArtifactRequirement,
    StageAdapterSpec,
    StageAdapterRegistry,
    artifact_ref_set_sha256,
    _issue_batch_current_execution_authority,
    _issue_batch_committed_artifact_authority,
    validate_artifact_ref,
)
from mif_pipeline.orchestration.batch_interaction_authority import (
    AuthorizedTrainingInteractionResponse,
    BatchInteractionAuthorityError,
    _issue_authorized_training_interaction_response,
)
from mif_pipeline.orchestration.batch_dag import (
    BatchArtifactEdge,
    BatchNode,
    BatchPlan,
    BatchPlanError,
    ClusteringGraphScope,
    DeferredNodeDescriptor,
    NodeScope,
    PhysicalSectionTarget,
    ResourceKind,
    dependent_node_ids,
)
from mif_pipeline.orchestration.batch_manifest import (
    BatchManifest,
    BatchManifestError,
    ValidatedBatchManifest,
    revalidate_retained_manifest,
    validate_manifest,
)
from mif_pipeline.orchestration.setup.batch_prerequisites import BatchPrerequisiteItem
from mif_pipeline.orchestration.batch_training_settings import TrainingSettingsRevision
from mif_pipeline.orchestration.batch_segmentation_settings import SegmentationSettingsRevision
from mif_pipeline.orchestration.batch_measurement_settings import MeasurementSettingsRevision
from mif_pipeline.orchestration.batch_settled_review_revision import SettledReviewRevision, SETTLED_REVIEW_KINDS, review_key
from .batch_saved_result_retirement import SavedResultRetirement
from .batch_cell_corrections import CellCorrectionRequest
from mif_pipeline.pipeline_core import (
    ClusteringMode,
    InteractionReason,
    PhenotypingMode,
    ROIAnalysisError,
    StageResult,
    StageStatus,
    assert_current_writer,
    validate_safe_path_segment,
)
from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityMemberRefV2,
    authority_member_ref_v2,
)
from mif_pipeline.pipeline_core.authority_provisioning import RetainedAuthorityLayoutV2
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    read_canonical_record,
)
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeCompositionBinding,
    RuntimeLifecycleBorrower,
    RuntimeLifecycleError,
    RuntimeUseToken,
)
from mif_pipeline.pipeline_core.transfer_review_authority import (
    TransferReviewAuthorityError,
    TransferReviewResponseResolver,
    _issue_transfer_review_response_resolver,
)
from mif_pipeline.pipeline_core.win32_file_authority import Win32FileAuthority

STATE_SCHEMA_NAME = "mif-batch-state-head"
STATE_SCHEMA_VERSION = 1
AUTHORIZATION_SCHEMA_NAME = "mif-batch-attempt-authorization"
AUTHORIZATION_SCHEMA_VERSION = 2
ATTEMPT_SCHEMA_NAME = "mif-batch-node-attempt"
ATTEMPT_SCHEMA_VERSION = 1
INTERACTION_RESPONSE_SCHEMA_NAME = "mif-batch-interaction-response"
INTERACTION_RESPONSE_SCHEMA_VERSION = 1
_PLAN_RUNTIME_CONTRACT_SCHEMA_NAME = "mif-batch-plan-runtime-contracts"
_PLAN_RUNTIME_CONTRACT_SCHEMA_VERSION = 1
_AUTHORIZATION_REQUEST_SCHEMA_NAME = "mif-batch-authorization-request-material"
_AUTHORIZATION_REQUEST_SCHEMA_VERSION = 1


# ==============================================================================
#                                  Batch state
# ==============================================================================


class BatchExecutionState(str, Enum):
    """Whether a batch head is permitted to admit new work."""

    ACTIVE = "active"
    PAUSED = "paused"


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_ID_RE = re.compile(r"attempt-[0-9a-f]{32}\Z")
_MAX_BATCH_REQUEST_ID_BYTES = 128
_PROJECT_STABLE_AUTHORITY_MAX_BYTES = 4 * 1024 * 1024
_PREPARATION_SCOPE_MAX_BYTES = 4 * 1024 * 1024
_SUPERVISED_EVIDENCE_MAX_BYTES = 16 * 1024 * 1024
_SUPERVISED_EVIDENCE_MAX_DEPTH = 32
_SUPERVISED_EVIDENCE_MAX_ITEMS = 100_000
_PLAN_PARSE_CACHE_MAX_BYTES = 64 * 1024 * 1024
_PLAN_PARSE_CACHE_MAX_ENTRIES = 128
_BATCH_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z")
_WINDOWS_RESERVED_DEVICE_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_OWNED_DIRECTORY_NAMES = (
    "manifest",
    "plans",
    "state",
    "attempts",
    "interactions",
    "catalogue",
    "summaries",
    "resume",
    "samples",
)
_PROHIBITED_PAYLOAD_KEYS = frozenset(
    {
        "pixels",
        "pixel_array",
        "ome",
        "ome_object",
        "ome_xml",
        "rois",
        "roi_geometry",
        "cells",
        "cell_table",
        "detections",
        "detection_array",
        "qupath_hierarchy",
        "hierarchy",
        "pathobjects",
        "path_objects",
        "geometry",
        "geometry_body",
        "stage_tags",
        "ome_stage_tags",
        "processed_stages",
        "processed_through_stage",
    }
)


class BatchStateError(ROIAnalysisError):
    """Raised when durable batch control state is unsafe or inconsistent."""


class BatchHeadConflictError(BatchStateError):
    """Raised when a state-head compare-and-swap observes another head."""


class BatchWorkspaceLeaseReleaseError(BatchStateError):
    """Raised when an exact workspace lease cannot yet be safely released."""


def _add_bounded_cleanup_note(
    primary: BaseException,
    cleanup: BaseException,
    *,
    context: str,
) -> None:
    """Best-effort cleanup diagnostics that can never replace the primary."""

    try:
        primary.add_note(f"{context}: {type(cleanup).__name__}: {str(cleanup)[:240]}"[:512])
    except BaseException:
        pass


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchStateError(f"{field} must be non-empty canonical text")
    return value


def _safe_segment(value: object, field: str) -> str:
    try:
        return validate_safe_path_segment(value, field, error_type=BatchStateError)
    except ROIAnalysisError as exc:
        raise BatchStateError(str(exc)) from exc


def _batch_request_id(value: object) -> str:
    if (
        type(value) is not str
        or _BATCH_REQUEST_ID_RE.fullmatch(value) is None
        or len(value.encode("utf-8")) > _MAX_BATCH_REQUEST_ID_BYTES
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_DEVICE_BASENAMES
    ):
        raise BatchStateError("pending interaction ID must be a portable bounded ASCII identifier")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise BatchStateError(f"{field} must be a lowercase SHA-256")
    return digest


def _optional_sha256(value: object, field: str) -> str | None:
    return None if value is None else _sha256(value, field)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchStateError(f"{field} must be a string-keyed object")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchStateError(f"{field} must be an array")
    return value


def _closed_mapping(value: object, *, fields: frozenset[str], field: str) -> Mapping[str, object]:
    mapping = _mapping(value, field)
    unknown = sorted(set(mapping) - fields)
    missing = sorted(fields - set(mapping))
    if unknown:
        raise BatchStateError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchStateError(f"{field} lacks required field(s): {', '.join(missing)}")
    return mapping


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, StageStatus):
        return value.value
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _export_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise BatchStateError("state material must be finite canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _classifier_record_native_fit_projection(
    *,
    training_plan_id: object,
    classifier_id: object,
    training_material_evidence_sha256: object,
    fit_plan_sha256: object,
    fit_execution_sha256: object,
    fit_material_sha256: object,
    classifier_version: object,
    project_relative_path: object,
    native_classifier_sha256: object,
    native_classifier_byte_count: object,
    project_identity_sha256: object,
) -> dict[str, object]:
    """Canonical cross-owner commitment to one exact native fit material."""

    if type(native_classifier_byte_count) is not int or native_classifier_byte_count <= 0:
        raise BatchStateError("native_classifier_byte_count must be positive")
    return {
        "training_plan_id": _sha256(training_plan_id, "training_plan_id"),
        "classifier_id": _text(classifier_id, "classifier_id"),
        "training_material_evidence_sha256": _sha256(
            training_material_evidence_sha256,
            "training_material_evidence_sha256",
        ),
        "fit_plan_sha256": _sha256(fit_plan_sha256, "fit_plan_sha256"),
        "fit_execution_sha256": _sha256(fit_execution_sha256, "fit_execution_sha256"),
        "fit_material_sha256": _sha256(fit_material_sha256, "fit_material_sha256"),
        "classifier_version": _text(classifier_version, "classifier_version"),
        "project_relative_path": _text(project_relative_path, "project_relative_path"),
        "native_classifier_sha256": _sha256(native_classifier_sha256, "native_classifier_sha256"),
        "native_classifier_byte_count": native_classifier_byte_count,
        "project_identity_sha256": _sha256(project_identity_sha256, "project_identity_sha256"),
    }


def _classifier_record_native_fit_inventory_sha256(
    projections: object,
) -> str:
    if type(projections) not in {tuple, list}:
        raise BatchStateError("native fit projections must be an exact sequence")
    items = tuple(projections)
    if not items or any(type(item) is not dict for item in items):
        raise BatchStateError("native fit projections are invalid")
    canonical = tuple(
        sorted(
            items,
            key=lambda item: (
                item["training_plan_id"],
                item["classifier_id"],
            ),
        )
    )
    if len({(item["training_plan_id"], item["classifier_id"]) for item in canonical}) != len(
        canonical
    ):
        raise BatchStateError("native fit projections are not bijective")
    return _digest(canonical)


def batch_interaction_request_id(
    *,
    node_id: str,
    attempt_id: str,
    attempt_authorization_sha256: str,
    interaction_context_sha256: str,
) -> str:
    """Derive the portable identity of one committed interaction request."""

    node_id = _safe_segment(node_id, "interaction request node_id")
    attempt_id = _safe_segment(attempt_id, "interaction request attempt_id")
    authorization = _sha256(
        attempt_authorization_sha256,
        "interaction request attempt_authorization_sha256",
    )
    context = _sha256(
        interaction_context_sha256,
        "interaction request interaction_context_sha256",
    )
    return "batch-interaction-" + _digest(
        {
            "node_id": node_id,
            "attempt_id": attempt_id,
            "attempt_authorization_sha256": authorization,
            "interaction_context_sha256": context,
        }
    )


def _batch_workspace_rebinding_sha256(
    original_output_root: Path,
    destination_root: Path,
    original_manifest_sha256: str,
) -> str:
    """Canonical operational identity for reopening below another batch root."""
    if not isinstance(original_output_root, Path) or not isinstance(destination_root, Path):
        raise BatchStateError("workspace rebinding roots must be Path values")
    original = original_output_root.expanduser().resolve(strict=False)
    destination = destination_root.expanduser().resolve(strict=False)
    return _digest(
        {
            "original_output_root": str(original),
            "destination_root": str(destination),
            "original_manifest_sha256": _sha256(
                original_manifest_sha256, "original_manifest_sha256"
            ),
        }
    )


def batch_project_workspace_storage_scope_sha256(
    *, batch_id: str, original_output_root: Path
) -> str:
    """Derive stable batch-project storage scope from locator spelling only."""

    batch_id = _safe_segment(batch_id, "batch_id")
    if not isinstance(original_output_root, Path) or not original_output_root.is_absolute():
        raise BatchStateError("original_output_root must be an absolute Path locator")
    return _digest(
        {
            "batch_id": batch_id,
            "original_output_root_locator": original_output_root.as_posix(),
        }
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BatchStateError(f"cannot hash required batch authority: {path}") from exc
    return digest.hexdigest()


def _reject_prohibited_payload(value: object, field: str = "state") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _PROHIBITED_PAYLOAD_KEYS:
                raise BatchStateError(
                    f"{field} cannot duplicate pixels, OME, ROI, cell, or stage-tag payloads"
                )
            _reject_prohibited_payload(item, f"{field}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_prohibited_payload(item, f"{field}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower().replace("-", "_").replace(" ", "_")
        if "processed_through_stage" in lowered or lowered.startswith("ome_processed_through_"):
            raise BatchStateError("free-form OME stage tags are forbidden in batch state")
        return
    if value is None or type(value) in {bool, int, float}:
        return
    raise BatchStateError(f"{field} contains a non-JSON control-plane object")


def _validate_compact_stage_result(result: StageResult) -> None:
    """Bound durable control JSON; scientific response parsers belong to their owners.

    StageResult has already validated its outcome and recursively frozen its
    mappings. The state store checks persistence limits and envelope identity,
    without interpreting measurement, marker, model or registration settings.
    """
    if type(result) is not StageResult:
        raise BatchStateError("terminal stage result must be an exact StageResult")
    if result.outputs is not None:
        raise BatchStateError("terminal StageResult outputs belong in typed artifact references")
    for name, limit in (("stage", 1024), ("slide_id", 1024),
                        ("generation_id", 1024), ("checkpoint_id", 1024),
                        ("message", 4096), ("error_message", 4096), ("error_code", 1024)):
        value = getattr(result, name)
        if value is not None and (len(value.encode("utf-8")) > limit or "\x00" in value):
            raise BatchStateError(f"terminal StageResult {name} exceeds its text boundary")
    if len(result.affected_object_ids) > 128 or any(
        len(item.encode("utf-8")) > 512 for item in result.affected_object_ids
    ):
        raise BatchStateError("terminal StageResult object identity set is unbounded")
    if result.resume_context is not None:
        version = result.resume_context.get("schema_version")
        if version is not None and (
            type(version) is not str
            or not version.strip()
            or len(version.encode("utf-8")) > 1024
            or "\x00" in version
        ):
            raise BatchStateError("terminal interaction context version is invalid or unbounded")
        service_context = result.resume_context.get("service_context")
        if service_context is not None and (
            not isinstance(service_context, Mapping)
            or service_context.get("schema_version") != version
        ):
            raise BatchStateError("interaction service context has a different producer version")
    if result.error_context is not None and len(result.error_context) > 16:
        raise BatchStateError("terminal StageResult error context is unbounded")

    # These limits cover compact review curves and previews, rather than whole
    # cell populations or pixels. Each scalar and container contributes once.
    pending = [(result.resume_context, 0), (result.error_context, 0)]
    item_count = 0
    text_bytes = 0
    forbidden = _PROHIBITED_PAYLOAD_KEYS | {"objects", "roi_payload", "cell_payload", "detection_payload"}
    while pending:
        value, depth = pending.pop()
        item_count += 1
        if item_count > 409600 or depth > 24:
            raise BatchStateError("terminal StageResult control projection is unbounded")
        value_type = type(value)
        if value_type is str:
            encoded_size = len(value.encode("utf-8"))
            if encoded_size > 4096 or "\x00" in value:
                raise BatchStateError("terminal StageResult control text is unbounded")
            text_bytes += len(json.encoder.encode_basestring(value).encode("utf-8"))
        elif value_type is float:
            if not math.isfinite(value):
                raise BatchStateError("terminal StageResult control number must be finite")
        elif value_type is int:
            if not -(2**63) <= value < 2**63:
                raise BatchStateError("terminal StageResult control integer exceeds signed 64 bits")
        elif value is None or value_type is bool:
            pass
        elif isinstance(value, Mapping):
            if len(value) > 4096:
                raise BatchStateError("terminal StageResult control mapping is unbounded")
            for key, child in value.items():
                if type(key) is not str or len(key.encode("utf-8")) > 1024 or "\x00" in key:
                    raise BatchStateError("terminal StageResult control key is invalid")
                normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2",
                                    re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key))
                normalized = normalized.lower().replace("-", "_").replace(" ", "_")
                if normalized in forbidden:
                    raise BatchStateError("terminal StageResult cannot retain scientific payload aliases")
                text_bytes += len(json.encoder.encode_basestring(key).encode("utf-8"))
                pending.append((child, depth + 1))
        elif isinstance(value, tuple):
            if len(value) > 16384:
                raise BatchStateError("terminal StageResult control sequence is unbounded")
            pending.extend((child, depth + 1) for child in value)
        else:
            raise BatchStateError("terminal StageResult control value is not JSON")
        if text_bytes + item_count * 24 > 8 * 1024**2:
            raise BatchStateError("terminal StageResult control projection exceeds 8 MiB")


def _node_project_entry_id_by_sample(
    node: Mapping[str, object],
) -> Mapping[str, str]:
    participants = tuple(str(item) for item in node["participant_ids"])
    entries = tuple(str(item) for item in node["project_entry_ids"])
    result: dict[str, str] = {}
    for raw_binding in node["analysis_domain_bindings"]:  # type: ignore[union-attr]
        binding = _mapping(raw_binding, "analysis domain binding")
        sample_id = _text(binding.get("sample_id"), "domain binding sample_id")
        entry_id = _text(binding.get("project_entry_id"), "domain binding project_entry_id")
        prior = result.setdefault(sample_id, entry_id)
        if prior != entry_id:
            raise BatchStateError("one participant cannot bind multiple project entries")
    if len(participants) == 1 and len(entries) == 1 and not result:
        result[participants[0]] = entries[0]
    if entries and (set(result) != set(participants) or set(result.values()) != set(entries)):
        raise BatchStateError("plan node project entries lack exact participant associations")
    if not entries and result:
        raise BatchStateError("plan node domain bindings contradict project-entry identity")
    return MappingProxyType(dict(sorted(result.items())))


def _node_uses_project(node: Mapping[str, object]) -> bool:
    return bool(_node_project_entry_id_by_sample(node)) or node["scope"] == "shared_project"


def _validate_clustering_scope_against_manifest(
    node: Mapping[str, object],
    manifest: ValidatedBatchManifest,
    plan_nodes: Mapping[str, Mapping[str, object]],
    *,
    selected_scope_by_sample: Mapping[str, ClusteringMode] | None = None,
) -> None:
    """Bind one clustering graph to its complete authenticated population."""

    raw_scope = node.get("clustering_scope")
    if raw_scope is None:
        return
    clustering_scope = _mapping(raw_scope, "persisted clustering scope")
    participants = tuple(str(item) for item in node["participant_ids"])
    sample_by_id = {item.sample_id: item for item in manifest.enabled_samples}
    if set(participants) - set(sample_by_id):
        raise BatchStateError("persisted clustering participants are absent from retained manifest")
    preparation_nodes = tuple(
        candidate
        for candidate in plan_nodes.values()
        if candidate["kind"] == "unsupervised_preparation"
    )
    prepared_sample_ids = {
        str(sample_id)
        for candidate in preparation_nodes
        for sample_id in candidate["participant_ids"]
    }
    eligible = tuple(
        sample
        for sample in manifest.enabled_samples
        if sample.config.phenotyping.mode is PhenotypingMode.UNSUPERVISED_CLUSTERING
        or (sample.config.phenotyping.mode is None and sample.sample_id in prepared_sample_ids)
    )
    eligible_by_id = {item.sample_id: item for item in eligible}
    selected = selected_scope_by_sample or {}
    effective_scope_by_sample: dict[str, ClusteringMode] = {}
    for item in eligible:
        configured_mode = item.config.clustering.mode
        if configured_mode is None:
            chosen = selected.get(item.sample_id)
            if chosen is None:
                raise BatchStateError(
                    "deferred clustering participant lacks committed preparation scope"
                )
            if configured_mode is not None and chosen is not configured_mode:
                raise BatchStateError(
                    "committed preparation contradicts configured clustering mode"
                )
            effective_scope_by_sample[item.sample_id] = chosen
        else:
            effective_scope_by_sample[item.sample_id] = configured_mode
    if prepared_sample_ids - set(eligible_by_id):
        raise BatchStateError("unsupervised preparation contradicts effective phenotyping config")
    try:
        mode = ClusteringMode(clustering_scope["mode"])
    except (TypeError, ValueError) as exc:
        raise BatchStateError("persisted clustering mode is invalid") from exc
    if mode is ClusteringMode.POOLED:
        expected_samples = tuple(
            item for item in eligible
            if item.validation_domain_id == node["validation_domain_id"]
            and effective_scope_by_sample[item.sample_id] is ClusteringMode.POOLED
        )
    else:
        expected_samples = tuple(item for item in eligible if item.sample_id in participants)
    expected_participants = tuple(sorted(item.sample_id for item in expected_samples))
    if (
        participants != expected_participants
        or not expected_participants
        or any(
            effective_scope_by_sample[item.sample_id] is not mode for item in expected_samples
        )
    ):
        raise BatchStateError(
            "persisted clustering participants do not exactly match effective config"
        )
    if mode is ClusteringMode.PER_SLIDE and len(participants) != 1:
        raise BatchStateError("per_slide clustering requires one exact configured participant")
    expected_domains = tuple(
        sorted(
            {eligible_by_id[sample_id].validation_domain_id for sample_id in expected_participants}
        )
    )
    actual_domains = tuple(str(item) for item in clustering_scope["validation_domain_ids"])
    if actual_domains != expected_domains:
        raise BatchStateError(
            "persisted clustering validation domains do not exactly cover participants"
        )
    configuration_by_sample = {
        str(binding["sample_id"]): str(binding["resolved_config_sha256"])
        for binding in node["configuration_bindings"]
    }
    if configuration_by_sample != {
        sample_id: eligible_by_id[sample_id].resolved_config_sha256
        for sample_id in expected_participants
    }:
        raise BatchStateError("persisted clustering configuration bindings are not current")
    declared_entries = tuple(str(item) for item in node["project_entry_ids"])
    relevant_preparations = tuple(
        candidate
        for candidate in preparation_nodes
        if set(str(item) for item in candidate["participant_ids"]) <= set(expected_participants)
        and (
            mode is not ClusteringMode.PER_SLIDE
            or set(str(item) for item in candidate["project_entry_ids"]) == set(declared_entries)
        )
    )
    for preparation in relevant_preparations:
        preparation_participants = tuple(str(item) for item in preparation["participant_ids"])
        preparation_entries = tuple(str(item) for item in preparation["project_entry_ids"])
        preparation_bindings = tuple(
            _mapping(item, "unsupervised preparation domain binding")
            for item in preparation["analysis_domain_bindings"]
        )
        preparation_configurations = tuple(
            _mapping(item, "unsupervised preparation configuration binding")
            for item in preparation["configuration_bindings"]
        )
        if (
            len(preparation_participants) != 1
            or len(preparation_entries) != 1
            or not preparation_bindings
            or {str(item["sample_id"]) for item in preparation_bindings}
            != set(preparation_participants)
            or {str(item["project_entry_id"]) for item in preparation_bindings}
            != set(preparation_entries)
            or len(preparation_configurations) != 1
            or str(preparation_configurations[0]["sample_id"]) != preparation_participants[0]
            or str(preparation_configurations[0]["resolved_config_sha256"])
            != eligible_by_id[preparation_participants[0]].resolved_config_sha256
        ):
            raise BatchStateError("unsupervised preparation plan authority is not exact")
    if {
        str(sample_id)
        for candidate in relevant_preparations
        for sample_id in candidate["participant_ids"]
    } != set(expected_participants):
        raise BatchStateError("persisted clustering plan lacks complete preparation participants")
    expected_entries = tuple(
        sorted(
            {
                str(entry_id)
                for candidate in relevant_preparations
                for entry_id in candidate["project_entry_ids"]
            }
        )
    )
    binding_key = lambda value: _canonical_bytes(
        dict(_mapping(value, "clustering analysis-domain binding"))
    )
    raw_expected_bindings = tuple(
        binding_key(binding)
        for candidate in relevant_preparations
        for binding in candidate["analysis_domain_bindings"]
    )
    if len(set(raw_expected_bindings)) != len(raw_expected_bindings):
        raise BatchStateError("unsupervised preparation domain authority is duplicated")
    expected_bindings = tuple(sorted(raw_expected_bindings))
    actual_bindings = tuple(
        sorted(binding_key(binding) for binding in node["analysis_domain_bindings"])
    )
    if (
        declared_entries != expected_entries
        or actual_bindings != expected_bindings
        or (mode is ClusteringMode.PER_SLIDE and len(declared_entries) != 1)
    ):
        raise BatchStateError("persisted clustering entry/domain inventory is incomplete")


def _dispatch_projection_target_sha256(
    *,
    scope: ArtifactScope,
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...],
    manifest_sha256: str,
    participants: tuple[str, ...],
) -> str:
    material = {
        "manifest_sha256": manifest_sha256,
        "participant_ids": list(sorted(participants)),
        "scope": scope.to_mapping(),
        "configuration_bindings": [item.to_mapping() for item in configuration_bindings],
    }
    if scope.validation_domain_id is None:
        if (
            scope.sample_id is not None
            or scope.slide_id is not None
            or scope.project_entry_id is not None
            or tuple(item.sample_id for item in configuration_bindings)
            != tuple(sorted(participants))
            or {item.sample_id for item in scope.analysis_domain_bindings} != set(participants)
        ):
            raise BatchStateError("pooled dispatch projection does not exactly cover participants")
        return _digest(material)
    return ArtifactProjectionTarget(
        scope=scope,
        configuration_bindings=configuration_bindings,
        target_sha256=_digest(material),
        manifest_sha256=manifest_sha256,
        participant_ids=tuple(sorted(participants)),
    ).target_sha256


def _matches_persisted_requirement(artifact: BatchArtifactRef, requirement: object) -> bool:
    """Compare a ref with the exact closed requirement retained in plan bytes."""
    try:
        raw = _closed_mapping(
            requirement,
            fields=frozenset(
                {
                    "artifact_kind",
                    "schema_name",
                    "schema_version",
                    "scope",
                    "owning_identity",
                    "owning_identity_contract_sha256",
                    "compatibility_signature_sha256",
                    "producer_version",
                    "runtime_versions",
                    "configuration_bindings",
                    "multiplicity",
                    "origin",
                    "producer_node_id",
                    "producer_attempt_id",
                    "producer_attempt_authorization_sha256",
                }
            ),
            field="persisted artifact requirement",
        )
    except BatchStateError:
        return False
    return all(
        (
            artifact.artifact_kind == raw["artifact_kind"],
            artifact.schema_name == raw["schema_name"],
            artifact.schema_version == raw["schema_version"],
            artifact.scope.to_mapping() == raw["scope"],
            _export_json(artifact.owning_identity) == raw["owning_identity"],
            artifact.owning_identity_contract_sha256 == raw["owning_identity_contract_sha256"],
            artifact.compatibility_signature_sha256 == raw["compatibility_signature_sha256"],
            artifact.producer_version == raw["producer_version"],
            dict(artifact.runtime_versions) == raw["runtime_versions"],
            [item.to_mapping() for item in artifact.configuration_bindings]
            == raw["configuration_bindings"],
            raw["origin"] is None or artifact.origin.value == raw["origin"],
            raw["producer_node_id"] is None or artifact.producer_node_id == raw["producer_node_id"],
            raw["producer_attempt_id"] is None
            or artifact.producer_attempt_id == raw["producer_attempt_id"],
            raw["producer_attempt_authorization_sha256"] is None
            or artifact.producer_attempt_authorization_sha256
            == raw["producer_attempt_authorization_sha256"],
        )
    )


def _string_map(value: object, field: str, *, values_are_sha: bool = False) -> Mapping[str, str]:
    mapping = _mapping(value, field)
    result: dict[str, str] = {}
    for key, item in mapping.items():
        parsed_key = _text(key, f"{field} key")
        parsed_value = (
            _sha256(item, f"{field}[{key!r}]")
            if values_are_sha
            else _text(item, f"{field}[{key!r}]")
        )
        result[parsed_key] = parsed_value
    return MappingProxyType(dict(sorted(result.items())))


def _optional_string_map(value: object, field: str) -> Mapping[str, str | None]:
    mapping = _mapping(value, field)
    result: dict[str, str | None] = {}
    for key, item in mapping.items():
        result[_text(key, f"{field} key")] = (
            None if item is None else _text(item, f"{field}[{key!r}]")
        )
    return MappingProxyType(dict(sorted(result.items())))


def _optional_sha256_map(value: object, field: str) -> Mapping[str, str | None]:
    mapping = _mapping(value, field)
    result: dict[str, str | None] = {}
    for key, item in mapping.items():
        result[_text(key, f"{field} key")] = (
            None if item is None else _sha256(item, f"{field}[{key!r}]")
        )
    return MappingProxyType(dict(sorted(result.items())))


def _tuple_map(value: object, field: str) -> Mapping[str, tuple[str, ...]]:
    mapping = _mapping(value, field)
    result: dict[str, tuple[str, ...]] = {}
    for key, raw_items in mapping.items():
        items = tuple(
            _text(item, f"{field}[{key!r}]") for item in _sequence(raw_items, f"{field}[{key!r}]")
        )
        if len(set(items)) != len(items):
            raise BatchStateError(f"{field}[{key!r}] must be unique")
        result[_text(key, f"{field} key")] = items
    return MappingProxyType(dict(sorted(result.items())))


def _status_map(value: object, field: str) -> Mapping[str, StageStatus]:
    mapping = _mapping(value, field)
    result: dict[str, StageStatus] = {}
    for key, item in mapping.items():
        try:
            result[_safe_segment(key, f"{field} key")] = StageStatus(item)
        except (TypeError, ValueError) as exc:
            raise BatchStateError(f"{field} contains an invalid terminal status") from exc
    return MappingProxyType(dict(sorted(result.items())))


def _scope_index(value: object) -> Mapping[str, Mapping[str, object]]:
    mapping = _mapping(value, "node_scope_by_id")
    result: dict[str, Mapping[str, object]] = {}
    expected = frozenset({"sample_ids", "validation_domain_id", "analysis_domain_ids"})
    for node_id, raw in mapping.items():
        record = _closed_mapping(raw, fields=expected, field="node scope")
        samples = tuple(
            _text(item, "node scope sample")
            for item in _sequence(record["sample_ids"], "node scope sample_ids")
        )
        domains = tuple(
            _sha256(item, "analysis_domain_id")
            for item in _sequence(record["analysis_domain_ids"], "node scope analysis_domain_ids")
        )
        if len(set(samples)) != len(samples) or len(set(domains)) != len(domains):
            raise BatchStateError("node scope indexes must be unique")
        result[_safe_segment(node_id, "node scope node_id")] = MappingProxyType(
            {
                "sample_ids": samples,
                "validation_domain_id": (
                    None
                    if record["validation_domain_id"] is None
                    else _text(
                        record["validation_domain_id"],
                        "node scope validation_domain_id",
                    )
                ),
                "analysis_domain_ids": domains,
            }
        )
    return MappingProxyType(dict(sorted(result.items())))


def _resolved_child(path: Path, owner: Path, field: str) -> Path:
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(owner)
    except (OSError, ValueError) as exc:
        raise BatchStateError(f"{field} escapes its owned workspace") from exc
    return resolved


def _reject_symlink_path(path: Path, owner: Path, field: str) -> None:
    _resolved_child(path, owner, field)
    relative = path.relative_to(owner)
    current = owner
    for segment in relative.parts:
        current = current / segment
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise BatchStateError(f"{field} cannot traverse a symlink or reparse point")


def _publication_name(value: object) -> str:
    name = _text(value, "publication file name")
    if Path(name).name != name or name in {".", ".."} or "\x00" in name:
        raise BatchStateError("publication file name must be one relative segment")
    return name


class BatchSecurePublicationProvider(Protocol):
    """Owner-directory-relative durable publication, separate from locking."""

    def publish_immutable(self, owner: Path, name: str, payload: bytes) -> None: ...

    def atomic_replace(self, owner: Path, name: str, payload: bytes) -> None: ...


def _posix_recheck_owner(owner: Path, descriptor: int, identity: tuple[int, int]) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(owner)
    except OSError as exc:
        raise BatchStateError("publication owner directory identity changed") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or (int(opened.st_dev), int(opened.st_ino)) != identity
        or (int(named.st_dev), int(named.st_ino)) != identity
    ):
        raise BatchStateError("publication owner directory identity changed")


def _posix_open_relative_owner(root_descriptor: int, relative: Path) -> tuple[int, tuple[int, int]]:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise BatchStateError("publication owner is outside workspace root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.dup(root_descriptor)
    try:
        for part in relative.parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise BatchStateError("publication owner is not a directory")
        return descriptor, (int(metadata.st_dev), int(metadata.st_ino))
    except (OSError, BatchStateError) as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if isinstance(exc, BatchStateError):
            raise
        raise BatchStateError(
            "cannot securely traverse publication owner from workspace root"
        ) from exc


def _posix_assert_final_owner_is_still_anchored(
    workspace_root: Path,
    root_descriptor: int,
    root_identity: tuple[int, int],
    relative: Path,
    identity: tuple[int, int],
    final_descriptor: int,
) -> None:
    _posix_recheck_owner(workspace_root, root_descriptor, root_identity)
    reopened, reopened_identity = _posix_open_relative_owner(root_descriptor, relative)
    try:
        final_metadata = os.fstat(final_descriptor)
        if (
            reopened_identity != identity
            or (int(final_metadata.st_dev), int(final_metadata.st_ino)) != identity
        ):
            raise BatchStateError("publication owner moved during final operation")
    finally:
        os.close(reopened)


class PosixBatchSecurePublicationProvider:
    """Descriptor-anchored POSIX publication with no-follow opens."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        before_final_resolution: Callable[[str, Path], None] | None = None,
        before_final_syscall: Callable[[str, Path], None] | None = None,
        before_final_verification: Callable[[str, Path], None] | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._before_final_resolution = before_final_resolution
        self._before_final_syscall = before_final_syscall
        self._before_final_verification = before_final_verification

    @staticmethod
    def _open_owner(owner: Path) -> tuple[int, tuple[int, int]]:
        if os.name != "posix":
            raise BatchStateError("POSIX secure publication is unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(owner, flags)
            opened = os.fstat(descriptor)
            named = os.lstat(owner)
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise BatchStateError("cannot securely open publication owner directory") from exc
        identity = (int(opened.st_dev), int(opened.st_ino))
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or identity != (int(named.st_dev), int(named.st_ino))
        ):
            os.close(descriptor)
            raise BatchStateError("publication owner directory identity is unsafe")
        return descriptor, identity

    @staticmethod
    def _recheck_owner(owner: Path, descriptor: int, identity: tuple[int, int]) -> None:
        _posix_recheck_owner(owner, descriptor, identity)

    @staticmethod
    def _open_relative_owner(root_descriptor: int, relative: Path) -> tuple[int, tuple[int, int]]:
        return _posix_open_relative_owner(root_descriptor, relative)

    def _open_anchored_owner(
        self, owner: Path
    ) -> tuple[int, tuple[int, int], int, Path, tuple[int, int]]:
        try:
            relative = Path(owner).relative_to(self._workspace_root)
        except ValueError as exc:
            raise BatchStateError("publication owner is outside workspace root") from exc
        root_descriptor, root_identity = self._open_owner(self._workspace_root)
        try:
            descriptor, identity = self._open_relative_owner(root_descriptor, relative)
        except Exception:
            os.close(root_descriptor)
            raise
        return (
            root_descriptor,
            root_identity,
            descriptor,
            relative,
            identity,
        )

    def _resolve_final_owner(
        self,
        root_descriptor: int,
        root_identity: tuple[int, int],
        relative: Path,
        identity: tuple[int, int],
        *,
        operation: str,
        owner: Path,
    ) -> int:
        hook = self._before_final_resolution
        if hook is not None:
            hook(operation, owner)
        self._recheck_owner(self._workspace_root, root_descriptor, root_identity)
        descriptor, final_identity = self._open_relative_owner(root_descriptor, relative)
        if final_identity != identity:
            os.close(descriptor)
            raise BatchStateError("publication owner changed before final root-anchored operation")
        return descriptor

    def _assert_final_owner_is_still_anchored(
        self,
        root_descriptor: int,
        root_identity: tuple[int, int],
        relative: Path,
        identity: tuple[int, int],
        final_descriptor: int,
    ) -> None:
        _posix_assert_final_owner_is_still_anchored(
            self._workspace_root,
            root_descriptor,
            root_identity,
            relative,
            identity,
            final_descriptor,
        )

    def _final_owner_success_gate(
        self,
        root_descriptor: int,
        root_identity: tuple[int, int],
        relative: Path,
        identity: tuple[int, int],
        final_descriptor: int,
    ) -> None:
        self._assert_final_owner_is_still_anchored(
            root_descriptor,
            root_identity,
            relative,
            identity,
            final_descriptor,
        )
        _posix_assert_final_owner_is_still_anchored(
            self._workspace_root,
            root_descriptor,
            root_identity,
            relative,
            identity,
            final_descriptor,
        )

    def _restore_prior_relative_authority(
        self,
        descriptor: int,
        name: str,
        payload: bytes,
    ) -> None:
        temporary: str | None = None
        try:
            temporary = PosixBatchSecurePublicationProvider._write_temporary(
                descriptor, name, payload
            )
            os.replace(
                temporary,
                name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            temporary = None
            os.fsync(descriptor)
        except (OSError, BatchStateError) as exc:
            if isinstance(exc, BatchStateError):
                raise
            raise BatchStateError("cannot restore prior head authority in retained owner") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _write_temporary(descriptor: int, name: str, payload: bytes) -> str:
        temporary = f".{name}.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            stream = os.open(temporary, flags, 0o600, dir_fd=descriptor)
            view = memoryview(payload)
            while view:
                written = os.write(stream, view)
                if written <= 0:
                    raise BatchStateError("secure publication write made no progress")
                view = view[written:]
            os.fsync(stream)
        except (OSError, BatchStateError) as exc:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            if isinstance(exc, BatchStateError):
                raise
            raise BatchStateError("cannot stage secure publication bytes") from exc
        finally:
            if "stream" in locals():
                os.close(stream)
        return temporary

    @staticmethod
    def _read_relative(descriptor: int, name: str) -> bytes:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            stream = os.open(name, flags, dir_fd=descriptor)
            metadata = os.fstat(stream)
            if not stat.S_ISREG(metadata.st_mode):
                raise BatchStateError("immutable publication path is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(stream, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        except OSError as exc:
            raise BatchStateError("immutable publication cannot be securely reopened") from exc
        finally:
            if "stream" in locals():
                os.close(stream)

    def publish_immutable(self, owner: Path, name: str, payload: bytes) -> None:
        name = _publication_name(name)
        root_descriptor, root_identity, descriptor, relative, identity = self._open_anchored_owner(
            owner
        )
        temporary: str | None = None
        final_descriptor: int | None = None
        published = False
        try:
            temporary = self._write_temporary(descriptor, name, payload)
            final_descriptor = self._resolve_final_owner(
                root_descriptor,
                root_identity,
                relative,
                identity,
                operation="publish_immutable",
                owner=owner,
            )
            hook = self._before_final_syscall
            if hook is not None:
                hook("publish_immutable", owner)
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=descriptor,
                    dst_dir_fd=final_descriptor,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError:
                if self._read_relative(final_descriptor, name) != payload:
                    raise BatchStateError("immutable content-addressed record conflicts")
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            temporary = None
            os.fsync(final_descriptor)
            hook = self._before_final_verification
            if hook is not None:
                hook("publish_immutable", owner)
            try:
                PosixBatchSecurePublicationProvider._final_owner_success_gate(
                    self,
                    root_descriptor,
                    root_identity,
                    relative,
                    identity,
                    final_descriptor,
                )
            except BatchStateError:
                if published:
                    try:
                        os.unlink(name, dir_fd=final_descriptor)
                        os.fsync(final_descriptor)
                    except OSError as exc:
                        raise BatchStateError(
                            "unsafe immutable publication cleanup failed"
                        ) from exc
                raise
        except BatchStateError:
            raise
        except OSError as exc:
            raise BatchStateError("secure immutable publication failed") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
            if final_descriptor is not None:
                os.close(final_descriptor)
            os.close(descriptor)
            os.close(root_descriptor)

    def atomic_replace(self, owner: Path, name: str, payload: bytes) -> None:
        name = _publication_name(name)
        root_descriptor, root_identity, descriptor, relative, identity = self._open_anchored_owner(
            owner
        )
        temporary: str | None = None
        final_descriptor: int | None = None
        prior_payload: bytes | None = None
        try:
            temporary = self._write_temporary(descriptor, name, payload)
            final_descriptor = self._resolve_final_owner(
                root_descriptor,
                root_identity,
                relative,
                identity,
                operation="atomic_replace",
                owner=owner,
            )
            try:
                current = os.stat(name, dir_fd=final_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and stat.S_ISLNK(current.st_mode):
                raise BatchStateError("state head cannot replace a symlink")
            if current is not None:
                if not stat.S_ISREG(current.st_mode):
                    raise BatchStateError("state head can replace only a regular file")
                prior_payload = self._read_relative(final_descriptor, name)
            hook = self._before_final_syscall
            if hook is not None:
                hook("atomic_replace", owner)
            os.replace(
                temporary,
                name,
                src_dir_fd=descriptor,
                dst_dir_fd=final_descriptor,
            )
            temporary = None
            os.fsync(final_descriptor)
            hook = self._before_final_verification
            if hook is not None:
                hook("atomic_replace", owner)
            try:
                PosixBatchSecurePublicationProvider._final_owner_success_gate(
                    self,
                    root_descriptor,
                    root_identity,
                    relative,
                    identity,
                    final_descriptor,
                )
            except BatchStateError:
                if prior_payload is not None:
                    self._restore_prior_relative_authority(
                        final_descriptor,
                        name,
                        prior_payload,
                    )
                else:
                    try:
                        os.unlink(name, dir_fd=final_descriptor)
                        os.fsync(final_descriptor)
                    except OSError as exc:
                        raise BatchStateError("unsafe atomic replacement cleanup failed") from exc
                raise
        except BatchStateError:
            raise
        except OSError as exc:
            raise BatchStateError("secure atomic replacement failed") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
            if final_descriptor is not None:
                os.close(final_descriptor)
            os.close(descriptor)
            os.close(root_descriptor)


@dataclass(frozen=True, slots=True)
class _NativeWindowsBatchPublicationRoot:
    authority: Win32FileAuthority
    retained: object


class NativeWindowsBatchSecurePublicationPrimitives:
    """Batch publication adapter over public Win32 retained-handle operations."""

    _MAX_PUBLICATION_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        *,
        authority_factory: Callable[[], Win32FileAuthority] = Win32FileAuthority,
    ) -> None:
        if os.name != "nt" and authority_factory is Win32FileAuthority:
            raise BatchStateError("native Windows publication is unavailable")
        self._authority_factory = authority_factory

    @staticmethod
    def _require_policy(*, no_follow: bool, reject_reparse_points: bool) -> None:
        if no_follow is not True or reject_reparse_points is not True:
            raise BatchStateError("native Windows publication requires no-follow policy")

    def open_workspace_root(
        self,
        owner: Path,
        *,
        no_follow: bool,
        reject_reparse_points: bool,
    ) -> object:
        self._require_policy(no_follow=no_follow, reject_reparse_points=reject_reparse_points)
        authority = self._authority_factory()
        retained = authority.retain_directory(Path(owner), create=False)
        return _NativeWindowsBatchPublicationRoot(authority, retained)

    @contextmanager
    def _owner(
        self,
        root: object,
        owner_parts: tuple[str, ...],
    ) -> Iterator[tuple[object, object]]:
        if not isinstance(root, _NativeWindowsBatchPublicationRoot):
            raise BatchStateError("native Windows publication root is invalid")
        if not isinstance(owner_parts, tuple) or any(
            not isinstance(part, str) or not part for part in owner_parts
        ):
            raise BatchStateError("native Windows publication owner is invalid")
        authority = root.authority
        retained = root.retained
        child = retained
        if owner_parts:
            child = authority.retain_child_directory(retained, Path(*owner_parts), create=False)
        primary: BaseException | None = None
        try:
            authority.validate_directory(child)
            yield authority, child
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if child is not retained:
                try:
                    authority.close_directory(child)
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    _add_bounded_cleanup_note(
                        primary,
                        cleanup,
                        context="native Windows publication child close failed",
                    )

    def publish_immutable_relative(
        self,
        handle: object,
        owner_parts: tuple[str, ...],
        name: str,
        payload: bytes,
        *,
        no_follow: bool,
        reject_reparse_points: bool,
    ) -> None:
        self._require_policy(no_follow=no_follow, reject_reparse_points=reject_reparse_points)
        with self._owner(handle, owner_parts) as (authority, retained):
            authority.publish_immutable(
                retained,
                name,
                payload,
                max_bytes=self._MAX_PUBLICATION_BYTES,
                label="batch immutable publication",
            )

    def atomic_replace_relative(
        self,
        handle: object,
        owner_parts: tuple[str, ...],
        name: str,
        payload: bytes,
        *,
        no_follow: bool,
        reject_reparse_points: bool,
    ) -> None:
        self._require_policy(no_follow=no_follow, reject_reparse_points=reject_reparse_points)
        with self._owner(handle, owner_parts) as (authority, retained):
            authority.replace_bytes(
                retained,
                name,
                payload,
                max_bytes=self._MAX_PUBLICATION_BYTES,
                label="batch atomic replacement",
            )

    def close_workspace_root(self, handle: object) -> None:
        if not isinstance(handle, _NativeWindowsBatchPublicationRoot):
            raise BatchStateError("native Windows publication root is invalid")
        handle.authority.close_directory(handle.retained)


class WindowsBatchSecurePublicationProvider:
    """Handle-relative Windows publication through reparse-safe primitives.

    The injected primitives own the native handle implementation.  They must open the
    owner without following reparse points and perform both publication operations
    relative to that handle; a prior path check is deliberately not treated as safety.
    """

    def __init__(self, *, workspace_root: Path, primitives: object | None = None) -> None:
        if primitives is None:
            if os.name != "nt":
                raise BatchStateError("Windows secure publication requires native Windows")
            primitives = NativeWindowsBatchSecurePublicationPrimitives()
        self._primitives = primitives
        self._workspace_root = Path(workspace_root)

    def _run(self, operation: str, owner: Path, name: str, payload: bytes) -> None:
        name = _publication_name(name)
        try:
            relative = Path(owner).relative_to(self._workspace_root)
        except ValueError as exc:
            raise BatchStateError("Windows publication owner is outside workspace root") from exc
        try:
            handle = self._primitives.open_workspace_root(
                self._workspace_root,
                no_follow=True,
                reject_reparse_points=True,
            )
            primary: BaseException | None = None
            try:
                getattr(self._primitives, operation)(
                    handle,
                    relative.parts,
                    name,
                    payload,
                    no_follow=True,
                    reject_reparse_points=True,
                )
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    self._primitives.close_workspace_root(handle)
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    try:
                        primary.add_note(
                            "Windows publication root close also failed: "
                            f"{type(cleanup).__name__}: {str(cleanup)[:240]}"
                        )
                    except BaseException:
                        pass
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError("Windows secure publication failed") from exc

    def publish_immutable(self, owner: Path, name: str, payload: bytes) -> None:
        self._run("publish_immutable_relative", owner, name, payload)

    def atomic_replace(self, owner: Path, name: str, payload: bytes) -> None:
        self._run("atomic_replace_relative", owner, name, payload)


@dataclass(frozen=True, slots=True)
class BatchWorkspaceLayout:
    """Deterministic contained control-plane paths for one batch identity."""

    output_root: Path
    batch_id: str
    root: Path
    manifest: Path
    plans: Path
    state: Path
    attempts: Path
    interactions: Path
    catalogue: Path
    summaries: Path
    resume: Path
    samples: Path

    def __post_init__(self) -> None:
        if not isinstance(self.output_root, Path) or not isinstance(self.root, Path):
            raise BatchStateError("workspace layout paths must be Path values")
        batch_id = _safe_segment(self.batch_id, "batch_id")
        try:
            output_root = self.output_root.resolve(strict=True)
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise BatchStateError("workspace layout authority is missing") from exc
        if (
            self.output_root != output_root
            or self.root != root
            or root != output_root / batch_id
            or not output_root.is_dir()
            or not root.is_dir()
            or root.is_symlink()
        ):
            raise BatchStateError("workspace layout root is not deterministic")
        for name in _OWNED_DIRECTORY_NAMES:
            path = getattr(self, name)
            if not isinstance(path, Path) or path != root / name:
                raise BatchStateError(f"workspace owned directory {name!r} is not deterministic")
            _reject_symlink_path(path, root, name)
            if not path.is_dir() or path.is_symlink() or path.resolve() != path:
                raise BatchStateError(f"workspace owned directory {name!r} is unsafe")
        object.__setattr__(self, "batch_id", batch_id)

    @classmethod
    def resolve(cls, output_root: Path, batch_id: str) -> "BatchWorkspaceLayout":
        if not isinstance(output_root, Path):
            raise BatchStateError("output_root must be a Path")
        safe_batch_id = _safe_segment(batch_id, "batch_id")
        normalized_output = output_root.expanduser().resolve(strict=False)
        try:
            normalized_output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BatchStateError("cannot create batch output root") from exc
        if not normalized_output.is_dir():
            raise BatchStateError("output_root must be a directory")
        root = normalized_output / safe_batch_id
        _reject_symlink_path(root, normalized_output, "batch root")
        try:
            root.mkdir(exist_ok=True)
        except OSError as exc:
            raise BatchStateError("batch root collides with a non-directory") from exc
        if not root.is_dir() or root.is_symlink():
            raise BatchStateError("batch root must be a real directory")
        root = root.resolve(strict=True)
        children: dict[str, Path] = {}
        for name in _OWNED_DIRECTORY_NAMES:
            child = root / name
            _reject_symlink_path(child, root, name)
            try:
                child.mkdir(exist_ok=True)
            except OSError as exc:
                raise BatchStateError(f"workspace path {name!r} collides") from exc
            if not child.is_dir() or child.is_symlink() or child.resolve() != child:
                raise BatchStateError(f"workspace path {name!r} is not contained")
            children[name] = child
        return cls(normalized_output, safe_batch_id, root, **children)

    @property
    def owned_directories(self) -> tuple[Path, ...]:
        return tuple(getattr(self, name) for name in _OWNED_DIRECTORY_NAMES)

    def manifest_paths(self, normalized_sha256: str) -> tuple[Path, Path]:
        """Resolve the immutable manifest generation selected by current or retained authority."""
        from .batch_manifest import manifest_generation_paths
        return manifest_generation_paths(self.manifest, normalized_sha256)

    @property
    def state_head_path(self) -> Path:
        return self.state / "head.json"

    @property
    def initialization_directory(self) -> Path:
        return self.state / "initialization"

    @property
    def initialization_active_path(self) -> Path:
        return self.state / "initialization-active.json"

    @property
    def writer_lock_path(self) -> Path:
        return self.state / ".writer.lock"


class _AdvisoryLockHandle(Protocol):
    @property
    def is_locked(self) -> bool: ...

    def validate(self, path: Path) -> bool: ...

    def release(self) -> None: ...


class BatchWorkspaceLockProvider(Protocol):
    def acquire(self, path: Path) -> _AdvisoryLockHandle: ...


class _FileLockHandle:
    def __init__(self, stream: BinaryIO, *, unlock: Any) -> None:
        self._stream = stream
        metadata = os.fstat(stream.fileno())
        self._identity = (int(metadata.st_dev), int(metadata.st_ino))
        self._unlock = unlock
        self._locked = True

    @property
    def is_locked(self) -> bool:
        return self._locked and not self._stream.closed

    def validate(self, path: Path) -> bool:
        if not self.is_locked:
            return False
        try:
            metadata = os.lstat(path)
            opened = os.fstat(self._stream.fileno())
        except OSError:
            return False
        return (
            not stat.S_ISLNK(metadata.st_mode)
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and (int(metadata.st_dev), int(metadata.st_ino)) == self._identity
            and (int(opened.st_dev), int(opened.st_ino)) == self._identity
        )

    def release(self) -> None:
        if not self._locked:
            return
        self._unlock(self._stream)
        self._locked = False
        self._stream.close()


def _open_lock_file(path: Path) -> BinaryIO:
    try:
        if path.is_symlink():
            raise BatchStateError("workspace lock cannot be a symlink")
        stream = path.open("a+b", buffering=0)
        metadata = os.fstat(stream.fileno())
        current = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_ISLNK(current.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise BatchStateError("workspace lock identity is unsafe")
        if metadata.st_size == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        return stream
    except Exception:
        if "stream" in locals():
            stream.close()
        raise


class PosixBatchWorkspaceLockProvider:
    """Non-blocking POSIX advisory writer lock with crash release."""

    def acquire(self, path: Path) -> _AdvisoryLockHandle:
        if os.name != "posix":
            raise BatchStateError("POSIX batch locking is unavailable")
        import fcntl

        stream = _open_lock_file(path)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BatchStateError("batch workspace already has a writer") from exc
            raise BatchStateError("cannot acquire batch workspace writer lock") from exc

        def unlock(locked: BinaryIO) -> None:
            fcntl.flock(locked.fileno(), fcntl.LOCK_UN)

        return _FileLockHandle(stream, unlock=unlock)


class WindowsBatchWorkspaceLockProvider:
    """Standard-library Windows byte-range writer lock with injectable primitives."""

    def __init__(self, *, primitives: object | None = None) -> None:
        if primitives is None:
            if os.name != "nt":
                raise BatchStateError("Windows batch locking is unavailable")
            import msvcrt

            primitives = msvcrt
        self._primitives = primitives

    def acquire(self, path: Path) -> _AdvisoryLockHandle:
        stream = _open_lock_file(path)
        stream.seek(0)
        try:
            self._primitives.locking(stream.fileno(), self._primitives.LK_NBLCK, 1)
        except OSError as exc:
            stream.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BatchStateError("batch workspace already has a writer") from exc
            raise BatchStateError("cannot acquire batch workspace writer lock") from exc

        def unlock(locked: BinaryIO) -> None:
            locked.seek(0)
            self._primitives.locking(locked.fileno(), self._primitives.LK_UNLCK, 1)

        return _FileLockHandle(stream, unlock=unlock)


@dataclass(frozen=True, slots=True, init=False)
class BatchWorkspaceLease:
    workspace_root: Path
    operation: str
    fencing_token: str
    _issuer_id: str
    _handle: _AdvisoryLockHandle
    _released: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchStateError("workspace leases are issued only by BatchStateStore")

    @classmethod
    def _issue(
        cls,
        *,
        workspace_root: Path,
        operation: str,
        issuer_id: str,
        handle: _AdvisoryLockHandle,
    ) -> "BatchWorkspaceLease":
        lease = object.__new__(cls)
        object.__setattr__(lease, "workspace_root", workspace_root)
        object.__setattr__(lease, "operation", _text(operation, "lease operation"))
        object.__setattr__(lease, "fencing_token", uuid4().hex)
        object.__setattr__(lease, "_issuer_id", issuer_id)
        object.__setattr__(lease, "_handle", handle)
        object.__setattr__(lease, "_released", False)
        return lease

    @property
    def is_live(self) -> bool:
        return not self._released and self._handle.is_locked

    def _release(self) -> None:
        if self._released:
            return
        try:
            self._handle.release()
        except BaseException:
            if not self._handle.is_locked:
                object.__setattr__(self, "_released", True)
            raise
        else:
            object.__setattr__(self, "_released", True)


@dataclass(frozen=True, slots=True, init=False)
class BatchWorkspaceLeaseUse:
    """Opaque store-issued pin of one exact live writer lease."""

    _controller: object
    _thread_id: int
    _released: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchStateError("workspace lease uses are issued only by BatchStateStore")

    @classmethod
    def _issue(cls, *, controller: object, thread_id: int) -> "BatchWorkspaceLeaseUse":
        use = object.__new__(cls)
        object.__setattr__(use, "_controller", controller)
        object.__setattr__(use, "_thread_id", thread_id)
        object.__setattr__(use, "_released", False)
        return use

    @property
    def is_live(self) -> bool:
        return not self._released


@dataclass(slots=True)
class _BatchWorkspaceLeaseController:
    lease: BatchWorkspaceLease
    runtime_token: RuntimeUseToken
    state: Literal["open", "release_pending", "released", "failed"] = "open"
    live_uses: int = 0
    uses_by_thread: dict[int, int] = field(default_factory=dict)
    release_owner_thread: int | None = None
    terminal_failure: BaseException | None = None


def _resume_import_graph_sha256(
    *,
    original_manifest_bytes: bytes,
    normalized_manifest_bytes: bytes,
    manifest_bytes_by_sha256: tuple[tuple[str, bytes, bytes], ...],
    plan_bytes_by_sha256: tuple[tuple[str, bytes], ...],
    authorization_bytes_by_sha256: tuple[tuple[str, bytes], ...],
    attempt_bytes_by_sha256: tuple[tuple[str, bytes], ...],
    interaction_bytes_by_name: tuple[tuple[str, bytes], ...],
    head_bytes: bytes,
    operational_rebinding_bytes: bytes | None,
    project_bytes_by_name: tuple[tuple[str, bytes], ...] = (),
    training_fit_set_bytes_by_sha256: tuple[tuple[str, bytes], ...] = (),
    native_fit_material_bytes_by_sha256: tuple[tuple[str, bytes], ...] = (),
    pause_record_bytes_by_sha256: tuple[tuple[str, bytes], ...] = (),
) -> str:
    return _digest(
        {
            "original_manifest_sha256": hashlib.sha256(original_manifest_bytes).hexdigest(),
            "normalized_manifest_sha256": hashlib.sha256(normalized_manifest_bytes).hexdigest(),
            "manifest_generations": {digest: [hashlib.sha256(original).hexdigest(), hashlib.sha256(normalized).hexdigest()]
                for digest, original, normalized in manifest_bytes_by_sha256},
            "plans": {
                key: hashlib.sha256(payload).hexdigest() for key, payload in plan_bytes_by_sha256
            },
            "authorizations": {
                key: hashlib.sha256(payload).hexdigest()
                for key, payload in authorization_bytes_by_sha256
            },
            "attempts": {
                key: hashlib.sha256(payload).hexdigest() for key, payload in attempt_bytes_by_sha256
            },
            "interactions": {
                key: hashlib.sha256(payload).hexdigest()
                for key, payload in interaction_bytes_by_name
            },
            "head_bytes_sha256": hashlib.sha256(head_bytes).hexdigest(),
            "operational_rebinding_bytes_sha256": (
                None
                if operational_rebinding_bytes is None
                else hashlib.sha256(operational_rebinding_bytes).hexdigest()
            ),
            "project_authority": {
                key: hashlib.sha256(payload).hexdigest() for key, payload in project_bytes_by_name
            },
            "training_fit_sets": {
                key: hashlib.sha256(payload).hexdigest()
                for key, payload in training_fit_set_bytes_by_sha256
            },
            "quiescent_pauses": {key: hashlib.sha256(payload).hexdigest()
                for key, payload in pause_record_bytes_by_sha256},
            "native_fit_materials": {
                key: hashlib.sha256(payload).hexdigest()
                for key, payload in native_fit_material_bytes_by_sha256
            },
        }
    )


class _AuthenticatedResumeCapsuleImport:
    """Private, one-shot authority for one completely pinned capsule graph."""

    __slots__ = (
        "capsule_sha256",
        "capsule_root_sha256",
        "destination_root",
        "layout_root",
        "head_sha256",
        "validated_locator_receipt_sha256s",
        "workspace_rebinding_sha256",
        "original_manifest_bytes",
        "normalized_manifest_bytes",
        "manifest_bytes_by_sha256",
        "plan_bytes_by_sha256",
        "authorization_bytes_by_sha256",
        "attempt_bytes_by_sha256",
        "interaction_bytes_by_name",
        "head_bytes",
        "operational_rebinding_bytes",
        "project_bytes_by_name",
        "training_fit_set_bytes_by_sha256",
        "native_fit_material_bytes_by_sha256",
        "pause_record_bytes_by_sha256",
        "_pause_quiescence_graph_sha256",
        "graph_sha256",
        "_issuer",
        "_consumed",
        "_recovery_allowed",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchStateError("resume capsule import authority is issued only by BatchStateStore")

    def _assert_bound(self, store: "BatchStateStore") -> None:
        if (
            self._consumed
            or self._issuer is not store._resume_capsule_import_capability
            or self.destination_root != store.layout.output_root
            or self.layout_root != store.layout.root
            or self.graph_sha256
            != _resume_import_graph_sha256(
                original_manifest_bytes=self.original_manifest_bytes,
                normalized_manifest_bytes=self.normalized_manifest_bytes,
                manifest_bytes_by_sha256=self.manifest_bytes_by_sha256,
                plan_bytes_by_sha256=self.plan_bytes_by_sha256,
                authorization_bytes_by_sha256=self.authorization_bytes_by_sha256,
                attempt_bytes_by_sha256=self.attempt_bytes_by_sha256,
                interaction_bytes_by_name=self.interaction_bytes_by_name,
                head_bytes=self.head_bytes,
                operational_rebinding_bytes=self.operational_rebinding_bytes,
                project_bytes_by_name=self.project_bytes_by_name,
                training_fit_set_bytes_by_sha256=(self.training_fit_set_bytes_by_sha256),
                native_fit_material_bytes_by_sha256=(self.native_fit_material_bytes_by_sha256),
                pause_record_bytes_by_sha256=self.pause_record_bytes_by_sha256,
            )
        ):
            raise BatchStateError(
                "resume capsule import authority is consumed, foreign, or changed"
            )

    def _authorize_paused_import(self, lease, drain) -> None:
        """Bind planned pause bytes to the actual fenced, process-free import."""
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationLease, ProjectMutationProcessDrainEvidence,
        )
        head = BatchStateHead.from_mapping(json.loads(self.head_bytes))
        if (
            not isinstance(lease, ProjectMutationLease) or not lease.is_live
            or lease.operation != "capsule_import"
            or not isinstance(drain, ProjectMutationProcessDrainEvidence)
            or drain._issuer is not lease._provider._provider_issuer
            or drain.role != "identity_materialization"
            or drain.project_identity_sha256 != head.project_identity_sha256
            or drain.acquisition_record_sha256 != lease.acquisition_record_sha256
            or drain.mutation_request_sha256 != lease.mutation_request_sha256
            or drain.fencing_token_sha256 != lease.fencing_token_sha256
            or lease.current_authority_head.authority_head_sha256 != head.project_authority_head_sha256
            or str(lease.designation.project_path) != head.project_path
            or drain.containment_drained is not True
            or any(getattr(drain, name) is not None for name in (
                "process_start_token", "launch_nonce_sha256", "terminal_status_sha256",
                "drain_evidence", "spawn_attempt_sha256", "process_launch_sha256"))
        ):
            raise BatchStateError("paused import lacks its exact no-process lease evidence")
        object.__setattr__(self, "_pause_quiescence_graph_sha256", self.graph_sha256)

    def _consume(self, store: "BatchStateStore") -> None:
        self._assert_bound(store)
        object.__setattr__(self, "_consumed", True)

    def _authorize_exact_recovery(self, store: "BatchStateStore") -> None:
        self._assert_bound(store)
        object.__setattr__(self, "_recovery_allowed", True)


def _destination_resume_import_operation(installer: Callable[..., BatchStateHead]):
    """Supply the lease to the one public high-level import operation."""

    def import_resume_capsule(
        store: BatchStateStore,
        plan: object,
        rebindings: object,
        validators: object,
    ) -> BatchStateHead:
        with store.acquire_writer_lease("import-resume") as lease:
            return installer(
                store,
                plan,
                rebindings,
                validators,
                lease=lease,
            )

    setattr(
        import_resume_capsule,
        "_runtime_guard_kind",
        getattr(installer, "_runtime_guard_kind", None),
    )
    return import_resume_capsule


@dataclass(frozen=True, slots=True)
class BatchEffectiveLocatorAuthority:
    requirement_id: str
    owner_validator_id: str
    effective_path: Path
    sha256: str | None
    byte_count: int | None
    owning_identity_sha256: str
    validation_receipt_sha256: str
    operational_rebinding_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _text(self.requirement_id, "requirement_id"))
        object.__setattr__(
            self,
            "owner_validator_id",
            _text(self.owner_validator_id, "owner_validator_id"),
        )
        if not isinstance(self.effective_path, Path):
            raise BatchStateError("effective locator must be a Path")
        object.__setattr__(
            self,
            "effective_path",
            self.effective_path.expanduser().resolve(strict=False),
        )
        object.__setattr__(self, "sha256", _optional_sha256(self.sha256, "locator sha256"))
        if self.byte_count is not None and (
            type(self.byte_count) is not int or self.byte_count < 0
        ):
            raise BatchStateError("locator byte_count must be a nonnegative integer")
        for field_name in (
            "owning_identity_sha256",
            "validation_receipt_sha256",
            "operational_rebinding_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))


@dataclass(frozen=True, slots=True)
class BatchStateHead:
    schema_name: str
    schema_version: int
    batch_id: str
    predecessor_head_sha256: str | None
    ancestor_head_sha256s: tuple[str, ...]
    active_plan_sha256: str
    plan_history_sha256s: tuple[str, ...]
    original_manifest_sha256: str
    normalized_manifest_sha256: str
    project_path: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_materialization_state: Literal["reserved", "materialized"]
    project_qpproj_snapshot_sha256: str | None
    project_mutation_attestation_sha256: str | None
    project_catalogue_sha256: str | None
    source_sha256_by_sample: Mapping[str, str]
    resolved_config_sha256_by_sample: Mapping[str, str]
    sample_project_entry_id_by_sample: Mapping[str, str | None]
    sample_project_entry_projection_sha256_by_sample: Mapping[str, str | None]
    accepted_domain_revision_sha256_by_id: Mapping[str, str]
    domain_set_sha256_by_sample: Mapping[str, str]
    node_status_by_id: Mapping[str, StageStatus]
    node_scope_by_id: Mapping[str, Mapping[str, object]]
    last_durable_node_ids_by_sample: Mapping[str, tuple[str, ...]]
    last_durable_node_ids_by_domain: Mapping[str, tuple[str, ...]]
    next_eligible_node_ids_by_sample: Mapping[str, tuple[str, ...]]
    next_eligible_node_ids_by_domain: Mapping[str, tuple[str, ...]]
    processed_image_paths_by_sample: Mapping[str, str]
    available_artifact_ref_sha256_by_key: Mapping[str, str]
    pending_interaction_ids: tuple[str, ...]
    pending_authorization_sha256_by_node: Mapping[str, str]
    pending_training_fit_set_sha256_by_node: Mapping[str, str]
    failure_codes_by_node: Mapping[str, str]
    domain_task_status_by_id: Mapping[str, StageStatus]
    generation_references_by_node: Mapping[str, tuple[str, ...]]
    checkpoint_descriptor_sha256_by_node: Mapping[str, str]
    attempt_ids_by_node: Mapping[str, tuple[str, ...]]
    terminal_attempt_sha256_by_attempt_id: Mapping[str, str]
    invalidated_node_ids: tuple[str, ...]
    revision_reason_by_plan_sha256: Mapping[str, str]
    operational_rebinding_sha256: str | None
    execution_state: BatchExecutionState
    quiescent_pause_sha256: str | None
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the BatchStateHead fields and compute its normalized checksum once.

        The checks cover ``accepted_domain_revision_sha256_by_id``,
        ``checkpoint_descriptor_sha256_by_node``, ``terminal_attempt_sha256_by_attempt_id``,
        ``predecessor_head_sha256``, ``active_plan_sha256``,
        ``project_materialization_state``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        if (self.schema_name, self.schema_version) != (
            STATE_SCHEMA_NAME,
            STATE_SCHEMA_VERSION,
        ):
            raise BatchStateError("state head must use mif-batch-state-head/v1")
        try:
            assert_current_writer(STATE_SCHEMA_NAME, str(STATE_SCHEMA_VERSION))
        except ValueError as exc:
            raise BatchStateError("state head schema is not current") from exc
        object.__setattr__(self, "batch_id", _safe_segment(self.batch_id, "batch_id"))
        predecessor = _optional_sha256(self.predecessor_head_sha256, "predecessor_head_sha256")
        ancestors = tuple(
            _sha256(item, "ancestor_head_sha256s") for item in self.ancestor_head_sha256s
        )
        if len(set(ancestors)) != len(ancestors):
            raise BatchStateError("head ancestor chain must not contain duplicates")
        if predecessor is None:
            if ancestors:
                raise BatchStateError("initial head cannot carry ancestors")
        elif not ancestors or ancestors[-1] != predecessor:
            raise BatchStateError("head predecessor must end its exact ancestor chain")
        history = tuple(_sha256(item, "plan_history_sha256s") for item in self.plan_history_sha256s)
        active_plan = _sha256(self.active_plan_sha256, "active_plan_sha256")
        if not history or history[-1] != active_plan or len(set(history)) != len(history):
            raise BatchStateError("active plan must end a unique plan history")
        pending_interactions = tuple(
            _batch_request_id(item) for item in self.pending_interaction_ids
        )
        invalidated = tuple(
            sorted(_safe_segment(item, "invalidated node ID") for item in self.invalidated_node_ids)
        )
        if len(set(pending_interactions)) != len(pending_interactions) or len(
            set(invalidated)
        ) != len(invalidated):
            raise BatchStateError("head indexes must be unique")
        object.__setattr__(self, "predecessor_head_sha256", predecessor)
        object.__setattr__(self, "ancestor_head_sha256s", ancestors)
        object.__setattr__(self, "active_plan_sha256", active_plan)
        object.__setattr__(self, "plan_history_sha256s", history)
        object.__setattr__(
            self,
            "original_manifest_sha256",
            _sha256(self.original_manifest_sha256, "original_manifest_sha256"),
        )
        object.__setattr__(
            self,
            "normalized_manifest_sha256",
            _sha256(self.normalized_manifest_sha256, "normalized_manifest_sha256"),
        )
        object.__setattr__(self, "project_path", _text(self.project_path, "project_path"))
        object.__setattr__(
            self,
            "project_identity_sha256",
            _sha256(self.project_identity_sha256, "project_identity_sha256"),
        )
        object.__setattr__(
            self,
            "project_authority_head_sha256",
            _sha256(self.project_authority_head_sha256, "project_authority_head_sha256"),
        )
        if self.project_materialization_state not in {"reserved", "materialized"}:
            raise BatchStateError("project materialization state is invalid")
        for field_name in (
            "project_qpproj_snapshot_sha256",
            "project_mutation_attestation_sha256",
            "project_catalogue_sha256",
        ):
            object.__setattr__(
                self, field_name, _optional_sha256(getattr(self, field_name), field_name)
            )
        if self.project_materialization_state == "reserved" and (
            self.project_qpproj_snapshot_sha256 is not None
            or self.project_mutation_attestation_sha256 is not None
            or self.project_catalogue_sha256 is not None
        ):
            raise BatchStateError("reserved project state cannot carry materialized attestations")
        if (
            self.project_materialization_state == "materialized"
            and self.project_qpproj_snapshot_sha256 is None
        ):
            raise BatchStateError("materialized project state requires qpproj snapshot")
        for field_name in (
            "source_sha256_by_sample",
            "resolved_config_sha256_by_sample",
            "accepted_domain_revision_sha256_by_id",
            "domain_set_sha256_by_sample",
            "available_artifact_ref_sha256_by_key",
            "pending_authorization_sha256_by_node",
            "pending_training_fit_set_sha256_by_node",
            "checkpoint_descriptor_sha256_by_node",
            "terminal_attempt_sha256_by_attempt_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_map(getattr(self, field_name), field_name, values_are_sha=True),
            )
        object.__setattr__(
            self,
            "sample_project_entry_id_by_sample",
            _optional_string_map(
                self.sample_project_entry_id_by_sample,
                "sample_project_entry_id_by_sample",
            ),
        )
        object.__setattr__(
            self,
            "sample_project_entry_projection_sha256_by_sample",
            _optional_sha256_map(
                self.sample_project_entry_projection_sha256_by_sample,
                "sample_project_entry_projection_sha256_by_sample",
            ),
        )
        for field_name in (
            "source_sha256_by_sample",
            "resolved_config_sha256_by_sample",
            "sample_project_entry_id_by_sample",
            "sample_project_entry_projection_sha256_by_sample",
            "domain_set_sha256_by_sample",
        ):
            for key in getattr(self, field_name):
                _safe_segment(key, f"{field_name} sample key")
        for domain_id in self.accepted_domain_revision_sha256_by_id:
            _sha256(domain_id, "accepted domain revision key")
        for node_id in self.checkpoint_descriptor_sha256_by_node:
            _safe_segment(node_id, "checkpoint descriptor node key")
        for key, value in self.available_artifact_ref_sha256_by_key.items():
            digest = _sha256(key, "available artifact projection key")
            if value != digest:
                raise BatchStateError("available artifact inventory must be digest-keyed exactly")
        for field_name in (
            "processed_image_paths_by_sample",
            "failure_codes_by_node",
            "revision_reason_by_plan_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_map(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self, "node_status_by_id", _status_map(self.node_status_by_id, "node_status_by_id")
        )
        object.__setattr__(
            self,
            "domain_task_status_by_id",
            _status_map(self.domain_task_status_by_id, "domain_task_status_by_id"),
        )
        object.__setattr__(self, "node_scope_by_id", _scope_index(self.node_scope_by_id))
        for field_name in (
            "last_durable_node_ids_by_sample",
            "last_durable_node_ids_by_domain",
            "next_eligible_node_ids_by_sample",
            "next_eligible_node_ids_by_domain",
            "generation_references_by_node",
            "attempt_ids_by_node",
        ):
            object.__setattr__(self, field_name, _tuple_map(getattr(self, field_name), field_name))
        object.__setattr__(self, "pending_interaction_ids", pending_interactions)
        object.__setattr__(self, "invalidated_node_ids", invalidated)
        object.__setattr__(
            self,
            "operational_rebinding_sha256",
            _optional_sha256(
                self.operational_rebinding_sha256,
                "operational_rebinding_sha256",
            ),
        )
        try:
            execution_state = BatchExecutionState(self.execution_state)
        except (TypeError, ValueError) as exc:
            raise BatchStateError("batch execution state is invalid") from exc
        quiescent_pause = _optional_sha256(self.quiescent_pause_sha256, "quiescent_pause_sha256")
        if (execution_state is BatchExecutionState.PAUSED) != (quiescent_pause is not None):
            raise BatchStateError("paused state must carry exactly one quiescent pause record")
        object.__setattr__(self, "execution_state", execution_state)
        object.__setattr__(self, "quiescent_pause_sha256", quiescent_pause)
        for node_id, digest in self.pending_authorization_sha256_by_node.items():
            _safe_segment(node_id, "pending authorization node ID")
            if node_id in invalidated:
                raise BatchStateError("invalidated nodes cannot retain pending authorization")
            _sha256(digest, "pending authorization digest")
        for node_id, digest in self.pending_training_fit_set_sha256_by_node.items():
            _safe_segment(node_id, "pending training fit-set node ID")
            if node_id in invalidated:
                raise BatchStateError("invalidated nodes cannot retain a pending training fit set")
            _sha256(digest, "pending training fit-set digest")
        for attempt_id in self.terminal_attempt_sha256_by_attempt_id:
            _safe_segment(attempt_id, "terminal attempt ID")
        for node_id, attempt_ids in self.attempt_ids_by_node.items():
            _safe_segment(node_id, "attempt index node ID")
            if any(item not in self.terminal_attempt_sha256_by_attempt_id for item in attempt_ids):
                raise BatchStateError("attempt index names a missing terminal attempt")
        if set(self.node_status_by_id) - set(self.attempt_ids_by_node):
            raise BatchStateError("terminal node status lacks immutable attempt history")
        normalized = self._unsigned_mapping()
        _reject_prohibited_payload(normalized)
        object.__setattr__(self, "sha256", _digest(normalized))

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "predecessor_head_sha256": self.predecessor_head_sha256,
            "ancestor_head_sha256s": list(self.ancestor_head_sha256s),
            "active_plan_sha256": self.active_plan_sha256,
            "plan_history_sha256s": list(self.plan_history_sha256s),
            "original_manifest_sha256": self.original_manifest_sha256,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "project_path": self.project_path,
            "project_identity_sha256": self.project_identity_sha256,
            "project_authority_head_sha256": self.project_authority_head_sha256,
            "project_materialization_state": self.project_materialization_state,
            "project_qpproj_snapshot_sha256": self.project_qpproj_snapshot_sha256,
            "project_mutation_attestation_sha256": self.project_mutation_attestation_sha256,
            "project_catalogue_sha256": self.project_catalogue_sha256,
            "source_sha256_by_sample": dict(self.source_sha256_by_sample),
            "resolved_config_sha256_by_sample": dict(self.resolved_config_sha256_by_sample),
            "sample_project_entry_id_by_sample": dict(self.sample_project_entry_id_by_sample),
            "sample_project_entry_projection_sha256_by_sample": dict(
                self.sample_project_entry_projection_sha256_by_sample
            ),
            "accepted_domain_revision_sha256_by_id": dict(
                self.accepted_domain_revision_sha256_by_id
            ),
            "domain_set_sha256_by_sample": dict(self.domain_set_sha256_by_sample),
            "node_status_by_id": {key: item.value for key, item in self.node_status_by_id.items()},
            "node_scope_by_id": {
                key: {
                    "sample_ids": list(item["sample_ids"]),
                    "validation_domain_id": item["validation_domain_id"],
                    "analysis_domain_ids": list(item["analysis_domain_ids"]),
                }
                for key, item in self.node_scope_by_id.items()
            },
            "last_durable_node_ids_by_sample": {
                key: list(item) for key, item in self.last_durable_node_ids_by_sample.items()
            },
            "last_durable_node_ids_by_domain": {
                key: list(item) for key, item in self.last_durable_node_ids_by_domain.items()
            },
            "next_eligible_node_ids_by_sample": {
                key: list(item) for key, item in self.next_eligible_node_ids_by_sample.items()
            },
            "next_eligible_node_ids_by_domain": {
                key: list(item) for key, item in self.next_eligible_node_ids_by_domain.items()
            },
            "processed_image_paths_by_sample": dict(self.processed_image_paths_by_sample),
            "available_artifact_ref_sha256_by_key": dict(self.available_artifact_ref_sha256_by_key),
            "pending_interaction_ids": list(self.pending_interaction_ids),
            "pending_authorization_sha256_by_node": dict(self.pending_authorization_sha256_by_node),
            "pending_training_fit_set_sha256_by_node": dict(
                self.pending_training_fit_set_sha256_by_node
            ),
            "failure_codes_by_node": dict(self.failure_codes_by_node),
            "domain_task_status_by_id": {
                key: item.value for key, item in self.domain_task_status_by_id.items()
            },
            "generation_references_by_node": {
                key: list(item) for key, item in self.generation_references_by_node.items()
            },
            "checkpoint_descriptor_sha256_by_node": dict(self.checkpoint_descriptor_sha256_by_node),
            "attempt_ids_by_node": {
                key: list(item) for key, item in self.attempt_ids_by_node.items()
            },
            "terminal_attempt_sha256_by_attempt_id": dict(
                self.terminal_attempt_sha256_by_attempt_id
            ),
            "invalidated_node_ids": list(self.invalidated_node_ids),
            "revision_reason_by_plan_sha256": dict(self.revision_reason_by_plan_sha256),
            "operational_rebinding_sha256": self.operational_rebinding_sha256,
            "execution_state": self.execution_state.value,
            "quiescent_pause_sha256": self.quiescent_pause_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "BatchStateHead":
        unsigned = dict(values)
        unsigned.setdefault("schema_name", STATE_SCHEMA_NAME)
        unsigned.setdefault("schema_version", STATE_SCHEMA_VERSION)
        unsigned.setdefault("operational_rebinding_sha256", None)
        unsigned.setdefault("execution_state", BatchExecutionState.ACTIVE)
        unsigned.setdefault("quiescent_pause_sha256", None)
        return cls(**unsigned)  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchStateHead":
        expected = frozenset(item.name for item in fields(cls))
        mapping = _closed_mapping(value, fields=expected, field="batch state head")
        supplied_digest = _sha256(mapping["sha256"], "state head sha256")
        head = cls(**{name: mapping[name] for name in expected if name != "sha256"})
        if head.sha256 != supplied_digest:
            raise BatchStateError("state head checksum does not match canonical bytes")
        return head

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "sha256": self.sha256}

    def evolve(self, **changes: object) -> "BatchStateHead":
        if set(changes) & {"sha256", "predecessor_head_sha256", "ancestor_head_sha256s"}:
            raise BatchStateError("head evolution owns predecessor and checksum fields")
        values = {
            item.name: getattr(self, item.name) for item in fields(self) if item.name != "sha256"
        }
        values.update(changes)
        values["predecessor_head_sha256"] = self.sha256
        values["ancestor_head_sha256s"] = (*self.ancestor_head_sha256s, self.sha256)
        return BatchStateHead.create(**values)

    @property
    def completed_node_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                node_id
                for node_id, status in self.node_status_by_id.items()
                if status is StageStatus.COMPLETED and node_id not in self.invalidated_node_ids
            )
        )


@dataclass(frozen=True, slots=True)
class BatchExecutionSnapshot:
    """Authenticated public projection of one stable current execution head."""

    head: BatchStateHead
    plan: BatchPlan
    manifest: ValidatedBatchManifest
    accepted_artifacts: tuple[BatchArtifactRef, ...]
    committed_artifact_authorities: tuple[BatchCommittedArtifactAuthority, ...]

    def __post_init__(self) -> None:
        """Validate the BatchExecutionSnapshot fields when the record is constructed.

        The checks cover ``accepted_artifacts``, ``committed_artifact_authorities``,
        ``head``, ``plan``, ``manifest``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        if not isinstance(self.head, BatchStateHead):
            raise BatchStateError("execution snapshot requires a state head")
        if not isinstance(self.plan, BatchPlan):
            raise BatchStateError("execution snapshot requires a typed batch plan")
        if not isinstance(self.manifest, ValidatedBatchManifest):
            raise BatchStateError("execution snapshot requires a validated manifest")
        artifacts = tuple(self.accepted_artifacts)
        authorities = tuple(self.committed_artifact_authorities)
        if not all(isinstance(item, BatchArtifactRef) for item in artifacts):
            raise BatchStateError("execution snapshot artifacts must be typed")
        if not all(isinstance(item, BatchCommittedArtifactAuthority) for item in authorities):
            raise BatchStateError("execution snapshot authorities must be typed")
        digests = tuple(item.artifact_ref_sha256 for item in artifacts)
        if digests != tuple(sorted(digests)) or len(set(digests)) != len(digests):
            raise BatchStateError("execution snapshot artifacts must be canonical and unique")
        authority_digests = tuple(item.artifact_ref.artifact_ref_sha256 for item in authorities)
        internal_digests = tuple(
            item.artifact_ref_sha256
            for item in artifacts
            if item.origin is ArtifactOrigin.INTERNAL_NODE
        )
        if authority_digests != internal_digests:
            raise BatchStateError(
                "execution snapshot requires exactly one authority per internal artifact"
            )
        object.__setattr__(self, "accepted_artifacts", artifacts)
        object.__setattr__(self, "committed_artifact_authorities", authorities)


@dataclass(frozen=True, slots=True)
class BatchAttemptAuthorizationRequest:
    node: BatchNode
    input_artifacts: tuple[BatchArtifactRef, ...]
    adapter_spec: StageAdapterSpec
    dispatch_authority: BatchPreparedDispatchAuthority
    retry_ordinal: int
    interaction_response_sha256: str | None = None
    interaction_predecessor_terminal_record_sha256: str | None = None
    failed_retry: BatchFailedRetry | None = None

    def __post_init__(self) -> None:
        """Validate the BatchAttemptAuthorizationRequest fields when the record is constructed.

        The checks cover ``input_artifacts``, ``interaction_response_sha256``,
        ``interaction_predecessor_terminal_record_sha256``, ``node``, ``adapter_spec``,
        ``dispatch_authority``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if not isinstance(self.node, BatchNode):
            raise BatchStateError("authorization request requires one exact BatchNode")
        try:
            replace(self.node)
        except (BatchPlanError, TypeError, ValueError) as exc:
            raise BatchStateError("authorization request node is not canonical") from exc
        if not isinstance(self.adapter_spec, StageAdapterSpec):
            raise BatchStateError("authorization request requires a typed adapter spec")
        if not isinstance(self.dispatch_authority, BatchPreparedDispatchAuthority):
            raise BatchStateError(
                "authorization request requires typed prepared dispatch authority"
            )
        inputs = tuple(self.input_artifacts)
        try:
            inputs = tuple(validate_artifact_ref(item) for item in inputs)
            artifact_ref_set_sha256(inputs)
        except BatchArtifactError as exc:
            raise BatchStateError("authorization request inputs are invalid") from exc
        if type(self.retry_ordinal) is not int or self.retry_ordinal < 0:
            raise BatchStateError("retry_ordinal must be a nonnegative integer")
        response = _optional_sha256(self.interaction_response_sha256, "interaction_response_sha256")
        predecessor = _optional_sha256(
            self.interaction_predecessor_terminal_record_sha256,
            "interaction_predecessor_terminal_record_sha256",
        )
        if (response is None) != (predecessor is None):
            raise BatchStateError("retry response and predecessor must be paired")
        if self.retry_ordinal == 0 and response is not None:
            raise BatchStateError("ordinary authorization cannot carry retry response")
        if self.failed_retry is not None and (
            type(self.failed_retry) is not BatchFailedRetry
            or self.retry_ordinal == 0 or response is not None
        ):
            raise BatchStateError("failed retry request provenance is invalid")
        object.__setattr__(self, "input_artifacts", inputs)
        object.__setattr__(self, "interaction_response_sha256", response)
        object.__setattr__(
            self,
            "interaction_predecessor_terminal_record_sha256",
            predecessor,
        )


@dataclass(frozen=True, slots=True)
class BatchInteractionRequestPublication:
    """Admit a frozen request once and derive its durable canonical bytes.

    Internal consumers reuse ``stage_result``. The state store still authenticates
    its plan, scope and terminal authority before publishing or reopening it;
    persisted request bytes are decoded independently at their owning read.
    """

    stage_result: StageResult
    canonical_request_bytes: bytes = field(init=False, repr=False)
    interaction_request_id: str
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    interaction_context_sha256: str
    terminal_record_sha256: str
    sample_id: str
    slide_id: str
    project_entry_id: str | None
    validation_domain_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    compatibility_signature_sha256: str

    def __post_init__(self) -> None:
        """Validate request identity, compact persistence limits and context evidence."""

        result = self.stage_result
        _validate_compact_stage_result(result)
        if result.status is not StageStatus.NEEDS_INTERACTION:
            raise BatchStateError("interaction request must be needs_interaction")
        mapping = result.to_mapping()
        payload = _canonical_bytes(mapping)
        request_id = _batch_request_id(self.interaction_request_id)
        node_id = _safe_segment(self.node_id, "interaction request node_id")
        attempt_id = _safe_segment(self.attempt_id, "interaction request attempt_id")
        authorization = _sha256(
            self.attempt_authorization_sha256,
            "interaction request attempt authorization",
        )
        context = _sha256(
            self.interaction_context_sha256,
            "interaction request context",
        )
        terminal = _sha256(
            self.terminal_record_sha256,
            "interaction request terminal record",
        )
        sample_id = _safe_segment(self.sample_id, "interaction request sample_id")
        slide_id = _text(self.slide_id, "interaction request slide_id")
        project_entry_id = (
            None
            if self.project_entry_id is None
            else _text(self.project_entry_id, "interaction request project_entry_id")
        )
        validation_domain_id = _text(
            self.validation_domain_id,
            "interaction request validation_domain_id",
        )
        bindings = tuple(self.analysis_domain_bindings)
        if not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise BatchStateError("interaction request analysis-domain bindings must be typed")
        if len({_digest(item.to_mapping()) for item in bindings}) != len(bindings):
            raise BatchStateError("interaction request analysis-domain bindings must be unique")
        compatibility = _sha256(
            self.compatibility_signature_sha256,
            "interaction request compatibility signature",
        )
        resume_context = result.resume_context
        if (
            not isinstance(resume_context, Mapping)
            or type(resume_context.get("schema_version")) is not str
            or not str(resume_context["schema_version"]).strip()
            or resume_context["schema_version"] != str(resume_context["schema_version"]).strip()
        ):
            raise BatchStateError(
                "interaction request lacks owning-producer schema_version evidence"
            )
        if _digest(mapping["resume_context"]) != context:
            raise BatchStateError("interaction request context checksum disagrees")
        if result.slide_id != slide_id:
            raise BatchStateError("interaction request slide disagrees with publication")
        expected = batch_interaction_request_id(
            node_id=node_id,
            attempt_id=attempt_id,
            attempt_authorization_sha256=authorization,
            interaction_context_sha256=context,
        )
        if request_id != expected:
            raise BatchStateError("interaction request identity is not canonical")
        if terminal.encode("ascii") in payload:
            raise BatchStateError(
                "interaction request bytes cannot embed their terminal predecessor"
            )
        object.__setattr__(self, "canonical_request_bytes", payload)
        object.__setattr__(self, "interaction_request_id", request_id)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "attempt_authorization_sha256", authorization)
        object.__setattr__(self, "interaction_context_sha256", context)
        object.__setattr__(self, "terminal_record_sha256", terminal)
        object.__setattr__(self, "sample_id", sample_id)
        object.__setattr__(self, "slide_id", slide_id)
        object.__setattr__(self, "project_entry_id", project_entry_id)
        object.__setattr__(self, "validation_domain_id", validation_domain_id)
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "compatibility_signature_sha256", compatibility)


@dataclass(frozen=True, slots=True)
class BatchInteractionResponsePublication:
    canonical_response_bytes: bytes
    response_sha256: str
    node_id: str
    predecessor_terminal_record_sha256: str

    def __post_init__(self) -> None:
        if type(self.canonical_response_bytes) is not bytes:
            raise BatchStateError("interaction response publication requires exact bytes")
        object.__setattr__(self, "canonical_response_bytes", bytes(self.canonical_response_bytes))
        object.__setattr__(
            self,
            "response_sha256",
            _sha256(self.response_sha256, "response_sha256"),
        )
        object.__setattr__(self, "node_id", _safe_segment(self.node_id, "response node_id"))
        object.__setattr__(
            self,
            "predecessor_terminal_record_sha256",
            _sha256(
                self.predecessor_terminal_record_sha256,
                "response predecessor terminal digest",
            ),
        )


@dataclass(frozen=True, slots=True)
class BatchMaterialAuthorityUpdate:
    """Closed external-material snapshot accepted by the dedicated mutation path."""

    source_sha256_by_sample: Mapping[str, str]
    resolved_config_sha256_by_sample: Mapping[str, str]
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_materialization_state: Literal["reserved", "materialized"]
    project_qpproj_snapshot_sha256: str | None
    project_mutation_attestation_sha256: str | None
    project_catalogue_sha256: str | None
    sample_project_entry_id_by_sample: Mapping[str, str | None]
    sample_project_entry_projection_sha256_by_sample: Mapping[str, str | None]
    accepted_domain_revision_sha256_by_id: Mapping[str, str]
    domain_set_sha256_by_sample: Mapping[str, str]
    checkpoint_descriptor_sha256_by_node: Mapping[str, str]
    external_artifact_ref_sha256_by_key: Mapping[str, str]

    def __post_init__(self) -> None:
        """Validate the BatchMaterialAuthorityUpdate fields when the record is constructed.

        The checks cover ``project_materialization_state``,
        ``sample_project_entry_id_by_sample``, ``project_identity_sha256``,
        ``project_authority_head_sha256``, ``project_qpproj_snapshot_sha256``,
        ``sample_project_entry_projection_sha256_by_sample``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        for field_name in (
            "source_sha256_by_sample",
            "resolved_config_sha256_by_sample",
            "accepted_domain_revision_sha256_by_id",
            "domain_set_sha256_by_sample",
            "checkpoint_descriptor_sha256_by_node",
            "external_artifact_ref_sha256_by_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_map(getattr(self, field_name), field_name, values_are_sha=True),
            )
        object.__setattr__(
            self,
            "sample_project_entry_id_by_sample",
            _optional_string_map(
                self.sample_project_entry_id_by_sample,
                "sample_project_entry_id_by_sample",
            ),
        )
        object.__setattr__(
            self,
            "project_identity_sha256",
            _sha256(self.project_identity_sha256, "project_identity_sha256"),
        )
        object.__setattr__(
            self,
            "project_authority_head_sha256",
            _sha256(self.project_authority_head_sha256, "project_authority_head_sha256"),
        )
        if self.project_materialization_state not in {"reserved", "materialized"}:
            raise BatchStateError("project materialization state is invalid")
        for field_name in (
            "project_qpproj_snapshot_sha256",
            "project_mutation_attestation_sha256",
            "project_catalogue_sha256",
        ):
            object.__setattr__(
                self, field_name, _optional_sha256(getattr(self, field_name), field_name)
            )
        if self.project_materialization_state == "reserved" and any(
            getattr(self, name) is not None
            for name in (
                "project_qpproj_snapshot_sha256",
                "project_mutation_attestation_sha256",
                "project_catalogue_sha256",
            )
        ):
            raise BatchStateError("reserved project authority cannot carry materialized evidence")
        if (
            self.project_materialization_state == "materialized"
            and self.project_qpproj_snapshot_sha256 is None
        ):
            raise BatchStateError("materialized project authority requires qpproj snapshot")
        object.__setattr__(
            self,
            "sample_project_entry_projection_sha256_by_sample",
            _optional_sha256_map(
                self.sample_project_entry_projection_sha256_by_sample,
                "sample_project_entry_projection_sha256_by_sample",
            ),
        )
        if any(key != value for key, value in self.external_artifact_ref_sha256_by_key.items()):
            raise BatchStateError("external artifact authority must be keyed by projection digest")


@dataclass(frozen=True, slots=True, init=False)
class BatchProjectMaterialSuccessor:
    expected_state_head_sha256: str
    execution_context_sha256: str
    prepared_result_binding_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_resolution_evidence_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    artifact_inventory_sha256: str
    project_fencing_token_sha256: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_materialization_state: Literal["materialized"]
    project_qpproj_snapshot_sha256: str
    project_mutation_attestation_sha256: str
    project_catalogue_sha256: str
    sample_id: str
    project_entry_id: str
    project_entry_projection_sha256: str
    _issuer: object

    @classmethod
    def _issue(cls, **values: object) -> "BatchProjectMaterialSuccessor":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class BatchProjectStageMaterialSuccessor:
    """Opaque exact bridge between project attestation and grouped batch CAS."""

    expected_state_head_sha256: str
    execution_context_sha256s: tuple[str, ...]
    prepared_result_binding_sha256s: tuple[str, ...]
    disposition: Literal["observed_mutation", "unchanged_project"]
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_resolution_evidence_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    artifact_inventory_sha256: str
    project_fencing_token_sha256: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_qpproj_snapshot_sha256: str
    project_mutation_attestation_sha256: str | None
    project_catalogue_sha256: str
    project_stage_evidence_sha256: str
    _issuer: object
    _project_lease: object

    @classmethod
    def _issue(cls, **values: object) -> "BatchProjectStageMaterialSuccessor":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True, init=False, repr=False)
class CurrentNativeFitSetAuthority:
    """Current state-selected native fits; never scientific acceptance."""

    checkpoint_set: object
    runtime_authorities: tuple[object, ...]
    state_head_sha256: str
    active_plan_sha256: str
    _current_execution: BatchCurrentExecutionAuthority
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("current native fit-set authorities are store-issued")

    @classmethod
    def _issue(cls, **values: object) -> "CurrentNativeFitSetAuthority":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True, init=False, repr=False)
class BatchNativeFitCheckpointSuccessor:
    """One-use prepared state transition for one authenticated native fit."""

    expected_state_head_sha256: str
    execution_context_sha256: str
    checkpoint_set: object
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_qpproj_snapshot_sha256: str
    project_mutation_attestation_sha256: str
    project_catalogue_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_resolution_evidence_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    artifact_inventory_sha256: str
    project_fencing_token_sha256: str
    _project_commit: object
    _registry_authority: object
    _registry: object
    _context: BatchNodeExecutionContext
    _current_parent: object
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("native fit checkpoint successors are store-issued")

    @classmethod
    def _issue(cls, **values: object) -> "BatchNativeFitCheckpointSuccessor":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True)
class BatchInitializationJournal:
    schema_name: Literal["mif-batch-initialization-journal"]
    schema_version: Literal[1]
    batch_id: str
    original_manifest_sha256: str
    normalized_manifest_sha256: str
    project_designation_request_sha256: str
    project_mutation_artifact_inventory_sha256: str
    project_mutation_acquisition_record_sha256: str
    journal_sha256: str

    def __post_init__(self) -> None:
        if self.schema_name != "mif-batch-initialization-journal" or self.schema_version != 1:
            raise BatchStateError("initialization journal schema is unsupported")
        _safe_segment(self.batch_id, "initialization batch_id")
        for name in (
            "original_manifest_sha256",
            "normalized_manifest_sha256",
            "project_designation_request_sha256",
            "project_mutation_artifact_inventory_sha256",
            "project_mutation_acquisition_record_sha256",
        ):
            _sha256(getattr(self, name), name)
        if _sha256(self.journal_sha256, "journal_sha256") != _digest(self._unsigned_mapping()):
            raise BatchStateError("initialization journal checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "journal_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "journal_sha256": self.journal_sha256}

    @classmethod
    def create(cls, **values: object) -> "BatchInitializationJournal":
        unsigned = {
            "schema_name": "mif-batch-initialization-journal",
            "schema_version": 1,
            **values,
        }
        return cls(**unsigned, journal_sha256=_digest(unsigned))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchInitializationJournal":
        return cls(
            **dict(
                _closed_mapping(
                    value,
                    fields=frozenset(cls.__dataclass_fields__),
                    field="initialization journal",
                )
            )
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, init=False)
class PreparedBatchInitialization:
    journal: BatchInitializationJournal
    validated_manifest: ValidatedBatchManifest
    designation_request: object
    artifact_inventory: object
    acquisition_record: object
    _issuer: object

    @classmethod
    def _issue(cls, **values: object) -> "PreparedBatchInitialization":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True)
class BatchProjectControlContext:
    """One exact state/project authority pair retained for pause or resume."""

    head: BatchStateHead
    project_lease: object
    project_processes: object

    def __post_init__(self) -> None:
        if not isinstance(self.head, BatchStateHead) or not callable(
            getattr(self.project_processes, "assert_all_drained", None)
        ):
            raise BatchStateError("project control context authority is invalid")


@dataclass(frozen=True, slots=True, init=False)
class PreStateInitializationRecovery:
    prepared: PreparedBatchInitialization
    project_discovery: object | None
    project_lease: object | None
    designation: object | None
    _issuer: object

    @classmethod
    def _issue(cls, **values: object) -> "PreStateInitializationRecovery":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class _BatchProjectDurabilityCapability:
    _store: "BatchStateStore"

    @classmethod
    def _issue(cls, store: "BatchStateStore") -> "_BatchProjectDurabilityCapability":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_store", store)
        return instance

    def _catalogue_predecessor_saved_states(self, *, project_lease, durability_authority):
        """Read accepted entry states for this exact live catalogue mutation."""
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationDurabilityAuthority

        if (not isinstance(durability_authority, ProjectMutationDurabilityAuthority)
                or durability_authority._registered_store_issuer is not self
                or durability_authority._provider_issuer is not getattr(
                    self._store._project_mutation_provider, "_provider_issuer", None)
                or getattr(project_lease, "_completion_bridge", None) is None
                or project_lease._completion_bridge.durability_authority is not durability_authority):
            raise BatchStateError("catalogue saved-state authority is foreign")
        with self._store._lease_operation_for_binding(
                durability_authority.workspace_lease_binding_sha256) as use:
            head, _ = self._store._read_canonical_state_head()
            return self._store.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=head.sha256, lease=use._controller.lease,
                _catalogue_lease=project_lease)

    def _validate_project_completion(
        self,
        *,
        receipt: object,
        challenge: object,
        binding: object,
        durability_authority: object,
    ) -> object:
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationCompletionBinding,
            ProjectMutationCompletionChallenge,
            ProjectMutationDurabilityAuthority,
            ProjectMutationDurabilityValidationResponse,
        )

        if (
            not isinstance(receipt, BatchProjectMutationDurabilityReceipt)
            or receipt._issuer is not self
            or receipt._consumed
            or not isinstance(challenge, ProjectMutationCompletionChallenge)
            or not isinstance(binding, ProjectMutationCompletionBinding)
            or not isinstance(durability_authority, ProjectMutationDurabilityAuthority)
            or durability_authority._registered_store_issuer is not self
            or challenge._provider_issuer is not durability_authority._provider_issuer
        ):
            raise BatchStateError("project completion capability is foreign")
        self._store._assert_live_workspace_lease_binding(receipt.workspace_lease_binding_sha256)
        expected = (
            binding.acquisition_record_sha256,
            binding.acquisition_session_challenge_sha256,
            binding.artifact_resolution_evidence_sha256,
            binding.mutation_request_sha256,
            binding.mutation_authorization_sha256,
            binding.artifact_inventory_sha256,
            binding.project_identity_sha256,
            binding.fencing_token_sha256,
            binding.project_authority_head_sha256,
            binding.mutation_attestation_sha256,
        )
        observed = (
            receipt.acquisition_record_sha256,
            receipt.acquisition_session_challenge_sha256,
            receipt.artifact_resolution_evidence_sha256,
            receipt.mutation_request_sha256,
            receipt.mutation_authorization_sha256,
            receipt.artifact_inventory_sha256,
            receipt.project_identity_sha256,
            receipt.project_fencing_token_sha256,
            receipt.project_authority_head_sha256,
            receipt.mutation_attestation_sha256,
        )
        if (
            expected != observed
            or receipt.workspace_lease_binding_sha256
            != durability_authority.workspace_lease_binding_sha256
            or challenge.acquisition_record_sha256 != binding.acquisition_record_sha256
            or challenge.acquisition_session_challenge_sha256
            != binding.acquisition_session_challenge_sha256
            or challenge.project_identity_sha256 != binding.project_identity_sha256
            or challenge.fencing_token_sha256 != binding.fencing_token_sha256
        ):
            raise BatchStateError("project completion receipt binding is stale")
        object.__setattr__(receipt, "_consumed", True)
        return ProjectMutationDurabilityValidationResponse._issue(
            acquisition_record_sha256=binding.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(binding.acquisition_session_challenge_sha256),
            project_identity_sha256=binding.project_identity_sha256,
            fencing_token_sha256=binding.fencing_token_sha256,
            challenge_nonce_sha256=challenge.challenge_nonce_sha256,
            completion_binding_sha256=binding.completion_binding_sha256,
            durability_authority_binding_sha256=(
                durability_authority.durability_authority_binding_sha256
            ),
            receipt_sha256=receipt.receipt_sha256,
            workspace_lease_binding_sha256=(receipt.workspace_lease_binding_sha256),
            _challenge=challenge,
            _registered_store_issuer=self,
            _provider_issuer=challenge._provider_issuer,
        )

    def _authenticate_project_stage_catalogue(
        self,
        *,
        mutation: object,
        project_lease: object,
        durability_authority: object,
    ) -> object:
        """Resolve the accepted image-entry index for a completed native operation.

        We compare the index identity, selected entry projections and project identity
        with the current analysis. Native saves advance the project revision while
        retaining that imported-entry index. Its stored project snapshot records the
        import that created the entries. The live lease and mutation predecessor below
        establish the current saved-project revision used by this operation.
        """

        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationDurabilityAuthority,
            _ProjectStageCatalogueAuthority,
        )
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceRecord,
        )
        from .batch_project import BatchProjectCatalogue

        if (
            type(mutation) is not ProjectStageMutationEvidenceRecord
            or not isinstance(durability_authority, ProjectMutationDurabilityAuthority)
            or durability_authority._registered_store_issuer is not self
            or durability_authority._provider_issuer
            is not getattr(
                self._store._project_mutation_provider,
                "_provider_issuer",
                None,
            )
            or getattr(project_lease, "_provider", None)
            is not self._store._project_mutation_provider
            or not getattr(project_lease, "is_live", False)
        ):
            raise BatchStateError("project-stage catalogue authentication authority is foreign")
        binding = durability_authority.workspace_lease_binding_sha256
        with self._store._lease_operation_for_binding(binding):
            # During a repeated same-session commit, the project head may be
            # exactly one authenticated mutation ahead of the batch head.
            # The checks below bind that predecessor and exact attestation;
            # ordinary load_head() would reject this permitted intermediate state.
            head, _ = self._store._read_canonical_state_head()
            matching = tuple(
                lease
                for lease in self._store._active_leases.values()
                if self._store._workspace_lease_binding_sha256(lease) == binding
            )
            if len(matching) != 1:
                raise BatchStateError("project-stage catalogue lost its workspace lease")
            workspace_lease = matching[0]
            catalogue = self._store.project_catalogue_store(workspace_lease).load_current()
            project_head = getattr(project_lease, "current_authority_head", None)
            designation = getattr(project_lease, "designation", None)
            identity = getattr(designation, "identity", None)
            predecessor = getattr(designation, "authority_head", None)
            if (
                getattr(predecessor, "authority_head_sha256", None)
                != head.project_authority_head_sha256
                or getattr(predecessor, "project_identity_sha256", None)
                != head.project_identity_sha256
                or getattr(predecessor, "qpproj_snapshot_sha256", None)
                != head.project_qpproj_snapshot_sha256
                or getattr(predecessor, "materialization_state", None)
                != head.project_materialization_state
                or getattr(predecessor, "latest_mutation_attestation_sha256", None)
                != head.project_mutation_attestation_sha256
            ):
                raise BatchStateError(
                    "Project-stage catalogue predecessor differs from its live lease"
                )
            current_is_predecessor = (
                getattr(project_head, "authority_head_sha256", None)
                == head.project_authority_head_sha256
                and getattr(project_head, "qpproj_snapshot_sha256", None)
                == head.project_qpproj_snapshot_sha256
            )
            current_is_same_session_successor = (
                getattr(
                    project_head,
                    "predecessor_authority_head_sha256",
                    None,
                )
                == head.project_authority_head_sha256
                and getattr(
                    project_head,
                    "latest_mutation_attestation_sha256",
                    None,
                )
                is not None
            )
            if (
                type(catalogue) is not BatchProjectCatalogue
                or head.project_catalogue_sha256 is None
                or catalogue.catalogue_sha256 != head.project_catalogue_sha256
                or catalogue.project_identity_sha256 != head.project_identity_sha256
                or catalogue.project_uri != getattr(designation, "project_uri", None)
                or mutation.project_identity_sha256 != head.project_identity_sha256
                or mutation.predecessor_project_authority_head_sha256
                != head.project_authority_head_sha256
                or getattr(identity, "project_identity_sha256", None)
                != head.project_identity_sha256
                or not (current_is_predecessor or current_is_same_session_successor)
            ):
                raise BatchStateError("project-stage current catalogue authority changed")
            projections: list[str] = []
            for observation in mutation.target_observations:
                try:
                    entry = catalogue.resolve(observation.project_entry_id)
                except KeyError as exc:
                    raise BatchStateError(
                        "project-stage observation names a foreign catalogue entry"
                    ) from exc
                if (
                    entry.sample_id != observation.sample_id
                    or entry.slide_id != observation.slide_id
                ):
                    raise BatchStateError(
                        "project-stage observation differs from current catalogue"
                    )
                projections.append(entry.entry_projection_sha256)
            derived = tuple(sorted(projections))
            if (
                not derived
                or len(derived) != len(mutation.target_observations)
                or len(set(derived)) != len(derived)
            ):
                raise BatchStateError("project-stage selected catalogue projections are ambiguous")
            if current_is_same_session_successor:
                existing = project_lease.current_mutation_attestation()
                if (
                    existing is None
                    or existing.mutation_kind != "project_stage"
                    or existing.mutation_attestation_sha256
                    != project_head.latest_mutation_attestation_sha256
                    or existing.project_identity_sha256 != mutation.project_identity_sha256
                    or existing.mutation_request_sha256 != mutation.mutation_request_sha256
                    or existing.mutation_authorization_sha256
                    != mutation.mutation_authorization_sha256
                    or existing.predecessor_authority_head_sha256
                    != mutation.predecessor_project_authority_head_sha256
                    or existing.post_save_qpproj_snapshot_sha256
                    != project_head.qpproj_snapshot_sha256
                    or existing.workspace_catalogue_sha256 != catalogue.catalogue_sha256
                    or existing.project_entry_projection_set_sha256 != _digest(list(derived))
                    or existing.operation_evidence_sha256 != mutation.mutation_evidence_sha256
                ):
                    raise BatchStateError("project-stage same-session catalogue authority changed")
            return _ProjectStageCatalogueAuthority._issue(
                workspace_catalogue_sha256=catalogue.catalogue_sha256,
                project_entry_projection_sha256s=derived,
                project_stage_mutation_evidence_sha256=(mutation.mutation_evidence_sha256),
                target_observation_set_sha256=(mutation.target_observation_set_sha256),
                batch_state_head_sha256=head.sha256,
                workspace_lease_binding_sha256=binding,
                _registered_store_issuer=self,
                _provider_issuer=durability_authority._provider_issuer,
            )

    def _authenticate_project_acquisition(
        self,
        *,
        record: object,
        request: object,
        inventory: object,
        workspace_lease_binding_sha256: str,
    ) -> None:
        """Authenticate the exact live store-issued acquisition chain."""

        with self._store._runtime_state_operation():
            with self._store._lease_operation_for_binding(workspace_lease_binding_sha256):
                self._store._authenticate_project_acquisition_material(
                    record=record,
                    request=request,
                    inventory=inventory,
                    workspace_lease_binding_sha256=(workspace_lease_binding_sha256),
                )


@dataclass(frozen=True, slots=True, init=False)
class BatchProjectMutationDurabilityReceipt:
    durability_kind: Literal[
        "initial_state",
        "project_node",
        "project_stage",
        "capsule_import",
        "classifier_fit",
        "project_control",
    ]
    workspace_lease_binding_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_resolution_evidence_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    artifact_inventory_sha256: str
    project_identity_sha256: str
    project_fencing_token_sha256: str
    project_authority_head_sha256: str
    mutation_attestation_sha256: str | None
    terminal_attempt_record_sha256s: tuple[str, ...]
    committed_batch_state_head_sha256: str
    receipt_sha256: str
    _issuer: object
    _consumed: bool

    @classmethod
    def _issue(cls, *, issuer: object, **values: object) -> "BatchProjectMutationDurabilityReceipt":
        unsigned = dict(values)
        unsigned["terminal_attempt_record_sha256s"] = tuple(
            sorted(unsigned["terminal_attempt_record_sha256s"])
        )
        digest_material = {
            **unsigned,
            "terminal_attempt_record_sha256s": list(unsigned["terminal_attempt_record_sha256s"]),
        }
        instance = object.__new__(cls)
        for name, value in unsigned.items():
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "receipt_sha256", _digest(digest_material))
        object.__setattr__(instance, "_issuer", issuer)
        object.__setattr__(instance, "_consumed", False)
        return instance

    def _validate_project_completion(self, project_lease: object) -> bool:
        if self._consumed or self._issuer is None:
            return False
        expected = (
            self.acquisition_record_sha256,
            self.acquisition_session_challenge_sha256,
            self.artifact_resolution_evidence_sha256,
            self.mutation_request_sha256,
            self.mutation_authorization_sha256,
            self.artifact_inventory_sha256,
            self.project_identity_sha256,
            self.project_fencing_token_sha256,
            self.project_authority_head_sha256,
        )
        observed = (
            getattr(project_lease, "acquisition_record_sha256", None),
            getattr(project_lease, "acquisition_session_challenge_sha256", None),
            getattr(project_lease, "artifact_resolution_evidence_sha256", None),
            getattr(project_lease, "mutation_request_sha256", None),
            getattr(project_lease, "mutation_authorization_sha256", None),
            getattr(project_lease, "artifact_inventory_sha256", None),
            (
                getattr(
                    getattr(project_lease, "designation", None), "identity", None
                ).project_identity_sha256
                if getattr(getattr(project_lease, "designation", None), "identity", None)
                is not None
                else None
            ),
            getattr(project_lease, "fencing_token_sha256", None),
            getattr(
                getattr(project_lease, "current_authority_head", None),
                "authority_head_sha256",
                None,
            ),
        )
        provider = getattr(project_lease, "_provider", None)
        registered = getattr(provider, "_registered_state_store_issuers", set())
        if (
            expected != observed
            or id(self._issuer) not in registered
            or not getattr(project_lease, "is_live", False)
        ):
            return False
        object.__setattr__(self, "_consumed", True)
        return True


@dataclass(frozen=True, slots=True)
class NativeFitDurabilityCleanupOutcome:
    """Typed idempotent result for state-selected native-fit lease cleanup."""

    disposition: Literal["completed_now", "already_completed"]
    receipt: BatchProjectMutationDurabilityReceipt | None

    def __post_init__(self) -> None:
        if self.disposition not in {"completed_now", "already_completed"}:
            raise BatchStateError("native fit durability cleanup disposition is invalid")
        if (self.receipt is None) != (self.disposition == "already_completed"):
            raise BatchStateError("native fit durability cleanup receipt differs from disposition")


def _selected_native_fit_runtime_for_current_attestation(
    current_fit_set: CurrentNativeFitSetAuthority,
    *,
    current_mutation_attestation_sha256: str | None,
) -> object | None:
    """Select the fit whose mutation is still current through relocations."""

    entries = tuple(current_fit_set.checkpoint_set.entries)
    runtime_authorities = tuple(current_fit_set.runtime_authorities)
    entry_matches = tuple(
        entry
        for entry in entries
        if entry.mutation_attestation_sha256 == current_mutation_attestation_sha256
    )
    if not entry_matches:
        # A different current attestation is possible only after the selected
        # fit's project mutation completed and a later authenticated mutation
        # advanced the project head.
        return None
    if len(entry_matches) != 1:
        raise BatchStateError("native fit durability recovery has ambiguous current attestation")
    material_digest = entry_matches[0].runtime_fit_material_sha256
    runtime_matches = tuple(
        runtime
        for runtime in runtime_authorities
        if runtime.material_record.fit_material_sha256 == material_digest
    )
    if len(runtime_matches) != 1:
        raise BatchStateError("native fit durability recovery lacks its current runtime authority")
    return runtime_matches[0]


def _identity_contract_mapping(contract: ArtifactIdentityContract) -> dict[str, object]:
    return {
        "contract_id": contract.contract_id,
        "artifact_kind": contract.artifact_kind,
        "schema_name": contract.schema_name,
        "schema_version": contract.schema_version,
        "required_fields": list(contract.required_fields),
        "optional_fields": list(contract.optional_fields),
        "required_runtime_names": list(contract.required_runtime_names),
    }


def _identity_contract_from_mapping(value: object) -> ArtifactIdentityContract:
    mapping = _closed_mapping(
        value,
        fields=frozenset(
            {
                "contract_id",
                "artifact_kind",
                "schema_name",
                "schema_version",
                "required_fields",
                "optional_fields",
                "required_runtime_names",
            }
        ),
        field="artifact identity contract",
    )
    try:
        return ArtifactIdentityContract(
            contract_id=mapping["contract_id"],  # type: ignore[arg-type]
            artifact_kind=mapping["artifact_kind"],  # type: ignore[arg-type]
            schema_name=mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            required_fields=tuple(
                _sequence(mapping["required_fields"], "required_fields")
            ),  # type: ignore[arg-type]
            optional_fields=tuple(
                _sequence(mapping["optional_fields"], "optional_fields")
            ),  # type: ignore[arg-type]
            required_runtime_names=tuple(
                _sequence(
                    mapping["required_runtime_names"],
                    "required_runtime_names",
                )
            ),  # type: ignore[arg-type]
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchStateError("artifact identity contract is invalid") from exc


def _artifact_bundle(artifact: BatchArtifactRef) -> dict[str, object]:
    return {
        "artifact_ref": artifact.to_mapping(),
        "identity_contract": _identity_contract_mapping(artifact.identity_contract),
    }


def _artifact_from_bundle(value: object) -> BatchArtifactRef:
    bundle = _closed_mapping(
        value,
        fields=frozenset({"artifact_ref", "identity_contract"}),
        field="terminal output artifact",
    )
    contract_mapping = _closed_mapping(
        bundle["identity_contract"],
        fields=frozenset(
            {
                "contract_id",
                "artifact_kind",
                "schema_name",
                "schema_version",
                "required_fields",
                "optional_fields",
                "required_runtime_names",
            }
        ),
        field="artifact identity contract",
    )
    try:
        contract = ArtifactIdentityContract(
            contract_id=contract_mapping["contract_id"],  # type: ignore[arg-type]
            artifact_kind=contract_mapping["artifact_kind"],  # type: ignore[arg-type]
            schema_name=contract_mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=contract_mapping["schema_version"],  # type: ignore[arg-type]
            required_fields=tuple(
                _sequence(contract_mapping["required_fields"], "required_fields")
            ),
            optional_fields=tuple(
                _sequence(contract_mapping["optional_fields"], "optional_fields")
            ),
            required_runtime_names=tuple(
                _sequence(
                    contract_mapping["required_runtime_names"],
                    "required_runtime_names",
                )
            ),
        )
        return BatchArtifactRef.from_mapping(bundle["artifact_ref"], identity_contract=contract)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchStateError("terminal output artifact is invalid") from exc


def _requirement_bundle(
    requirement: StageArtifactRequirement,
) -> dict[str, object]:
    return {
        "requirement": requirement.to_mapping(),
        "identity_contract": _identity_contract_mapping(requirement.identity_contract),
    }


def _requirement_from_bundle(value: object) -> StageArtifactRequirement:
    bundle = _closed_mapping(
        value,
        fields=frozenset({"requirement", "identity_contract"}),
        field="stage artifact requirement bundle",
    )
    mapping = _closed_mapping(
        bundle["requirement"],
        fields=frozenset(
            {
                "artifact_kind",
                "schema_name",
                "schema_version",
                "scope",
                "owning_identity",
                "owning_identity_contract_sha256",
                "compatibility_signature_sha256",
                "producer_version",
                "runtime_versions",
                "configuration_bindings",
                "multiplicity",
                "origin",
                "producer_node_id",
                "producer_attempt_id",
                "producer_attempt_authorization_sha256",
            }
        ),
        field="stage artifact requirement",
    )
    contract = _identity_contract_from_mapping(bundle["identity_contract"])
    if mapping["owning_identity_contract_sha256"] != contract.contract_sha256:
        raise BatchStateError("stage requirement identity contract changed")
    try:
        return StageArtifactRequirement(
            artifact_kind=mapping["artifact_kind"],  # type: ignore[arg-type]
            schema_name=mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            scope=ArtifactScope.from_mapping(mapping["scope"]),
            owning_identity=_mapping(mapping["owning_identity"], "requirement owning_identity"),
            identity_contract=contract,
            compatibility_signature_sha256=mapping[
                "compatibility_signature_sha256"
            ],  # type: ignore[arg-type]
            producer_version=mapping["producer_version"],  # type: ignore[arg-type]
            runtime_versions=_mapping(mapping["runtime_versions"], "requirement runtime_versions"),
            configuration_bindings=tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in _sequence(
                    mapping["configuration_bindings"],
                    "requirement configuration_bindings",
                )
            ),
            multiplicity=ArtifactMultiplicity(mapping["multiplicity"]),
            origin=(None if mapping["origin"] is None else ArtifactOrigin(mapping["origin"])),
            producer_node_id=mapping["producer_node_id"],  # type: ignore[arg-type]
            producer_attempt_id=mapping["producer_attempt_id"],  # type: ignore[arg-type]
            producer_attempt_authorization_sha256=mapping[
                "producer_attempt_authorization_sha256"
            ],  # type: ignore[arg-type]
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchStateError("stage artifact requirement is invalid") from exc


def _equivalence_contract_mapping(
    contract: ArtifactEquivalenceContract,
) -> dict[str, object]:
    return {
        "contract_id": contract.contract_id,
        "byte_deterministic": contract.byte_deterministic,
        "canonical_identity_paths": [list(item) for item in contract.canonical_identity_paths],
        "stable_inventory_paths": [list(item) for item in contract.stable_inventory_paths],
        "scientific_value_paths": [list(item) for item in contract.scientific_value_paths],
        "qc_evidence_paths": [list(item) for item in contract.qc_evidence_paths],
    }


def _equivalence_contract_from_mapping(
    value: object,
) -> ArtifactEquivalenceContract:
    mapping = _closed_mapping(
        value,
        fields=frozenset(
            {
                "contract_id",
                "byte_deterministic",
                "canonical_identity_paths",
                "stable_inventory_paths",
                "scientific_value_paths",
                "qc_evidence_paths",
            }
        ),
        field="artifact equivalence contract",
    )

    def paths(name: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            tuple(_sequence(item, name))  # type: ignore[arg-type]
            for item in _sequence(mapping[name], name)
        )

    try:
        return ArtifactEquivalenceContract(
            contract_id=mapping["contract_id"],  # type: ignore[arg-type]
            byte_deterministic=mapping["byte_deterministic"],  # type: ignore[arg-type]
            canonical_identity_paths=paths("canonical_identity_paths"),
            stable_inventory_paths=paths("stable_inventory_paths"),
            scientific_value_paths=paths("scientific_value_paths"),
            qc_evidence_paths=paths("qc_evidence_paths"),
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchStateError("artifact equivalence contract is invalid") from exc


def _adapter_spec_bundle(spec: StageAdapterSpec) -> dict[str, object]:
    return {
        "node_kind": spec.node_kind,
        "node_version": spec.node_version,
        "required_inputs": [_requirement_bundle(item) for item in spec.required_inputs],
        "optional_inputs": [_requirement_bundle(item) for item in spec.optional_inputs],
        "declared_outputs": list(spec.declared_outputs),
        "output_identity_contracts": [
            _identity_contract_mapping(item) for item in spec.output_identity_contracts
        ],
        "compatibility_signature_id": spec.compatibility_signature_id,
        "equivalence_contract": _equivalence_contract_mapping(spec.equivalence_contract),
        "resource_kind": spec.resource_kind,
        "dispatch_mode": spec.dispatch_mode.value,
        "spec_sha256": spec.spec_sha256,
    }


def _adapter_spec_from_bundle(value: object) -> StageAdapterSpec:
    mapping = _closed_mapping(
        value,
        fields=frozenset(
            {
                "node_kind",
                "node_version",
                "required_inputs",
                "optional_inputs",
                "declared_outputs",
                "output_identity_contracts",
                "compatibility_signature_id",
                "equivalence_contract",
                "resource_kind",
                "dispatch_mode",
                "spec_sha256",
            }
        ),
        field="adapter spec material",
    )
    try:
        spec = StageAdapterSpec(
            node_kind=mapping["node_kind"],  # type: ignore[arg-type]
            node_version=mapping["node_version"],  # type: ignore[arg-type]
            required_inputs=tuple(
                _requirement_from_bundle(item)
                for item in _sequence(mapping["required_inputs"], "required_inputs")
            ),
            optional_inputs=tuple(
                _requirement_from_bundle(item)
                for item in _sequence(mapping["optional_inputs"], "optional_inputs")
            ),
            declared_outputs=tuple(
                _sequence(mapping["declared_outputs"], "declared_outputs")
            ),  # type: ignore[arg-type]
            output_identity_contracts=tuple(
                _identity_contract_from_mapping(item)
                for item in _sequence(
                    mapping["output_identity_contracts"],
                    "output_identity_contracts",
                )
            ),
            compatibility_signature_id=mapping[
                "compatibility_signature_id"
            ],  # type: ignore[arg-type]
            equivalence_contract=_equivalence_contract_from_mapping(
                mapping["equivalence_contract"]
            ),
            resource_kind=mapping["resource_kind"],  # type: ignore[arg-type]
            dispatch_mode=DispatchMode(mapping["dispatch_mode"]),
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchStateError("adapter spec material is invalid") from exc
    if spec.spec_sha256 != mapping["spec_sha256"]:
        raise BatchStateError("adapter spec material checksum changed")
    return spec


@dataclass(frozen=True, slots=True)
class _BatchExecutionContextBinding:
    authorization_sha256: str
    node_id: str
    adapter_spec_sha256: str
    input_artifact_ref_sha256s: tuple[str, ...]
    projection_target_sha256: str
    current_image_identity_sha256: str | None
    script_sha256: str | None
    project_identity_sha256: str | None
    project_authority_head_sha256: str | None
    project_qpproj_snapshot_sha256: str | None
    project_entry_projection_sha256: str | None
    compatibility_signature_sha256: str
    context_sha256: str

    def __post_init__(self) -> None:
        """Validate the _BatchExecutionContextBinding fields when the record is constructed.

        The checks cover ``context_sha256``, ``authorization_sha256``, ``node_id``,
        ``input_artifact_ref_sha256s``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        object.__setattr__(
            self,
            "authorization_sha256",
            _sha256(self.authorization_sha256, "context authorization_sha256"),
        )
        object.__setattr__(self, "node_id", _safe_segment(self.node_id, "context node_id"))
        for field_name in (
            "adapter_spec_sha256",
            "projection_target_sha256",
            "compatibility_signature_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _sha256(getattr(self, field_name), f"context {field_name}"),
            )
        for field_name in (
            "current_image_identity_sha256",
            "script_sha256",
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_qpproj_snapshot_sha256",
            "project_entry_projection_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_sha256(getattr(self, field_name), f"context {field_name}"),
            )
        inputs = tuple(
            sorted(
                _sha256(item, "context input artifact reference")
                for item in self.input_artifact_ref_sha256s
            )
        )
        if len(set(inputs)) != len(inputs):
            raise BatchStateError("execution context input references must be unique")
        object.__setattr__(self, "input_artifact_ref_sha256s", inputs)
        digest = _sha256(self.context_sha256, "execution context digest")
        if digest != _digest(self._unsigned_mapping()):
            raise BatchStateError("execution context binding checksum is not canonical")
        object.__setattr__(self, "context_sha256", digest)

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "authorization_sha256": self.authorization_sha256,
            "node_id": self.node_id,
            "adapter_spec_sha256": self.adapter_spec_sha256,
            "input_artifact_ref_sha256s": list(self.input_artifact_ref_sha256s),
            "projection_target_sha256": self.projection_target_sha256,
            "current_image_identity_sha256": self.current_image_identity_sha256,
            "script_sha256": self.script_sha256,
            "project_identity_sha256": self.project_identity_sha256,
            "project_authority_head_sha256": self.project_authority_head_sha256,
            "project_qpproj_snapshot_sha256": self.project_qpproj_snapshot_sha256,
            "project_entry_projection_sha256": self.project_entry_projection_sha256,
            "compatibility_signature_sha256": (self.compatibility_signature_sha256),
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "context_sha256": self.context_sha256}

    @classmethod
    def from_context(cls, context: BatchNodeExecutionContext) -> "_BatchExecutionContextBinding":
        if not isinstance(context, BatchNodeExecutionContext):
            raise BatchStateError("execution binding requires exact node context")
        unsigned = {
            "authorization_sha256": context.authorization.authorization_sha256,
            "node_id": context.node_id,
            "adapter_spec_sha256": context.adapter_spec.spec_sha256,
            "input_artifact_ref_sha256s": sorted(
                item.artifact_ref_sha256 for item in context.inputs
            ),
            "projection_target_sha256": context.projection_target_sha256,
            "current_image_identity_sha256": (context.current_image_identity_sha256),
            "script_sha256": context.script_sha256,
            "project_identity_sha256": context.project_identity_sha256,
            "project_authority_head_sha256": context.project_authority_head_sha256,
            "project_qpproj_snapshot_sha256": context.project_qpproj_snapshot_sha256,
            "project_entry_projection_sha256": context.project_entry_projection_sha256,
            "compatibility_signature_sha256": (context.compatibility_signature_sha256),
        }
        return cls(**unsigned, context_sha256=_digest(unsigned))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "_BatchExecutionContextBinding":
        """Decode the serialized fields into a validated _BatchExecutionContextBinding record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        expected = frozenset(
            {
                "authorization_sha256",
                "node_id",
                "adapter_spec_sha256",
                "input_artifact_ref_sha256s",
                "projection_target_sha256",
                "current_image_identity_sha256",
                "script_sha256",
                "project_identity_sha256",
                "project_authority_head_sha256",
                "project_qpproj_snapshot_sha256",
                "project_entry_projection_sha256",
                "compatibility_signature_sha256",
                "context_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=expected, field="execution context binding")
        return cls(
            authorization_sha256=mapping["authorization_sha256"],  # type: ignore[arg-type]
            node_id=mapping["node_id"],  # type: ignore[arg-type]
            adapter_spec_sha256=mapping["adapter_spec_sha256"],  # type: ignore[arg-type]
            input_artifact_ref_sha256s=tuple(
                _sequence(
                    mapping["input_artifact_ref_sha256s"],
                    "context input_artifact_ref_sha256s",
                )
            ),  # type: ignore[arg-type]
            projection_target_sha256=mapping["projection_target_sha256"],  # type: ignore[arg-type]
            current_image_identity_sha256=mapping["current_image_identity_sha256"],  # type: ignore[arg-type]
            script_sha256=mapping["script_sha256"],  # type: ignore[arg-type]
            project_identity_sha256=mapping["project_identity_sha256"],  # type: ignore[arg-type]
            project_authority_head_sha256=mapping["project_authority_head_sha256"],  # type: ignore[arg-type]
            project_qpproj_snapshot_sha256=mapping["project_qpproj_snapshot_sha256"],  # type: ignore[arg-type]
            project_entry_projection_sha256=mapping["project_entry_projection_sha256"],  # type: ignore[arg-type]
            compatibility_signature_sha256=mapping["compatibility_signature_sha256"],  # type: ignore[arg-type]
            context_sha256=mapping["context_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BatchNodeAttempt:
    authorization_sha256: str
    attempt_id: str
    node_id: str
    stage_result: StageResult
    output_artifacts: tuple[BatchArtifactRef, ...]
    execution_exchange_sha256: str | None
    execution_context_binding: _BatchExecutionContextBinding
    execution_context_sha256: str
    terminal_record_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchNodeAttempt fields when the record is constructed.

        The checks cover ``authorization_sha256``, ``attempt_id``, ``node_id``,
        ``output_artifacts``, ``execution_exchange_sha256``, ``execution_context_sha256``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        try:
            assert_current_writer(ATTEMPT_SCHEMA_NAME, str(ATTEMPT_SCHEMA_VERSION))
        except ValueError as exc:
            raise BatchStateError("node attempt schema is not current") from exc
        authorization = _sha256(self.authorization_sha256, "authorization_sha256")
        attempt_id = _safe_segment(self.attempt_id, "attempt_id")
        node_id = _safe_segment(self.node_id, "node_id")
        if not isinstance(self.stage_result, StageResult):
            raise BatchStateError("node attempt requires an exact StageResult")
        stage_result = self.stage_result
        _validate_compact_stage_result(stage_result)
        outputs = tuple(self.output_artifacts)
        if not all(isinstance(item, BatchArtifactRef) for item in outputs):
            raise BatchStateError("node attempt outputs must be typed artifact references")
        if len({item.artifact_ref_sha256 for item in outputs}) != len(outputs):
            raise BatchStateError("node attempt output references must be unique")
        for output in outputs:
            try:
                validate_artifact_ref(output)
            except BatchArtifactError as exc:
                raise BatchStateError("node attempt output reference is invalid") from exc
            if (
                output.origin is not ArtifactOrigin.INTERNAL_NODE
                or output.producer_node_id != node_id
                or output.producer_attempt_id != attempt_id
                or output.producer_attempt_authorization_sha256 != authorization
            ):
                raise BatchStateError("node attempt output has foreign producer identity")
        exchange = _optional_sha256(self.execution_exchange_sha256, "execution_exchange_sha256")
        if type(self.execution_context_binding) is not _BatchExecutionContextBinding:
            raise BatchStateError("node attempt lacks typed execution binding")
        context_binding = self.execution_context_binding
        context_digest = _sha256(self.execution_context_sha256, "execution_context_sha256")
        if (
            context_binding.context_sha256 != context_digest
            or context_binding.authorization_sha256 != authorization
            or context_binding.node_id != node_id
        ):
            raise BatchStateError("node attempt execution binding is foreign to its authority")
        object.__setattr__(self, "authorization_sha256", authorization)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "stage_result", stage_result)
        object.__setattr__(
            self,
            "output_artifacts",
            tuple(sorted(outputs, key=lambda item: item.artifact_ref_sha256)),
        )
        object.__setattr__(self, "execution_exchange_sha256", exchange)
        object.__setattr__(self, "execution_context_binding", context_binding)
        object.__setattr__(self, "execution_context_sha256", context_digest)
        terminal = _sha256(self.terminal_record_sha256, "terminal_record_sha256")
        if terminal != _digest(self._unsigned_mapping()):
            raise BatchStateError("terminal attempt checksum does not match canonical bytes")
        if any(
            terminal in _canonical_bytes(item.to_mapping()).decode("utf-8")
            for item in self.output_artifacts
        ):
            raise BatchStateError("terminal digest cannot be embedded in its own outputs")
        object.__setattr__(self, "terminal_record_sha256", terminal)

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": ATTEMPT_SCHEMA_NAME,
            "schema_version": ATTEMPT_SCHEMA_VERSION,
            "authorization_sha256": self.authorization_sha256,
            "attempt_id": self.attempt_id,
            "node_id": self.node_id,
            "stage_result": self.stage_result.to_mapping(),
            "output_artifacts": [_artifact_bundle(item) for item in self.output_artifacts],
            "execution_exchange_sha256": self.execution_exchange_sha256,
            "execution_context_binding": self.execution_context_binding.to_mapping(),
            "execution_context_sha256": self.execution_context_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        authorization: BatchAttemptAuthorization,
        stage_result: StageResult,
        output_artifacts: tuple[BatchArtifactRef, ...],
        execution_exchange_sha256: str | None,
        execution_context_binding: _BatchExecutionContextBinding,
    ) -> "BatchNodeAttempt":
        values = {
            "authorization_sha256": authorization.authorization_sha256,
            "attempt_id": authorization.attempt_id,
            "node_id": authorization.node_id,
            "stage_result": stage_result,
            "output_artifacts": output_artifacts,
            "execution_exchange_sha256": execution_exchange_sha256,
            "execution_context_binding": execution_context_binding,
            "execution_context_sha256": execution_context_binding.context_sha256,
        }
        canonical_outputs = tuple(
            sorted(output_artifacts, key=lambda item: item.artifact_ref_sha256)
        )
        material = {
            "schema_name": ATTEMPT_SCHEMA_NAME,
            "schema_version": ATTEMPT_SCHEMA_VERSION,
            "authorization_sha256": authorization.authorization_sha256,
            "attempt_id": authorization.attempt_id,
            "node_id": authorization.node_id,
            "stage_result": stage_result.to_mapping(),
            "output_artifacts": [_artifact_bundle(item) for item in canonical_outputs],
            "execution_exchange_sha256": execution_exchange_sha256,
            "execution_context_binding": execution_context_binding.to_mapping(),
            "execution_context_sha256": execution_context_binding.context_sha256,
        }
        return cls(**values, terminal_record_sha256=_digest(material))

    @classmethod
    def from_mapping(cls, value: object) -> "BatchNodeAttempt":
        """Decode the serialized fields into a validated BatchNodeAttempt record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        expected = frozenset(
            {
                "schema_name",
                "schema_version",
                "authorization_sha256",
                "attempt_id",
                "node_id",
                "stage_result",
                "output_artifacts",
                "execution_exchange_sha256",
                "execution_context_binding",
                "execution_context_sha256",
                "terminal_record_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=expected, field="batch node attempt")
        if (mapping["schema_name"], mapping["schema_version"]) != (
            ATTEMPT_SCHEMA_NAME,
            ATTEMPT_SCHEMA_VERSION,
        ):
            raise BatchStateError("node attempt uses an unknown schema")
        try:
            result = StageResult.from_mapping(mapping["stage_result"])
        except ROIAnalysisError as exc:
            raise BatchStateError("node attempt StageResult is invalid") from exc
        return cls(
            authorization_sha256=mapping["authorization_sha256"],  # type: ignore[arg-type]
            attempt_id=mapping["attempt_id"],  # type: ignore[arg-type]
            node_id=mapping["node_id"],  # type: ignore[arg-type]
            stage_result=result,
            output_artifacts=tuple(
                _artifact_from_bundle(item)
                for item in _sequence(mapping["output_artifacts"], "output_artifacts")
            ),
            execution_exchange_sha256=mapping["execution_exchange_sha256"],  # type: ignore[arg-type]
            execution_context_binding=_BatchExecutionContextBinding.from_mapping(
                mapping["execution_context_binding"]
            ),
            execution_context_sha256=mapping["execution_context_sha256"],  # type: ignore[arg-type]
            terminal_record_sha256=mapping["terminal_record_sha256"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "terminal_record_sha256": self.terminal_record_sha256,
        }


def _execution_context_sha256(context: BatchNodeExecutionContext) -> str:
    return _BatchExecutionContextBinding.from_context(context).context_sha256


@dataclass(frozen=True, slots=True)
class BatchRawNodeOutcome:
    """Typed adapter outcome that carries no state-store mutation authority."""

    context: BatchNodeExecutionContext
    stage_result: StageResult
    execution_result: BatchNodeExecutionResult
    execution_exchange_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.context, BatchNodeExecutionContext):
            raise BatchStateError("raw outcome requires exact execution context")
        if not isinstance(self.stage_result, StageResult):
            raise BatchStateError("raw outcome requires exact StageResult")
        stage_result = self.stage_result
        _validate_compact_stage_result(stage_result)
        if not isinstance(self.execution_result, BatchNodeExecutionResult):
            raise BatchStateError("raw outcome requires exact BatchNodeExecutionResult")
        if self.execution_result.authorization_sha256 != (
            self.context.authorization.authorization_sha256
        ):
            raise BatchStateError("raw outcome execution result has foreign authorization")
        if stage_result.stage != self.context.adapter_spec.node_kind:
            raise BatchStateError("raw outcome StageResult names a foreign stage")
        object.__setattr__(self, "stage_result", stage_result)
        object.__setattr__(
            self,
            "execution_exchange_sha256",
            _optional_sha256(self.execution_exchange_sha256, "execution_exchange_sha256"),
        )


@dataclass(frozen=True, slots=True, init=False)
class BatchPreparedNodeResult:
    """Exact pre-commit executor outcome consumed by the one-shot committer."""

    stage_result: StageResult
    execution_result: BatchNodeExecutionResult
    execution_exchange_sha256: str | None
    execution_context_binding: _BatchExecutionContextBinding
    execution_context_sha256: str
    _issuer: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchStateError("prepared node results are issued only by BatchStateStore")

    @classmethod
    def _issue(
        cls,
        *,
        stage_result: StageResult,
        execution_result: BatchNodeExecutionResult,
        execution_exchange_sha256: str | None,
        execution_context_binding: _BatchExecutionContextBinding,
        issuer: object,
    ) -> "BatchPreparedNodeResult":
        if not isinstance(stage_result, StageResult):
            raise BatchStateError("prepared result requires StageResult")
        if not isinstance(execution_result, BatchNodeExecutionResult):
            raise BatchStateError("prepared result requires BatchNodeExecutionResult")
        if type(execution_context_binding) is not _BatchExecutionContextBinding:
            raise BatchStateError("prepared result requires an exact execution binding")
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "stage_result", stage_result)
        object.__setattr__(prepared, "execution_result", execution_result)
        object.__setattr__(
            prepared,
            "execution_exchange_sha256",
            _optional_sha256(execution_exchange_sha256, "execution_exchange_sha256"),
        )
        object.__setattr__(
            prepared,
            "execution_context_binding",
            execution_context_binding,
        )
        object.__setattr__(
            prepared,
            "execution_context_sha256",
            execution_context_binding.context_sha256,
        )
        object.__setattr__(prepared, "_issuer", issuer)
        return prepared


@dataclass(frozen=True, slots=True)
class ScheduledNodeResult:
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    stage_result: StageResult
    terminal_attempt_record_sha256: str
    committed_state_head_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _safe_segment(self.node_id, "node_id"))
        object.__setattr__(self, "attempt_id", _safe_segment(self.attempt_id, "attempt_id"))
        object.__setattr__(
            self,
            "attempt_authorization_sha256",
            _sha256(
                self.attempt_authorization_sha256,
                "attempt_authorization_sha256",
            ),
        )
        if not isinstance(self.stage_result, StageResult):
            raise BatchStateError("scheduled result requires StageResult")
        object.__setattr__(
            self,
            "terminal_attempt_record_sha256",
            _sha256(
                self.terminal_attempt_record_sha256,
                "terminal_attempt_record_sha256",
            ),
        )
        object.__setattr__(
            self,
            "committed_state_head_sha256",
            _sha256(
                self.committed_state_head_sha256,
                "committed_state_head_sha256",
            ),
        )


@dataclass(frozen=True, slots=True)
class BatchPlanRevisionResult:
    head: BatchStateHead
    reusable_node_ids: tuple[str, ...]
    invalidated_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.head, BatchStateHead):
            raise BatchStateError("plan revision result requires a state head")
        reusable = tuple(
            sorted(_safe_segment(item, "reusable node ID") for item in self.reusable_node_ids)
        )
        invalidated = tuple(
            sorted(_safe_segment(item, "invalidated node ID") for item in self.invalidated_node_ids)
        )
        if set(reusable) & set(invalidated):
            raise BatchStateError("reusable and invalidated node sets must be disjoint")
        object.__setattr__(self, "reusable_node_ids", reusable)
        object.__setattr__(self, "invalidated_node_ids", invalidated)


@dataclass(frozen=True, slots=True)
class _PlanSnapshot:
    plan_sha256: str
    predecessor_plan_sha256: str | None
    workspace_rebinding_sha256: str | None
    revision: int
    batch_id: str
    normalized_manifest_sha256: str
    node_by_id: Mapping[str, Mapping[str, object]]
    edges: tuple[Mapping[str, object], ...]
    external_input_sha256s: tuple[str, ...]
    supervised_cutoff_selections: tuple[SupervisedCutoffSelection, ...] = ()


def _plan_supervised_cutoffs(mapping):
    if "supervised_cutoff_selections" not in mapping:
        return ()
    try:
        selections = tuple(SupervisedCutoffSelection.from_mapping(item) for item in
            _sequence(mapping["supervised_cutoff_selections"], "supervised cutoff selections"))
    except (TypeError, ValueError) as exc:
        raise BatchStateError("The saved assignment cutoff selection is invalid.") from exc
    if (not selections or selections != tuple(sorted(selections, key=lambda item: item.sort_key))
            or len({item.sort_key for item in selections}) != len(selections)):
        raise BatchStateError("Saved assignment cutoff selections must be unique and canonical.")
    return selections


def _pending_supervised_prediction(terminal):
    context = terminal.stage_result.resume_context
    return (terminal.stage_result.status is StageStatus.NEEDS_INTERACTION
        and not terminal.output_artifacts and isinstance(context, Mapping)
        and context.get("schema_version") == "supervised-phenotyping-interaction/v1"
        and context.get("phase") == "prediction_review"
        and isinstance(context.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", context["sha256"]) is not None
        and type(context.get("byte_count")) is int and context["byte_count"] > 0)


def _plan_runtime_contract_material(plan: BatchPlan) -> dict[str, object]:
    contracts: dict[str, ArtifactIdentityContract] = {}
    for artifact in plan.external_inputs:
        contracts[artifact.identity_contract.contract_sha256] = artifact.identity_contract
    for edge in plan.edges:
        contract = edge.requirement.identity_contract
        contracts[contract.contract_sha256] = contract
    return {
        "schema_name": _PLAN_RUNTIME_CONTRACT_SCHEMA_NAME,
        "schema_version": _PLAN_RUNTIME_CONTRACT_SCHEMA_VERSION,
        "plan_sha256": plan.plan_sha256,
        "identity_contracts": [
            _identity_contract_mapping(contracts[digest]) for digest in sorted(contracts)
        ],
    }


def _plan_contract_catalog(
    value: object, plan_sha256: str
) -> Mapping[str, ArtifactIdentityContract]:
    mapping = _closed_mapping(
        value,
        fields=frozenset({"schema_name", "schema_version", "plan_sha256", "identity_contracts"}),
        field="plan runtime contract material",
    )
    if (
        mapping["schema_name"] != _PLAN_RUNTIME_CONTRACT_SCHEMA_NAME
        or mapping["schema_version"] != _PLAN_RUNTIME_CONTRACT_SCHEMA_VERSION
        or mapping["plan_sha256"] != plan_sha256
    ):
        raise BatchStateError("plan runtime contract material is foreign")
    contracts = tuple(
        _identity_contract_from_mapping(item)
        for item in _sequence(mapping["identity_contracts"], "plan identity contracts")
    )
    by_digest = {item.contract_sha256: item for item in contracts}
    if len(by_digest) != len(contracts) or tuple(by_digest) != tuple(sorted(by_digest)):
        raise BatchStateError("plan identity contracts are not canonical")
    return MappingProxyType(by_digest)


def _plan_node_from_mapping(value: object) -> BatchNode:
    from .batch_project import PairedMovingReferenceAuthority

    mapping = _validate_node_mapping(value)
    secondary = mapping.get("secondary_target")
    section = mapping.get("section_target")
    clustering = mapping.get("clustering_scope")
    try:
        return BatchNode(
            node_id=mapping["node_id"],  # type: ignore[arg-type]
            kind=mapping["kind"],  # type: ignore[arg-type]
            version=mapping["version"],  # type: ignore[arg-type]
            batch_id=mapping["batch_id"],  # type: ignore[arg-type]
            scope=NodeScope(mapping["scope"]),
            validation_domain_id=mapping["validation_domain_id"],  # type: ignore[arg-type]
            analysis_domain_bindings=tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in _sequence(
                    mapping["analysis_domain_bindings"],
                    "plan node analysis_domain_bindings",
                )
            ),
            participant_ids=tuple(
                _sequence(mapping["participant_ids"], "plan node participant_ids")
            ),  # type: ignore[arg-type]
            project_entry_ids=tuple(
                _sequence(mapping["project_entry_ids"], "plan node project_entry_ids")
            ),  # type: ignore[arg-type]
            configuration_bindings=tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in _sequence(
                    mapping["configuration_bindings"],
                    "plan node configuration_bindings",
                )
            ),
            dependency_ids=tuple(
                _sequence(mapping["dependency_ids"], "plan node dependency_ids")
            ),  # type: ignore[arg-type]
            input_artifact_ids=tuple(
                _sequence(mapping["input_artifact_ids"], "plan node input_artifact_ids")
            ),  # type: ignore[arg-type]
            input_edge_material_sha256s=tuple(
                _sequence(
                    mapping["input_edge_material_sha256s"],
                    "plan node input_edge_material_sha256s",
                )
            ),  # type: ignore[arg-type]
            declared_output_kinds=tuple(
                _sequence(
                    mapping["declared_output_kinds"],
                    "plan node declared_output_kinds",
                )
            ),  # type: ignore[arg-type]
            resource=ResourceKind(mapping["resource"]),
            adapter_spec_sha256=mapping["adapter_spec_sha256"],  # type: ignore[arg-type]
            dispatch_mode=DispatchMode(mapping["dispatch_mode"]),
            material_sha256=mapping["material_sha256"],  # type: ignore[arg-type]
            segmentation_settings_revision=(
                None if mapping.get("segmentation_settings_revision") is None
                else SegmentationSettingsRevision.from_mapping(mapping["segmentation_settings_revision"])
            ),
            measurement_settings_revision=(
                None if mapping.get("measurement_settings_revision") is None
                else MeasurementSettingsRevision.from_mapping(mapping["measurement_settings_revision"])
            ),
            settled_review_revision=(None if mapping.get("settled_review_revision") is None
                else SettledReviewRevision.from_mapping(mapping["settled_review_revision"])),
            paired_reference=(None if "paired_reference" not in mapping
                else PairedMovingReferenceAuthority.from_mapping(mapping["paired_reference"])),
            saved_result_retirement=(None if "saved_result_retirement" not in mapping
                else SavedResultRetirement.from_mapping(mapping["saved_result_retirement"])),
            cell_correction=(None if "cell_correction" not in mapping
                else CellCorrectionRequest.from_mapping(mapping["cell_correction"])),
            training_settings_revision=(
                None
                if mapping.get("training_settings_revision") is None
                else TrainingSettingsRevision.from_mapping(mapping["training_settings_revision"])
            ),
            structure_id=(
                None
                if secondary is None
                else _mapping(secondary, "secondary_target")["structure_id"]
            ),  # type: ignore[arg-type]
            classifier_id=(
                None
                if secondary is None
                else _mapping(secondary, "secondary_target")["classifier_id"]
            ),  # type: ignore[arg-type]
            eligible_parent_ids=(
                ()
                if secondary is None
                else tuple(
                    _sequence(
                        _mapping(secondary, "secondary_target")["eligible_parent_ids"],
                        "eligible_parent_ids",
                    )
                )
            ),  # type: ignore[arg-type]
            section_target=(
                None
                if section is None
                else PhysicalSectionTarget(
                    **dict(_mapping(section, "section_target"))  # type: ignore[arg-type]
                )
            ),
            clustering_scope=(
                None
                if clustering is None
                else ClusteringGraphScope(
                    mode=ClusteringMode(_mapping(clustering, "clustering_scope")["mode"]),
                    phenotyping_group_id=_mapping(clustering, "clustering_scope")[
                        "phenotyping_group_id"
                    ],  # type: ignore[arg-type]
                    validation_domain_ids=tuple(
                        _sequence(
                            _mapping(clustering, "clustering_scope")["validation_domain_ids"],
                            "clustering validation_domain_ids",
                        )
                    ),  # type: ignore[arg-type]
                )
            ),
        )
    except (BatchPlanError, BatchArtifactError, KeyError, TypeError, ValueError) as exc:
        raise BatchStateError("persisted plan node cannot be reconstructed") from exc


def _plan_from_material(
    plan_mapping: Mapping[str, object],
    contracts: Mapping[str, ArtifactIdentityContract],
) -> BatchPlan:
    try:
        external_inputs = tuple(
            BatchArtifactRef.from_mapping(
                item,
                identity_contract=contracts[
                    str(
                        _mapping(item, "persisted external input")[
                            "owning_identity_contract_sha256"
                        ]
                    )
                ],
            )
            for item in _sequence(plan_mapping["external_inputs"], "persisted external inputs")
        )
        edges: list[BatchArtifactEdge] = []
        for item in _sequence(plan_mapping["edges"], "persisted plan edges"):
            edge = _mapping(item, "persisted plan edge")
            requirement_mapping = _mapping(edge["requirement"], "persisted edge requirement")
            contract = contracts[str(requirement_mapping["owning_identity_contract_sha256"])]
            requirement = _requirement_from_bundle(
                {
                    "requirement": requirement_mapping,
                    "identity_contract": _identity_contract_mapping(contract),
                }
            )
            edges.append(
                BatchArtifactEdge(
                    artifact_id=edge["artifact_id"],  # type: ignore[arg-type]
                    artifact_kind=edge["artifact_kind"],  # type: ignore[arg-type]
                    producer_node_id=edge["producer_node_id"],  # type: ignore[arg-type]
                    consumer_node_id=edge["consumer_node_id"],  # type: ignore[arg-type]
                    requirement=requirement,
                    accepted_artifact_ref_sha256=edge[
                        "accepted_artifact_ref_sha256"
                    ],  # type: ignore[arg-type]
                )
            )
        deferred = tuple(
            DeferredNodeDescriptor(
                node_kind=_mapping(item, "deferred node")["node_kind"],  # type: ignore[arg-type]
                node_version=_mapping(item, "deferred node")["node_version"],  # type: ignore[arg-type]
                scope=NodeScope(_mapping(item, "deferred node")["scope"]),
                participant_ids=tuple(
                    _sequence(
                        _mapping(item, "deferred node")["participant_ids"],
                        "deferred participants",
                    )
                ),  # type: ignore[arg-type]
                prerequisite_node_kinds=tuple(
                    _sequence(
                        _mapping(item, "deferred node")["prerequisite_node_kinds"],
                        "deferred prerequisite kinds",
                    )
                ),  # type: ignore[arg-type]
                unresolved_identity_kinds=tuple(
                    _sequence(
                        _mapping(item, "deferred node")["unresolved_identity_kinds"],
                        "deferred unresolved identities",
                    )
                ),  # type: ignore[arg-type]
                resource=ResourceKind(_mapping(item, "deferred node")["resource"]),
                descriptor_sha256=_mapping(item, "deferred node")[
                    "descriptor_sha256"
                ],  # type: ignore[arg-type]
            )
            for item in _sequence(plan_mapping["deferred_nodes"], "persisted deferred nodes")
        )
        plan = BatchPlan(
            schema_name=plan_mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=plan_mapping["schema_version"],  # type: ignore[arg-type]
            batch_id=plan_mapping["batch_id"],  # type: ignore[arg-type]
            normalized_manifest_sha256=plan_mapping[
                "normalized_manifest_sha256"
            ],  # type: ignore[arg-type]
            revision=plan_mapping["revision"],  # type: ignore[arg-type]
            predecessor_plan_sha256=plan_mapping[
                "predecessor_plan_sha256"
            ],  # type: ignore[arg-type]
            workspace_rebinding_sha256=plan_mapping[
                "workspace_rebinding_sha256"
            ],  # type: ignore[arg-type]
            nodes=tuple(
                _plan_node_from_mapping(item)
                for item in _sequence(plan_mapping["nodes"], "persisted plan nodes")
            ),
            edges=tuple(edges),
            external_inputs=external_inputs,
            deferred_nodes=deferred,
            prerequisites=tuple(
                BatchPrerequisiteItem.from_mapping(item)
                for item in _sequence(plan_mapping["prerequisites"], "persisted plan prerequisites")
            ),
            adapter_spec_set_sha256=plan_mapping[
                "adapter_spec_set_sha256"
            ],  # type: ignore[arg-type]
            plan_sha256=plan_mapping["plan_sha256"],  # type: ignore[arg-type]
            supervised_cutoff_selections=_plan_supervised_cutoffs(plan_mapping),
        )
    except (
        BatchArtifactError,
        BatchPlanError,
        BatchStateError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        if isinstance(exc, BatchStateError):
            raise
        raise BatchStateError("persisted batch plan cannot be reconstructed") from exc
    if plan.to_mapping() != dict(plan_mapping):
        raise BatchStateError("reconstructed plan differs from retained bytes")
    return plan


def _validate_node_mapping(value: object) -> Mapping[str, object]:
    """Validate node mapping for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    required = frozenset(
        {
            "node_id",
            "kind",
            "version",
            "batch_id",
            "scope",
            "validation_domain_id",
            "analysis_domain_bindings",
            "participant_ids",
            "project_entry_ids",
            "configuration_bindings",
            "dependency_ids",
            "input_artifact_ids",
            "input_edge_material_sha256s",
            "declared_output_kinds",
            "resource",
            "adapter_spec_sha256",
            "dispatch_mode",
            "material_sha256",
        }
    )
    optional = frozenset(
        {"secondary_target", "section_target", "clustering_scope", "training_settings_revision",
         "segmentation_settings_revision", "measurement_settings_revision", "settled_review_revision", "saved_result_retirement", "paired_reference", "cell_correction"}
    )
    candidate = _mapping(value, "persisted plan node")
    unknown = sorted(set(candidate) - required - optional)
    missing = sorted(required - set(candidate))
    if unknown:
        raise BatchStateError("persisted plan node has unknown field(s): " + ", ".join(unknown))
    if missing:
        raise BatchStateError("persisted plan node lacks required field(s): " + ", ".join(missing))
    mapping = candidate
    kind = _text(mapping["kind"], "persisted plan node kind")
    training_review_kind = kind in {
        "primary_classifier_training_plan_review",
        "secondary_classifier_training_plan_review",
        "supervised_phenotype_training_plan_review",
    }
    if training_review_kind != ("training_settings_revision" in mapping):
        raise BatchStateError("training plan review lacks its exact settings-revision field")
    if training_review_kind:
        if mapping["version"] != "2":
            raise BatchStateError("training plan review version is unsupported")
        if mapping["training_settings_revision"] is not None:
            try:
                TrainingSettingsRevision.from_mapping(mapping["training_settings_revision"])
            except (TypeError, ValueError) as exc:
                raise BatchStateError("training settings revision is invalid") from exc
    if "segmentation_settings_revision" in mapping:
        if kind != "segmentation":
            raise BatchStateError("only segmentation may bind its settings revision")
        try:
            SegmentationSettingsRevision.from_mapping(mapping["segmentation_settings_revision"])
        except (TypeError, ValueError) as exc:
            raise BatchStateError("segmentation settings revision is invalid") from exc
    if "measurement_settings_revision" in mapping:
        if kind != "measurements":
            raise BatchStateError("only measurements may bind its settings revision")
        try:
            MeasurementSettingsRevision.from_mapping(mapping["measurement_settings_revision"])
        except (TypeError, ValueError) as exc:
            raise BatchStateError("measurement settings revision is invalid") from exc
    if "settled_review_revision" in mapping:
        if kind not in SETTLED_REVIEW_KINDS:
            raise BatchStateError("only a supported settled review may bind its revision")
        try:
            revision = SettledReviewRevision.from_mapping(mapping["settled_review_revision"])
        except (TypeError, ValueError) as exc:
            raise BatchStateError("settled review revision is invalid") from exc
        if revision.paired_reference is not None and kind != "registration":
            raise BatchStateError("Only Alignment may select a revised paired image.")
        if revision.cluster_refit is not None and kind not in {'clustering_per_slide','clustering_pooled'}:
            raise BatchStateError('Only a completed clustering calculation may bind a selected-cluster refit.')
    if (kind == "paired_image_preparation") != ("paired_reference" in mapping):
        raise BatchStateError("Paired-image preparation requires its selected image identity.")
    if "paired_reference" in mapping:
        from .batch_project import PairedMovingReferenceAuthority

        PairedMovingReferenceAuthority.from_mapping(mapping["paired_reference"])
    if (kind == "saved_result_retirement") != ("saved_result_retirement" in mapping):
        raise BatchStateError("saved result retirement task requires its exact binding")
    if "saved_result_retirement" in mapping:
        try:
            SavedResultRetirement.from_mapping(mapping["saved_result_retirement"])
        except (TypeError, ValueError) as exc:
            raise BatchStateError("saved result retirement binding is invalid") from exc
    if (kind == "segmentation_correction") != ("cell_correction" in mapping):
        raise BatchStateError("cell correction task requires its reviewed draft")
    if "cell_correction" in mapping:
        try:
            CellCorrectionRequest.from_mapping(mapping["cell_correction"])
        except (TypeError, ValueError) as exc:
            raise BatchStateError("cell correction request is invalid") from exc
    if kind in {"clustering_per_sample", "clustering_across_samples"}:
        raise BatchStateError("retired clustering node name: workspace regeneration required")
    clustering_mode_by_kind = {
        "clustering_per_slide": "per_slide",
        "clustering_pooled": "pooled",
    }
    graph_scoped_kinds = {
        *clustering_mode_by_kind,
        "cluster_review_evidence",
        "cluster_label_review",
    }
    has_clustering_scope = "clustering_scope" in mapping
    if (kind in graph_scoped_kinds) != has_clustering_scope:
        raise BatchStateError("persisted clustering node scope material is not canonical")
    clustering_scope = mapping.get("clustering_scope")
    if has_clustering_scope:
        clustering = _closed_mapping(
            clustering_scope,
            fields=frozenset(
                {
                    "mode",
                    "phenotyping_group_id",
                    "validation_domain_ids",
                }
            ),
            field="persisted plan node clustering_scope",
        )
        mode = _text(clustering["mode"], "persisted clustering scope mode")
        if kind in clustering_mode_by_kind and mode != clustering_mode_by_kind[kind]:
            raise BatchStateError("persisted clustering scope mode contradicts node kind")
        group_id = clustering["phenotyping_group_id"]
        if mode not in {"per_slide", "pooled"}:
            raise BatchStateError("persisted clustering scope is unsupported")
        if group_id is not None:
            raise BatchStateError("phenotyping_group_id is a reserved null field")
        domain_ids = tuple(
            _text(item, "persisted clustering scope validation_domain_id")
            for item in _sequence(
                clustering["validation_domain_ids"],
                "persisted clustering scope validation_domain_ids",
            )
        )
        if (
            not domain_ids
            or len(set(domain_ids)) != len(domain_ids)
            or tuple(sorted(domain_ids)) != domain_ids
        ):
            raise BatchStateError("persisted clustering scope validation domains are not canonical")
        participant_ids = tuple(
            _text(item, "persisted clustering participant_id")
            for item in _sequence(
                mapping["participant_ids"],
                "persisted clustering participant_ids",
            )
        )
        if (
            not participant_ids
            or len(set(participant_ids)) != len(participant_ids)
            or tuple(sorted(participant_ids)) != participant_ids
        ):
            raise BatchStateError("persisted clustering participants are not canonical")
        domain_bindings = tuple(
            _mapping(item, "persisted clustering analysis-domain binding")
            for item in _sequence(
                mapping["analysis_domain_bindings"],
                "persisted clustering analysis_domain_bindings",
            )
        )
        binding_participants = {
            _text(
                item.get("sample_id"),
                "persisted clustering binding sample_id",
            )
            for item in domain_bindings
        }
        binding_entries = {
            _text(
                item.get("project_entry_id"),
                "persisted clustering binding project_entry_id",
            )
            for item in domain_bindings
        }
        project_entries = tuple(
            _text(item, "persisted clustering project_entry_id")
            for item in _sequence(
                mapping["project_entry_ids"],
                "persisted clustering project_entry_ids",
            )
        )
        configuration_participants = tuple(
            _text(
                _mapping(item, "persisted clustering configuration binding").get("sample_id"),
                "persisted clustering configuration sample_id",
            )
            for item in _sequence(
                mapping["configuration_bindings"],
                "persisted clustering configuration_bindings",
            )
        )
        if (
            not domain_bindings
            or binding_participants != set(participant_ids)
            or not project_entries
            or len(set(project_entries)) != len(project_entries)
            or tuple(sorted(project_entries)) != project_entries
            or binding_entries != set(project_entries)
            or configuration_participants != participant_ids
        ):
            raise BatchStateError(
                "persisted clustering scope does not exactly cover participants and entries"
            )
        if mode == "per_slide":
            if (
                mapping["scope"] != "sample"
                or len(participant_ids) != 1
                or len(domain_ids) != 1
                or mapping["validation_domain_id"] != domain_ids[0]
            ):
                raise BatchStateError("persisted per_slide clustering scope is inconsistent")
        elif (mapping["scope"] != "validation_domain"
              or domain_ids != (mapping["validation_domain_id"],)):
            raise BatchStateError("pooled clustering requires one Processing group")
    elif mapping["validation_domain_id"] is None:
        raise BatchStateError("persisted non-clustering node requires one validation domain")
    has_secondary_target = "secondary_target" in mapping
    secondary_target = mapping.get("secondary_target")
    if (kind == "secondary_classifier_inference") != has_secondary_target:
        raise BatchStateError("persisted secondary inference node target is not canonical")
    if has_secondary_target:
        secondary = _closed_mapping(
            secondary_target,
            fields=frozenset({"structure_id", "classifier_id", "eligible_parent_ids"}),
            field="persisted plan node secondary_target",
        )
        _text(
            secondary["structure_id"],
            "persisted plan node secondary target structure_id",
        )
        _text(
            secondary["classifier_id"],
            "persisted plan node secondary target classifier_id",
        )
        parents = tuple(
            _text(item, "persisted plan node secondary target eligible_parent_id")
            for item in _sequence(
                secondary["eligible_parent_ids"],
                "persisted plan node secondary target eligible_parent_ids",
            )
        )
        if not parents or len(set(parents)) != len(parents):
            raise BatchStateError("persisted plan node secondary target parents are not canonical")
    has_section_target = "section_target" in mapping
    section_target = mapping.get("section_target")
    if (
        kind
        in {
            "segmentation",
            "segmentation_correction",
            "measurements",
            "marker_rule_application",
            "clustering_label_import",
            "supervised_phenotype_inference",
            "phenotype_hierarchy_attestation",
        }
    ) != has_section_target:
        raise BatchStateError("persisted section-level node target is not canonical")
    if has_section_target:
        section = _closed_mapping(
            section_target,
            fields=frozenset(
                {
                    "project_entry_id",
                    "tissue_generation_id",
                    "section_id",
                    "tissue_path_object_id",
                }
            ),
            field="persisted plan node section_target",
        )
        for target_field in (
            "project_entry_id",
            "tissue_generation_id",
            "section_id",
            "tissue_path_object_id",
        ):
            _text(
                section[target_field],
                f"persisted plan node section target {target_field}",
            )
        section_entry_id = _text(
            section["project_entry_id"],
            "persisted plan node section target project_entry_id",
        )
        project_entry_ids = tuple(
            _text(item, "persisted plan node project_entry_id")
            for item in _sequence(
                mapping["project_entry_ids"],
                "persisted plan node project_entry_ids",
            )
        )
        domain_bindings = tuple(
            _mapping(item, "persisted plan node analysis-domain binding")
            for item in _sequence(
                mapping["analysis_domain_bindings"],
                "persisted plan node analysis_domain_bindings",
            )
        )
        domain_entry_ids = {
            _text(
                item.get("project_entry_id"),
                "persisted plan node analysis-domain project_entry_id",
            )
            for item in domain_bindings
        }
        if (
            len(project_entry_ids) != 1
            or project_entry_ids[0] != section_entry_id
            or not domain_bindings
            or domain_entry_ids != {section_entry_id}
        ):
            raise BatchStateError("physical section target conflicts with node entry or domains")
    try:
        DispatchMode(mapping["dispatch_mode"])
    except (TypeError, ValueError) as exc:
        raise BatchStateError("persisted plan node dispatch mode is invalid") from exc
    material = {
        "batch_id": mapping["batch_id"],
        "kind": mapping["kind"],
        "version": mapping["version"],
        "scope": mapping["scope"],
        "validation_domain_id": mapping["validation_domain_id"],
        "participant_ids": mapping["participant_ids"],
        "analysis_domain_bindings": mapping["analysis_domain_bindings"],
        "project_entry_ids": mapping["project_entry_ids"],
        "configuration_bindings": mapping["configuration_bindings"],
        "dependency_ids": mapping["dependency_ids"],
        "input_artifact_ids": mapping["input_artifact_ids"],
        "input_edge_material_sha256s": mapping["input_edge_material_sha256s"],
        "declared_output_kinds": mapping["declared_output_kinds"],
        "resource": mapping["resource"],
        "adapter_spec_sha256": mapping["adapter_spec_sha256"],
        "dispatch_mode": mapping["dispatch_mode"],
    }
    if has_secondary_target:
        material["secondary_target"] = secondary_target
    if has_section_target:
        material["section_target"] = section_target
    if has_clustering_scope:
        material["clustering_scope"] = clustering_scope
    if "segmentation_settings_revision" in mapping:
        material["segmentation_settings_revision"] = mapping["segmentation_settings_revision"]
    if "measurement_settings_revision" in mapping:
        material["measurement_settings_revision"] = mapping["measurement_settings_revision"]
    if "settled_review_revision" in mapping:
        material["settled_review_revision"] = mapping["settled_review_revision"]
    if "paired_reference" in mapping:
        material["paired_reference"] = mapping["paired_reference"]
    if "saved_result_retirement" in mapping:
        material["saved_result_retirement"] = mapping["saved_result_retirement"]
    if "cell_correction" in mapping:
        material["cell_correction"] = mapping["cell_correction"]
    if training_review_kind:
        material["training_settings_revision"] = mapping["training_settings_revision"]
    material_digest = _digest(material)
    if mapping["material_sha256"] != material_digest or mapping["node_id"] != (
        "node-" + material_digest[:48]
    ):
        raise BatchStateError("persisted plan node self-hash is invalid")
    _reject_prohibited_payload(mapping, "persisted plan node")
    return MappingProxyType(dict(mapping))


def _plan_snapshot_from_bytes(payload: bytes) -> _PlanSnapshot:
    """Decode exact canonical plan bytes without granting execution authority."""
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchStateError("persisted plan is not UTF-8 canonical JSON") from exc
    if _canonical_bytes(decoded) != payload:
        raise BatchStateError("persisted plan bytes are not canonical")
    if (
        isinstance(decoded, dict)
        and decoded.get("schema_name") == "mif-batch-plan"
        and decoded.get("schema_version") == 1
    ):
        raise BatchStateError("mif-batch-plan/v1 requires regeneration")
    expected = frozenset(
        {
            "schema_name",
            "schema_version",
            "batch_id",
            "normalized_manifest_sha256",
            "revision",
            "predecessor_plan_sha256",
            "workspace_rebinding_sha256",
            "nodes",
            "edges",
            "external_inputs",
            "deferred_nodes",
            "prerequisites",
            "adapter_spec_set_sha256",
            "plan_sha256",
        }
    )
    if isinstance(decoded, Mapping) and "supervised_cutoff_selections" in decoded:
        expected |= {"supervised_cutoff_selections"}
    mapping = _closed_mapping(decoded, fields=expected, field="persisted plan")
    if (mapping["schema_name"], mapping["schema_version"]) != (
        "mif-batch-plan",
        2,
    ):
        raise BatchStateError("persisted plan schema is invalid")
    unsigned = {key: mapping[key] for key in mapping if key != "plan_sha256"}
    plan_sha256 = _sha256(mapping["plan_sha256"], "plan_sha256")
    if _digest(unsigned) != plan_sha256:
        raise BatchStateError("persisted plan checksum mismatch")
    revision = mapping["revision"]
    if type(revision) is not int or revision < 1:
        raise BatchStateError("persisted plan revision is invalid")
    predecessor = _optional_sha256(mapping["predecessor_plan_sha256"], "predecessor_plan_sha256")
    workspace_rebinding = _optional_sha256(
        mapping["workspace_rebinding_sha256"], "workspace_rebinding_sha256"
    )
    if (revision == 1) != (predecessor is None):
        raise BatchStateError("persisted plan predecessor is invalid")
    nodes = tuple(
        _validate_node_mapping(item) for item in _sequence(mapping["nodes"], "persisted plan nodes")
    )
    node_by_id = {str(item["node_id"]): item for item in nodes}
    if len(node_by_id) != len(nodes):
        raise BatchStateError("persisted plan node IDs are not unique")
    raw_edges = tuple(
        MappingProxyType(dict(_mapping(item, "persisted plan edge")))
        for item in _sequence(mapping["edges"], "persisted plan edges")
    )
    external_inputs = tuple(
        _mapping(item, "persisted external input")
        for item in _sequence(mapping["external_inputs"], "persisted plan external_inputs")
    )
    external_input_sha256s = tuple(sorted(_digest(item) for item in external_inputs))
    if len(set(external_input_sha256s)) != len(external_input_sha256s):
        raise BatchStateError("persisted plan external inputs are not unique")
    return _PlanSnapshot(
        plan_sha256=plan_sha256,
        predecessor_plan_sha256=predecessor,
        workspace_rebinding_sha256=workspace_rebinding,
        revision=revision,
        batch_id=_safe_segment(mapping["batch_id"], "plan batch_id"),
        normalized_manifest_sha256=_sha256(
            mapping["normalized_manifest_sha256"],
            "plan normalized_manifest_sha256",
        ),
        node_by_id=MappingProxyType(node_by_id),
        edges=raw_edges,
        external_input_sha256s=external_input_sha256s,
        supervised_cutoff_selections=_plan_supervised_cutoffs(mapping),
    )


def _guarded_terminal_commit(method: Callable[..., Any]) -> Callable[..., Any]:
    """Retain store and writer authority before claiming one-shot mutation."""

    @wraps(method)
    def guarded(self: "BatchTerminalCommitter", *args: object, **kwargs: object) -> Any:
        with self._store._runtime_state_operation():
            with self._store._lease_operation(self._lease):
                return method(self, *args, **kwargs)

    setattr(guarded, "_runtime_guard_kind", "state-and-lease")
    return guarded


def _guarded_native_fit_checkpoint_write(
    method: Callable[..., Any],
) -> Callable[..., Any]:
    """Retain state and lease authority for each reusable checkpoint write."""

    @wraps(method)
    def guarded(
        self: "BatchNativeFitCheckpointWriter",
        *args: object,
        **kwargs: object,
    ) -> Any:
        with self._store._runtime_state_operation():
            with self._store._lease_operation(self._lease):
                return method(self, *args, **kwargs)

    setattr(guarded, "_runtime_guard_kind", "state-and-lease")
    return guarded


class BatchNativeFitCheckpointWriter:
    """Reusable state-issued authority for one aggregate's fit checkpoints."""

    def __init__(
        self,
        *,
        store: "BatchStateStore",
        context: BatchNodeExecutionContext,
        lease: BatchWorkspaceLease,
        _capability: object,
    ) -> None:
        if _capability is not store._native_fit_checkpoint_writer_capability:
            raise BatchStateError(
                "native fit checkpoint writers are issued only by BatchStateStore"
            )
        self._store = store
        self._context = context
        self._lease = lease

    @_guarded_native_fit_checkpoint_write
    def commit(
        self,
        successor: BatchNativeFitCheckpointSuccessor,
    ) -> tuple[
        CurrentNativeFitSetAuthority,
        BatchProjectMutationDurabilityReceipt,
    ]:
        """Select one durable fit while retaining reusable writer authority."""

        return self._store._commit_native_fit_checkpoint(
            context=self._context,
            successor=successor,
            lease=self._lease,
        )


class BatchTerminalCommitter:
    """One-shot scheduler-owned public authority for exact terminal results."""

    def __init__(
        self,
        *,
        store: "BatchStateStore",
        contexts: tuple[BatchNodeExecutionContext, ...],
        lease: BatchWorkspaceLease,
        _capability: object,
    ) -> None:
        if _capability is not store._terminal_committer_capability:
            raise BatchStateError("terminal committers are issued only by BatchStateStore")
        self._store = store
        self._contexts = contexts
        self._lease = lease
        self._condition = Condition()
        self._state = "open"
        self._committed_results: tuple[ScheduledNodeResult, ...] | None = None

    @property
    def committed_results(self) -> tuple[ScheduledNodeResult, ...]:
        """Return the exact store-issued result set after successful persistence."""

        with self._condition:
            if self._committed_results is None:
                raise BatchStateError("terminal committer has not persisted an exact result set")
            return self._committed_results

    @property
    def _is_unused_sealed(self) -> bool:
        with self._condition:
            return self._state == "sealed" and self._committed_results is None

    def _claim(self) -> None:
        with self._condition:
            if self._state != "open":
                raise BatchStateError("terminal committer is one-shot or has been sealed")
            self._state = "claimed"

    def _finish_claim(self, committed: tuple[ScheduledNodeResult, ...] | None) -> None:
        with self._condition:
            if committed is not None:
                self._committed_results = committed
            self._state = "used"
            self._condition.notify_all()

    def seal(self) -> None:
        """Close unused authority, waiting for an in-progress durable commit."""

        with self._condition:
            if self._state == "open":
                self._state = "sealed"
                self._condition.notify_all()
                return
            while self._state == "claimed":
                self._condition.wait()

    @_guarded_terminal_commit
    def commit(
        self, results: tuple[BatchPreparedNodeResult, ...]
    ) -> tuple[ScheduledNodeResult, ...]:
        self._claim()
        try:
            self._store._assert_lease(self._lease)
            committed = self._commit_prepared(tuple(results))
        except BaseException:
            self._finish_claim(None)
            raise
        self._finish_claim(committed)
        return committed

    @_guarded_terminal_commit
    def commit_raw(
        self, outcomes: tuple[BatchRawNodeOutcome, ...]
    ) -> tuple[ScheduledNodeResult, ...]:
        """Validate, prepare, and commit one raw outcome per owned context."""

        self._claim()
        try:
            self._store._assert_lease(self._lease)
            values = tuple(outcomes)
            if not all(isinstance(item, BatchRawNodeOutcome) for item in values):
                raise BatchStateError("terminal committer requires typed raw outcomes")
            expected = {_execution_context_sha256(context): context for context in self._contexts}
            actual = {_execution_context_sha256(item.context): item for item in values}
            if len(actual) != len(values) or set(actual) != set(expected):
                raise BatchStateError(
                    "raw outcomes must contain exactly one outcome per owned context"
                )
            for digest, outcome in actual.items():
                if outcome.context != expected[digest]:
                    raise BatchStateError("raw outcome carries a foreign execution context")
            prepared = tuple(
                self._store.prepare_node_result(
                    context,
                    actual[_execution_context_sha256(context)].stage_result,
                    actual[_execution_context_sha256(context)].execution_result,
                    execution_exchange_sha256=actual[
                        _execution_context_sha256(context)
                    ].execution_exchange_sha256,
                )
                for context in self._contexts
            )
            committed = self._commit_prepared(prepared)
        except BaseException:
            self._finish_claim(None)
            raise
        self._finish_claim(committed)
        return committed

    @_guarded_terminal_commit
    def commit_project_mutation(
        self,
        result: BatchPreparedNodeResult,
        project_material: BatchProjectMaterialSuccessor,
    ) -> tuple[ScheduledNodeResult, BatchProjectMutationDurabilityReceipt]:
        """Publish project material and its one terminal in one state-head CAS."""
        self._claim()
        try:
            if len(self._contexts) != 1:
                raise BatchStateError("project mutation committer must own exactly one context")
            scheduled, receipt = self._store._commit_project_terminal_result(
                context=self._contexts[0],
                prepared=result,
                material=project_material,
                lease=self._lease,
            )
        except BaseException:
            self._finish_claim(None)
            raise
        self._finish_claim((scheduled,))
        return scheduled, receipt

    @_guarded_terminal_commit
    def commit_project_stage(
        self,
        outcomes: tuple[BatchRawNodeOutcome, ...],
        project_material: BatchProjectStageMaterialSuccessor,
    ) -> tuple[
        tuple[ScheduledNodeResult, ...],
        BatchProjectMutationDurabilityReceipt,
    ]:
        """Publish every project-stage terminal under one batch state head."""

        self._claim()
        try:
            self._store._assert_lease(self._lease)
            values = tuple(outcomes)
            expected = {_execution_context_sha256(context): context for context in self._contexts}
            actual = {
                _execution_context_sha256(item.context): item
                for item in values
                if isinstance(item, BatchRawNodeOutcome)
            }
            if (
                len(actual) != len(values)
                or set(actual) != set(expected)
                or any(actual[digest].context != context for digest, context in expected.items())
            ):
                raise BatchStateError("project-stage outcomes must exactly cover owned contexts")
            prepared = tuple(
                self._store.prepare_project_mutation_node_result(
                    context,
                    actual[_execution_context_sha256(context)].stage_result,
                    actual[_execution_context_sha256(context)].execution_result,
                    execution_exchange_sha256=actual[
                        _execution_context_sha256(context)
                    ].execution_exchange_sha256,
                    project_lease=project_material._project_lease,
                    expected_head_sha256=(project_material.expected_state_head_sha256),
                    lease=self._lease,
                )
                for context in self._contexts
            )
            scheduled, receipt = self._store._commit_project_stage_results(
                contexts=self._contexts,
                prepared=prepared,
                material=project_material,
                lease=self._lease,
            )
        except BaseException:
            self._finish_claim(None)
            raise
        self._finish_claim(scheduled)
        return scheduled, receipt

    def _commit_prepared(
        self, values: tuple[BatchPreparedNodeResult, ...]
    ) -> tuple[ScheduledNodeResult, ...]:
        values = tuple(values)
        if not all(isinstance(item, BatchPreparedNodeResult) for item in values):
            raise BatchStateError("terminal committer requires prepared node results")
        if any(item._issuer is not self._store._prepared_result_capability for item in values):
            raise BatchStateError("terminal committer received a foreign result capability")
        expected = {item.authorization.authorization_sha256: item for item in self._contexts}
        actual = {item.execution_result.authorization_sha256: item for item in values}
        if len(actual) != len(values) or set(actual) != set(expected):
            raise BatchStateError(
                "terminal results must contain exactly one result per authorized context"
            )
        for digest, prepared in actual.items():
            context = expected[digest]
            if prepared.execution_context_sha256 != _execution_context_sha256(context):
                raise BatchStateError("prepared result execution-context binding is foreign")
            if prepared.stage_result.stage != context.adapter_spec.node_kind:
                raise BatchStateError("terminal StageResult names a foreign node stage")
            if prepared.stage_result.status is StageStatus.COMPLETED:
                try:
                    outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
                        context.adapter_spec,
                        prepared.execution_result.outputs,
                        authorization=context.authorization,
                    )
                except BatchArtifactError as exc:
                    raise BatchStateError(
                        "terminal outputs violate the authorized stage contract"
                    ) from exc
                if set(item.artifact_ref_sha256 for item in outputs) != set(
                    item.artifact_ref_sha256 for item in prepared.execution_result.outputs
                ):
                    raise BatchStateError("terminal output validation changed the result set")
            else:
                declared = set(context.adapter_spec.declared_outputs)
                outputs = prepared.execution_result.outputs
                if len({item.artifact_kind for item in outputs}) != len(outputs) or any(
                    item.artifact_kind not in declared for item in outputs
                ):
                    raise BatchStateError("non-completed result has foreign prepared outputs")
                output_contracts = {
                    item.artifact_kind: item
                    for item in context.adapter_spec.output_identity_contracts
                }
                for output in outputs:
                    try:
                        validate_artifact_ref(output, authorization=context.authorization)
                    except BatchArtifactError as exc:
                        raise BatchStateError("non-completed prepared output is invalid") from exc
                    if (
                        output.owning_identity_contract_sha256
                        != output_contracts[output.artifact_kind].contract_sha256
                    ):
                        raise BatchStateError(
                            "non-completed output uses a foreign identity contract"
                        )
        committed: list[ScheduledNodeResult] = []
        for context in self._contexts:
            committed.append(
                self._store._commit_terminal_result(
                    context=context,
                    prepared=actual[context.authorization.authorization_sha256],
                    lease=self._lease,
                )
            )
        result = tuple(committed)
        return result


def _guarded_batch_state_operation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Own one transient retained-layout lifetime token for a public root."""

    @wraps(method)
    def guarded(self: "BatchStateStore", *args: object, **kwargs: object) -> Any:
        with self._runtime_state_operation():
            return method(self, *args, **kwargs)

    setattr(guarded, "_runtime_guard_kind", "state")
    return guarded


def _guarded_batch_state_lease_operation(
    method: Callable[..., Any],
) -> Callable[..., Any]:
    """Own independent layout and exact writer-lease pins for a public root."""

    @wraps(method)
    def guarded(self: "BatchStateStore", *args: object, **kwargs: object) -> Any:
        candidates = tuple(
            item for item in (*args, *kwargs.values()) if isinstance(item, BatchWorkspaceLease)
        )
        if len(candidates) != 1:
            raise BatchStateError("guarded state operation requires one exact lease")
        with self._runtime_state_operation():
            with self._lease_operation(candidates[0]):
                return method(self, *args, **kwargs)

    setattr(guarded, "_runtime_guard_kind", "state-and-lease")
    return guarded


def _guarded_batch_state_lease_context(
    method: Callable[..., Any],
) -> Callable[..., Any]:
    """Retain both guards for the full lifetime of a returned context."""

    @contextmanager
    @wraps(method)
    def guarded(self: "BatchStateStore", *args: object, **kwargs: object) -> Iterator[Any]:
        candidates = tuple(
            item for item in (*args, *kwargs.values()) if isinstance(item, BatchWorkspaceLease)
        )
        if len(candidates) != 1:
            raise BatchStateError("guarded state operation requires one exact lease")
        with self._runtime_state_operation():
            with self._lease_operation(candidates[0]):
                with method(self, *args, **kwargs) as value:
                    yield value

    setattr(guarded, "_runtime_guard_kind", "state-and-lease")
    return guarded


class BatchStateStore:
    """Durable current head plus immutable content-addressed attempt records."""

    def __init__(
        self,
        layout: BatchWorkspaceLayout,
        *,
        lock_provider: BatchWorkspaceLockProvider | None = None,
        publication_provider: BatchSecurePublicationProvider | None = None,
        project_mutation_provider: object | None = None,
        authority_layout: RetainedAuthorityLayoutV2,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
        workspace_storage_scope_sha256: str,
    ) -> None:
        if not isinstance(layout, BatchWorkspaceLayout):
            raise BatchStateError("state store requires BatchWorkspaceLayout")
        self.layout = layout
        if not isinstance(authority_layout, RetainedAuthorityLayoutV2):
            raise BatchStateError("state store retained authority layout is invalid")
        self._authority_layout = authority_layout
        self._authority_layout_sha256 = authority_layout.layout.layout_sha256
        # Composition has checked the original manifest or selected capsule.
        # Destination paths locate files; they do not change imported authority.
        self._workspace_storage_scope_sha256 = _sha256(
            workspace_storage_scope_sha256, "workspace storage scope sha256",
        )
        self._typed_plan_cache = None
        self._retained_manifest_cache = None
        self._plan_parse_cache: dict[bytes, _PlanSnapshot] = {}
        self._plan_parse_cache_bytes = 0
        if not isinstance(runtime_lifecycle, RuntimeLifecycleBorrower):
            raise BatchStateError("state store runtime lifecycle is invalid")
        try:
            runtime_lifecycle.authenticate_authority_binding(
                runtime_binding,
                authority_layout=authority_layout,
                workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
            )
        except RuntimeLifecycleError as exc:
            raise BatchStateError("state store runtime binding is invalid") from exc
        try:
            authority_layout.validate()
        except DurableRecordError as exc:
            raise BatchStateError("state store retained authority layout is unavailable") from exc
        self._runtime_lifecycle = runtime_lifecycle
        self._runtime_binding = runtime_binding
        self._issued_project_stage_runtime: object | None = None
        self._issuer_id = uuid4().hex
        self._current_progress_cache = None
        self._history_cache = None
        self._decoded_head_cache = None
        self._validated_head_sha256 = None
        self._execution_snapshot_cache = None
        self._prepared_result_capability = object()
        self._terminal_committer_capability = object()
        self._native_fit_checkpoint_writer_capability = object()
        self._native_fit_checkpoint_capability = object()
        self._resume_capsule_import_capability = object()
        self._initialization_capability = object()
        self._project_durability_capability = _BatchProjectDurabilityCapability._issue(self)
        if project_mutation_provider is not None:
            from mif_pipeline.qupath_runtime.project_authority import (
                ProjectAuthorityV2,
            )

            project_authority = getattr(project_mutation_provider, "project_authority", None)
            if project_authority is not None and not isinstance(
                project_authority, ProjectAuthorityV2
            ):
                raise BatchStateError("project mutation authority v2 is invalid")
            if (
                project_authority is None
                or project_authority.authority_layout_sha256
                != authority_layout.layout.layout_sha256
            ):
                raise BatchStateError(
                    "batch and project authorities use different retained layouts"
                )
        self._project_mutation_provider = project_mutation_provider
        self._initial_project_identity_record: object | None = None
        self._initial_project_authority_head_record: object | None = None
        self._head_transition_lock = Lock()
        self._lease_condition = Condition(RLock())
        self._active_leases: dict[str, BatchWorkspaceLease] = {}
        self._resume_import_scope_by_fencing_token: dict[str, str] = {}
        self._lease_controllers: dict[str, _BatchWorkspaceLeaseController] = {}
        self._issued_project_inventory_capabilities: dict[
            int, tuple[object, BatchWorkspaceLease, AuthorityMemberRefV2]
        ] = {}
        if lock_provider is None:
            if os.name == "posix":
                lock_provider = PosixBatchWorkspaceLockProvider()
            elif os.name == "nt":
                lock_provider = WindowsBatchWorkspaceLockProvider()
            else:
                raise BatchStateError(f"unsupported workspace locking platform {os.name!r}")
        self._lock_provider = lock_provider
        if publication_provider is None:
            if os.name == "posix":
                publication_provider = PosixBatchSecurePublicationProvider(
                    workspace_root=layout.root
                )
            elif os.name == "nt":
                publication_provider = WindowsBatchSecurePublicationProvider(
                    workspace_root=layout.root
                )
            else:
                raise BatchStateError(f"unsupported secure publication platform {os.name!r}")
        self._publication_provider = publication_provider
        if project_mutation_provider is not None:
            register = getattr(project_mutation_provider, "register_state_store", None)
            if not callable(register):
                raise BatchStateError("project mutation provider lacks trusted store registration")
            register(self._project_durability_capability)

    @property
    def is_quiescent(self) -> bool:
        """Return whether this facade has no live state, writer, or catalogue use."""

        return all(
            self._runtime_lifecycle.is_kind_quiescent(kind)
            for kind in (
                "batch-writer-lease",
                "batch-state-operation",
                "batch-catalogue-operation",
            )
        )

    @property
    def authority_layout_sha256(self) -> str:
        return self._authority_layout_sha256

    @property
    def workspace_storage_scope_sha256(self) -> str:
        base_scope = self._workspace_storage_scope_sha256
        resume_scopes = getattr(self, "_resume_import_scope_by_fencing_token", {})
        active_leases = getattr(self, "_active_leases", {})
        active_resume_scopes = {
            scope
            for fencing_token, scope in resume_scopes.items()
            if fencing_token in active_leases
        }
        if len(active_resume_scopes) > 1:
            raise BatchStateError("active resume imports disagree on workspace scope")
        resume_scope = next(iter(active_resume_scopes), None)
        if not self.layout.state_head_path.is_file():
            return resume_scope or base_scope
        try:
            selection = json.loads(self.layout.state_head_path.read_bytes())["normalized_manifest_sha256"]
            retained_manifest = self.retained_manifest(selection).manifest
        except (OSError, ValueError, KeyError, BatchManifestError):
            # The authoritative head/manifest validator reports malformed or
            # missing retained bytes.  Scope lookup must not create an
            # alternate error path before that validation boundary runs.
            return base_scope
        if retained_manifest.batch_id != self.layout.batch_id:
            return base_scope
        original_output_root = retained_manifest.output_root.expanduser().resolve(strict=False)
        retained_scope = batch_project_workspace_storage_scope_sha256(
            batch_id=retained_manifest.batch_id,
            original_output_root=original_output_root,
        )
        if resume_scope is not None and resume_scope != retained_scope:
            raise BatchStateError("authenticated resume scope disagrees with retained manifest")
        return retained_scope

    @contextmanager
    def _runtime_state_operation(self) -> Iterator[None]:
        token = self._runtime_lifecycle.acquire("batch-state-operation")
        try:
            yield
        finally:
            token.release()

    def _lease_controller(self, lease: BatchWorkspaceLease) -> _BatchWorkspaceLeaseController:
        if (
            not isinstance(lease, BatchWorkspaceLease)
            or lease._issuer_id != self._issuer_id
            or lease.workspace_root != self.layout.root
        ):
            raise BatchWorkspaceLeaseReleaseError(
                "workspace lease is stale, foreign, or no longer live"
            )
        controller = self._lease_controllers.get(lease.fencing_token)
        if controller is None or controller.lease is not lease:
            raise BatchWorkspaceLeaseReleaseError(
                "workspace lease is stale, foreign, or no longer live"
            )
        return controller

    @contextmanager
    def _lease_operation(self, lease: BatchWorkspaceLease) -> Iterator[BatchWorkspaceLeaseUse]:
        thread_id = get_ident()
        with self._lease_condition:
            controller = self._lease_controller(lease)
            if controller.state == "open":
                pass
            elif (
                controller.state == "release_pending"
                and controller.uses_by_thread.get(thread_id, 0) > 0
            ):
                pass
            else:
                raise BatchStateError("workspace lease is stale, foreign, or no longer live")
            use = BatchWorkspaceLeaseUse._issue(controller=controller, thread_id=thread_id)
            controller.live_uses += 1
            controller.uses_by_thread[thread_id] = controller.uses_by_thread.get(thread_id, 0) + 1
        try:
            yield use
        finally:
            with self._lease_condition:
                if use._controller is not controller or use._released:
                    raise BatchStateError("workspace lease-use accounting is invalid")
                remaining = controller.uses_by_thread.get(thread_id, 0) - 1
                if remaining < 0 or controller.live_uses <= 0:
                    raise BatchStateError("workspace lease-use accounting underflow")
                if remaining:
                    controller.uses_by_thread[thread_id] = remaining
                else:
                    controller.uses_by_thread.pop(thread_id, None)
                controller.live_uses -= 1
                object.__setattr__(use, "_released", True)
                self._lease_condition.notify_all()

    def _runtime_binding_for_runner(self, runner: object) -> RuntimeCompositionBinding:
        try:
            self._runtime_lifecycle.authenticate_binding(
                self._runtime_binding,
                authority_layout=self._authority_layout,
                runner=runner,
                workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
            )
        except RuntimeLifecycleError as exc:
            raise BatchStateError("state store runtime binding differs") from exc
        return self._runtime_binding

    def authenticate_project_operations(
        self,
        operations: "object",
        *,
        runner: object,
    ) -> None:
        """Authenticate an exact prebuilt operations facade without exposing layout."""

        from mif_pipeline.qupath_runtime.catalogue_operations import (
            QuPathRunnerProjectOperations,
        )

        if not isinstance(operations, QuPathRunnerProjectOperations):
            raise BatchStateError("project operations facade is invalid")
        binding = self._runtime_binding
        try:
            operations.authenticate_runtime_binding(binding, runner=runner)
        except Exception as exc:
            raise BatchStateError("project operations runtime binding differs") from exc

    def _project_stage_mutation_authorization(
        self,
        *,
        request: object,
        contexts: tuple[BatchNodeExecutionContext, ...],
        lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> str:
        from .project_stage_plan import ProjectStageRunRequest

        self._assert_lease(lease)
        if not isinstance(request, ProjectStageRunRequest):
            raise BatchStateError("project-stage request must be typed")
        values = tuple(contexts)
        if (
            not values
            or not all(isinstance(item, BatchNodeExecutionContext) for item in values)
            or not isinstance(terminal_committer, BatchTerminalCommitter)
            or terminal_committer._store is not self
            or terminal_committer._lease is not lease
        ):
            raise BatchStateError("project-stage scheduler authority is foreign")
        with terminal_committer._condition:
            if terminal_committer._state != "open":
                raise BatchStateError("project-stage terminal committer is not open")
            issued_contexts = terminal_committer._contexts
        by_node = {context.node_id: context for context in values}
        issued_by_node = {context.node_id: context for context in issued_contexts}
        if (
            len(by_node) != len(values)
            or set(by_node) != {target.node_id for target in request.targets}
            or set(issued_by_node) != set(by_node)
            or any(issued_by_node[node_id] is not context for node_id, context in by_node.items())
        ):
            raise BatchStateError("project-stage targets require their exact scheduler contexts")
        head = self.load_head()
        catalogue = self.project_catalogue_store(lease).load_current()
        if (
            request.authority_layout_sha256 != self.authority_layout_sha256
            or request.batch_id != self.layout.batch_id
            or request.project_uri != Path(head.project_path).resolve().as_uri()
            or request.project_identity_sha256 != head.project_identity_sha256
            or request.expected_project_authority_head_sha256 != head.project_authority_head_sha256
            or request.project_catalogue_sha256 != head.project_catalogue_sha256
            or catalogue is None
            or catalogue.catalogue_sha256 != request.project_catalogue_sha256
            or catalogue.project_identity_sha256 != request.project_identity_sha256
            or catalogue.project_uri != request.project_uri
        ):
            raise BatchStateError("project-stage request authority is stale")
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        ordered: list[dict[str, str]] = []
        for target in request.targets:
            context = by_node[target.node_id]
            authorization = context.authorization
            if (
                self.load_authorization(authorization.authorization_sha256) != authorization
                or head.pending_authorization_sha256_by_node.get(target.node_id)
                != authorization.authorization_sha256
                or context.project_identity_sha256 != request.project_identity_sha256
                or authorization.dispatch_binding.project_entry_ids != (target.project_entry_id,)
                or head.sample_project_entry_id_by_sample.get(target.sample_id)
                != target.project_entry_id
                or head.resolved_config_sha256_by_sample.get(target.sample_id)
                != target.resolved_config_sha256
            ):
                raise BatchStateError("project-stage target and execution context authority differ")
            self._validate_persisted_dispatch_binding(
                authorization,
                snapshot,
                head=head, check_current_binding=True,
            )
            ordered.append(
                {
                    "node_id": target.node_id,
                    "attempt_id": authorization.attempt_id,
                    "attempt_authorization_sha256": (authorization.authorization_sha256),
                    "input_artifact_set_sha256": (authorization.input_artifact_set_sha256),
                }
            )
        return _digest(
            {
                "kind": "project_stage_mutation_authorization/v1",
                "request_sha256": request.request_sha256,
                "target_context_authorizations": ordered,
            }
        )

    def _authenticate_project_stage_catalogue_store(
        self,
        store: object,
        lease: BatchWorkspaceLease,
    ) -> None:
        from .batch_project import BatchProjectCatalogueStore

        if (
            not isinstance(store, BatchProjectCatalogueStore)
            or store._capability is not lease
            or store._registered_store_issuer is not self._project_durability_capability
            or store.authority_layout_sha256 != self.authority_layout_sha256
            or store.workspace_storage_scope_sha256 != self.workspace_storage_scope_sha256
        ):
            raise BatchStateError("project-stage catalogue store is foreign")

    def _publish_project_stage_request(
        self,
        store: object,
        request: object,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> object:
        with self._runtime_state_operation():
            with self._lease_operation(lease):
                self._authenticate_project_stage_catalogue_store(store, lease)
                authorization = self._project_stage_mutation_authorization(
                    request=request,
                    contexts=contexts,
                    lease=lease,
                    terminal_committer=terminal_committer,
                )
                return store._publish_project_stage_request(  # type: ignore[attr-defined]
                    request,
                    mutation_authorization_sha256=authorization,
                    workspace_lease=lease,
                    state_store_issuer=self._project_durability_capability,
                )

    def _authenticate_registration_project_request_current(
        self,
        store: object,
        request: object,
        context: BatchNodeExecutionContext,
    ) -> None:
        from .batch_project import BatchRegistrationProjectRequest
        from .batch_artifacts import validate_committed_artifact_authority_set
        from .batch_tissue_artifacts import (
            decode_committed_tissue_acceptance_result,
        )

        if (
            type(request) is not BatchRegistrationProjectRequest
            or type(context) is not BatchNodeExecutionContext
        ):
            raise BatchStateError("registration-project request requires exact typed inputs")
        current = self.authenticate_current_execution_context(context)
        head = self.load_head()
        catalogue = store.load_current()  # type: ignore[attr-defined]
        matching_entries = tuple(
            entry
            for entry in (() if catalogue is None else catalogue.entries)
            if entry.project_entry_id
            in {
                request.fixed_project_entry_id,
                request.moving_project_entry_id,
            }
        )
        entry_by_role = {entry.image_role: entry for entry in matching_entries}
        fixed_entry = entry_by_role.get("primary_mif")
        moving_entry = entry_by_role.get("moving_reference")
        input_by_kind = {item.artifact_kind: item for item in context.inputs}
        if set(input_by_kind) != {
            "analysis_frame",
            "tissue_acceptance_result",
        }:
            raise BatchStateError("registration-project request inputs are not closed")
        frame_authority = self.authenticate_available_artifact(input_by_kind["analysis_frame"])
        tissue_authority = self.authenticate_available_artifact(
            input_by_kind["tissue_acceptance_result"]
        )
        authorities = validate_committed_artifact_authority_set(
            (frame_authority, tissue_authority),
            expected_active_plan_sha256=current.active_plan_sha256,
        )
        tissue = decode_committed_tissue_acceptance_result(tissue_authority)
        if (
            current.node_kind != "registration"
            or current.node_scope != "shared_project"
            or len(current.participant_ids) != 1
            or current.node_id != request.node_id
            or context.authorization.attempt_id != request.attempt_id
            or current.authorization_sha256 != request.attempt_authorization_sha256
            or current.authorization_sha256 != request.authorizing_record_sha256
            or current.execution_context_sha256 != request.execution_context_sha256
            or current.state_head_sha256 != request.state_head_sha256
            or current.active_plan_sha256 != request.active_plan_sha256
            or current.project_entry_ids != (request.fixed_project_entry_id,)
            or context.project_entry_projection_sha256 != request.fixed_entry_projection_sha256
            or head.sha256 != request.state_head_sha256
            or head.active_plan_sha256 != request.active_plan_sha256
            or head.batch_id != request.batch_id
            or head.project_materialization_state != "materialized"
            or Path(head.project_path).resolve(strict=False).as_uri() != request.project_uri
            or head.project_identity_sha256 != request.project_identity_sha256
            or head.project_authority_head_sha256 != request.project_authority_head_sha256
            or head.project_qpproj_snapshot_sha256 != request.project_qpproj_snapshot_sha256
            or head.project_catalogue_sha256 != request.project_catalogue_sha256
            or self.workspace_storage_scope_sha256 != request.workspace_storage_scope_sha256
            or input_by_kind["analysis_frame"].artifact_ref_sha256
            != request.analysis_frame_ref_sha256
            or input_by_kind["tissue_acceptance_result"].artifact_ref_sha256
            != request.tissue_acceptance_result_ref_sha256
            or len(authorities) != 2
            or frame_authority.producer_node_kind != "analysis_preflight"
            or tissue_authority.producer_node_kind != "tissue_acceptance"
            or any(
                authority.state_head_sha256 != current.state_head_sha256
                or authority.execution_exchange_sha256 is not None
                for authority in authorities
            )
            or tissue.generation_id != request.tissue_generation_id
            or tissue.generation_content_sha256 != request.tissue_generation_content_sha256
            or tissue.section_inventory_sha256 != request.tissue_section_inventory_sha256
            or _digest(list(tissue.section_ids)) != request.tissue_section_ids_sha256
            or _digest(list(tissue.roi_ids)) != request.tissue_roi_ids_sha256
            or _digest(list(tissue.path_object_ids)) != request.tissue_path_object_ids_sha256
            or catalogue is None
            or catalogue.batch_id != request.batch_id
            or catalogue.project_uri != request.project_uri
            or catalogue.project_identity_sha256 != request.project_identity_sha256
            or not same_project_snapshot(
                Path(head.project_path), catalogue.qpproj_snapshot_sha256,
                request.project_qpproj_snapshot_sha256,
            )
            or catalogue.catalogue_sha256 != request.project_catalogue_sha256
            or len(matching_entries) != 2
            or len(entry_by_role) != 2
            or fixed_entry is None
            or moving_entry is None
            or fixed_entry.project_entry_id != request.fixed_project_entry_id
            or fixed_entry.entry_projection_sha256 != request.fixed_entry_projection_sha256
            or fixed_entry.image_sha256 != request.fixed_image_sha256
            or fixed_entry.sample_id != current.participant_ids[0]
            or fixed_entry.validation_domain_id != current.validation_domain_id
            or fixed_entry.paired_entry_id != request.moving_project_entry_id
            or moving_entry.project_entry_id != request.moving_project_entry_id
            or moving_entry.entry_projection_sha256 != request.moving_entry_projection_sha256
            or moving_entry.image_sha256 != request.moving_image_sha256
            or moving_entry.sample_id != fixed_entry.sample_id
            or moving_entry.slide_id != fixed_entry.slide_id
            or moving_entry.validation_domain_id != fixed_entry.validation_domain_id
            or moving_entry.reference_modality != request.moving_reference_modality
            or moving_entry.paired_entry_id != request.fixed_project_entry_id
        ):
            raise BatchStateError("registration-project request authority is stale")

    def _publish_registration_project_request(
        self,
        store: object,
        request: object,
        context: BatchNodeExecutionContext,
        *,
        lease: BatchWorkspaceLease,
    ) -> object:
        """Publish one exact current registration request through state."""

        with self._runtime_state_operation():
            with self._lease_operation(lease):
                self._authenticate_project_stage_catalogue_store(store, lease)
                self._authenticate_registration_project_request_current(
                    store,
                    request,
                    context,
                )
                return store._publish_registration_project_request(  # type: ignore[attr-defined]
                    request,
                    mutation_authorization_sha256=(request.authorizing_record_sha256),
                    workspace_lease=lease,
                    state_store_issuer=self._project_durability_capability,
                )

    def _reopen_registration_project_request(
        self,
        store: object,
        request_sha256: str,
        context: BatchNodeExecutionContext,
        *,
        lease: BatchWorkspaceLease,
    ) -> tuple[object, object]:
        """Reopen one retained registration request through current state."""

        with self._runtime_state_operation():
            with self._lease_operation(lease):
                self._authenticate_project_stage_catalogue_store(store, lease)
                request, capability = (
                    store._reopen_registration_project_request(  # type: ignore[attr-defined]
                        request_sha256,
                        workspace_lease=lease,
                        state_store_issuer=self._project_durability_capability,
                    )
                )
                self._authenticate_registration_project_request_current(
                    store,
                    request,
                    context,
                )
                self._authenticate_project_request_capability(
                    capability,
                    expected_kind="registration_project",
                )
                return request, capability

    @_guarded_batch_state_lease_operation
    def _publish_registration_project_mutation_acquisition_record(
        self,
        store: object,
        context: BatchNodeExecutionContext,
        *,
        request: object,
        inventory: object,
        snapshot_resolver: object,
        target_plan: object,
        project_provider: object,
        runner: object,
        project: object,
        lease: BatchWorkspaceLease,
    ) -> object:
        """Admit only the exact persisted registration request inventory."""

        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationArtifactInventoryCapability,
            ProjectMutationLeaseProvider,
            ProjectMutationRequestCapability,
        )
        from mif_pipeline.qupath_runtime.project import ProjectLayout
        from mif_pipeline.qupath_runtime.registration_session import (
            CurrentRegistrationProjectSnapshotResolver,
        )
        from mif_pipeline.qupath_runtime.runner import (
            QuPathRegistrationRuntimeTargetPlan,
            QuPathRunner,
        )

        from .batch_project import BatchRegistrationProjectRequest
        from .batch_registration_session import _registration_static_targets

        self._assert_lease(lease)
        self._authenticate_project_stage_catalogue_store(store, lease)
        if (
            type(context) is not BatchNodeExecutionContext
            or not isinstance(request, ProjectMutationRequestCapability)
            or not isinstance(
                inventory,
                ProjectMutationArtifactInventoryCapability,
            )
            or type(snapshot_resolver) is not CurrentRegistrationProjectSnapshotResolver
            or type(target_plan) is not QuPathRegistrationRuntimeTargetPlan
            or type(project_provider) is not ProjectMutationLeaseProvider
            or self._project_mutation_provider is not project_provider
            or type(runner) is not QuPathRunner
            or type(project) is not ProjectLayout
        ):
            raise BatchStateError("registration acquisition requires its exact composed owners")
        self._runtime_binding_for_runner(runner)
        self._authenticate_project_request_capability(
            request,
            expected_kind="registration_project",
        )
        self._authenticate_project_inventory_capability(inventory)
        persisted, persisted_capability = self._reopen_registration_project_request(
            store,
            request.mutation_request_sha256,
            context,
            lease=lease,
        )
        if (
            type(persisted) is not BatchRegistrationProjectRequest
            or persisted.intent != "open_existing"
            or request.mutation_request_sha256 != persisted.request_sha256
            or request.mutation_authorization_sha256 != persisted.authorizing_record_sha256
        ):
            raise BatchStateError(
                "registration acquisition request differs from retained authority"
            )
        try:
            snapshot = project_provider.authenticate_current_registration_project_snapshot(
                snapshot_resolver
            )
            authenticated_target_plan = runner.authenticate_registration_runtime_target_plan(
                target_plan
            )
        except Exception as exc:
            raise BatchStateError(
                "registration acquisition snapshot or target plan is foreign"
            ) from exc
        fixed = snapshot.fixed_entry
        moving = snapshot.moving_entry
        transform = snapshot.transform
        if (
            snapshot.project_uri != persisted.project_uri
            or snapshot.project_identity_sha256 != persisted.project_identity_sha256
            or snapshot.project_authority_head_sha256 != persisted.project_authority_head_sha256
            or snapshot.qpproj_sha256 != persisted.project_qpproj_snapshot_sha256
            or snapshot.snapshot_resolution_sha256 != persisted.snapshot_resolution_sha256
            or fixed.project_entry_id != persisted.fixed_project_entry_id
            or fixed.source_image_sha256 != persisted.fixed_image_sha256
            or fixed.image_role != "primary_mif"
            or moving.project_entry_id != persisted.moving_project_entry_id
            or moving.source_image_sha256 != persisted.moving_image_sha256
            or moving.image_role != "moving_reference"
            or transform.presence != "present"
            or transform.transform_uri != persisted.transform_uri
            or transform.content_sha256 != persisted.transform_sha256
            or authenticated_target_plan.registration_exchange_id
            != persisted.registration_exchange_id
            or authenticated_target_plan.snapshot_resolution_sha256
            != persisted.snapshot_resolution_sha256
            or authenticated_target_plan.target_plan_sha256 != persisted.runtime_target_plan_sha256
            or tuple(role_script.role for role_script in authenticated_target_plan.role_scripts)
            != persisted.ordered_roles
            or project.project_file.absolute().as_uri() != persisted.project_uri
        ):
            raise BatchStateError("registration acquisition material differs from its request")
        expected_inventory = ProjectMutationArtifactInventory.create(
            mutation_request_sha256=persisted.request_sha256,
            mutation_authorization_sha256=(persisted.authorizing_record_sha256),
            expected_project_identity_sha256=(persisted.project_identity_sha256),
            static_targets=_registration_static_targets(
                snapshot=snapshot,
                target_plan=authenticated_target_plan,
                project_provider=project_provider,
                state_store=self,
                project=project,
            ),
            dynamic_targets=(),
        )
        if inventory.inventory != expected_inventory:
            raise BatchStateError("registration acquisition inventory is not the exact closed set")
        return self._publish_project_mutation_acquisition_record_owned(
            request=persisted_capability,
            inventory=inventory,
            operation="registration_project_write",
            intent=persisted.intent,
            project_uri=persisted.project_uri,
            expected_project_identity_sha256=(persisted.project_identity_sha256),
            expected_authority_head_sha256=(persisted.project_authority_head_sha256),
            recovery_predecessor_acquisition_record_sha256=None,
            lease=lease,
        )

    def _reopen_project_stage_request(
        self,
        store: object,
        request_sha256: str,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> tuple[object, object]:
        with self._runtime_state_operation():
            with self._lease_operation(lease):
                self._authenticate_project_stage_catalogue_store(store, lease)
                request = store.reopen_project_stage_request_value(  # type: ignore[attr-defined]
                    request_sha256,
                    workspace_lease=lease,
                )
                authorization = self._project_stage_mutation_authorization(
                    request=request,
                    contexts=contexts,
                    lease=lease,
                    terminal_committer=terminal_committer,
                )
                return store._reopen_project_stage_request(  # type: ignore[attr-defined]
                    request_sha256,
                    mutation_authorization_sha256=authorization,
                    workspace_lease=lease,
                    state_store_issuer=self._project_durability_capability,
                )

    def _publish_project_stage_run_plan(
        self,
        store: object,
        plan: object,
        *,
        lease: BatchWorkspaceLease,
    ) -> object:
        """Publish a facade-built plan through this store's private issuer."""

        with self._runtime_state_operation():
            with self._lease_operation(lease):
                self._authenticate_project_stage_catalogue_store(store, lease)
                return store._publish_project_stage_run_plan(  # type: ignore[attr-defined]
                    plan,
                    workspace_lease=lease,
                    state_store_issuer=self._project_durability_capability,
                )

    @_guarded_batch_state_operation
    def project_stage_runtime(
        self,
        *,
        provider: object,
        runner: object,
        project_operations: object,
        codec_registry: object,
    ) -> object:
        """Issue the sole exact orchestration-owned project-stage facade."""

        from .project_stage_plan import (
            ProjectStageRuntime,
            _PROJECT_STAGE_RUNTIME_ISSUER,
        )

        if provider is not self._project_mutation_provider:
            raise BatchStateError("project-stage runtime components are foreign")
        self.authenticate_project_operations(project_operations, runner=runner)
        try:
            self._runtime_lifecycle.authenticate_binding(
                self._runtime_binding,
                authority_layout=self._authority_layout,
                runner=runner,
                workspace_storage_scope_sha256=(self.workspace_storage_scope_sha256),
            )
        except RuntimeLifecycleError as exc:
            raise BatchStateError("project-stage runtime binding differs") from exc
        if self._issued_project_stage_runtime is not None:
            runtime = self._issued_project_stage_runtime
            if not runtime._authenticates(  # type: ignore[attr-defined]
                state_store=self,
                provider=provider,
                runner=runner,
                project_operations=project_operations,
                runtime_binding=self._runtime_binding,
                codec_registry=codec_registry,
            ):
                raise BatchStateError("project-stage runtime components differ")
            return runtime
        runtime = ProjectStageRuntime._issue(
            state_store=self,
            provider=provider,
            runner=runner,
            project_operations=project_operations,
            runtime_binding=self._runtime_binding,
            runtime_lifecycle=self._runtime_lifecycle,
            codec_registry=codec_registry,
            _issuer=_PROJECT_STAGE_RUNTIME_ISSUER,
        )
        self._issued_project_stage_runtime = runtime
        return runtime

    @contextmanager
    def _lease_operation_for_binding(self, binding_sha256: str) -> Iterator[BatchWorkspaceLeaseUse]:
        with self._lease_condition:
            matching = tuple(
                lease
                for lease in self._active_leases.values()
                if self._workspace_lease_binding_sha256(lease) == binding_sha256
            )
            if len(matching) != 1:
                raise BatchStateError(
                    "project completion requires its originating live workspace lease"
                )
            operation = self._lease_operation(matching[0])
            use = operation.__enter__()
        try:
            yield use
        finally:
            operation.__exit__(None, None, None)

    @contextmanager
    def acquire_writer_lease(self, operation: str) -> Iterator[BatchWorkspaceLease]:
        runtime_token = self._runtime_lifecycle.acquire("batch-writer-lease")
        try:
            operation = _text(operation, "operation")
            self._assert_workspace_integrity()
            try:
                handle = self._lock_provider.acquire(self.layout.writer_lock_path)
            except BatchStateError:
                raise
            except Exception as exc:
                raise BatchStateError("cannot acquire batch workspace writer lock") from exc
            try:
                lease = BatchWorkspaceLease._issue(
                    workspace_root=self.layout.root,
                    operation=operation,
                    issuer_id=self._issuer_id,
                    handle=handle,
                )
                controller = _BatchWorkspaceLeaseController(
                    lease=lease, runtime_token=runtime_token
                )
                with self._lease_condition:
                    self._active_leases[lease.fencing_token] = lease
                    try:
                        self._lease_controllers[lease.fencing_token] = controller
                    except BaseException:
                        self._active_leases.pop(lease.fencing_token, None)
                        raise
            except BaseException as issuance_failure:
                try:
                    handle.release()
                except BaseException as cleanup:
                    _add_bounded_cleanup_note(
                        issuance_failure,
                        cleanup,
                        context="workspace writer-lock rollback failed",
                    )
                raise
        except BaseException as primary:
            try:
                runtime_token.release()
            except BaseException as cleanup:
                _add_bounded_cleanup_note(
                    primary,
                    cleanup,
                    context="workspace lifecycle rollback failed",
                )
            raise
        body_failure: BaseException | None = None
        try:
            # A committed head can outlive an interrupted display publication.
            # Repair only after acquiring the writer lease; polling never writes.
            if self.layout.state_head_path.exists():
                try:
                    self.read_current_progress()
                except BatchStateError:
                    self.reopen_execution_snapshot(full_recovery=True, recovery_lease=lease)
            yield lease
        except BaseException as exc:
            body_failure = exc
            raise
        finally:
            try:
                self.release_writer_lease(lease)
            except BaseException as release_failure:
                if body_failure is None:
                    raise
                _add_bounded_cleanup_note(
                    body_failure,
                    release_failure,
                    context="workspace lease release failed",
                )

    def release_writer_lease(self, lease: BatchWorkspaceLease) -> None:
        """Release one exact lease after every admitted operation has drained."""

        thread_id = get_ident()
        with self._lease_condition:
            controller = self._lease_controller(lease)
            if controller.state == "released":
                return
            if controller.state == "failed":
                assert controller.terminal_failure is not None
                raise controller.terminal_failure
            if controller.uses_by_thread.get(thread_id, 0):
                controller.state = "release_pending"
                raise BatchWorkspaceLeaseReleaseError(
                    "cannot release a workspace lease from its active use"
                )
            controller.state = "release_pending"
            while controller.release_owner_thread not in {None, thread_id}:
                self._lease_condition.wait()
                if controller.state == "released":
                    return
                if controller.state == "failed":
                    assert controller.terminal_failure is not None
                    raise controller.terminal_failure
            controller.release_owner_thread = thread_id
            while controller.live_uses:
                self._lease_condition.wait()
            try:
                lease._release()
            except BaseException as exc:
                failure = BatchWorkspaceLeaseReleaseError(
                    "cannot release batch workspace writer lock"
                )
                failure.__cause__ = exc
                controller.release_owner_thread = None
                if lease._handle.is_locked:
                    self._lease_condition.notify_all()
                    raise failure
                controller.state = "failed"
                controller.terminal_failure = failure
                self._active_leases.pop(lease.fencing_token, None)
                self._resume_import_scope_by_fencing_token.pop(lease.fencing_token, None)
                controller.runtime_token.release()
                self._lease_condition.notify_all()
                raise failure
            self._active_leases.pop(lease.fencing_token, None)
            self._resume_import_scope_by_fencing_token.pop(lease.fencing_token, None)
            controller.state = "released"
            controller.release_owner_thread = None
            controller.runtime_token.release()
            self._lease_condition.notify_all()

    def _assert_workspace_integrity(self) -> None:
        if not self.layout.root.is_dir() or self.layout.root.is_symlink():
            raise BatchStateError("batch workspace root is not a real directory")
        for name, path in zip(_OWNED_DIRECTORY_NAMES, self.layout.owned_directories):
            _reject_symlink_path(path, self.layout.root, name)
            if not path.is_dir() or path.is_symlink() or path.resolve() != path:
                raise BatchStateError(f"workspace directory {name!r} is unsafe")

    def _assert_lease(self, lease: BatchWorkspaceLease) -> None:
        with self._lease_condition:
            try:
                controller = self._lease_controller(lease)
            except BatchWorkspaceLeaseReleaseError as exc:
                raise BatchStateError(str(exc)) from exc
            if controller.state != "open" and controller.uses_by_thread.get(get_ident(), 0) <= 0:
                raise BatchStateError("workspace lease is stale, foreign, or no longer live")
            active = self._active_leases.get(lease.fencing_token)
            if (
                active is not lease
                or not lease.is_live
                or not lease._handle.validate(self.layout.writer_lock_path)
            ):
                raise BatchStateError("workspace lease is stale, foreign, or no longer live")
        self._assert_workspace_integrity()

    def _assert_live_workspace_lease_binding(self, binding_sha256: str) -> None:
        matching = tuple(
            lease
            for lease in self._active_leases.values()
            if self._workspace_lease_binding_sha256(lease) == binding_sha256
        )
        if len(matching) != 1:
            raise BatchStateError(
                "project completion requires its originating live workspace lease"
            )
        self._assert_lease(matching[0])

    def validate_writer_lease(self, lease: BatchWorkspaceLease) -> None:
        """Revalidate one exact live service-owned writer lease."""

        self._assert_lease(lease)

    @_guarded_batch_state_lease_operation
    def project_catalogue_store(self, lease: BatchWorkspaceLease) -> "BatchProjectCatalogueStore":
        """Issue the batch-local catalogue mutator for this exact live lease."""

        self._assert_lease(lease)
        return self._issue_project_catalogue_store(
            lease,
            workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
        )

    @_guarded_batch_state_lease_operation
    def current_project_entry_saved_state_sha256_by_id(
        self,
        *,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        _catalogue_lease: object | None = None,
    ) -> Mapping[str, str]:
        """Project exact current per-entry saved state from authenticated history.

        The catalogue is the immutable baseline. Accepted annotation publications
        and project-stage observations advance each entry's saved state. Native
        classifier mutations retain their authenticated fit lineage.
        """

        from .batch_project import BatchProjectCatalogue
        from .batch_training_checkpoint import BatchNativeFitCheckpointEntry
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceRecord,
        )

        self._assert_lease(lease)
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = (self.load_head() if _catalogue_lease is None
                else self._read_canonical_state_head()[0])
        if head.sha256 != expected:
            raise BatchHeadConflictError("saved-state projection head changed")
        provider = self._project_mutation_provider
        if provider is None or head.project_catalogue_sha256 is None or head.project_qpproj_snapshot_sha256 is None:
            raise BatchStateError("saved-state projection authority is unavailable")
        catalogue = self.project_catalogue_store(lease).load_current()
        if (
            type(catalogue) is not BatchProjectCatalogue
            or catalogue.batch_id != head.batch_id
            or catalogue.catalogue_sha256 != head.project_catalogue_sha256
            or catalogue.project_identity_sha256 != head.project_identity_sha256
        ):
            raise BatchStateError("saved-state projection catalogue is stale")
        try:
            current = provider.load_authority_head(head.project_identity_sha256)
            if _catalogue_lease is not None:
                from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLease

                if (not isinstance(_catalogue_lease, ProjectMutationLease)
                        or _catalogue_lease._provider is not provider
                        or not _catalogue_lease.is_live
                        or _catalogue_lease.operation != "batch_catalogue_upsert"
                        or _catalogue_lease.current_authority_head != current):
                    raise BatchStateError("catalogue saved-state lease is foreign")
                predecessor = _catalogue_lease.designation.authority_head
                if current != predecessor:
                    attestation = provider.load_mutation_attestation(
                        head.project_identity_sha256, current.latest_mutation_attestation_sha256)
                    if (current.predecessor_authority_head_sha256 != predecessor.authority_head_sha256
                            or attestation.predecessor_authority_head_sha256 != predecessor.authority_head_sha256
                            or attestation.mutation_kind != "batch_catalogue_upsert"
                            or attestation.mutation_request_sha256 != _catalogue_lease.mutation_request_sha256
                            or attestation.mutation_authorization_sha256 != _catalogue_lease.mutation_authorization_sha256
                            or attestation.post_save_qpproj_snapshot_sha256 != current.qpproj_snapshot_sha256):
                        raise BatchStateError("catalogue saved-state predecessor changed")
                current = predecessor
            if (
                current.authority_head_sha256 != head.project_authority_head_sha256
                or current.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                or current.latest_mutation_attestation_sha256
                != head.project_mutation_attestation_sha256
            ):
                raise BatchStateError("saved-state projection project authority is stale")
            # A plan revision may retire the pending pointer while retaining a
            # completed classifier mutation in the provider authority chain.
            # Reopen the bounded immutable set inventory, rather than treating
            # current pending pointers as a history index.
            checkpoint_sha256s = self._retained_training_fit_set_digests(head)
            fit_entries: dict[str, BatchNativeFitCheckpointEntry] = {}
            for checkpoint_sha256 in checkpoint_sha256s:
                fit_set = self._load_training_fit_set(checkpoint_sha256)
                if fit_set.batch_id != head.batch_id:
                    continue
                for entry in fit_set.entries:
                    prior = fit_entries.setdefault(
                        entry.successor_project_authority_head_sha256, entry
                    )
                    if prior is not entry and prior != entry:
                        raise BatchStateError("saved-state projection fit lineage is ambiguous")
            def validate_native_fit(cursor, attestation, predecessor_sha256):
                entry = fit_entries.get(cursor.authority_head_sha256)
                if entry is None:
                    raise BatchStateError("saved-state projection fit lineage is absent")
                material = provider.load_native_classifier_fit_material(
                    head.project_identity_sha256,
                    entry.runtime_fit_material_sha256,
                    workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
                )
                provider.reopen_native_classifier_fit(
                    material,
                    workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
                    mutation_attestation_sha256=entry.mutation_attestation_sha256,
                    successor_project_authority_head_sha256=(
                        entry.successor_project_authority_head_sha256
                    ),
                )
                if (
                    entry.project_identity_sha256 != head.project_identity_sha256
                    or entry.predecessor_project_authority_head_sha256 != predecessor_sha256
                    or entry.successor_project_authority_head_sha256
                    != cursor.authority_head_sha256
                    or entry.qpproj_snapshot_sha256 != cursor.qpproj_snapshot_sha256
                    or entry.mutation_attestation_sha256 != attestation.mutation_attestation_sha256
                    or attestation.mutation_kind != "annotation_classifier"
                    or attestation.mutation_authorization_sha256
                    != material.mutation_authorization_sha256
                    or attestation.operation_evidence_sha256
                    != entry.runtime_fit_material_sha256
                    or material.predecessor_project_authority_head_sha256
                    != entry.predecessor_project_authority_head_sha256
                    or material.qpproj_snapshot_sha256 != entry.qpproj_snapshot_sha256
                    or material.project_identity_sha256 != head.project_identity_sha256
                    or attestation.workspace_catalogue_sha256 is not None
                ):
                    raise BatchStateError("saved-state projection fit lineage differs")
            from .cluster.cluster_annotations import accepted_annotation_transitions
            from .batch_project_saved_state import _project_location_history, project_saved_states

            saved_heads, relocations, project_locations = _project_location_history(
                current, load_head=lambda digest: provider._load_head_version(
                    head.project_identity_sha256, digest))
            if head.operational_rebinding_sha256 is not None:
                self._load_operational_locator_authorities(head)
                rebinding = json.loads((self.layout.resume / (
                    "locator-rebindings-" + head.operational_rebinding_sha256 + ".json")).read_bytes())
                pairs = [(Path(row["requirement"]["original_path"]), Path(row["effective_path"]))
                         for row in rebinding["effective_locators"]
                         if row["requirement"]["role"] == "qupath_project"]
                if len(pairs) != 1 or pairs[0][1] != Path(head.project_path):
                    raise BatchStateError("saved-state project relocation lacks its exact location receipt")
                original_project, effective_project = pairs[0]
                if (original_project != effective_project
                        and (original_project.as_uri(), effective_project.as_uri()) not in relocations):
                    raise BatchStateError("saved-state project receipt lacks its exact relocation")
            annotation_transitions = accepted_annotation_transitions(
                self.layout.root, batch_id=head.batch_id, project_file=head.project_path,
                entries=catalogue.entries, load_interaction_request=self.load_interaction_request,
                load_attempt=self.load_attempt,
                project_locations=project_locations,
            )
            from .batch_native_abandon import restoration_transitions
            for key, value in restoration_transitions(self, head).items():
                if key in annotation_transitions and annotation_transitions[key] != value:
                    raise BatchStateError("administrative restoration conflicts with an annotation publication")
                annotation_transitions[key] = value
            values = project_saved_states(
                catalogue=catalogue, current=current,
                project_identity_sha256=head.project_identity_sha256,
                load_head=saved_heads.__getitem__,
                load_attestation=lambda digest: provider.load_mutation_attestation(head.project_identity_sha256, digest),
                load_stage_evidence=lambda digest: ProjectStageMutationEvidenceRecord.from_mapping(
                    self._read_batch_project_ref(self._batch_project_ref(
                        "batch_project_runtime_evidence", evidence_sha256=digest),
                        label="saved-state project-stage evidence")),
                validate_native_fit=validate_native_fit,
                annotation_transitions=annotation_transitions,
                load_catalogue=self.project_catalogue_store(lease).load_generation,
                relocations=relocations,
            )
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError("saved-state projection authority is invalid") from exc
        after = (self.load_head() if _catalogue_lease is None
                 else self._read_canonical_state_head()[0])
        if after.sha256 != expected:
            raise BatchHeadConflictError("saved-state projection head changed")
        return MappingProxyType(dict(sorted(values.items())))

    def _issue_project_catalogue_store(
        self,
        lease: BatchWorkspaceLease,
        *,
        workspace_storage_scope_sha256: str | None,
    ) -> "BatchProjectCatalogueStore":
        from .batch_project import BatchProjectCatalogueStore

        return BatchProjectCatalogueStore._issue(
            self.layout.catalogue,
            lease,
            self.validate_writer_lease,
            self._project_durability_capability,
            authority_layout=self._authority_layout,
            workspace_storage_scope_sha256=workspace_storage_scope_sha256,
            runtime_lifecycle=self._runtime_lifecycle,
            runtime_binding=self._runtime_binding,
            _lease_use_factory=lambda: self._lease_operation(lease),
        )

    @_guarded_batch_state_lease_operation
    def _resume_capsule_catalogue_store(
        self,
        authority: object,
        lease: BatchWorkspaceLease,
    ) -> "BatchProjectCatalogueStore":
        """Issue the import store from the authenticated original locator scope."""

        self._assert_lease(lease)
        assert_bound = getattr(authority, "_assert_bound", None)
        if not callable(assert_bound):
            raise BatchStateError("resume catalogue store requires authenticated capsule authority")
        assert_bound(self)
        original_manifest_bytes = getattr(authority, "original_manifest_bytes", None)
        if type(original_manifest_bytes) is not bytes:
            raise BatchStateError("resume original manifest authority is invalid")
        try:
            manifest = BatchManifest.from_json_bytes(original_manifest_bytes)
        except BatchManifestError as exc:
            raise BatchStateError("resume original manifest authority is invalid") from exc
        if manifest.batch_id != self.layout.batch_id:
            raise BatchStateError("resume original manifest batch differs")
        scope = batch_project_workspace_storage_scope_sha256(
            batch_id=manifest.batch_id,
            original_output_root=manifest.output_root.expanduser().resolve(strict=False),
        )
        existing = self._resume_import_scope_by_fencing_token.get(lease.fencing_token)
        if existing not in {None, scope}:
            raise BatchStateError("resume catalogue scope changed")
        self._resume_import_scope_by_fencing_token[lease.fencing_token] = scope
        return self._issue_project_catalogue_store(
            lease,
            workspace_storage_scope_sha256=scope,
        )

    def project_mutation_durability_authority(self, lease: BatchWorkspaceLease) -> "object":
        self._assert_lease(lease)
        if self._project_mutation_provider is None:
            raise BatchStateError("no project mutation provider is registered")
        issuer = getattr(self._project_mutation_provider, "issue_durability_authority", None)
        if not callable(issuer):
            raise BatchStateError("project mutation provider cannot issue durability authority")
        return issuer(
            workspace_lease_binding_sha256=(self._workspace_lease_binding_sha256(lease)),
            registered_store_issuer=self._project_durability_capability,
        )

    @staticmethod
    def _project_control_facts(head: BatchStateHead) -> tuple[object, ...]:
        return (
            head.batch_id,
            head.project_path,
            head.project_identity_sha256,
            head.project_authority_head_sha256,
            head.project_materialization_state,
            head.project_qpproj_snapshot_sha256,
            head.project_mutation_attestation_sha256,
            head.project_catalogue_sha256,
            tuple(sorted(head.sample_project_entry_id_by_sample.items())),
            tuple(sorted(head.sample_project_entry_projection_sha256_by_sample.items())),
        )

    @staticmethod
    def _project_control_lease_facts(
        provider: object,
        project_lease: object,
    ) -> tuple[object, ...]:
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLease

        if not isinstance(project_lease, ProjectMutationLease):
            raise BatchStateError("project control lease authority is invalid")
        validate = getattr(provider, "_validate_live_lease_authority", None)
        if not callable(validate):
            raise BatchStateError("project control provider cannot validate authority")
        project_head = project_lease.current_authority_head
        pin = project_lease._project_path_pin
        require_project = pin.exists_at_pin or project_head.materialization_state == "materialized"
        # Project control never creates a file. Preserve an authenticated absent
        # target across early preprocessing pause/review/resume transitions.
        pin.validate(require_exists=require_project, allow_initial_creation=False)
        active = validate(project_lease, require_project=require_project)
        if require_project:
            file_identity_before = pin.retained_file_identity
            qpproj_bytes = pin.read_bytes()
            file_identity_after = pin.retained_file_identity
            qpproj_sha256 = hashlib.sha256(qpproj_bytes).hexdigest()
        else:
            file_identity_before = file_identity_after = None
            qpproj_sha256 = None
        if file_identity_after != file_identity_before or (
            project_head.materialization_state == "materialized"
            and not same_project_snapshot(
                project_lease.designation.project_path,
                project_head.qpproj_snapshot_sha256,
                qpproj_sha256,
            )
        ):
            raise BatchStateError("project control qpproj authority changed")
        return (
            active.lease_record_sha256,
            active.project_identity_sha256,
            active.project_uri,
            active.operation,
            active.acquisition_record_sha256,
            active.mutation_request_sha256,
            active.mutation_authorization_sha256,
            active.artifact_inventory_sha256,
            active.fencing_token_sha256,
            project_head.authority_head_sha256,
            project_head.latest_mutation_attestation_sha256,
            file_identity_before,
            qpproj_sha256,
            project_lease.current_mutation_attestation(),
        )

    def _complete_project_control(
        self,
        *,
        initial_head: BatchStateHead,
        initial_project_facts: tuple[object, ...],
        project_lease: object,
        project_processes: object,
        lease: BatchWorkspaceLease,
    ) -> None:
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLease

        if not isinstance(project_lease, ProjectMutationLease):
            raise BatchStateError("project control lease authority is invalid")
        project_processes.assert_all_drained(
            batch_id=initial_head.batch_id,
            project_identity_sha256=initial_head.project_identity_sha256,
        )
        current_head = self.load_head()
        if self._project_control_facts(current_head) != self._project_control_facts(initial_head):
            raise BatchStateError("project control changed batch-project authority")
        provider = self._project_mutation_provider
        current_project_facts = self._project_control_lease_facts(
            provider,
            project_lease,
        )
        if (
            current_project_facts != initial_project_facts
            or current_project_facts[9] != current_head.project_authority_head_sha256
            or (
                current_head.project_qpproj_snapshot_sha256 is not None
                and not same_project_snapshot(
                    project_lease.designation.project_path,
                    current_head.project_qpproj_snapshot_sha256,
                    current_project_facts[12],
                )
            )
        ):
            raise BatchStateError("project control project authority changed")
        receipt = BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="project_control",
            workspace_lease_binding_sha256=(self._workspace_lease_binding_sha256(lease)),
            acquisition_record_sha256=project_lease.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(
                project_lease.acquisition_session_challenge_sha256
            ),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=project_lease.mutation_request_sha256,
            mutation_authorization_sha256=(project_lease.mutation_authorization_sha256),
            artifact_inventory_sha256=project_lease.artifact_inventory_sha256,
            project_identity_sha256=current_head.project_identity_sha256,
            project_fencing_token_sha256=project_lease.fencing_token_sha256,
            project_authority_head_sha256=(current_head.project_authority_head_sha256),
            mutation_attestation_sha256=(current_head.project_mutation_attestation_sha256),
            terminal_attempt_record_sha256s=(),
            committed_batch_state_head_sha256=current_head.sha256,
        )
        project_lease.complete(receipt)

    @_guarded_batch_state_lease_context
    @contextmanager
    def current_project_control(
        self,
        *,
        operation: Literal["pause", "resume", "interaction-response", "retry", "revise-options"],
        lease: BatchWorkspaceLease,
    ) -> Iterator[BatchProjectControlContext]:
        """Borrow one current exact-project lease for a pause/resume transition."""

        from mif_pipeline.orchestration.batch_project import (
            ProjectDesignationRequest,
        )
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactTarget,
            ProjectMutationLeaseProvider,
            canonical_project_uri,
        )

        self._assert_lease(lease)
        if operation not in {"pause", "resume", "interaction-response", "retry", "revise-options"}:
            raise BatchStateError("project control operation is invalid")
        provider = self._project_mutation_provider
        if type(provider) is not ProjectMutationLeaseProvider:
            raise BatchStateError("project control requires its registered provider")
        head = self.load_head()
        expected_initial_state = (
            BatchExecutionState.ACTIVE
            if operation in {"pause", "retry"}
            else head.execution_state if operation == "revise-options"
            else BatchExecutionState.PAUSED
        )
        project_path = Path(head.project_path).expanduser().resolve(strict=False)
        reserved_absence = False
        if not project_path.exists() and head.project_materialization_state == "reserved":
            snapshot = self.reopen_execution_snapshot()
            reserved_absence = (
                snapshot.head.sha256 == head.sha256
                and snapshot.manifest.manifest.create_project_path == project_path
                and head.project_qpproj_snapshot_sha256 is None
                and not any(head.sample_project_entry_id_by_sample.values())
            )
        if (
            head.execution_state is not expected_initial_state
            or not project_path.is_absolute()
            or project_path.is_symlink()
            or (not project_path.is_file() and not reserved_absence)
            or canonical_project_uri(project_path) != project_path.as_uri()
        ):
            raise BatchStateError("project control current project is unavailable")
        for checkpoint_digest in head.pending_training_fit_set_sha256_by_node.values():
            checkpoint = self._load_training_fit_set(checkpoint_digest)
            if set(checkpoint.completed_lease_archive_ref_by_fit_material_sha256) != {
                entry.runtime_fit_material_sha256 for entry in checkpoint.entries
            }:
                raise BatchStateError("project control requires completed native fit proof")
        authorization = _digest(
            {
                "schema_name": "mif-batch-project-control-authorization",
                "schema_version": 1,
                "operation": operation,
                "state_head_sha256": head.sha256,
                "batch_id": head.batch_id,
                "project_identity_sha256": head.project_identity_sha256,
                "project_authority_head_sha256": (head.project_authority_head_sha256),
                "project_qpproj_snapshot_sha256": (head.project_qpproj_snapshot_sha256),
            }
        )
        initial_qpproj_sha256 = None if reserved_absence else _file_sha256(project_path)
        if head.project_qpproj_snapshot_sha256 is not None and not same_project_snapshot(
            project_path, head.project_qpproj_snapshot_sha256, initial_qpproj_sha256
        ):
            raise BatchStateError(
                "The QuPath image inputs differ from the saved analysis. Check image entries, "
                "source locations, channel names and pixel calibration. Routine display "
                "changes can continue when their original project snapshot was recorded."
            )
        request_record = ProjectDesignationRequest.create(
            batch_id=head.batch_id,
            intent="open_existing",
            project_uri=canonical_project_uri(project_path),
            authorizing_record_sha256=authorization,
            expected_project_identity_sha256=head.project_identity_sha256,
            expected_authority_head_sha256=head.project_authority_head_sha256,
            relocation_from_uri=None,
            clone_source_project_uri=None,
            clone_source_project_identity_sha256=None,
            clone_source_qpproj_snapshot_sha256=None,
            clone_source_identity_sidecar_sha256=None,
            clone_source_entry_projection_set_sha256=None,
            clone_required_saved_state_set_sha256=None,
        )
        catalogue = self.project_catalogue_store(lease)
        request = catalogue.publish_request(
            request_record,
            mutation_authorization_sha256=authorization,
            workspace_lease=lease,
        )
        qpproj_target = ProjectMutationArtifactTarget.create(
            target_kind="qpproj",
            resolution_kind="exact_uri",
            owner_root_uri=project_path.parent.resolve().as_uri(),
            relative_locator_or_metadata_key=project_path.name,
            predecessor_sha256=initial_qpproj_sha256,
        )
        prepared_inventory = self.prepare_project_mutation_artifact_inventory(
            request=request,
            expected_project_identity_sha256=head.project_identity_sha256,
            static_targets=(qpproj_target,),
            dynamic_targets=(),
            lease=lease,
        )
        inventory = self.publish_project_mutation_artifact_inventory(
            prepared_inventory,
            lease=lease,
        )
        acquisition = self.publish_project_mutation_acquisition_record(
            request=request,
            inventory=inventory,
            operation="project_control",
            intent="open_existing",
            project_uri=request_record.project_uri,
            expected_project_identity_sha256=head.project_identity_sha256,
            expected_authority_head_sha256=head.project_authority_head_sha256,
            recovery_predecessor_acquisition_record_sha256=None,
            lease=lease,
        )
        session = provider.issue_acquisition_session(
            acquisition,
            request=request,
            inventory=inventory,
            durability_authority=self.project_mutation_durability_authority(lease),
        )
        with provider.acquire(session) as project_lease:
            initial_project_facts = self._project_control_lease_facts(
                provider,
                project_lease,
            )
            if (
                initial_project_facts[1] != head.project_identity_sha256
                or initial_project_facts[9] != head.project_authority_head_sha256
                or (
                    head.project_qpproj_snapshot_sha256 is not None
                    and not same_project_snapshot(
                        project_path, head.project_qpproj_snapshot_sha256, initial_project_facts[12]
                    )
                )
                or initial_project_facts[10] != head.project_mutation_attestation_sha256
                or initial_project_facts[12] != initial_qpproj_sha256
            ):
                raise BatchStateError("project control authority differs from state")
            project_processes = provider.issue_process_registry_view(
                project_lease,
                batch_id=head.batch_id,
            )
            control = BatchProjectControlContext(
                head=head,
                project_lease=project_lease,
                project_processes=project_processes,
            )
            try:
                yield control
            except BaseException as primary:
                try:
                    self._complete_project_control(
                        initial_head=head,
                        initial_project_facts=initial_project_facts,
                        project_lease=project_lease,
                        project_processes=project_processes,
                        lease=lease,
                    )
                except BaseException as cleanup:
                    _add_bounded_cleanup_note(
                        primary,
                        cleanup,
                        context="project control completion also failed",
                    )
                raise
            else:
                self._complete_project_control(
                    initial_head=head,
                    initial_project_facts=initial_project_facts,
                    project_lease=project_lease,
                    project_processes=project_processes,
                    lease=lease,
                )

    @_guarded_batch_state_lease_context
    @contextmanager
    def annotation_classifier_project_acquisition(
        self,
        reservation: object,
        context: BatchNodeExecutionContext,
        *,
        lease: BatchWorkspaceLease,
        project_leases: object,
    ) -> Iterator[object]:
        """Derive and retain one exact annotation-classifier acquisition."""

        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactTarget,
            ProjectMutationLeaseProvider,
            canonical_project_uri,
        )
        from .batch_project import AnnotationClassifierProjectRequest
        from .batch_training.engines import (
            BatchTrainingError,
            _authenticate_training_reservation_for_project_acquisition,
        )

        self._assert_lease(lease)
        if (
            type(self) is not BatchStateStore
            or type(project_leases) is not ProjectMutationLeaseProvider
            or project_leases is not self._project_mutation_provider
        ):
            raise BatchStateError(
                "annotation-classifier acquisition requires the registered provider"
            )

        def reopen_current() -> tuple[BatchCurrentExecutionAuthority, BatchStateHead, object]:
            current = self.authenticate_current_execution_context(context)
            head = self.load_head()
            try:
                projection = _authenticate_training_reservation_for_project_acquisition(
                    reservation,
                    expected_context=context,
                    current_execution=current,
                    state_store=self,
                    state_head_sha256=head.sha256,
                    active_plan_sha256=head.active_plan_sha256,
                )
            except BatchTrainingError as exc:
                raise BatchStateError("annotation-classifier reservation is not current") from exc
            if (
                head.project_materialization_state != "materialized"
                or head.project_qpproj_snapshot_sha256 is None
            ):
                raise BatchStateError(
                    "annotation-classifier acquisition requires a materialized project"
                )
            return current, head, projection

        current, head, projection = reopen_current()
        project_path = Path(head.project_path).expanduser().resolve(strict=False)
        if (
            not project_path.is_absolute()
            or project_path.is_symlink()
            or not project_path.is_file()
        ):
            raise BatchStateError("annotation-classifier current qpproj is unavailable")
        project_uri = canonical_project_uri(project_path)
        authorization = _digest(
            {
                "schema_name": ("mif-annotation-classifier-project-acquisition-authorization"),
                "schema_version": 1,
                "execution_context_sha256": current.execution_context_sha256,
                "authorization_sha256": current.authorization_sha256,
                "current_execution_seal_sha256": current._seal_sha256,
                "parent_execution_seal_sha256": (projection.parent_execution_seal_sha256),
                "state_head_sha256": head.sha256,
                "active_plan_sha256": head.active_plan_sha256,
                "fit_plan_sha256": projection.fit_plan_sha256,
                "fit_execution_sha256": projection.fit_execution_sha256,
                "project_uri": project_uri,
                "project_identity_sha256": head.project_identity_sha256,
                "project_authority_head_sha256": (head.project_authority_head_sha256),
                "qpproj_snapshot_sha256": (head.project_qpproj_snapshot_sha256),
                "native_classifier_relative_path": (projection.project_relative_path),
                **({"cv_review_resource_paths": projection.review_resource_paths}
                   if projection.review_resource_paths else {}),
                "classifier_version": projection.classifier_version,
            }
        )
        request_record = AnnotationClassifierProjectRequest.create(
            batch_id=head.batch_id,
            intent="open_existing",
            project_uri=project_uri,
            authorizing_record_sha256=authorization,
            expected_project_identity_sha256=head.project_identity_sha256,
            expected_authority_head_sha256=(head.project_authority_head_sha256),
        )
        catalogue = self.project_catalogue_store(lease)
        request = catalogue._publish_annotation_classifier_request(
            request_record,
            mutation_authorization_sha256=authorization,
            workspace_lease=lease,
            state_store_issuer=self._project_durability_capability,
        )
        owner_root_uri = project_path.parent.resolve().as_uri()
        qpproj_target = ProjectMutationArtifactTarget.create(
            target_kind="qpproj",
            resolution_kind="exact_uri",
            owner_root_uri=owner_root_uri,
            relative_locator_or_metadata_key=project_path.name,
            predecessor_sha256=head.project_qpproj_snapshot_sha256,
        )
        classifier_target = ProjectMutationArtifactTarget.create(
            target_kind="native_classifier",
            resolution_kind="exact_uri",
            owner_root_uri=owner_root_uri,
            relative_locator_or_metadata_key=projection.project_relative_path,
            predecessor_sha256=None,
        )
        review_targets = tuple(ProjectMutationArtifactTarget.create(
            target_kind="native_classifier", resolution_kind="exact_uri",
            owner_root_uri=owner_root_uri, relative_locator_or_metadata_key=path,
            predecessor_sha256=None,
        ) for path in projection.review_resource_paths)
        prepared_inventory = self.prepare_project_mutation_artifact_inventory(
            request=request,
            expected_project_identity_sha256=head.project_identity_sha256,
            static_targets=(qpproj_target, classifier_target, *review_targets),
            dynamic_targets=(),
            lease=lease,
        )
        inventory = self.publish_project_mutation_artifact_inventory(
            prepared_inventory,
            lease=lease,
        )
        acquisition = self.publish_project_mutation_acquisition_record(
            request=request,
            inventory=inventory,
            operation="annotation_classifier",
            intent="open_existing",
            project_uri=project_uri,
            expected_project_identity_sha256=head.project_identity_sha256,
            expected_authority_head_sha256=head.project_authority_head_sha256,
            recovery_predecessor_acquisition_record_sha256=None,
            lease=lease,
        )
        durability = self.project_mutation_durability_authority(lease)
        session = project_leases.issue_acquisition_session(
            acquisition,
            request=request,
            inventory=inventory,
            durability_authority=durability,
        )
        before_yield = reopen_current()
        if before_yield != (current, head, projection):
            raise BatchStateError(
                "annotation-classifier acquisition authority changed before yield"
            )

        def is_exact_failed_terminal_return(after_head: BatchStateHead) -> bool:
            authorization = context.authorization
            terminal_digest = after_head.terminal_attempt_sha256_by_attempt_id.get(
                authorization.attempt_id
            )
            if (
                terminal_digest is None
                or after_head.predecessor_head_sha256 != head.sha256
                or after_head.ancestor_head_sha256s != (*head.ancestor_head_sha256s, head.sha256)
            ):
                return False
            try:
                attempt = self.load_attempt(terminal_digest)
                reopened_authorization = self.load_authorization(attempt.authorization_sha256)
                context_binding = _BatchExecutionContextBinding.from_context(context)
            except BatchStateError:
                return False
            if (
                reopened_authorization != authorization
                or attempt.node_id != context.node_id
                or attempt.attempt_id != authorization.attempt_id
                or attempt.authorization_sha256 != authorization.authorization_sha256
                or attempt.execution_context_binding != context_binding
                or attempt.execution_context_sha256 != context_binding.context_sha256
                or attempt.stage_result.status is not StageStatus.FAILED
                or attempt.stage_result.error_code is None
                or attempt.stage_result.generation_id is not None
                or attempt.output_artifacts
            ):
                return False

            pending = dict(head.pending_authorization_sha256_by_node)
            if pending.pop(context.node_id, None) != (authorization.authorization_sha256):
                return False
            statuses = dict(head.node_status_by_id)
            statuses[context.node_id] = StageStatus.FAILED
            attempt_ids = {key: tuple(value) for key, value in head.attempt_ids_by_node.items()}
            attempt_ids[context.node_id] = (
                *attempt_ids.get(context.node_id, ()),
                authorization.attempt_id,
            )
            terminals = dict(head.terminal_attempt_sha256_by_attempt_id)
            terminals[authorization.attempt_id] = terminal_digest
            failures = dict(head.failure_codes_by_node)
            failures[context.node_id] = attempt.stage_result.error_code
            durable_by_sample = {
                key: tuple(value) for key, value in head.last_durable_node_ids_by_sample.items()
            }
            node_scope = head.node_scope_by_id[context.node_id]
            for sample_id in node_scope["sample_ids"]:
                prior_nodes = durable_by_sample.get(sample_id, ())
                durable_by_sample[sample_id] = (
                    prior_nodes
                    if context.node_id in prior_nodes
                    else (*prior_nodes, context.node_id)
                )
            durable_by_domain = {
                key: tuple(value) for key, value in head.last_durable_node_ids_by_domain.items()
            }
            domain_id = node_scope["validation_domain_id"]
            if domain_id is not None:
                canonical_domain_id = _text(domain_id, "node scope validation_domain_id")
                prior_nodes = durable_by_domain.get(canonical_domain_id, ())
                durable_by_domain[canonical_domain_id] = (
                    prior_nodes
                    if context.node_id in prior_nodes
                    else (*prior_nodes, context.node_id)
                )
            expected = head.evolve(
                node_status_by_id=statuses,
                pending_authorization_sha256_by_node=pending,
                failure_codes_by_node=failures,
                attempt_ids_by_node=attempt_ids,
                terminal_attempt_sha256_by_attempt_id=terminals,
                last_durable_node_ids_by_sample=durable_by_sample,
                last_durable_node_ids_by_domain=durable_by_domain,
            )
            return after_head == expected

        def reauthenticate_return_state() -> None:
            after_head = self.load_head()
            if after_head == head:
                after_current = self.authenticate_current_execution_context(context)
                if after_current != current:
                    raise BatchStateError(
                        "annotation-classifier acquisition authority changed on return"
                    )
                return
            if is_exact_failed_terminal_return(after_head):
                return
            after_current = self.authenticate_current_execution_context(context)
            stable_execution_fields = tuple(
                name
                for name in BatchCurrentExecutionAuthority.__dataclass_fields__
                if name
                not in {
                    "execution_context_sha256",
                    "state_head_sha256",
                    "_seal_sha256",
                    "_issuer",
                }
            )
            stable_head_fields = tuple(
                name
                for name in BatchStateHead.__dataclass_fields__
                if name
                not in {
                    "sha256",
                    "predecessor_head_sha256",
                    "ancestor_head_sha256s",
                    "project_authority_head_sha256",
                    "project_mutation_attestation_sha256",
                    "pending_training_fit_set_sha256_by_node",
                }
            )
            selected = after_head.pending_training_fit_set_sha256_by_node.get(context.node_id)
            prior = head.pending_training_fit_set_sha256_by_node.get(context.node_id)
            try:
                checkpoint = None if selected is None else self._load_training_fit_set(selected)
                prior_checkpoint = None if prior is None else self._load_training_fit_set(prior)
            except Exception as exc:
                raise BatchStateError(
                    "annotation-classifier return checkpoint is unavailable"
                ) from exc
            prior_entries = () if prior_checkpoint is None else prior_checkpoint.entries
            added_entries = (
                ()
                if checkpoint is None
                else tuple(item for item in checkpoint.entries if item not in prior_entries)
            )
            # A normal fit selects its checkpoint, archives its lease, then
            # retains only that exact completion ref in a second state CAS.
            # Derive both heads; do not accept an arbitrary descendant chain.
            exact_checkpoint_return = False
            if checkpoint is not None and len(added_entries) == 1:
                from .batch_training_checkpoint import BatchNativeFitCheckpointSet

                prior_refs = (
                    {}
                    if prior_checkpoint is None
                    else prior_checkpoint.completed_lease_archive_ref_by_fit_material_sha256
                )
                values = checkpoint.to_mapping()
                values.pop("checkpoint_set_sha256")
                values["entries"] = checkpoint.entries
                values["completed_lease_archive_ref_by_fit_material_sha256"] = prior_refs
                initial_checkpoint = BatchNativeFitCheckpointSet.create(**values)
                pending_fits = dict(head.pending_training_fit_set_sha256_by_node)
                pending_fits[context.node_id] = initial_checkpoint.checkpoint_set_sha256
                expected_fit_head = head.evolve(
                    project_authority_head_sha256=after_head.project_authority_head_sha256,
                    project_mutation_attestation_sha256=after_head.project_mutation_attestation_sha256,
                    pending_training_fit_set_sha256_by_node=pending_fits,
                )
                if checkpoint == initial_checkpoint:
                    exact_checkpoint_return = after_head == expected_fit_head
                else:
                    material_digest = added_entries[0].runtime_fit_material_sha256
                    ref = checkpoint.completed_lease_archive_ref_by_fit_material_sha256.get(
                        material_digest
                    )
                    if (
                        ref is not None
                        and initial_checkpoint.with_completed_lease_archive_reference(
                            material_digest, ref
                        )
                        == checkpoint
                    ):
                        pending_fits[context.node_id] = checkpoint.checkpoint_set_sha256
                        exact_checkpoint_return = after_head == expected_fit_head.evolve(
                            pending_training_fit_set_sha256_by_node=pending_fits,
                        )
            if (
                any(
                    getattr(after_current, name) != getattr(current, name)
                    for name in stable_execution_fields
                )
                or any(
                    getattr(after_head, name) != getattr(head, name) for name in stable_head_fields
                )
                or not exact_checkpoint_return
                or selected is None
                or selected == prior
                or checkpoint is None
                or checkpoint.node_id != context.node_id
                or checkpoint.input_artifact_set_sha256
                != context.authorization.input_artifact_set_sha256
                or (
                    prior_checkpoint is not None
                    and (
                        checkpoint.originating_attempt_authorization_sha256
                        != prior_checkpoint.originating_attempt_authorization_sha256
                        or not set(prior_entries) < set(checkpoint.entries)
                    )
                )
                or len(checkpoint.entries) != len(prior_entries) + 1
                or len(added_entries) != 1
            ):
                raise BatchStateError(
                    "annotation-classifier acquisition authority changed on return"
                )
            added = added_entries[0]
            if (
                added.fit_plan_sha256 != projection.fit_plan_sha256
                or added.fit_execution_sha256 != projection.fit_execution_sha256
                or added.project_relative_path != projection.project_relative_path
                or added.classifier_version != projection.classifier_version
                or added.project_identity_sha256 != head.project_identity_sha256
                or added.predecessor_project_authority_head_sha256
                != head.project_authority_head_sha256
                or added.successor_project_authority_head_sha256
                != after_head.project_authority_head_sha256
                or added.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                or added.mutation_attestation_sha256
                != after_head.project_mutation_attestation_sha256
            ):
                raise BatchStateError(
                    "annotation-classifier return checkpoint differs from its fit"
                )

        try:
            yield session
        except BaseException as body_failure:
            try:
                reauthenticate_return_state()
            except BaseException as return_failure:
                _add_bounded_cleanup_note(
                    body_failure,
                    return_failure,
                    context=("annotation-classifier return reauthentication also failed"),
                )
            raise
        else:
            reauthenticate_return_state()

    @staticmethod
    def _workspace_lease_binding_sha256(lease: BatchWorkspaceLease) -> str:
        return _digest(
            {
                "workspace_root": lease.workspace_root.as_posix(),
                "operation": lease.operation,
                "fencing_token": lease.fencing_token,
            }
        )

    def _batch_project_ref(self, family: str, **natural_key: object) -> AuthorityMemberRefV2:
        try:
            return authority_member_ref_v2(
                self._authority_layout.layout,
                family=family,
                scope={"workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256)},
                natural_key=natural_key,
            )
        except DurableRecordError as exc:
            raise BatchStateError("batch project authority member key is invalid") from exc

    def _publish_batch_project_ref(
        self,
        ref: AuthorityMemberRefV2,
        payload: bytes,
        *,
        label: str,
    ) -> None:
        try:
            family = self._authority_layout.layout.family(ref.family)
            family.validate_payload(payload)
            bucket = self._authority_layout.bucket(family.physical_bucket)
            bucket.publish_immutable(
                ref.physical_name,
                payload,
                max_bytes=family.max_member_bytes,
                label=label,
            )
            reopened = bucket.read_immutable(
                ref.physical_name,
                max_bytes=family.max_member_bytes,
                label=label,
            )
        except DurableRecordError as exc:
            raise BatchStateError(f"{label} publication failed") from exc
        if reopened != payload:
            raise BatchStateError(f"{label} changed after publication")

    def _read_batch_project_ref(
        self, ref: AuthorityMemberRefV2, *, label: str
    ) -> Mapping[str, object]:
        if ref.authority_layout_sha256 != self.authority_layout_sha256:
            raise BatchStateError(f"{label} belongs to another authority layout")
        try:
            family = self._authority_layout.layout.family(ref.family)
            payload = self._authority_layout.bucket(family.physical_bucket).read_immutable(
                ref.physical_name,
                max_bytes=family.max_member_bytes,
                label=label,
            )
            if payload is None:
                raise BatchStateError(f"{label} bytes are unavailable")
            mapping = json.loads(payload.decode("utf-8", errors="strict"))
        except (DurableRecordError, UnicodeError, json.JSONDecodeError) as exc:
            raise BatchStateError(f"{label} bytes are unavailable") from exc
        if not isinstance(mapping, Mapping) or _canonical_bytes(mapping) != payload:
            raise BatchStateError(f"{label} bytes are not canonical")
        return mapping

    @_guarded_batch_state_lease_operation
    def prepare_project_mutation_artifact_inventory(
        self,
        *,
        request: object,
        expected_project_identity_sha256: str | None,
        static_targets: tuple[object, ...],
        dynamic_targets: tuple[object, ...],
        lease: BatchWorkspaceLease,
    ) -> "object":
        """Prepare a closed inventory from one persisted request capability."""

        self._assert_lease(lease)
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationArtifactTarget,
            ProjectMutationRequestCapability,
        )

        if not isinstance(request, ProjectMutationRequestCapability):
            raise BatchStateError("artifact inventory requires a request capability")
        self._authenticate_project_request_capability(request)
        binding = self._workspace_lease_binding_sha256(lease)
        if request.workspace_lease_binding_sha256 != binding:
            raise BatchStateError("project request belongs to another workspace lease")
        if not all(
            isinstance(item, ProjectMutationArtifactTarget)
            for item in (*static_targets, *dynamic_targets)
        ):
            raise BatchStateError("artifact inventory targets must be typed")
        return ProjectMutationArtifactInventory.create(
            mutation_request_sha256=request.mutation_request_sha256,
            mutation_authorization_sha256=request.mutation_authorization_sha256,
            expected_project_identity_sha256=expected_project_identity_sha256,
            static_targets=tuple(static_targets),
            dynamic_targets=tuple(dynamic_targets),
        )

    @_guarded_batch_state_lease_operation
    def publish_project_mutation_artifact_inventory(
        self,
        inventory: object,
        *,
        lease: BatchWorkspaceLease,
    ) -> "object":
        self._assert_lease(lease)
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationArtifactInventoryCapability,
        )

        if not isinstance(inventory, ProjectMutationArtifactInventory):
            raise BatchStateError("artifact inventory must use its current schema")
        ref = self._batch_project_ref(
            "batch_project_inventory",
            inventory_sha256=inventory.artifact_inventory_sha256,
        )
        path = self.layout.catalogue / ref.physical_name
        self._publish_batch_project_ref(
            ref,
            _canonical_bytes(inventory.to_mapping()),
            label="artifact inventory",
        )
        binding = self._workspace_lease_binding_sha256(lease)
        capability_binding = _digest(
            {
                "artifact_inventory_sha256": inventory.artifact_inventory_sha256,
                "authority_layout_sha256": self.authority_layout_sha256,
                "workspace_storage_scope_sha256": self.workspace_storage_scope_sha256,
                "persisted_ref": ref.to_mapping(),
                "workspace_lease_binding_sha256": binding,
            }
        )
        capability = ProjectMutationArtifactInventoryCapability._issue(
            inventory=inventory,
            persisted_path=path,
            workspace_lease_binding_sha256=binding,
            artifact_inventory_capability_binding_sha256=capability_binding,
            _store_issuer=self,
        )
        self._issued_project_inventory_capabilities[id(capability)] = (
            capability,
            lease,
            ref,
        )
        return capability

    @_guarded_batch_state_lease_operation
    def reopen_project_mutation_artifact_inventory(
        self,
        artifact_inventory_sha256: str,
        *,
        lease: BatchWorkspaceLease,
    ) -> "object":
        self._assert_lease(lease)
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationArtifactInventory

        digest = _sha256(artifact_inventory_sha256, "artifact_inventory_sha256")
        ref = self._batch_project_ref("batch_project_inventory", inventory_sha256=digest)
        mapping = self._read_batch_project_ref(ref, label="artifact inventory")
        inventory = ProjectMutationArtifactInventory.from_mapping(mapping)
        if inventory.artifact_inventory_sha256 != digest:
            raise BatchStateError("artifact inventory filename disagrees")
        return self.publish_project_mutation_artifact_inventory(inventory, lease=lease)

    @_guarded_batch_state_lease_operation
    def publish_project_mutation_acquisition_record(
        self,
        *,
        request: object,
        inventory: object,
        operation: str,
        intent: Literal["create", "open_existing", "relocate", "cloned_as_new"],
        project_uri: str,
        expected_project_identity_sha256: str | None,
        expected_authority_head_sha256: str | None,
        recovery_predecessor_acquisition_record_sha256: str | None,
        lease: BatchWorkspaceLease,
    ) -> "object":
        if operation == "registration_project_write":
            raise BatchStateError("registration acquisition requires its exact state owner")
        return self._publish_project_mutation_acquisition_record_owned(
            request=request,
            inventory=inventory,
            operation=operation,
            intent=intent,
            project_uri=project_uri,
            expected_project_identity_sha256=expected_project_identity_sha256,
            expected_authority_head_sha256=expected_authority_head_sha256,
            recovery_predecessor_acquisition_record_sha256=(
                recovery_predecessor_acquisition_record_sha256
            ),
            lease=lease,
        )

    def _publish_project_mutation_acquisition_record_owned(
        self,
        *,
        request: object,
        inventory: object,
        operation: str,
        intent: Literal["create", "open_existing", "relocate", "cloned_as_new"],
        project_uri: str,
        expected_project_identity_sha256: str | None,
        expected_authority_head_sha256: str | None,
        recovery_predecessor_acquisition_record_sha256: str | None,
        lease: BatchWorkspaceLease,
    ) -> "object":
        self._assert_lease(lease)
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationAcquisitionRecord,
            ProjectMutationArtifactInventoryCapability,
            ProjectMutationRequestCapability,
        )

        binding = self._workspace_lease_binding_sha256(lease)
        self._authenticate_project_request_capability(
            request,
            expected_kind=(
                "project_stage"
                if operation == "project_stage"
                else (
                    "annotation_classifier"
                    if operation == "annotation_classifier"
                    else (
                        "registration_project"
                        if operation == "registration_project_write"
                        else "catalogue_upsert"
                    )
                )
            ),
        )
        self._authenticate_project_inventory_capability(inventory)
        if (
            not isinstance(request, ProjectMutationRequestCapability)
            or not isinstance(inventory, ProjectMutationArtifactInventoryCapability)
            or request.workspace_lease_binding_sha256 != binding
            or inventory.workspace_lease_binding_sha256 != binding
            or inventory._store_issuer is not self
            or inventory.inventory.mutation_request_sha256 != request.mutation_request_sha256
            or inventory.inventory.mutation_authorization_sha256
            != request.mutation_authorization_sha256
            or inventory.inventory.expected_project_identity_sha256
            != expected_project_identity_sha256
        ):
            raise BatchStateError("project acquisition capabilities disagree")
        record = ProjectMutationAcquisitionRecord.create(
            mutation_request_sha256=request.mutation_request_sha256,
            mutation_authorization_sha256=request.mutation_authorization_sha256,
            artifact_inventory_sha256=inventory.inventory.artifact_inventory_sha256,
            operation=operation,
            intent=intent,
            project_uri=project_uri,
            expected_project_identity_sha256=expected_project_identity_sha256,
            expected_authority_head_sha256=expected_authority_head_sha256,
            recovery_predecessor_acquisition_record_sha256=(
                recovery_predecessor_acquisition_record_sha256
            ),
        )
        ref = self._batch_project_ref(
            "batch_project_acquisition",
            acquisition_sha256=record.acquisition_record_sha256,
        )
        self._publish_batch_project_ref(
            ref,
            _canonical_bytes(record.to_mapping()),
            label="project acquisition record",
        )
        return record

    def _authenticate_project_request_capability(
        self,
        request: object,
        *,
        expected_kind: (
            Literal[
                "catalogue_upsert",
                "project_stage",
                "annotation_classifier",
                "registration_project",
            ]
            | None
        ) = None,
    ) -> None:
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationRequestCapability,
        )

        if not isinstance(request, ProjectMutationRequestCapability):
            raise BatchStateError("project request capability is invalid")
        issuer = request._store_issuer
        if (
            getattr(issuer, "_registered_store_issuer", None)
            is not self._project_durability_capability
        ):
            raise BatchStateError("project request capability is from a foreign store")
        authenticate = getattr(issuer, "_authenticate_request_capability", None)
        if not callable(authenticate):
            raise BatchStateError("project request store cannot authenticate capability")
        try:
            authenticate(request, expected_kind=expected_kind)
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError(
                "project request capability kind or retained authority differs"
            ) from exc

    def _authenticate_project_inventory_capability(self, inventory: object) -> None:
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactInventory,
            ProjectMutationArtifactInventoryCapability,
        )

        if (
            not isinstance(inventory, ProjectMutationArtifactInventoryCapability)
            or inventory._store_issuer is not self
        ):
            raise BatchStateError("artifact inventory capability is foreign")
        issued = self._issued_project_inventory_capabilities.get(id(inventory))
        if issued is None or issued[0] is not inventory:
            raise BatchStateError("artifact inventory capability is not store-issued")
        lease = issued[1]
        issued_ref = issued[2]
        self._assert_lease(lease)
        binding = self._workspace_lease_binding_sha256(lease)
        expected_ref = self._batch_project_ref(
            "batch_project_inventory", inventory_sha256=inventory.inventory.artifact_inventory_sha256
        )
        expected_path = self.layout.catalogue / expected_ref.physical_name
        expected_binding = _digest(
            {
                "artifact_inventory_sha256": (inventory.inventory.artifact_inventory_sha256),
                "authority_layout_sha256": self.authority_layout_sha256,
                "workspace_storage_scope_sha256": self.workspace_storage_scope_sha256,
                "persisted_ref": expected_ref.to_mapping(),
                "workspace_lease_binding_sha256": binding,
            }
        )
        if (
            inventory.workspace_lease_binding_sha256 != binding
            or inventory.persisted_path != expected_path
            or issued_ref != expected_ref
            or inventory.artifact_inventory_capability_binding_sha256 != expected_binding
        ):
            raise BatchStateError("artifact inventory capability binding differs")
        mapping = self._read_batch_project_ref(expected_ref, label="artifact inventory")
        reopened = ProjectMutationArtifactInventory.from_mapping(mapping)
        if reopened != inventory.inventory:
            raise BatchStateError("artifact inventory capability bytes differ")

    def _authenticate_project_acquisition_record(self, record: object) -> None:
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationAcquisitionRecord,
        )

        if not isinstance(record, ProjectMutationAcquisitionRecord):
            raise BatchStateError("project acquisition record is invalid")
        ref = self._batch_project_ref(
            "batch_project_acquisition",
            acquisition_sha256=record.acquisition_record_sha256,
        )
        mapping = self._read_batch_project_ref(ref, label="project acquisition record")
        reopened = ProjectMutationAcquisitionRecord.from_mapping(mapping)
        if reopened != record:
            raise BatchStateError("project acquisition record bytes differ")

    def _authenticate_project_acquisition_material(
        self,
        *,
        record: object,
        request: object,
        inventory: object,
        workspace_lease_binding_sha256: str,
    ) -> None:
        self._assert_live_workspace_lease_binding(workspace_lease_binding_sha256)
        operation = getattr(record, "operation", None)
        self._authenticate_project_request_capability(
            request,
            expected_kind=(
                "project_stage"
                if operation == "project_stage"
                else (
                    "annotation_classifier"
                    if operation == "annotation_classifier"
                    else (
                        "registration_project"
                        if operation == "registration_project_write"
                        else "catalogue_upsert"
                    )
                )
            ),
        )
        self._authenticate_project_inventory_capability(inventory)
        self._authenticate_project_acquisition_record(record)

    @_guarded_batch_state_lease_operation
    def reopen_project_mutation_acquisition_record(
        self,
        acquisition_record_sha256: str,
        *,
        lease: BatchWorkspaceLease,
    ) -> "object":
        self._assert_lease(lease)
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationAcquisitionRecord

        digest = _sha256(acquisition_record_sha256, "acquisition_record_sha256")
        ref = self._batch_project_ref("batch_project_acquisition", acquisition_sha256=digest)
        mapping = self._read_batch_project_ref(ref, label="project acquisition record")
        record = ProjectMutationAcquisitionRecord.from_mapping(mapping)
        if record.acquisition_record_sha256 != digest:
            raise BatchStateError("project acquisition record filename disagrees")
        return record

    def _owned_path(self, path: Path, owner: Path, field: str) -> Path:
        _reject_symlink_path(path, owner, field)
        _resolved_child(path, owner, field)
        return path

    def _publish_immutable(self, path: Path, payload: bytes, owner: Path) -> None:
        self._owned_path(path, owner, "immutable publication")
        try:
            self._publication_provider.publish_immutable(owner, path.name, payload)
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError("immutable secure publication failed") from exc

    def _atomic_replace(self, path: Path, payload: bytes, owner: Path) -> None:
        self._owned_path(path, owner, "atomic state head")
        try:
            self._publication_provider.atomic_replace(owner, path.name, payload)
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError("secure atomic state replacement failed") from exc

    def _install_plan(self, plan: BatchPlan) -> _PlanSnapshot:
        if not isinstance(plan, BatchPlan):
            raise BatchStateError("state store requires an exact BatchPlan")
        try:
            canonical_plan = replace(plan)
        except (BatchPlanError, TypeError, ValueError) as exc:
            raise BatchStateError("batch plan is not internally canonical") from exc
        payload = _canonical_bytes(canonical_plan.to_mapping())
        snapshot = _plan_snapshot_from_bytes(payload)
        if snapshot.plan_sha256 != plan.plan_sha256:
            raise BatchStateError("plan bytes do not match plan identity")
        path = self.layout.plans / f"{plan.plan_sha256}.json"
        runtime_contract_path = self._plan_runtime_contract_path(plan.plan_sha256)
        self._publish_immutable(
            runtime_contract_path,
            _canonical_bytes(_plan_runtime_contract_material(canonical_plan)),
            self.layout.plans,
        )
        self._publish_immutable(path, payload, self.layout.plans)
        return snapshot

    def _plan_runtime_contract_path(self, digest: str) -> Path:
        return self.layout.plans / (f"{_sha256(digest, 'plan digest')}.runtime-contracts")

    def _load_plan_snapshot(self, digest: str) -> _PlanSnapshot:
        """Reopen plan bytes and reuse their private decoded view within this owner.

        Path and file reads run on every access. Only identical payload bytes
        reuse parsing; changed bytes still pass the full canonical/hash parser.
        These read-only views do not cache current-head or execution permissions.
        """
        digest = _sha256(digest, "plan digest")
        path = self.layout.plans / f"{digest}.json"
        self._owned_path(path, self.layout.plans, "persisted plan")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise BatchStateError("active plan bytes are missing") from exc
        with self._lease_condition:
            snapshot = self._plan_parse_cache.pop(payload, None)
            if snapshot is not None:
                self._plan_parse_cache[payload] = snapshot
        if snapshot is None:
            snapshot = _plan_snapshot_from_bytes(payload)
        if snapshot.plan_sha256 != digest:
            raise BatchStateError("plan path and checksum disagree")
        if len(payload) <= _PLAN_PARSE_CACHE_MAX_BYTES:
            with self._lease_condition:
                # Another reader may have parsed the same bytes concurrently.
                if payload not in self._plan_parse_cache:
                    self._plan_parse_cache[payload] = snapshot
                    self._plan_parse_cache_bytes += len(payload)
                while (
                    len(self._plan_parse_cache) > _PLAN_PARSE_CACHE_MAX_ENTRIES
                    or self._plan_parse_cache_bytes > _PLAN_PARSE_CACHE_MAX_BYTES
                ):
                    oldest = next(iter(self._plan_parse_cache))
                    self._plan_parse_cache.pop(oldest)
                    self._plan_parse_cache_bytes -= len(oldest)
        return snapshot

    def _load_typed_plan(self, digest: str) -> BatchPlan:
        """Read the selected plan once per digest during this open execution.

        A changed digest replaces the single retained plan after its files and
        runtime contracts pass validation. Full recovery clears this slot before
        reopening persisted records; current source checks remain at admission.
        """
        digest = _sha256(digest, "plan digest")
        cached = self._typed_plan_cache
        if cached is not None and cached.plan_sha256 == digest:
            return cached
        path = self.layout.plans / f"{digest}.json"
        contract_path = self._plan_runtime_contract_path(digest)
        self._owned_path(path, self.layout.plans, "persisted plan")
        self._owned_path(contract_path, self.layout.plans, "plan runtime contract material")
        try:
            payload = path.read_bytes()
            mapping = json.loads(payload.decode("utf-8"))
            contract_payload = contract_path.read_bytes()
            contract_mapping = json.loads(contract_payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("typed plan authority is missing or invalid") from exc
        if (
            _canonical_bytes(mapping) != payload
            or _canonical_bytes(contract_mapping) != contract_payload
        ):
            raise BatchStateError("typed plan authority is not canonical")
        from mif_pipeline.pipeline_core.schema_registry import schema_validation_snapshot

        # All nodes in this plan use the same installed format definitions.
        with schema_validation_snapshot():
            snapshot = _plan_snapshot_from_bytes(payload)
            if snapshot.plan_sha256 != digest:
                raise BatchStateError("typed plan path and checksum disagree")
            contracts = _plan_contract_catalog(contract_mapping, digest)
            plan = _plan_from_material(_mapping(mapping, "persisted batch plan"), contracts)
        self._typed_plan_cache = plan
        return plan

    def _validate_source_manifest_history(self, snapshots, manifests=None, *, head, attempts=None, setup_materials=None, typed_plans=None, secondary_definition_materials=None) -> None:
        """Reopen image replacements and saved-model choices from their accepted steps."""
        from .batch_source_revision import source_revision_sample
        manifests = {} if manifests is None else manifests
        previous = None
        for snapshot in snapshots:
            digest = snapshot.normalized_manifest_sha256
            if digest not in manifests:
                manifests[digest] = self.retained_manifest(digest)
            if previous is not None and previous.normalized_manifest_sha256 != digest:
                before, after = manifests[previous.normalized_manifest_sha256], manifests[digest]
                from .batch_manifest import is_memory_limit_change
                if is_memory_limit_change(before, after):
                    self._validate_memory_limit_plan(previous.plan_sha256, snapshot.plan_sha256, after, typed_plans=typed_plans)
                    previous = snapshot
                    continue
                from .batch_training.accepted_model_reuse import validate_classifier_manifest_transition
                from .batch_training import BatchTrainingError
                try:
                    validate_classifier_manifest_transition(before, after)
                except BatchTrainingError:
                    pass
                else:
                    self._validate_classifier_manifest_history(previous, snapshot, before, after, head, attempts, setup_materials)
                    previous = snapshot
                    continue
                from .batch_secondary_definitions import validate_secondary_profile_manifest_transition
                try:
                    validate_secondary_profile_manifest_transition(before, after)
                except BatchArtifactError:
                    pass
                else:
                    self._validate_secondary_profile_manifest_history(previous, snapshot, before, after, head, attempts, secondary_definition_materials)
                    previous = snapshot
                    continue
                sample_id = source_revision_sample(before, after)
                old = next((node for node in previous.node_by_id.values()
                    if node["kind"] == "inspection" and tuple(node["participant_ids"]) == (sample_id,)), None)
                current = next((node for node in snapshot.node_by_id.values()
                    if node["kind"] == "inspection" and tuple(node["participant_ids"]) == (sample_id,)), None)
                if old is None or current is None or current.get("settled_review_revision", {}).get("predecessor_node_id") != old["node_id"]:
                    raise BatchStateError("source history lacks its exact inspection predecessor")
                from .batch_source_revision import source_processing_root
                binding = SettledReviewRevision.from_mapping(current["settled_review_revision"])
                sample = next(item for item in after.manifest.samples if item.sample_id == sample_id)
                if sample.processing_root is None or sample.processing_root != source_processing_root(
                        before, sample_id, binding, sample.processing_root.parents[3]):
                    raise BatchStateError("source history lacks its inspection-owned processing placement")
            previous = snapshot

    def _validate_classifier_manifest_history(self, previous, current, before, after, head, attempts, setup_materials=None):
        """Match a declaration change to one completed setup in its predecessor plan."""
        from .batch_training.runtime import TrainingPlanSetStore
        from .batch_training.planning import _training_plan_set_compact_facts
        from .batch_training.accepted_model_reuse import _manifest_for_classifier_setup_material
        matches = 0
        from contextlib import nullcontext
        owner = (TrainingPlanSetStore(self.layout.state / "training-plan-sets")
                 if setup_materials is None else nullcontext())
        with owner as store:
            for node_id, node in previous.node_by_id.items():
                if (node["kind"] != "primary_classifier_training_plan_review"
                        or current.node_by_id.get(node_id) != node):
                    continue
                for attempt_id in head.attempt_ids_by_node.get(node_id, ()):
                    digest = head.terminal_attempt_sha256_by_attempt_id.get(attempt_id)
                    if digest is None:
                        continue
                    attempt = self.load_attempt(digest) if attempts is None else attempts[digest][1]
                    if attempt.node_id != node_id or attempt.stage_result.status is not StageStatus.COMPLETED:
                        continue
                    for artifact in attempt.output_artifacts:
                        identity = artifact.owning_identity
                        if (artifact.artifact_kind != "training_plan_set"
                                or identity.get("batch_plan_sha256") != previous.plan_sha256
                                or identity.get("normalized_manifest_sha256") != before.normalized_manifest_sha256):
                            continue
                        if setup_materials is None:
                            if identity["store_identity_sha256"] != store.store_identity_sha256:
                                raise BatchStateError("Saved classifier history belongs to another setup store")
                            material = store.reopen(identity["store_relative_locator"],
                                sha256=identity["body_sha256"], byte_count=identity["body_byte_count"])
                        else:
                            material = setup_materials[identity["body_sha256"]]
                        facts = _training_plan_set_compact_facts(material)
                        if (facts["training_plan_set_sha256"] != identity["training_plan_set_sha256"]
                                or facts["participant_ids"] != tuple(node["participant_ids"])
                                or facts["analysis_domain_binding_set_sha256"] != identity["analysis_domain_binding_set_sha256"]
                                or facts["normalized_manifest_sha256"] != before.normalized_manifest_sha256):
                            raise BatchStateError("Saved classifier history differs from its accepted setup")
                        expected = _manifest_for_classifier_setup_material(before, material, facts["participant_ids"])
                        if expected.normalized_manifest_bytes == after.normalized_manifest_bytes:
                            matches += 1
        if matches != 1:
            raise BatchStateError("Saved classifier history lacks one exact accepted setup")

    def _validate_secondary_profile_manifest_history(self, previous, current, before, after, head, attempts, materials=None):
        """Reproduce a profile-only successor from its authenticated completed choice."""
        from .batch_secondary_definitions import (
            NODE_KIND, ARTIFACT_KIND, _decode_definition_material, _manifest_for_definition_material,
        )
        matches = 0
        for node_id, node in previous.node_by_id.items():
            if node["kind"] != NODE_KIND or current.node_by_id.get(node_id) != node:
                continue
            for attempt_id in head.attempt_ids_by_node.get(node_id, ()):
                digest = head.terminal_attempt_sha256_by_attempt_id.get(attempt_id)
                if digest is None:
                    continue
                attempt = self.load_attempt(digest) if attempts is None else attempts[digest][1]
                if attempt.node_id != node_id or attempt.stage_result.status is not StageStatus.COMPLETED:
                    continue
                for artifact in attempt.output_artifacts:
                    if artifact.artifact_kind != ARTIFACT_KIND:
                        continue
                    material = (_decode_definition_material(artifact,
                        record_directory=self.layout.state / "secondary-definitions")
                        if materials is None else materials[artifact.owning_identity["body_sha256"]])
                    context, _structures, declarations = material
                    if (tuple(context["participant_ids"]) != tuple(node["participant_ids"])
                            or context["validation_domain_id"] != node["validation_domain_id"]):
                        raise BatchStateError("Criterion profile history differs from its accepted group")
                    expected = _manifest_for_definition_material(before, context, declarations)
                    if expected.normalized_manifest_bytes == after.normalized_manifest_bytes:
                        matches += 1
        if matches != 1:
            raise BatchStateError("Criterion profile history lacks one exact accepted definition choice")

    def retained_manifest(self, normalized_sha256: str | None = None) -> ValidatedBatchManifest:
        """Reuse one checked manifest selected by a current head or an admission plan."""
        from .batch_manifest import read_retained_manifest
        if normalized_sha256 is None:
            normalized_sha256 = self.load_head().normalized_manifest_sha256
        cached = self._retained_manifest_cache
        if cached is not None and cached.normalized_manifest_sha256 == normalized_sha256:
            return cached
        manifest = read_retained_manifest(self.layout.manifest, normalized_sha256)
        self._retained_manifest_cache = manifest
        return manifest

    def _retain_manifest(self, manifest: ValidatedBatchManifest) -> None:
        """Publish immutable source selections before the single head switches to them."""
        original, normalized = self.layout.manifest_paths(manifest.normalized_manifest_sha256)
        _reject_symlink_path(original.parent, self.layout.manifest, "manifest generation")
        original.parent.mkdir(exist_ok=True)
        descriptor = os.open(self.layout.manifest, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._publish_immutable(original, manifest.original_manifest_bytes, original.parent)
        self._publish_immutable(normalized, manifest.normalized_manifest_bytes, normalized.parent)

    def prepare_initialization(
        self,
        *,
        original_manifest_bytes: bytes,
        validated_manifest: ValidatedBatchManifest,
        designation_request: object,
        artifact_inventory: object,
        acquisition_record: object,
        lease: BatchWorkspaceLease,
    ) -> PreparedBatchInitialization:
        """Persist the exact pre-state graph before project authority is acquired."""

        self._assert_lease(lease)
        from mif_pipeline.orchestration.batch_project import ProjectDesignationRequest
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationAcquisitionRecord,
            ProjectMutationArtifactInventoryCapability,
        )

        if self.layout.state_head_path.exists():
            raise BatchStateError("batch state is already initialized")
        if (
            not isinstance(validated_manifest, ValidatedBatchManifest)
            or original_manifest_bytes != validated_manifest.original_manifest_bytes
            or not isinstance(designation_request, ProjectDesignationRequest)
            or not isinstance(artifact_inventory, ProjectMutationArtifactInventoryCapability)
            or artifact_inventory._store_issuer is not self
            or not isinstance(acquisition_record, ProjectMutationAcquisitionRecord)
        ):
            raise BatchStateError("initialization inputs are foreign")
        binding = self._workspace_lease_binding_sha256(lease)
        inventory = artifact_inventory.inventory
        if (
            artifact_inventory.workspace_lease_binding_sha256 != binding
            or inventory.mutation_request_sha256 != designation_request.request_sha256
            or inventory.mutation_authorization_sha256
            != designation_request.authorizing_record_sha256
            or acquisition_record.mutation_request_sha256 != designation_request.request_sha256
            or acquisition_record.mutation_authorization_sha256
            != designation_request.authorizing_record_sha256
            or acquisition_record.artifact_inventory_sha256 != inventory.artifact_inventory_sha256
            or acquisition_record.operation != "identity_materialization"
            or acquisition_record.intent != designation_request.intent
            or acquisition_record.project_uri != designation_request.project_uri
        ):
            raise BatchStateError("initialization project authorities disagree")
        retained = revalidate_retained_manifest(
            original_manifest_bytes,
            validated_manifest.normalized_manifest_bytes,
        )
        if retained != validated_manifest:
            raise BatchStateError("initialization manifest evidence changed")
        self._retain_manifest(validated_manifest)
        journal = BatchInitializationJournal.create(
            batch_id=validated_manifest.manifest.batch_id,
            original_manifest_sha256=validated_manifest.original_manifest_sha256,
            normalized_manifest_sha256=(validated_manifest.normalized_manifest_sha256),
            project_designation_request_sha256=designation_request.request_sha256,
            project_mutation_artifact_inventory_sha256=(inventory.artifact_inventory_sha256),
            project_mutation_acquisition_record_sha256=(
                acquisition_record.acquisition_record_sha256
            ),
        )
        self.layout.initialization_directory.mkdir(parents=True, exist_ok=True)
        self._publish_immutable(
            self.layout.initialization_directory / f"{journal.journal_sha256}.json",
            _canonical_bytes(journal.to_mapping()),
            self.layout.initialization_directory,
        )
        self._atomic_replace(
            self.layout.initialization_active_path,
            _canonical_bytes(
                {
                    "schema_name": "mif-batch-initialization-active",
                    "schema_version": 1,
                    "journal_sha256": journal.journal_sha256,
                }
            ),
            self.layout.state,
        )
        return PreparedBatchInitialization._issue(
            journal=journal,
            validated_manifest=validated_manifest,
            designation_request=designation_request,
            artifact_inventory=artifact_inventory,
            acquisition_record=acquisition_record,
            _issuer=self._initialization_capability,
        )

    def _reopen_prepared_initialization(
        self, lease: BatchWorkspaceLease
    ) -> PreparedBatchInitialization:
        self._assert_lease(lease)
        from mif_pipeline.orchestration.batch_project import ProjectDesignationRequest

        try:
            pointer_bytes = self.layout.initialization_active_path.read_bytes()
            pointer = json.loads(pointer_bytes.decode("utf-8"))
            pointer = _closed_mapping(
                pointer,
                fields=frozenset({"schema_name", "schema_version", "journal_sha256"}),
                field="initialization active pointer",
            )
            if (
                pointer["schema_name"] != "mif-batch-initialization-active"
                or pointer["schema_version"] != 1
            ):
                raise BatchStateError("initialization pointer schema is unsupported")
            journal_sha256 = _sha256(pointer["journal_sha256"], "initialization journal digest")
            journal_path = self.layout.initialization_directory / f"{journal_sha256}.json"
            journal_bytes = journal_path.read_bytes()
            journal = BatchInitializationJournal.from_mapping(
                json.loads(journal_bytes.decode("utf-8"))
            )
            if (
                journal.journal_sha256 != journal_sha256
                or _canonical_bytes(journal.to_mapping()) != journal_bytes
            ):
                raise BatchStateError("initialization journal bytes changed")
            original = self.retained_manifest(journal.normalized_manifest_sha256).original_manifest_bytes
            normalized = self.retained_manifest(journal.normalized_manifest_sha256).normalized_manifest_bytes
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("retained initialization evidence is unavailable") from exc
        manifest = revalidate_retained_manifest(original, normalized)
        if (
            manifest.original_manifest_sha256 != journal.original_manifest_sha256
            or manifest.normalized_manifest_sha256 != journal.normalized_manifest_sha256
            or manifest.manifest.batch_id != journal.batch_id
        ):
            raise BatchStateError("initialization manifest and journal disagree")
        catalogue = self.project_catalogue_store(lease)
        try:
            request, request_capability = catalogue.reopen_designation_request(
                journal.project_designation_request_sha256,
                workspace_lease=lease,
            )
        except ValueError as exc:
            raise BatchStateError("initialization designation request is unavailable") from exc
        if not isinstance(request, ProjectDesignationRequest):
            raise BatchStateError("initialization designation request is invalid")
        inventory = self.reopen_project_mutation_artifact_inventory(
            journal.project_mutation_artifact_inventory_sha256,
            lease=lease,
        )
        acquisition = self.reopen_project_mutation_acquisition_record(
            journal.project_mutation_acquisition_record_sha256,
            lease=lease,
        )
        if (
            acquisition.mutation_request_sha256 != request.request_sha256
            or acquisition.artifact_inventory_sha256
            != inventory.inventory.artifact_inventory_sha256
            or inventory.inventory.mutation_request_sha256 != request.request_sha256
        ):
            raise BatchStateError("retained initialization authorities disagree")
        # Re-bind the inventory to the request capability reopened under this
        # exact workspace lease, not to a capability retained from a prior process.
        if request_capability.workspace_lease_binding_sha256 != (
            inventory.workspace_lease_binding_sha256
        ):
            raise BatchStateError("reopened initialization lease bindings disagree")
        return PreparedBatchInitialization._issue(
            journal=journal,
            validated_manifest=manifest,
            designation_request=request,
            artifact_inventory=inventory,
            acquisition_record=acquisition,
            _issuer=self._initialization_capability,
        )

    @_guarded_batch_state_lease_context
    @contextmanager
    def reopen_pre_state_initialization(
        self,
        *,
        project_leases: object,
        lease: BatchWorkspaceLease,
    ) -> Iterator[PreStateInitializationRecovery]:
        """Reopen exact retained authority and acquire only its recovery path."""

        self._assert_lease(lease)
        prepared = self._reopen_prepared_initialization(lease)
        catalogue = self.project_catalogue_store(lease)
        request_capability = catalogue.reopen_request(
            prepared.designation_request.request_sha256,
            mutation_authorization_sha256=(prepared.designation_request.authorizing_record_sha256),
            workspace_lease=lease,
        )
        inventory = self.reopen_project_mutation_artifact_inventory(
            prepared.journal.project_mutation_artifact_inventory_sha256,
            lease=lease,
        )
        durability = self.project_mutation_durability_authority(lease)
        issue = getattr(project_leases, "issue_acquisition_session", None)
        discover = getattr(project_leases, "discover_pre_state_recovery", None)
        if not callable(issue) or not callable(discover):
            raise BatchStateError("project lease provider lacks recovery operations")
        session = issue(
            prepared.acquisition_record,
            request=request_capability,
            inventory=inventory,
            durability_authority=durability,
        )
        discovery = discover(session)
        recovery_kind = getattr(discovery, "recovery_kind", None)
        if recovery_kind == "not_started":
            operation = getattr(project_leases, "acquire", None)
            kwargs: dict[str, object] = {}
        else:
            operation = getattr(project_leases, "recover", None)
            stale = getattr(discovery, "stale_active_record_sha256", None)
            if stale is None:
                raise BatchStateError("pre-state recovery lacks its active record")
            kwargs = {"stale_active_record_sha256": stale}
        if not callable(operation):
            raise BatchStateError("project lease provider cannot acquire recovery")
        with operation(session, **kwargs) as project_lease:
            designation = project_lease.designation
            yield PreStateInitializationRecovery._issue(
                prepared=prepared,
                project_discovery=discovery,
                project_lease=project_lease,
                designation=designation,
                _issuer=self._initialization_capability,
            )

    def _initial_plan_for_manifest(self, manifest: ValidatedBatchManifest) -> _PlanSnapshot:
        candidates: list[_PlanSnapshot] = []
        for path in self.layout.plans.glob("*.json"):
            try:
                candidates.append(_plan_snapshot_from_bytes(path.read_bytes()))
            except (OSError, BatchStateError) as exc:
                raise BatchStateError("retained initialization plan is invalid") from exc
        candidates = [
            item
            for item in candidates
            if item.revision == 1
            and item.predecessor_plan_sha256 is None
            and item.batch_id == manifest.manifest.batch_id
            and item.normalized_manifest_sha256 == manifest.normalized_manifest_sha256
        ]
        if len(candidates) != 1:
            raise BatchStateError("initialization requires one exact revision-one plan")
        return candidates[0]

    @_guarded_batch_state_lease_operation
    def commit_initialization(
        self,
        prepared: PreparedBatchInitialization,
        designation: object,
        *,
        project_lease: object | None,
        lease: BatchWorkspaceLease,
    ) -> tuple[BatchStateHead, BatchProjectMutationDurabilityReceipt | None]:
        self._assert_lease(lease)
        from mif_pipeline.qupath_runtime.catalogue import ProjectDesignation

        if (
            not isinstance(prepared, PreparedBatchInitialization)
            or prepared._issuer is not self._initialization_capability
            or not isinstance(designation, ProjectDesignation)
        ):
            raise BatchStateError("initialization commit authority is foreign or stale")
        if project_lease is not None and (
            not getattr(project_lease, "is_live", False)
            or getattr(project_lease, "designation", None) != designation
        ):
            raise BatchStateError("initialization project lease is stale")
        plan = self._initial_plan_for_manifest(prepared.validated_manifest)
        initial = self._build_initial_head(plan, prepared.validated_manifest, designation)
        if self.layout.state_head_path.exists():
            if self.load_head() != initial:
                raise BatchStateError("retained revision-one state disagrees with initialization")
        else:
            self._write_current_head(initial)
            self._publish_current_progress(initial)
        if project_lease is None:
            return initial, None
        receipt = BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="initial_state",
            workspace_lease_binding_sha256=self._workspace_lease_binding_sha256(lease),
            acquisition_record_sha256=project_lease.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(
                project_lease.acquisition_session_challenge_sha256
            ),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=project_lease.mutation_request_sha256,
            mutation_authorization_sha256=(project_lease.mutation_authorization_sha256),
            artifact_inventory_sha256=project_lease.artifact_inventory_sha256,
            project_identity_sha256=designation.identity.project_identity_sha256,
            project_fencing_token_sha256=project_lease.fencing_token_sha256,
            project_authority_head_sha256=(
                project_lease.current_authority_head.authority_head_sha256
            ),
            mutation_attestation_sha256=(
                project_lease.current_authority_head.latest_mutation_attestation_sha256
            ),
            terminal_attempt_record_sha256s=(),
            committed_batch_state_head_sha256=initial.sha256,
        )
        return initial, receipt

    def _finish_initialization(self, prepared: PreparedBatchInitialization) -> None:
        if prepared._issuer is not self._initialization_capability:
            raise BatchStateError("initialization capability is foreign")
        try:
            self.layout.initialization_active_path.unlink()
        except FileNotFoundError:
            pass

    def _build_initial_head(
        self,
        plan: _PlanSnapshot,
        manifest: ValidatedBatchManifest,
        designation: object,
    ) -> BatchStateHead:
        from mif_pipeline.qupath_runtime.catalogue import ProjectDesignation

        if not isinstance(designation, ProjectDesignation):
            raise BatchStateError("initial state requires a project designation")
        project = designation.project_path
        identity = designation.identity
        authority_head = designation.authority_head
        source_hashes = {
            item.sample_id: item.source_file_sha256 or _file_sha256(item.source_mif_path)
            for item in manifest.samples
        }
        scope_index = {
            node_id: {
                "sample_ids": list(node["participant_ids"]),
                "validation_domain_id": node["validation_domain_id"],
                "analysis_domain_ids": [
                    item["analysis_domain_id"] for item in node["analysis_domain_bindings"]
                ],
            }
            for node_id, node in plan.node_by_id.items()
        }
        return BatchStateHead.create(
            batch_id=plan.batch_id,
            predecessor_head_sha256=None,
            ancestor_head_sha256s=(),
            active_plan_sha256=plan.plan_sha256,
            plan_history_sha256s=(plan.plan_sha256,),
            original_manifest_sha256=manifest.original_manifest_sha256,
            normalized_manifest_sha256=manifest.normalized_manifest_sha256,
            project_path=str(project),
            project_identity_sha256=identity.project_identity_sha256,
            project_authority_head_sha256=authority_head.authority_head_sha256,
            project_materialization_state=authority_head.materialization_state,
            project_qpproj_snapshot_sha256=authority_head.qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=(authority_head.latest_mutation_attestation_sha256),
            project_catalogue_sha256=None,
            source_sha256_by_sample=source_hashes,
            resolved_config_sha256_by_sample={
                item.sample_id: item.resolved_config_sha256 for item in manifest.samples
            },
            sample_project_entry_id_by_sample={item.sample_id: None for item in manifest.samples},
            sample_project_entry_projection_sha256_by_sample={
                item.sample_id: None for item in manifest.samples
            },
            accepted_domain_revision_sha256_by_id={},
            domain_set_sha256_by_sample={},
            node_status_by_id={},
            node_scope_by_id=scope_index,
            last_durable_node_ids_by_sample={},
            last_durable_node_ids_by_domain={},
            next_eligible_node_ids_by_sample={},
            next_eligible_node_ids_by_domain={},
            processed_image_paths_by_sample={},
            available_artifact_ref_sha256_by_key={
                digest: digest for digest in plan.external_input_sha256s
            },
            pending_interaction_ids=(),
            pending_authorization_sha256_by_node={},
            pending_training_fit_set_sha256_by_node={},
            failure_codes_by_node={},
            domain_task_status_by_id={},
            generation_references_by_node={},
            checkpoint_descriptor_sha256_by_node={},
            attempt_ids_by_node={},
            terminal_attempt_sha256_by_attempt_id={},
            invalidated_node_ids=(),
            revision_reason_by_plan_sha256={},
        )

    def _prepare_create_project_parent(self, project: Path) -> None:
        """Provision only the validated new project's owned parent directories."""
        owner = self.layout.output_root
        _reject_symlink_path(project, owner, "create project")
        if project.exists() or project.is_symlink():
            raise BatchStateError("create project target already exists")
        directory = owner
        for segment in project.parent.relative_to(owner).parts:
            directory = directory / segment
            _reject_symlink_path(directory, owner, "create project parent")
            try:
                directory.mkdir(exist_ok=True, mode=0o700)
            except OSError as exc:
                raise BatchStateError("cannot prepare create project parent") from exc
            _reject_symlink_path(directory, owner, "create project parent")
            if not directory.is_dir():
                raise BatchStateError("create project parent must be a directory")
        _reject_symlink_path(project, owner, "create project")
        if project.exists() or project.is_symlink():
            raise BatchStateError("create project target already exists")

    @_guarded_batch_state_lease_operation
    def initialize(
        self,
        plan: BatchPlan,
        manifest: ValidatedBatchManifest,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        """Initialize through the persisted active-first project protocol.

        This compatibility entry point remains synchronous, but it no longer
        authors project identity or revision one directly.  The retained
        pre-state journal is sufficient to resume the exact acquisition after
        a crash.
        """
        self._assert_lease(lease)
        state_exists = self.layout.state_head_path.exists()
        if state_exists and not self.layout.initialization_active_path.exists():
            raise BatchStateError("batch state is already initialized")
        if not isinstance(manifest, ValidatedBatchManifest):
            raise BatchStateError("initialize requires ValidatedBatchManifest")
        if (
            plan.batch_id != manifest.manifest.batch_id
            or plan.normalized_manifest_sha256 != manifest.normalized_manifest_sha256
            or self.layout.batch_id != plan.batch_id
            or self.layout.root != (manifest.manifest.output_root / plan.batch_id)
        ):
            raise BatchStateError("plan, manifest, and workspace authority disagree")
        if plan.revision != 1 or plan.predecessor_plan_sha256 is not None:
            raise BatchStateError("initial state requires plan revision one")
        if any(sample.processing_root is not None for sample in manifest.manifest.samples):
            raise BatchStateError("A processing root is owned only by an existing source-inspection return.")
        if self._project_mutation_provider is None:
            raise BatchStateError("initialization requires the project mutation provider")
        self._install_plan(plan)
        if not self.layout.initialization_active_path.exists():
            try:
                authoritative = validate_manifest(
                    BatchManifest.from_json_bytes(manifest.original_manifest_bytes)
                )
            except BatchManifestError as exc:
                raise BatchStateError("retained manifest evidence is invalid") from exc
            if authoritative != manifest:
                raise BatchStateError("validated manifest was not derived from retained bytes")
            from mif_pipeline.orchestration.batch_project import (
                ProjectDesignationRequest,
            )
            from mif_pipeline.qupath_runtime.catalogue import (
                ProjectMutationArtifactTarget,
                canonical_project_uri,
            )

            project = manifest.manifest.project_path or manifest.manifest.create_project_path
            if project is None:
                raise BatchStateError("validated manifest lacks designated project authority")
            intent = "open_existing" if manifest.manifest.project_path is not None else "create"
            if intent == "create":
                self._prepare_create_project_parent(project)
            managed_designation = None
            if intent == "open_existing" and Path(f"{project}.mif-project-identity.json").is_file():
                discover_managed = getattr(
                    self._project_mutation_provider, "discover_managed", None
                )
                if not callable(discover_managed):
                    raise BatchStateError("project lease provider lacks managed discovery")
                managed_designation = discover_managed(project)
            expected_identity_sha256 = (
                None
                if managed_designation is None
                else managed_designation.identity.project_identity_sha256
            )
            expected_head_sha256 = (
                None
                if managed_designation is None
                else managed_designation.authority_head.authority_head_sha256
            )
            request = ProjectDesignationRequest.create(
                batch_id=plan.batch_id,
                intent=intent,
                project_uri=canonical_project_uri(project),
                authorizing_record_sha256=plan.plan_sha256,
                expected_project_identity_sha256=expected_identity_sha256,
                expected_authority_head_sha256=expected_head_sha256,
                relocation_from_uri=None,
                clone_source_project_uri=None,
                clone_source_project_identity_sha256=None,
                clone_source_qpproj_snapshot_sha256=None,
                clone_source_identity_sidecar_sha256=None,
                clone_source_entry_projection_set_sha256=None,
                clone_required_saved_state_set_sha256=None,
            )
            catalogue_store = self.project_catalogue_store(lease)
            request_capability = catalogue_store.publish_request(
                request,
                mutation_authorization_sha256=request.authorizing_record_sha256,
                workspace_lease=lease,
            )
            project_parent_uri = project.parent.resolve().as_uri()
            static_targets = (
                ProjectMutationArtifactTarget.create(
                    target_kind="qpproj",
                    resolution_kind="exact_uri",
                    owner_root_uri=project_parent_uri,
                    relative_locator_or_metadata_key=project.name,
                    predecessor_sha256=(_file_sha256(project) if project.is_file() else None),
                ),
                ProjectMutationArtifactTarget.create(
                    target_kind="identity_sidecar",
                    resolution_kind="exact_uri",
                    owner_root_uri=project_parent_uri,
                    relative_locator_or_metadata_key=(f"{project.name}.mif-project-identity.json"),
                    predecessor_sha256=(
                        None
                        if managed_designation is None
                        else _file_sha256(Path(f"{project}.mif-project-identity.json"))
                    ),
                ),
                ProjectMutationArtifactTarget.create(
                    target_kind="identity_metadata",
                    resolution_kind="project_metadata_key",
                    owner_root_uri=request.project_uri,
                    relative_locator_or_metadata_key=("mif_pipeline.project_identity_record_v1"),
                    predecessor_sha256=(
                        None
                        if managed_designation is None
                        else hashlib.sha256(
                            _canonical_bytes(managed_designation.identity.to_mapping())
                        ).hexdigest()
                    ),
                ),
                ProjectMutationArtifactTarget.create(
                    target_kind="batch_state_head",
                    resolution_kind="exact_uri",
                    owner_root_uri=self.layout.state.as_uri(),
                    relative_locator_or_metadata_key=self.layout.state_head_path.name,
                    predecessor_sha256=None,
                ),
            )
            inventory_value = self.prepare_project_mutation_artifact_inventory(
                request=request_capability,
                expected_project_identity_sha256=expected_identity_sha256,
                static_targets=static_targets,
                dynamic_targets=(),
                lease=lease,
            )
            inventory = self.publish_project_mutation_artifact_inventory(
                inventory_value, lease=lease
            )
            acquisition = self.publish_project_mutation_acquisition_record(
                request=request_capability,
                inventory=inventory,
                operation="identity_materialization",
                intent=intent,
                project_uri=request.project_uri,
                expected_project_identity_sha256=expected_identity_sha256,
                expected_authority_head_sha256=expected_head_sha256,
                recovery_predecessor_acquisition_record_sha256=None,
                lease=lease,
            )
            self.prepare_initialization(
                original_manifest_bytes=manifest.original_manifest_bytes,
                validated_manifest=manifest,
                designation_request=request,
                artifact_inventory=inventory,
                acquisition_record=acquisition,
                lease=lease,
            )
        with self.reopen_pre_state_initialization(
            project_leases=self._project_mutation_provider,
            lease=lease,
        ) as recovery:
            if recovery.designation is None:
                raise BatchStateError("initialization has no project designation")
            initial, receipt = self.commit_initialization(
                recovery.prepared,
                recovery.designation,
                project_lease=recovery.project_lease,
                lease=lease,
            )
            if recovery.project_lease is not None:
                if receipt is None:
                    raise BatchStateError("initialization durability receipt is missing")
                recovery.project_lease.complete(receipt)
            self._finish_initialization(recovery.prepared)
            self._initial_project_identity_record = recovery.designation.identity
            self._initial_project_authority_head_record = recovery.designation.authority_head
            return initial

    @_destination_resume_import_operation
    @_guarded_batch_state_lease_operation
    def import_resume_capsule(
        self,
        plan: object,
        rebindings: object,
        validators: object,
        *,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        """Authenticate and install one capsule with head-last publication.

        The complete transitive graph is preflighted before the capability is
        consumed and any immutable member is published.
        """
        try:
            from mif_pipeline.orchestration.batch_resume import (
                _authenticate_resume_capsule_for_destination,
            )
        except ImportError as exc:
            raise BatchStateError("resume capsule authenticator is unavailable") from exc
        authority = _authenticate_resume_capsule_for_destination(
            plan,
            rebindings,
            self,
            validators,
        )
        return self._install_resume_capsule_graph(authority, lease)

    def _install_resume_capsule_graph(
        self,
        authority: _AuthenticatedResumeCapsuleImport,
        lease: BatchWorkspaceLease,
        *,
        catalogue_import_authority: object | None = None,
    ) -> BatchStateHead:
        """Consume one exact authenticated graph under an existing workspace lease."""
        if not isinstance(authority, _AuthenticatedResumeCapsuleImport):
            raise BatchStateError("resume installation requires authenticated authority")
        authority._assert_bound(self)
        self._assert_lease(lease)
        selected_head = BatchStateHead.from_mapping(json.loads(authority.head_bytes))
        if (selected_head.execution_state is BatchExecutionState.PAUSED
                and authority._pause_quiescence_graph_sha256 != authority.graph_sha256):
            raise BatchStateError("paused import requires its verified no-process completion")
        if self.layout.state_head_path.exists():
            head, _ = self._read_canonical_state_head()
            if _canonical_bytes(head.to_mapping()) != authority.head_bytes or not authority._recovery_allowed:
                raise BatchStateError("batch state already exists at import destination")
            self._validate_loaded_head(head)
            self._commit_resume_capsule_catalogue_owner(
                authority,
                head,
                catalogue_import_authority,
                lease,
            )
            authority._consume(self)
            return head
        original_manifest_bytes = authority.original_manifest_bytes
        normalized_manifest_bytes = authority.normalized_manifest_bytes
        plan_bytes_by_sha256 = dict(authority.plan_bytes_by_sha256)
        authorization_bytes_by_sha256 = dict(authority.authorization_bytes_by_sha256)
        attempt_bytes_by_sha256 = dict(authority.attempt_bytes_by_sha256)
        interaction_bytes_by_name = dict(authority.interaction_bytes_by_name)
        head_bytes = authority.head_bytes
        operational_rebinding_bytes = authority.operational_rebinding_bytes
        project_bytes_by_name = dict(authority.project_bytes_by_name)
        byte_values = (original_manifest_bytes, normalized_manifest_bytes, head_bytes)
        if not all(type(item) is bytes for item in byte_values):
            raise BatchStateError("resume snapshot members must be bytes")
        try:
            head_mapping = json.loads(head_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("resume state head is invalid JSON") from exc
        if _canonical_bytes(head_mapping) != head_bytes:
            raise BatchStateError("resume state head bytes are not canonical")
        head = BatchStateHead.from_mapping(head_mapping)
        if head.sha256 != authority.head_sha256:
            raise BatchStateError("resume authority is bound to another state head")
        try:
            manifest = BatchManifest.from_json_bytes(original_manifest_bytes)
        except BatchManifestError as exc:
            raise BatchStateError("resume original manifest is invalid") from exc
        if (
            _canonical_bytes(manifest.to_mapping()) != normalized_manifest_bytes
            or hashlib.sha256(original_manifest_bytes).hexdigest() != head.original_manifest_sha256
            or hashlib.sha256(normalized_manifest_bytes).hexdigest()
            != head.normalized_manifest_sha256
            or manifest.batch_id != head.batch_id
            or self.layout.batch_id != head.batch_id
        ):
            raise BatchStateError("resume manifest and head authority disagree")

        plans: dict[str, bytes] = {}
        snapshots: dict[str, _PlanSnapshot] = {}
        for raw_digest, payload in plan_bytes_by_sha256.items():
            digest = _sha256(raw_digest, "resume plan digest")
            if type(payload) is not bytes:
                raise BatchStateError("resume plan payload must be bytes")
            snapshot = _plan_snapshot_from_bytes(payload)
            if snapshot.plan_sha256 != digest:
                raise BatchStateError("resume plan key and checksum disagree")
            plans[digest] = payload
            snapshots[digest] = snapshot
        if set(plans) != set(head.plan_history_sha256s):
            raise BatchStateError("resume plan inventory is not the exact head history")
        for index, digest in enumerate(head.plan_history_sha256s):
            snapshot = snapshots[digest]
            expected_predecessor = None if index == 0 else head.plan_history_sha256s[index - 1]
            if (
                snapshot.predecessor_plan_sha256 != expected_predecessor
                or snapshot.revision != index + 1
                or snapshot.batch_id != head.batch_id
            ):
                raise BatchStateError("resume plan history is not one exact chain")

        attempts: dict[str, tuple[bytes, BatchNodeAttempt]] = {}
        for raw_digest, payload in attempt_bytes_by_sha256.items():
            digest = _sha256(raw_digest, "resume terminal attempt digest")
            if type(payload) is not bytes:
                raise BatchStateError("resume terminal attempt must be bytes")
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BatchStateError("resume terminal attempt is invalid JSON") from exc
            if _canonical_bytes(decoded) != payload:
                raise BatchStateError("resume terminal attempt is not canonical")
            attempt = BatchNodeAttempt.from_mapping(decoded)
            if attempt.terminal_record_sha256 != digest:
                raise BatchStateError("resume terminal attempt key disagrees")
            attempts[digest] = (payload, attempt)
        if set(attempts) != set(head.terminal_attempt_sha256_by_attempt_id.values()):
            raise BatchStateError("resume terminal attempt inventory is not exact")
        if {attempt.attempt_id: digest for digest, (_, attempt) in attempts.items()} != dict(
            head.terminal_attempt_sha256_by_attempt_id
        ):
            raise BatchStateError("resume terminal attempt index disagrees")

        authorizations: dict[str, tuple[bytes, BatchAttemptAuthorization]] = {}
        for raw_digest, payload in authorization_bytes_by_sha256.items():
            digest = _sha256(raw_digest, "resume authorization digest")
            if type(payload) is not bytes:
                raise BatchStateError("resume authorization must be bytes")
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BatchStateError("resume authorization is invalid JSON") from exc
            if _canonical_bytes(decoded) != payload:
                raise BatchStateError("resume authorization is not canonical")
            try:
                authorization = BatchAttemptAuthorization.from_mapping(decoded)
            except BatchArtifactError as exc:
                raise BatchStateError("resume authorization is invalid") from exc
            if authorization.authorization_sha256 != digest:
                raise BatchStateError("resume authorization key disagrees")
            authorizations[digest] = (payload, authorization)
        expected_authorizations = {
            attempt.authorization_sha256 for _, attempt in attempts.values()
        } | set(head.pending_authorization_sha256_by_node.values())
        if set(authorizations) != expected_authorizations:
            raise BatchStateError("resume authorization inventory is not exact")

        from .batch_training_checkpoint import BatchNativeFitCheckpointSet
        from mif_pipeline.qupath_runtime.native_classifier_fit import (
            QuPathNativeClassifierFitMaterialRecord,
        )

        training_fit_sets: dict[str, tuple[bytes, BatchNativeFitCheckpointSet]] = {}
        for raw_digest, payload in authority.training_fit_set_bytes_by_sha256:
            digest = _sha256(raw_digest, "resume training fit-set digest")
            if type(payload) is not bytes:
                raise BatchStateError("resume training fit set must be bytes")
            try:
                decoded = json.loads(payload.decode("utf-8"))
                record = BatchNativeFitCheckpointSet.from_mapping(decoded)
            except Exception as exc:
                raise BatchStateError("resume training fit set is invalid") from exc
            if _canonical_bytes(decoded) != payload or record.checkpoint_set_sha256 != digest:
                raise BatchStateError("resume training fit-set key or bytes disagree")
            training_fit_sets[digest] = (payload, record)
        if not set(head.pending_training_fit_set_sha256_by_node.values()) <= set(training_fit_sets):
            raise BatchStateError("resume training fit-set inventory lacks pending checkpoints")
        for _payload, record in training_fit_sets.values():
            authorization_pair = authorizations.get(record.originating_attempt_authorization_sha256)
            if authorization_pair is None:
                raise BatchStateError("resume training fit-set authorization is missing")
            authorization = authorization_pair[1]
            if (
                record.batch_id != head.batch_id
                or record.node_id != authorization.node_id
                or record.originating_attempt_id != authorization.attempt_id
                or record.admission_plan_sha256 != authorization.admission_plan_sha256
                or record.node_material_sha256 != authorization.node_material_sha256
                or record.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
            ):
                raise BatchStateError("resume training fit-set authorization differs")
        native_fit_materials: dict[str, QuPathNativeClassifierFitMaterialRecord] = {}
        for raw_digest, payload in authority.native_fit_material_bytes_by_sha256:
            digest = _sha256(raw_digest, "resume native fit material digest")
            if type(payload) is not bytes:
                raise BatchStateError("resume native fit material must be bytes")
            try:
                decoded = json.loads(payload.decode("utf-8"))
                material = QuPathNativeClassifierFitMaterialRecord.from_mapping(decoded)
            except Exception as exc:
                raise BatchStateError("resume native fit material is invalid") from exc
            if _canonical_bytes(decoded) != payload or material.fit_material_sha256 != digest:
                raise BatchStateError("resume native fit material key or bytes disagree")
            native_fit_materials[digest] = material
        expected_native_materials = {
            entry.runtime_fit_material_sha256
            for _payload, record in training_fit_sets.values()
            for entry in record.entries
        }
        if set(native_fit_materials) != expected_native_materials:
            raise BatchStateError("resume native fit material inventory is not selected exactly")
        if native_fit_materials and self._project_mutation_provider is None:
            raise BatchStateError("resume native fit materials require their project owner")
        for digest, material in native_fit_materials.items():
            try:
                reopened_material = (
                    self._project_mutation_provider.load_native_classifier_fit_material(
                        material.project_identity_sha256,
                        digest,
                        workspace_storage_scope_sha256=(material.workspace_storage_scope_sha256),
                    )
                )
                self._project_mutation_provider.reopen_native_classifier_fit(
                    reopened_material,
                    workspace_storage_scope_sha256=(material.workspace_storage_scope_sha256),
                    mutation_attestation_sha256=next(
                        entry.mutation_attestation_sha256
                        for _payload, record in training_fit_sets.values()
                        for entry in record.entries
                        if entry.runtime_fit_material_sha256 == digest
                    ),
                    successor_project_authority_head_sha256=next(
                        entry.successor_project_authority_head_sha256
                        for _payload, record in training_fit_sets.values()
                        for entry in record.entries
                        if entry.runtime_fit_material_sha256 == digest
                    ),
                )
            except Exception as exc:
                raise BatchStateError(
                    "resume native fit material cannot be reauthenticated"
                ) from exc

        interactions: dict[str, bytes] = {}
        response_digests: set[str] = set()
        request_ids: set[str] = set()
        for raw_name, payload in interaction_bytes_by_name.items():
            name = _publication_name(raw_name)
            if type(payload) is not bytes:
                raise BatchStateError("resume interaction member must be bytes")
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BatchStateError("resume interaction member is invalid JSON") from exc
            if _canonical_bytes(decoded) != payload:
                raise BatchStateError("resume interaction member is not canonical")
            if name.startswith("response-"):
                digest = _sha256(name[len("response-") : -len(".json")], "response digest")
                if decoded.get("response_sha256") != digest:
                    raise BatchStateError("resume response name and digest disagree")
                response_digests.add(digest)
            elif name.startswith("request-"):
                request_ids.add(_batch_request_id(name[len("request-") : -len(".json")]))
            else:
                raise BatchStateError("resume interaction member name is unsupported")
            interactions[name] = payload
        expected_responses = {
            item.interaction_response_sha256
            for _, item in authorizations.values()
            if item.interaction_response_sha256 is not None
        }
        from .batch_resume import _aggregate_review_request_ids, _annotation_request_ids
        aggregate_ids = _aggregate_review_request_ids(
            head, snapshots[head.active_plan_sha256], lambda digest: attempts[digest][1])
        expected_request_ids = ((set(head.pending_interaction_ids) - aggregate_ids)
                                | _annotation_request_ids(project_bytes_by_name))
        if response_digests != expected_responses or request_ids != expected_request_ids:
            raise BatchStateError("resume interaction inventory is not exact")

        manifests = {}
        for digest, original, normalized in authority.manifest_bytes_by_sha256:
            retained = revalidate_retained_manifest(original, normalized)
            if retained.normalized_manifest_sha256 != digest or digest in manifests:
                raise BatchStateError("resume manifest generation and content disagree")
            manifests[digest] = retained
        if set(manifests) != {item.normalized_manifest_sha256 for item in snapshots.values()}:
            raise BatchStateError("resume manifest generations do not match exact plan history")
        from .batch_resume import _parse_setup_store, _SETUP_STORE_PREFIX
        setup_materials = _parse_setup_store(project_bytes_by_name,
            attempts=(record for _payload, record in attempts.values()))
        from .batch_resume import _parse_secondary_definition_records, _SECONDARY_DEFINITION_PREFIX
        secondary_definition_materials = _parse_secondary_definition_records(project_bytes_by_name,
            attempts=(record for _payload, record in attempts.values()))
        from .batch_resume import _parse_plan_contracts, _PLAN_CONTRACT_PREFIX
        typed_plans = _parse_plan_contracts(project_bytes_by_name,
            plans={digest: json.loads(payload) for digest, payload in plans.items()},
            manifests={digest: value.manifest.to_mapping() for digest, value in manifests.items()},
            current_digest=head.active_plan_sha256)
        self._validate_source_manifest_history(tuple(snapshots[key] for key in head.plan_history_sha256s), manifests,
            head=head, attempts=attempts, setup_materials=setup_materials, typed_plans=typed_plans,
            secondary_definition_materials=secondary_definition_materials)
        publications: list[tuple[Path, bytes, Path]] = [
            (self._plan_runtime_contract_path(name[len(_PLAN_CONTRACT_PREFIX):-5]), payload, self.layout.plans)
            for name, payload in project_bytes_by_name.items() if name.startswith(_PLAN_CONTRACT_PREFIX)]
        setup_root = self.layout.state / "training-plan-sets"
        for name, payload in project_bytes_by_name.items():
            if name.startswith(_SETUP_STORE_PREFIX):
                path = setup_root / name[len(_SETUP_STORE_PREFIX):]
                path.parent.mkdir(parents=True, exist_ok=True)
                publications.append((path, payload, path.parent))
            elif name.startswith(_SECONDARY_DEFINITION_PREFIX):
                path = self.layout.state / "secondary-definitions" / name[len(_SECONDARY_DEFINITION_PREFIX):]
                path.parent.mkdir(parents=True, exist_ok=True)
                publications.append((path, payload, path.parent))
        for retained in manifests.values():
            self._retain_manifest(retained)
        publications.extend(
            (self.layout.plans / f"{digest}.json", payload, self.layout.plans)
            for digest, payload in plans.items()
        )
        publications.extend(
            (self._authorization_path(digest), payload, self.layout.attempts)
            for digest, (payload, _) in authorizations.items()
        )
        publications.extend(
            (self._attempt_path(digest), payload, self.layout.attempts)
            for digest, (payload, _) in attempts.items()
        )
        from .batch_pause import BatchQuiescentPauseRecord
        for digest, payload in authority.pause_record_bytes_by_sha256:
            record = BatchQuiescentPauseRecord.from_mapping(json.loads(payload))
            if (record.sha256 != digest or record.batch_id != head.batch_id
                    or _canonical_bytes(record.to_mapping()) != payload):
                raise BatchStateError("imported pause record bytes or identity differ")
            publications.append((self._quiescent_pause_path(digest), payload, self.layout.state))
        fit_set_root = self._training_fit_set_root
        publications.extend(
            (
                fit_set_root / f"{digest}.json",
                payload,
                fit_set_root,
            )
            for digest, (payload, _record) in training_fit_sets.items()
        )
        publications.extend(
            (self.layout.interactions / name, payload, self.layout.interactions)
            for name, payload in interactions.items()
        )
        from .batch_resume import _parse_stage_evidence, _annotation_capsule_records
        for digest, evidence in _parse_stage_evidence(project_bytes_by_name).items():
            self._publish_batch_project_ref(
                self._batch_project_ref("batch_project_runtime_evidence", evidence_sha256=digest),
                _canonical_bytes(evidence.to_mapping()), label="imported accepted project-stage observation",
            )
        from .batch_project import BatchProjectCatalogue
        for name, payload in project_bytes_by_name.items():
            if not name.startswith("project/catalogues/"):
                continue
            record = BatchProjectCatalogue.from_mapping(json.loads(payload))
            if name != f"project/catalogues/{record.catalogue_sha256}.json":
                raise BatchStateError("imported catalogue generation filename differs")
            self._publish_batch_project_ref(
                self._batch_project_ref("batch_project_generation", catalogue_sha256=record.catalogue_sha256),
                payload, label="imported original catalogue generation",
            )
        annotation_root = self.layout.root / "cluster-reviews" / "annotations"
        for name, payload in _annotation_capsule_records(project_bytes_by_name).items():
            path = annotation_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            publications.append((path, payload, path.parent))
        catalogue_bytes = project_bytes_by_name.get("project/catalogue.json")
        if catalogue_bytes is not None:
            from mif_pipeline.orchestration.batch_project import (
                BatchProjectCatalogue,
            )

            try:
                catalogue_mapping = json.loads(catalogue_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BatchStateError("resume project catalogue is invalid JSON") from exc
            if _canonical_bytes(catalogue_mapping) != catalogue_bytes:
                raise BatchStateError("resume project catalogue bytes are not canonical")
            catalogue = BatchProjectCatalogue.from_mapping(catalogue_mapping)
            if catalogue.catalogue_sha256 != head.project_catalogue_sha256:
                raise BatchStateError("resume project catalogue disagrees with state head")
        if operational_rebinding_bytes is not None:
            if type(operational_rebinding_bytes) is not bytes:
                raise BatchStateError("operational rebinding record must be bytes")
            try:
                rebinding_mapping = json.loads(operational_rebinding_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BatchStateError("operational rebinding record is invalid JSON") from exc
            if _canonical_bytes(rebinding_mapping) != operational_rebinding_bytes:
                raise BatchStateError("operational rebinding record is not canonical")
            if not isinstance(rebinding_mapping, Mapping):
                raise BatchStateError("operational rebinding record must be an object")
            rebinding_unsigned = dict(rebinding_mapping)
            rebinding_digest = _sha256(
                rebinding_unsigned.pop("operational_rebinding_sha256", None),
                "operational_rebinding_sha256",
            )
            if (
                rebinding_digest != _digest(rebinding_unsigned)
                or rebinding_digest != head.operational_rebinding_sha256
                or rebinding_mapping.get("capsule_sha256") != authority.capsule_sha256
                or rebinding_mapping.get("capsule_root_sha256") != authority.capsule_root_sha256
                or rebinding_mapping.get("workspace_rebinding_sha256")
                != authority.workspace_rebinding_sha256
                or tuple(
                    sorted(
                        hashlib.sha256(
                            _canonical_bytes(
                                _mapping(row, "effective locator row")["validation_receipt"]
                            )
                        ).hexdigest()
                        for row in _sequence(
                            rebinding_mapping.get("effective_locators"),
                            "effective locators",
                        )
                    )
                )
                != authority.validated_locator_receipt_sha256s
            ):
                raise BatchStateError(
                    "operational rebinding record disagrees with import authority"
                )
            publications.append(
                (
                    self.layout.resume / f"locator-rebindings-{rebinding_digest}.json",
                    operational_rebinding_bytes,
                    self.layout.resume,
                )
            )
        for path, payload, owner in publications:
            self._owned_path(path, owner, "resume import member")
            if path.exists():
                try:
                    existing = path.read_bytes()
                except OSError as exc:
                    raise BatchStateError("cannot inspect resume import collision") from exc
                if existing != payload:
                    raise BatchStateError("resume import collides with existing state")
        self._commit_resume_capsule_catalogue_owner(
            authority,
            head,
            catalogue_import_authority,
            lease,
        )
        authority._consume(self)
        for path, payload, owner in publications:
            self._assert_lease(lease)
            self._publish_immutable(path, payload, owner)
        self._validate_loaded_head(head)
        self._assert_lease(lease)
        self._write_current_head(head)
        self._publish_current_progress(head)
        return head

    def _commit_resume_capsule_catalogue_owner(
        self,
        graph_authority: _AuthenticatedResumeCapsuleImport,
        head: BatchStateHead,
        catalogue_import_authority: object | None,
        lease: BatchWorkspaceLease,
    ) -> None:
        """Commit a v2 catalogue through its prepared owner before state head."""

        catalogue_bytes = dict(graph_authority.project_bytes_by_name).get("project/catalogue.json")
        if catalogue_bytes is None:
            if catalogue_import_authority is not None:
                raise BatchStateError("state-only resume carries unexpected catalogue authority")
            if head.project_catalogue_sha256 is not None:
                raise BatchStateError("resume state selects a missing project catalogue")
            return
        from .batch_project import (
            BatchProjectCatalogue,
            BatchProjectCatalogueImportAuthority,
        )

        if type(catalogue_import_authority) is not BatchProjectCatalogueImportAuthority:
            raise BatchStateError("v2 resume catalogue requires its prepared owner authority")
        try:
            mapping = json.loads(catalogue_bytes.decode("utf-8"))
            catalogue = BatchProjectCatalogue.from_mapping(mapping)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchStateError("resume project catalogue is invalid") from exc
        if (
            _canonical_bytes(mapping) != catalogue_bytes
            or catalogue.catalogue_sha256 != head.project_catalogue_sha256
            or catalogue.project_identity_sha256 != head.project_identity_sha256
            or catalogue.project_uri != Path(head.project_path).resolve().as_uri()
        ):
            raise BatchStateError("resume catalogue owner authority differs from state")
        store = catalogue_import_authority._store
        if (
            getattr(store, "_registered_store_issuer", None)
            is not self._project_durability_capability
            or getattr(store, "authority_layout_sha256", None) != self.authority_layout_sha256
            or getattr(store, "workspace_storage_scope_sha256", None)
            != self.workspace_storage_scope_sha256
        ):
            raise BatchStateError("resume catalogue owner is foreign")
        try:
            store.authenticate_capsule_import_authority(
                catalogue_import_authority,
                authenticated_graph_sha256=graph_authority.graph_sha256,
                catalogue=catalogue,
                workspace_lease=lease,
            )
            committed = store.commit_capsule_import_current(
                catalogue_import_authority,
                workspace_lease=lease,
            )
        except Exception as exc:
            raise BatchStateError("resume catalogue owner commit failed") from exc
        if committed != catalogue or store.load_current() != catalogue:
            raise BatchStateError("resume catalogue owner commit changed")

    def _read_history_event(self, digest: str) -> bytes:
        path = self.layout.state / f"history-{digest}.json"
        self._owned_path(path, self.layout.state, "execution history event")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise BatchStateError("execution history event is missing") from exc

    def _write_current_head(self, head: BatchStateHead) -> None:
        """Publish one incremental history event before the selected current file."""
        from .batch_history import (
            HISTORY_FIELDS, canonical_bytes, current_mapping, empty_history,
            history_event, read_history,
        )

        before = empty_history()
        predecessor = None
        if self.layout.state_head_path.exists():
            current = json.loads(self.layout.state_head_path.read_bytes())
            predecessor = current["history_tail_sha256"]
            before = read_history(predecessor, self._read_history_event, self._history_cache)
        full = head.to_mapping()
        after = {name: full[name] for name in HISTORY_FIELDS}
        event = history_event(before, after, predecessor)
        event_bytes = canonical_bytes(event)
        tail = hashlib.sha256(event_bytes).hexdigest()
        self._publish_immutable(
            self.layout.state / f"history-{tail}.json", event_bytes, self.layout.state,
        )
        payload = canonical_bytes(current_mapping(full, tail))
        self._atomic_replace(
            self.layout.state_head_path, payload, self.layout.state,
        )
        self._history_cache = (tail, after)
        self._decoded_head_cache = (payload, head)

    def _read_canonical_state_head(self) -> tuple[BatchStateHead, bytes]:
        """Read current state, reusing its decoded form when the bytes match.

        We check the workspace and read the current file on every call. Exact
        byte equality with the previously checked record avoids parsing and
        re-encoding unchanged JSON. Changed bytes undergo the canonical-format
        and history checks before replacing the cached result. Callers retain
        their checks of the current operation and referenced inputs.
        """

        self._assert_workspace_integrity()
        path = self.layout.state_head_path
        self._owned_path(path, self.layout.state, "state head")
        try:
            payload = path.read_bytes()
            if self._decoded_head_cache is not None and self._decoded_head_cache[0] == payload:
                return self._decoded_head_cache[1], payload
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("state head is missing or invalid JSON") from exc
        if _canonical_bytes(decoded) != payload:
            raise BatchStateError("state head bytes are not canonical")
        from .batch_history import full_mapping, read_history
        try:
            tail = decoded["history_tail_sha256"]
            history = read_history(tail, self._read_history_event, self._history_cache)
            head = BatchStateHead.from_mapping(full_mapping(decoded, history))
        except (ValueError, TypeError, KeyError) as exc:
            raise BatchStateError("compact current state or its execution history is invalid") from exc
        self._history_cache = (tail, history)
        self._decoded_head_cache = (payload, head)
        return head, payload

    @_guarded_batch_state_operation
    def load_head(self) -> BatchStateHead:
        head, _ = self._read_canonical_state_head()
        self._validate_loaded_head(head)
        return head

    def _publish_current_progress(self, head: BatchStateHead) -> None:
        from .batch_progress import build_projection

        projection = build_projection(self, head)
        self._atomic_replace(
            self.layout.state / "current-progress.json",
            _canonical_bytes(projection), self.layout.state,
        )
        self._current_progress_cache = None

    @_guarded_batch_state_operation
    def read_current_progress(self):
        """Read current display state without reopening the execution history."""
        from .batch_progress import decode_projection, revision

        self._assert_workspace_integrity()
        path = self.layout.state / "current-progress.json"
        self._owned_path(path, self.layout.state, "current progress")
        try:
            head_revision = revision(self.layout.state_head_path)
            projection_revision = revision(path)
            cache = self._current_progress_cache
            if cache is not None and cache[:2] == (head_revision, projection_revision):
                return cache[2]
            mapping = json.loads(path.read_bytes())
            if tuple(mapping.get("head_revision", ())) != head_revision:
                raise BatchStateError("current progress publication was interrupted; reopen execution to recover")
            snapshot = decode_projection(mapping)
            if revision(self.layout.state_head_path) != head_revision or revision(path) != projection_revision:
                raise BatchHeadConflictError("current progress changed during read")
        except BatchStateError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BatchStateError("current progress is missing or invalid; reopen execution to recover") from exc
        self._current_progress_cache = (head_revision, projection_revision, snapshot)
        return snapshot

    def _current_artifact_material(
        self,
        head: BatchStateHead,
        plan: BatchPlan,
        *,
        checked_attempts: Mapping[str, BatchNodeAttempt] | None = None,
    ) -> tuple[tuple[BatchArtifactRef, ...], dict[str, BatchNodeAttempt]]:
        """Select current references while retaining their checked terminal records.

        Only completed, active nodes can supply currently available results.
        Read their terminal records once, preserving every output occurrence
        when finding the head's selected references. Pending reviews and earlier
        invalidated nodes cannot contribute; their history is checked during
        recovery rather than reread for each result lookup.
        """

        candidates: dict[str, list[BatchArtifactRef]] = {}
        attempts: dict[str, BatchNodeAttempt] = {}
        for artifact in plan.external_inputs:
            candidates.setdefault(artifact.artifact_ref_sha256, []).append(artifact)
        completed_attempt_ids = (
            attempt_id
            for node in plan.nodes
            if head.node_status_by_id.get(node.node_id) is StageStatus.COMPLETED
            and node.node_id not in head.invalidated_node_ids
            and node.node_id not in head.pending_authorization_sha256_by_node
            for attempt_id in head.attempt_ids_by_node.get(node.node_id, ())
        )
        for attempt_id in completed_attempt_ids:
            terminal_digest = head.terminal_attempt_sha256_by_attempt_id[attempt_id]
            attempt = attempts.get(terminal_digest)
            if attempt is None:
                attempt = (
                    None if checked_attempts is None else checked_attempts.get(terminal_digest)
                )
                if attempt is None:
                    attempt = self.load_attempt(terminal_digest)
                if (
                    type(attempt) is not BatchNodeAttempt
                    or attempt.terminal_record_sha256 != terminal_digest
                ):
                    raise BatchStateError("current terminal record differs from its head digest")
                attempts[terminal_digest] = attempt
            for artifact in attempt.output_artifacts:
                candidates.setdefault(artifact.artifact_ref_sha256, []).append(artifact)
        accepted: list[BatchArtifactRef] = []
        for digest in sorted(head.available_artifact_ref_sha256_by_key):
            matches = candidates.get(digest, ())
            if len(matches) != 1:
                raise BatchStateError("current artifact inventory lacks one exact typed reference")
            accepted.append(matches[0])
        return tuple(accepted), attempts

    def _current_internal_artifact_authority(
        self,
        artifact: BatchArtifactRef,
        head: BatchStateHead,
        plan: BatchPlan,
    ) -> BatchCommittedArtifactAuthority:
        """Read one producer's records and bind its artifact to the supplied head.

        Standalone callers, including candidate-head review, enter through this
        reader. Projection callers use the same association checks below with
        records they have already read in their current operation.
        """

        node = next(
            (item for item in plan.nodes if item.node_id == artifact.producer_node_id),
            None,
        )
        terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(
            artifact.producer_attempt_id or ""
        )
        if terminal_digest is None:
            raise BatchStateError("current internal artifact producer is unavailable")
        return self._current_artifact_authority_from_records(
            artifact,
            head,
            node,
            self.load_attempt(terminal_digest),
            self.load_authorization(artifact.producer_attempt_authorization_sha256 or ""),
        )

    def _current_artifact_authority_from_records(
        self,
        artifact: BatchArtifactRef,
        head: BatchStateHead,
        node: BatchNode | None,
        attempt: BatchNodeAttempt,
        authorization: BatchAttemptAuthorization,
    ) -> BatchCommittedArtifactAuthority:
        """Bind already checked owner records without reading them a second time.

        We require the supplied head to select this completed, valid producer and
        its exact terminal. The authorization and output membership must agree
        before we issue the head-bound artifact authority. This shared predicate
        supports both current projections and candidate-head review.
        """

        producer_node_id = artifact.producer_node_id
        producer_attempt_id = artifact.producer_attempt_id
        authorization_sha256 = artifact.producer_attempt_authorization_sha256
        terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(producer_attempt_id or "")
        if (
            artifact.origin is not ArtifactOrigin.INTERNAL_NODE
            or node is None
            or node.node_id != producer_node_id
            or producer_node_id in head.invalidated_node_ids
            or head.node_status_by_id.get(producer_node_id) is not StageStatus.COMPLETED
            or terminal_digest is None
        ):
            raise BatchStateError("current internal artifact producer is unavailable")
        if (
            attempt.terminal_record_sha256 != terminal_digest
            or attempt.node_id != producer_node_id
            or attempt.attempt_id != producer_attempt_id
            or attempt.authorization_sha256 != authorization_sha256
            or authorization.authorization_sha256 != authorization_sha256
            or authorization.node_id != producer_node_id
            or authorization.attempt_id != producer_attempt_id
            or sum(item == artifact for item in attempt.output_artifacts) != 1
        ):
            raise BatchStateError("current internal artifact authority is contradictory")
        try:
            return _issue_batch_committed_artifact_authority(
                artifact_ref=artifact,
                state_head_sha256=head.sha256,
                active_plan_sha256=head.active_plan_sha256,
                producer_node_kind=node.kind,
                terminal_record_sha256=attempt.terminal_record_sha256,
                execution_exchange_sha256=attempt.execution_exchange_sha256,
            )
        except BatchArtifactError as exc:
            raise BatchStateError("current internal artifact authority cannot be issued") from exc

    @_guarded_batch_state_operation
    def reopen_current_project_catalogue(
        self, *, expected_head_sha256: str
    ) -> "BatchProjectCatalogue":
        """Read the selected image catalogue and verify the latest accepted project.

        We retain the catalogue's image and entry identities while accepted native
        operations advance the project file. The current execution head supplies
        that file's expected content identity. The returned catalogue retains its
        original creation snapshot for provenance and entry reconstruction.
        """

        from .batch_project import BatchProjectError, _read_current_project_catalogue
        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectAuthorityError,
            verify_read_only_project_snapshot,
        )

        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("project catalogue projection head changed")
        def read_record(digest: str | None) -> bytes | None:
            ref = (
                self._batch_project_ref("batch_project_current")
                if digest is None
                else self._batch_project_ref(
                    "batch_project_generation", catalogue_sha256=digest
                )
            )
            family = self._authority_layout.layout.family(ref.family)
            bucket = self._authority_layout.bucket(family.physical_bucket)
            return bucket.read_immutable(
                ref.physical_name,
                max_bytes=family.max_member_bytes,
                label=(
                    "catalogue current pointer"
                    if digest is None
                    else "catalogue immutable generation"
                ),
                optional=digest is None,
            )

        try:
            catalogue = _read_current_project_catalogue(
                read_record=read_record
            )
        except BatchProjectError as exc:
            raise BatchStateError("current project catalogue is invalid") from exc
        if (
            catalogue is None
            or catalogue.catalogue_sha256 != head.project_catalogue_sha256
            or catalogue.project_identity_sha256 != head.project_identity_sha256
            or catalogue.project_uri != Path(head.project_path).resolve().as_uri()
            or catalogue.batch_id != head.batch_id
        ):
            raise BatchStateError("current project catalogue differs from state head")
        try:
            verify_read_only_project_snapshot(
                project_path=Path(head.project_path),
                project_uri=catalogue.project_uri,
                qpproj_snapshot_sha256=head.project_qpproj_snapshot_sha256,
            )
        except ProjectAuthorityError as exc:
            raise BatchStateError("current project snapshot is invalid") from exc
        if self.load_head().sha256 != head.sha256:
            raise BatchHeadConflictError("project catalogue projection head changed before return")
        return catalogue

    @_guarded_batch_state_operation
    def execution_snapshot_if_present(self) -> BatchExecutionSnapshot | None:
        """Reopen authenticated execution or prove this workspace is unstarted.

        Only known pre-execution scaffolding may remain without a head.
        Other retained entries witness interrupted or damaged execution.
        """
        self._assert_workspace_integrity()
        head = self.layout.state_head_path
        if head.exists() or head.is_symlink():
            return self.reopen_execution_snapshot()
        publications = (
            self.layout.initialization_active_path,
        )
        initialization = self.layout.initialization_directory
        if initialization.is_symlink() or (initialization.exists() and not initialization.is_dir()):
            raise BatchStateError("initialization publication directory is unsafe")
        if any(path.exists() or path.is_symlink() for path in publications):
            raise BatchStateError("state head is missing from initialized execution")
        training_root = self.layout.state / "training-plan-sets"
        classifier_root = self.layout.state / "classifier-record-sets"
        scaffold_directories = {
            initialization,
            training_root,
            training_root / "objects",
            classifier_root,
            classifier_root / "objects",
        }
        # Store identity is setup metadata, authenticated by its store owner;
        # it grants no execution authority. Published store objects do.
        scaffold_files = {
            self.layout.writer_lock_path,
            training_root / ".mif-training-plan-set-store.json",
        }
        pending = list(self.layout.owned_directories)
        try:
            while pending:
                for path in pending.pop().iterdir():
                    if not path.is_symlink():
                        if path in scaffold_directories and path.is_dir():
                            pending.append(path)
                            continue
                        if path in scaffold_files and path.is_file():
                            continue
                    raise BatchStateError("state head is missing from retained execution")
        except OSError as exc:
            raise BatchStateError("pre-execution workspace cannot be inspected") from exc
        return None

    @_guarded_batch_state_operation
    def reopen_execution_snapshot(
        self, *, expected_head_sha256: str | None = None, full_recovery: bool = False,
        recovery_lease: BatchWorkspaceLease | None = None,
    ) -> BatchExecutionSnapshot:
        """Reuse one checked current execution view, or explicitly recover its graph.

        A changed current head rebuilds selected result associations, reusing
        the checked plan and manifest when their digests are unchanged. Full
        recovery clears those single-current values and reopens retained records.
        Current source/native revision checks remain at admission and commit.
        """
        if full_recovery:
            if recovery_lease is not None:
                self._assert_lease(recovery_lease)
            self._validated_head_sha256 = None
            self._decoded_head_cache = None
            self._history_cache = None
            self._execution_snapshot_cache = None
            self._typed_plan_cache = None
            self._retained_manifest_cache = None
            with self._lease_condition:
                self._plan_parse_cache.clear()
                self._plan_parse_cache_bytes = 0

        expected = (
            None
            if expected_head_sha256 is None
            else _sha256(expected_head_sha256, "expected_head_sha256")
        )
        head, _ = self._read_canonical_state_head()
        checked_records = self._validate_loaded_head(head)
        if expected is not None and head.sha256 != expected:
            raise BatchHeadConflictError("execution snapshot head changed")
        cached = self._execution_snapshot_cache
        if cached is not None and cached.head.sha256 == head.sha256:
            return cached
        plan = self._load_typed_plan(head.active_plan_sha256)
        manifest = self._retained_manifest_cache
        if manifest is None or (
            manifest.original_manifest_sha256 != head.original_manifest_sha256
            or manifest.normalized_manifest_sha256 != head.normalized_manifest_sha256
        ):
            try:
                manifest = self.retained_manifest(head.normalized_manifest_sha256)
            except (OSError, BatchManifestError) as exc:
                raise BatchStateError("execution snapshot manifest is unavailable or invalid") from exc
        if (
            manifest.original_manifest_sha256 != head.original_manifest_sha256
            or manifest.normalized_manifest_sha256 != head.normalized_manifest_sha256
            or plan.plan_sha256 != head.active_plan_sha256
            or plan.normalized_manifest_sha256 != manifest.normalized_manifest_sha256
        ):
            raise BatchStateError("execution snapshot plan, manifest, and head disagree")
        self._retained_manifest_cache = manifest
        artifacts, attempts = self._current_artifact_material(
            head,
            plan,
            checked_attempts=None if checked_records is None else checked_records[0],
        )
        nodes = {node.node_id: node for node in plan.nodes}
        authorizations: dict[str, BatchAttemptAuthorization] = (
            {} if checked_records is None else checked_records[1]
        )
        authorities: list[BatchCommittedArtifactAuthority] = []
        for artifact in artifacts:
            if artifact.origin is not ArtifactOrigin.INTERNAL_NODE:
                continue
            terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(
                artifact.producer_attempt_id or ""
            )
            if terminal_digest is None:
                raise BatchStateError("current internal artifact producer is unavailable")
            authorization_digest = artifact.producer_attempt_authorization_sha256 or ""
            authorization = authorizations.get(authorization_digest)
            if authorization is None:
                authorization = self.load_authorization(authorization_digest)
                authorizations[authorization_digest] = authorization
            authorities.append(self._current_artifact_authority_from_records(
                artifact, head, nodes.get(artifact.producer_node_id or ""),
                attempts[terminal_digest], authorization,
            ))
        snapshot = BatchExecutionSnapshot(
            head=head,
            plan=plan,
            manifest=manifest,
            accepted_artifacts=artifacts,
            committed_artifact_authorities=tuple(authorities),
        )
        final_head = self.load_head()
        if final_head.sha256 != head.sha256 or (
            expected is not None and final_head.sha256 != expected
        ):
            raise BatchHeadConflictError("execution snapshot head changed before return")
        self._execution_snapshot_cache = snapshot
        if full_recovery and recovery_lease is not None:
            self._assert_lease(recovery_lease)
            self._publish_current_progress(head)
        return snapshot

    def _quiescent_pause_path(self, digest: str) -> Path:
        return self.layout.state / f"quiescent-pause-{_sha256(digest, 'pause record digest')}.json"

    def _validate_pause_state_authority(
        self,
        authority: object,
        *,
        operation: str,
        pause_record_sha256: str,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        require_scheduler_receipt: bool,
        candidate_head_sha256: str | None = None,
    ) -> None:
        """Accept only the private owner-issued one-shot pause capability."""

        from .batch_pause import (
            _PAUSE_STATE_AUTHORITY_ISSUER,
            _PauseStateTransitionAuthority,
        )

        if (
            type(authority) is not _PauseStateTransitionAuthority
            or authority._issuer is not _PAUSE_STATE_AUTHORITY_ISSUER
            or authority._used
            or authority._state_store is not self
            or authority._workspace_lease is not lease
            or authority._operation != operation
            or authority._pause_record_sha256 != _sha256(pause_record_sha256, "pause record digest")
            or authority._expected_head_sha256
            != _sha256(expected_head_sha256, "expected head sha256")
            or authority._candidate_head_sha256 != candidate_head_sha256
            or (require_scheduler_receipt and authority._scheduler_receipt_capability is None)
            or (
                not require_scheduler_receipt
                and authority._scheduler_receipt_capability is not None
            )
        ):
            raise BatchStateError("pause mutation lacks owner-issued transition authority")
        authority._used = True

    def _replace_paused_response_head_cas(
        self,
        candidate: BatchStateHead,
        expected_predecessor_sha256: str,
        lease: BatchWorkspaceLease,
        *,
        authority: object,
    ) -> BatchStateHead:
        """Replace one paused response head under its exact owner-issued proof."""

        self._assert_lease(lease)
        current = self.load_head()
        if current.sha256 != _sha256(expected_predecessor_sha256, "expected predecessor head"):
            raise BatchHeadConflictError("paused response predecessor changed")
        if (
            current.execution_state is not BatchExecutionState.PAUSED
            or candidate.execution_state is not BatchExecutionState.PAUSED
            or candidate.predecessor_head_sha256 != current.sha256
            or candidate.quiescent_pause_sha256 == current.quiescent_pause_sha256
        ):
            raise BatchStateError("paused response successor is invalid")
        permitted = {
            "sha256",
            "predecessor_head_sha256",
            "ancestor_head_sha256s",
            "pending_authorization_sha256_by_node",
            "pending_interaction_ids",
            "invalidated_node_ids",
            "quiescent_pause_sha256",
        }
        if any(
            old != candidate.to_mapping()[field_name]
            for field_name, old in current.to_mapping().items()
            if field_name not in permitted
        ):
            raise BatchStateError("paused response changed unrelated state")
        self._validate_pause_state_authority(
            authority,
            operation="response",
            pause_record_sha256=candidate.quiescent_pause_sha256 or "",
            expected_head_sha256=current.sha256,
            lease=lease,
            require_scheduler_receipt=True,
            candidate_head_sha256=candidate.sha256,
        )
        return self._replace_head_cas(
            candidate,
            current.sha256,
            lease,
            allow_interaction_requests=True,
            allow_execution_state_transition=True,
        )

    def _publish_quiescent_pause_record(
        self, record: object, lease: BatchWorkspaceLease, authority: object
    ) -> object:
        """Persist one authenticated pause record before its head transition."""

        from .batch_pause import BatchQuiescentPauseRecord

        self._assert_lease(lease)
        if not isinstance(record, BatchQuiescentPauseRecord):
            raise BatchStateError("pause publication requires an exact pause record")
        self._validate_pause_state_authority(
            authority,
            operation="publish",
            pause_record_sha256=record.sha256,
            expected_head_sha256=self.load_head().sha256,
            lease=lease,
            require_scheduler_receipt=False,
        )
        if record.batch_id != self.layout.batch_id:
            raise BatchStateError("pause record batch differs from this workspace")
        path = self._quiescent_pause_path(record.sha256)
        self._owned_path(path, self.layout.state, "quiescent pause record")
        self._publish_immutable(path, _canonical_bytes(record.to_mapping()), self.layout.state)
        return record

    @_guarded_batch_state_operation
    def load_quiescent_pause_record(self, digest: str) -> object:
        """Reopen one immutable pause record by its content digest."""

        return self._read_quiescent_pause_record(digest)

    def _read_quiescent_pause_record(self, digest: str) -> object:
        """Read one pause record without acquiring a second runtime-use token."""

        from .batch_pause import BatchQuiescentPauseRecord

        path = self._quiescent_pause_path(digest)
        self._owned_path(path, self.layout.state, "quiescent pause record")
        try:
            payload = path.read_bytes()
            mapping = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("quiescent pause record is missing or invalid") from exc
        if _canonical_bytes(mapping) != payload:
            raise BatchStateError("quiescent pause record bytes are not canonical")
        record = BatchQuiescentPauseRecord.from_mapping(mapping)
        if record.sha256 != _sha256(digest, "pause record digest"):
            raise BatchStateError("quiescent pause record digest disagrees with path")
        return record

    def _transition_to_quiescent_pause(
        self,
        *,
        pause_record_sha256: str,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        authority: object,
    ) -> BatchStateHead:
        """CAS the active head to paused without changing scientific projection."""

        self._assert_lease(lease)
        self._validate_pause_state_authority(
            authority,
            operation="pause",
            pause_record_sha256=pause_record_sha256,
            expected_head_sha256=expected_head_sha256,
            lease=lease,
            require_scheduler_receipt=True,
        )
        digest = _sha256(pause_record_sha256, "pause record digest")
        current = self.load_head()
        if current.sha256 != _sha256(expected_head_sha256, "expected head sha256"):
            raise BatchHeadConflictError("state head changed before pause publication")
        if (
            current.execution_state is not BatchExecutionState.ACTIVE
            or current.quiescent_pause_sha256 is not None
        ):
            raise BatchStateError("only an active head may enter quiescent pause")
        from .batch_pause import (
            _pending_interaction_inventory_sha256,
            _terminal_inventory_sha256,
        )

        record = self._read_quiescent_pause_record(digest)
        if (
            record.batch_id != current.batch_id
            or record.predecessor_state_head_sha256 != current.sha256
            or record.scheduler_quiescence_state_head_sha256 != current.sha256
            or record.active_plan_sha256 != current.active_plan_sha256
            or record.project_authority_head_sha256 != current.project_authority_head_sha256
            or record.terminal_attempt_inventory_sha256 != _terminal_inventory_sha256(current)
            or record.pending_interaction_inventory_sha256
            != _pending_interaction_inventory_sha256(current)
        ):
            raise BatchStateError("pause record does not authenticate current head")
        candidate = current.evolve(
            execution_state=BatchExecutionState.PAUSED,
            quiescent_pause_sha256=digest,
        )
        return self._replace_head_cas(
            candidate,
            current.sha256,
            lease,
            allow_execution_state_transition=True,
        )

    def _consume_quiescent_pause(
        self,
        *,
        pause_record_sha256: str,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        authority: object,
    ) -> BatchStateHead:
        """CAS one exact paused head back to active before new admission."""

        self._assert_lease(lease)
        self._validate_pause_state_authority(
            authority,
            operation="consume",
            pause_record_sha256=pause_record_sha256,
            expected_head_sha256=expected_head_sha256,
            lease=lease,
            require_scheduler_receipt=False,
        )
        digest = _sha256(pause_record_sha256, "pause record digest")
        current = self.load_head()
        if current.sha256 != _sha256(expected_head_sha256, "expected head sha256"):
            raise BatchHeadConflictError("state head changed before pause consumption")
        if (
            current.execution_state is not BatchExecutionState.PAUSED
            or current.quiescent_pause_sha256 != digest
        ):
            raise BatchStateError("paused head does not name the supplied pause record")
        candidate = current.evolve(
            execution_state=BatchExecutionState.ACTIVE,
            quiescent_pause_sha256=None,
        )
        return self._replace_head_cas(
            candidate,
            current.sha256,
            lease,
            allow_execution_state_transition=True,
        )

    def _load_operational_locator_authorities(
        self, head: BatchStateHead
    ) -> tuple[BatchEffectiveLocatorAuthority, ...]:
        """Reopen the immutable input locations accepted when this analysis was imported.

        Import admission checks this complete inventory against the capsule's
        original manifest, plans and results. The current head retains its digest
        and origin ancestry. Later results do not become imported inputs, so we
        check the retained receipt and each input identity here without rebuilding
        its inventory from the changing current analysis.
        """

        digest = head.operational_rebinding_sha256
        if digest is None:
            return ()
        path = self.layout.resume / f"locator-rebindings-{digest}.json"
        self._owned_path(path, self.layout.resume, "operational rebinding record")
        try:
            payload = path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("operational rebinding authority is missing or invalid") from exc
        if _canonical_bytes(decoded) != payload:
            raise BatchStateError("operational rebinding authority is not canonical")
        expected_fields = {
            "schema_name",
            "schema_version",
            "capsule_sha256",
            "capsule_root_sha256",
            "batch_id",
            "original_manifest_sha256",
            "normalized_manifest_sha256",
            "source_head_sha256",
            "installed_plan_sha256",
            "destination_root",
            "workspace_rebinding_sha256",
            "effective_locators",
            "operational_rebinding_sha256",
        }
        mapping = _closed_mapping(
            decoded,
            fields=expected_fields,
            field="operational rebinding authority",
        )
        if (
            mapping["schema_name"] != "mif-batch-operational-rebinding"
            or mapping["schema_version"] != 1
        ):
            raise BatchStateError("operational rebinding schema is not current")
        try:
            assert_current_writer("mif-batch-operational-rebinding", "1")
        except ValueError as exc:
            raise BatchStateError("operational rebinding schema is not registered current") from exc
        unsigned = dict(mapping)
        claimed_digest = _sha256(
            unsigned.pop("operational_rebinding_sha256"),
            "operational_rebinding_sha256",
        )
        source_head_sha256 = _sha256(mapping["source_head_sha256"], "source_head_sha256")
        installed_plan_sha256 = _sha256(mapping["installed_plan_sha256"], "installed_plan_sha256")
        destination_root = (
            Path(_text(mapping["destination_root"], "destination_root"))
            .expanduser()
            .resolve(strict=False)
        )
        if (
            claimed_digest != digest
            or _digest(unsigned) != digest
            or mapping["batch_id"] != head.batch_id
            or mapping["original_manifest_sha256"] != head.original_manifest_sha256
            or mapping["normalized_manifest_sha256"] != head.normalized_manifest_sha256
            or source_head_sha256 not in head.ancestor_head_sha256s
            or installed_plan_sha256 not in head.plan_history_sha256s
            or destination_root != self.layout.output_root
        ):
            raise BatchStateError("operational rebinding authority disagrees with state head")
        workspace_digest = _optional_sha256(
            mapping["workspace_rebinding_sha256"],
            "workspace_rebinding_sha256",
        )
        rows = mapping["effective_locators"]
        if not isinstance(rows, list):
            raise BatchStateError("effective locator authority must be a list")
        authorities: list[BatchEffectiveLocatorAuthority] = []
        seen: set[str] = set()
        for raw_row in rows:
            row = _closed_mapping(
                raw_row,
                fields={"requirement", "effective_path", "validation_receipt"},
                field="effective locator authority",
            )
            requirement = _closed_mapping(
                row["requirement"],
                fields={
                    "requirement_id",
                    "owner_validator_id",
                    "role",
                    "original_path",
                    "expected_sha256",
                    "expected_byte_count",
                    "owning_identity_sha256",
                    "owning_identity",
                },
                field="effective locator requirement",
            )
            receipt = _closed_mapping(
                row["validation_receipt"],
                fields={
                    "requirement_id",
                    "owner_validator_id",
                    "validated_path",
                    "sha256",
                    "byte_count",
                    "owning_identity_sha256",
                    "receipt_sha256",
                },
                field="effective locator validation receipt",
            )
            requirement_id = _text(requirement["requirement_id"], "requirement_id")
            owner_validator_id = _text(requirement["owner_validator_id"], "owner_validator_id")
            owning_identity_sha256 = _sha256(
                requirement["owning_identity_sha256"],
                "owning_identity_sha256",
            )
            receipt_unsigned = dict(receipt)
            receipt_sha256 = _sha256(receipt_unsigned.pop("receipt_sha256"), "receipt_sha256")
            effective_path = (
                Path(_text(row["effective_path"], "effective_path"))
                .expanduser()
                .resolve(strict=False)
            )
            receipt_path = (
                Path(_text(receipt["validated_path"], "validated_path"))
                .expanduser()
                .resolve(strict=False)
            )
            expected_sha256 = _optional_sha256(requirement["expected_sha256"], "expected_sha256")
            receipt_material_sha256 = _optional_sha256(receipt["sha256"], "receipt material sha256")
            expected_byte_count = requirement["expected_byte_count"]
            receipt_byte_count = receipt["byte_count"]
            if any(
                type(value) is not int or value < 0
                for value in (expected_byte_count, receipt_byte_count)
                if value is not None
            ):
                raise BatchStateError("effective locator byte counts are invalid")
            if (
                requirement_id in seen
                or _digest(requirement["owning_identity"]) != owning_identity_sha256
                or receipt_sha256 != _digest(receipt_unsigned)
                or receipt["requirement_id"] != requirement_id
                or receipt["owner_validator_id"] != owner_validator_id
                or receipt["owning_identity_sha256"] != owning_identity_sha256
                or receipt_path != effective_path
                or (expected_sha256 is not None and receipt_material_sha256 != expected_sha256)
                or (expected_byte_count is not None and receipt_byte_count != expected_byte_count)
            ):
                raise BatchStateError("effective locator authority is inconsistent")
            seen.add(requirement_id)
            authorities.append(
                BatchEffectiveLocatorAuthority(
                    requirement_id=requirement_id,
                    owner_validator_id=owner_validator_id,
                    effective_path=effective_path,
                    sha256=receipt_material_sha256,
                    byte_count=receipt_byte_count,
                    owning_identity_sha256=owning_identity_sha256,
                    validation_receipt_sha256=receipt_sha256,
                    operational_rebinding_sha256=digest,
                )
            )
        return tuple(authorities)

    @_guarded_batch_state_operation
    def resolve_effective_locator(self, requirement_id: str) -> BatchEffectiveLocatorAuthority:
        requirement_id = _text(requirement_id, "requirement_id")
        head = self.load_head()
        matches = tuple(
            item
            for item in self._load_operational_locator_authorities(head)
            if item.requirement_id == requirement_id
        )
        if len(matches) != 1:
            raise BatchStateError("effective locator requirement lacks one operational authority")
        return matches[0]

    @staticmethod
    def _node_matches_material_authority(
        node: Mapping[str, object],
        head: BatchStateHead,
        *,
        resolved_config_sha256_by_sample: Mapping[str, str] | None = None,
        sample_project_entry_id_by_sample: Mapping[str, str | None] | None = None,
        accepted_domain_revision_sha256_by_id: Mapping[str, str] | None = None,
        domain_set_sha256_by_sample: Mapping[str, str] | None = None,
    ) -> bool:
        resolved_configs = (
            head.resolved_config_sha256_by_sample
            if resolved_config_sha256_by_sample is None
            else resolved_config_sha256_by_sample
        )
        project_entries = (
            head.sample_project_entry_id_by_sample
            if sample_project_entry_id_by_sample is None
            else sample_project_entry_id_by_sample
        )
        domain_revisions = (
            head.accepted_domain_revision_sha256_by_id
            if accepted_domain_revision_sha256_by_id is None
            else accepted_domain_revision_sha256_by_id
        )
        domain_sets = (
            head.domain_set_sha256_by_sample
            if domain_set_sha256_by_sample is None
            else domain_set_sha256_by_sample
        )
        participants = tuple(str(item) for item in node["participant_ids"])
        bindings = {
            str(item["sample_id"]): str(item["resolved_config_sha256"])
            for item in node["configuration_bindings"]
        }
        if set(bindings) != set(participants) or any(
            resolved_configs.get(sample_id) != bindings[sample_id] for sample_id in participants
        ):
            return False
        if node["kind"] == "saved_result_retirement":
            return True
        entries_by_sample = _node_project_entry_id_by_sample(node)
        if any(
            project_entries.get(sample_id) != entry_id
            for sample_id, entry_id in entries_by_sample.items()
        ):
            return False
        return not any(
            domain_revisions.get(str(binding["analysis_domain_id"]))
            != binding["analysis_domain_revision_sha256"]
            or domain_sets.get(str(binding["sample_id"])) != binding["domain_set_sha256"]
            for binding in node["analysis_domain_bindings"]
        )

    def _validate_loaded_head(
        self, head: BatchStateHead,
    ) -> tuple[dict[str, BatchNodeAttempt], dict[str, BatchAttemptAuthorization]] | None:
        """Validate a new head and return records reusable within this read only."""
        from mif_pipeline.pipeline_core.schema_registry import schema_validation_snapshot

        if self._validated_head_sha256 == head.sha256:
            return
        with schema_validation_snapshot():
            records = self._validate_loaded_head_records(head)
        self._validated_head_sha256 = head.sha256
        return records

    def _validate_loaded_head_records(
        self, head: BatchStateHead,
    ) -> tuple[dict[str, BatchNodeAttempt], dict[str, BatchAttemptAuthorization]]:
        """Reopen every authority transitively named by the lock-free head.

        Structured inputs are decoded through their typed ``from_mapping`` constructors. The
        remaining comparisons check the relationships required by this operation before the
        caller can use those records."""
        try:
            retained = self.retained_manifest(head.normalized_manifest_sha256)
            original_manifest_bytes = retained.original_manifest_bytes
            normalized_manifest_bytes = retained.normalized_manifest_bytes
        except OSError as exc:
            raise BatchStateError("retained manifest authority is missing") from exc
        if (
            hashlib.sha256(original_manifest_bytes).hexdigest() != head.original_manifest_sha256
            or hashlib.sha256(normalized_manifest_bytes).hexdigest()
            != head.normalized_manifest_sha256
        ):
            raise BatchStateError("retained manifest checksum disagrees with head")
        retained_manifest = retained.manifest
        active = self._load_plan_snapshot(head.active_plan_sha256)
        original_output_root = retained_manifest.output_root.expanduser().resolve(strict=False)
        original_workspace_root = original_output_root / retained_manifest.batch_id
        workspace_is_original = self.layout.root == original_workspace_root
        expected_rebinding = _batch_workspace_rebinding_sha256(
            original_output_root,
            self.layout.output_root,
            head.original_manifest_sha256,
        )
        workspace_has_exact_successor = (
            active.predecessor_plan_sha256 is not None
            and active.revision > 1
            and active.plan_sha256 == head.active_plan_sha256
            and active.plan_sha256 == head.plan_history_sha256s[-1]
            and active.predecessor_plan_sha256 == head.plan_history_sha256s[-2]
            and self._load_plan_snapshot(
                active.predecessor_plan_sha256
            ).batch_id
            == retained_manifest.batch_id
            and active.workspace_rebinding_sha256 == expected_rebinding
        )
        if (
            _canonical_bytes(retained_manifest.to_mapping()) != normalized_manifest_bytes
            or retained_manifest.batch_id != head.batch_id
            or self.layout.batch_id != head.batch_id
            or not (workspace_is_original or workspace_has_exact_successor)
        ):
            raise BatchStateError("retained manifest, workspace, and state-head authority disagree")
        locator_authorities: tuple[BatchEffectiveLocatorAuthority, ...] = ()
        if head.operational_rebinding_sha256 is not None:
            locator_authorities = self._load_operational_locator_authorities(head)
        manifest_sample_ids = {item.sample_id for item in retained_manifest.samples}
        if (
            set(head.source_sha256_by_sample) != manifest_sample_ids
            or set(head.resolved_config_sha256_by_sample) != manifest_sample_ids
            or set(head.sample_project_entry_id_by_sample) != manifest_sample_ids
            or set(head.sample_project_entry_projection_sha256_by_sample) != manifest_sample_ids
        ):
            raise BatchStateError(
                "state material indexes do not exactly cover retained manifest samples"
            )
        project = retained_manifest.project_path or retained_manifest.create_project_path
        effective_project = project
        if locator_authorities:
            project_authorities = tuple(
                item for item in locator_authorities if item.requirement_id == "project:designated"
            )
            if len(project_authorities) != 1:
                raise BatchStateError("operational project locator authority is not singular")
            effective_project = project_authorities[0].effective_path
        if project is None or str(effective_project) != head.project_path:
            raise BatchStateError("retained manifest project authority disagrees with head")
        if (
            active.batch_id != head.batch_id
            or active.normalized_manifest_sha256 != head.normalized_manifest_sha256
        ):
            raise BatchStateError("active plan identity disagrees with state head")
        active_node_ids = set(active.node_by_id)
        if not set(head.invalidated_node_ids) <= active_node_ids:
            raise BatchStateError("active invalidation index names historical or foreign nodes")
        # Reopening a region decision can leave earlier accepted domain indexes
        # without active consumers. Each active node's exact bindings are checked
        # below; only checkpoints must belong to the current plan here.
        if not set(head.checkpoint_descriptor_sha256_by_node) <= active_node_ids:
            raise BatchStateError(
                "A saved processing checkpoint refers to a step outside the current workflow."
            )
        if any(
            (
                head.next_eligible_node_ids_by_sample,
                head.next_eligible_node_ids_by_domain,
                head.processed_image_paths_by_sample,
                head.domain_task_status_by_id,
            )
        ):
            raise BatchStateError(
                "later-task indexes require their dedicated typed mutation authority"
            )
        for node_id, node in active.node_by_id.items():
            if node_id in head.invalidated_node_ids:
                continue
            if not self._node_matches_material_authority(node, head):
                raise BatchStateError("active node binding contradicts state material authority")
        reverse_history: list[str] = []
        history_snapshots: list[_PlanSnapshot] = []
        cursor = active
        seen: set[str] = set()
        while True:
            if cursor.plan_sha256 in seen:
                raise BatchStateError("state head plan history contains a cycle")
            seen.add(cursor.plan_sha256)
            reverse_history.append(cursor.plan_sha256)
            history_snapshots.append(cursor)
            if cursor.predecessor_plan_sha256 is None:
                break
            cursor = self._load_plan_snapshot(
                cursor.predecessor_plan_sha256
            )
        if tuple(reversed(reverse_history)) != head.plan_history_sha256s:
            raise BatchStateError("state head plan history is not an exact chain")

        self._validate_source_manifest_history(tuple(reversed(history_snapshots)), head=head)
        # Attempt bindings reuse the immutable plans just read for this head.
        plans_by_digest = {snapshot.plan_sha256: snapshot for snapshot in history_snapshots}

        known_entry_ids_by_sample: dict[str, set[str]] = {
            sample_id: set() for sample_id in manifest_sample_ids
        }
        for snapshot in history_snapshots:
            for node in snapshot.node_by_id.values():
                for sample_id, entry_id in _node_project_entry_id_by_sample(node).items():
                    known_entry_ids_by_sample[sample_id].add(entry_id)
        if head.project_catalogue_sha256 is not None:
            from .batch_project import BatchProjectCatalogue

            generation_mapping = self._read_batch_project_ref(
                self._batch_project_ref(
                    "batch_project_generation",
                    catalogue_sha256=head.project_catalogue_sha256,
                ),
                label="project catalogue generation",
            )
            current_mapping = self._read_batch_project_ref(
                self._batch_project_ref("batch_project_current"),
                label="project catalogue current",
            )
            try:
                catalogue = BatchProjectCatalogue.from_mapping(generation_mapping)
            except ValueError as exc:
                raise BatchStateError("project catalogue authority cannot be reopened") from exc
            authority_differs = generation_mapping != current_mapping
            if (
                authority_differs
                or catalogue.catalogue_sha256 != head.project_catalogue_sha256
                or catalogue.project_identity_sha256 != head.project_identity_sha256
            ):
                raise BatchStateError("project catalogue authority disagrees with state")
            primary_entries = {
                entry.sample_id: entry
                for entry in catalogue.entries
                if entry.image_role == "primary_mif"
            }
            if any(
                entry_id is not None
                and (
                    sample_id not in primary_entries
                    or primary_entries[sample_id].project_entry_id != entry_id
                    or primary_entries[sample_id].entry_projection_sha256
                    != head.sample_project_entry_projection_sha256_by_sample[sample_id]
                )
                for sample_id, entry_id in head.sample_project_entry_id_by_sample.items()
            ):
                raise BatchStateError("project catalogue entry projection disagrees with state")
            for entry in catalogue.entries:
                known_entry_ids_by_sample[entry.sample_id].add(entry.project_entry_id)
        if any(
            entry_id is not None and entry_id not in known_entry_ids_by_sample[sample_id]
            for sample_id, entry_id in head.sample_project_entry_id_by_sample.items()
        ):
            raise BatchStateError("sample project-entry authority is outside exact plan history")

        expected_scopes: dict[str, Mapping[str, object]] = {}
        for snapshot in reversed(history_snapshots):
            for node_id, node in snapshot.node_by_id.items():
                scope = {
                    "sample_ids": tuple(node["participant_ids"]),
                    "validation_domain_id": node["validation_domain_id"],
                    "analysis_domain_ids": tuple(
                        item["analysis_domain_id"] for item in node["analysis_domain_bindings"]
                    ),
                }
                prior = expected_scopes.get(node_id)
                if prior is not None and dict(prior) != scope:
                    raise BatchStateError("plan history reuses a node ID with foreign scope")
                expected_scopes[node_id] = scope
        if {node_id: dict(scope) for node_id, scope in head.node_scope_by_id.items()} != {
            node_id: dict(scope) for node_id, scope in expected_scopes.items()
        }:
            raise BatchStateError("node scope index disagrees with immutable plan history")

        indexed_attempt_ids = {
            attempt_id
            for attempt_ids in head.attempt_ids_by_node.values()
            for attempt_id in attempt_ids
        }
        if indexed_attempt_ids != set(head.terminal_attempt_sha256_by_attempt_id):
            raise BatchStateError("terminal attempt indexes disagree")
        attempts: dict[str, BatchNodeAttempt] = {}
        attempts_by_digest = {
            digest: self.load_attempt(digest)
            for digest in dict.fromkeys(head.terminal_attempt_sha256_by_attempt_id.values())
        }
        authorizations: dict[str, BatchAttemptAuthorization] = {}

        def authorization_record(digest: str) -> BatchAttemptAuthorization:
            """Read each immutable authorization once in this head validation."""
            if digest not in authorizations:
                authorizations[digest] = self.load_authorization(digest)
            return authorizations[digest]

        retained_slide_by_sample = {
            item.sample_id: item.slide_id for item in retained_manifest.samples
        }
        for attempt_id, terminal_digest in head.terminal_attempt_sha256_by_attempt_id.items():
            attempt = attempts_by_digest[terminal_digest]
            authorization = authorization_record(attempt.authorization_sha256)
            if (
                attempt.attempt_id != attempt_id
                or authorization.attempt_id != attempt_id
                or authorization.node_id != attempt.node_id
            ):
                raise BatchStateError("terminal attempt authority is contradictory")
            admission = plans_by_digest.get(authorization.admission_plan_sha256)
            if admission is None:
                admission = self._load_plan_snapshot(authorization.admission_plan_sha256)
            self._validate_persisted_dispatch_binding(
                authorization,
                admission,
                head=head,
                _retained_manifest=((original_manifest_bytes, retained_manifest)
                    if admission.normalized_manifest_sha256 == head.normalized_manifest_sha256 else None),
            )
            admission_node = admission.node_by_id.get(attempt.node_id)
            binding = attempt.execution_context_binding
            dispatch = authorization.dispatch_binding
            if (
                admission_node is None
                or binding.authorization_sha256 != authorization.authorization_sha256
                or binding.node_id != attempt.node_id
                or binding.adapter_spec_sha256 != admission_node["adapter_spec_sha256"]
                or binding.projection_target_sha256 != dispatch.projection_target_sha256
                or binding.current_image_identity_sha256 != dispatch.current_image_identity_sha256
                or binding.script_sha256 != dispatch.script_sha256
                or binding.project_identity_sha256 != dispatch.project_identity_sha256
                or binding.project_authority_head_sha256 != dispatch.project_authority_head_sha256
                or binding.project_qpproj_snapshot_sha256 != dispatch.project_qpproj_snapshot_sha256
                or binding.project_entry_projection_sha256
                != dispatch.project_entry_projection_sha256
                or binding.compatibility_signature_sha256 != dispatch.compatibility_signature_sha256
                or _digest({"artifact_ref_sha256s": list(binding.input_artifact_ref_sha256s)})
                != authorization.input_artifact_set_sha256
                or attempt.stage_result.stage != admission_node["kind"]
            ):
                raise BatchStateError("terminal StageResult execution binding is foreign")
            declared_outputs = set(admission_node["declared_output_kinds"])
            actual_output_kinds = {output.artifact_kind for output in attempt.output_artifacts}
            if (
                len(actual_output_kinds) != len(attempt.output_artifacts)
                or (
                    attempt.stage_result.status is StageStatus.COMPLETED
                    and actual_output_kinds != declared_outputs
                )
                or (
                    attempt.stage_result.status is not StageStatus.COMPLETED
                    and not actual_output_kinds <= declared_outputs
                )
            ):
                raise BatchStateError("terminal attempt outputs contradict declared typed outputs")
            participant_ids = {str(sample_id) for sample_id in admission_node["participant_ids"]}
            if not participant_ids <= set(retained_slide_by_sample):
                raise BatchStateError(
                    "terminal StageResult participant is outside retained manifest"
                )
            participant_slides = {
                retained_slide_by_sample[sample_id] for sample_id in participant_ids
            }
            if (
                len(participant_slides) == 1
                and attempt.stage_result.slide_id not in participant_slides
            ):
                raise BatchStateError("terminal StageResult slide binding is foreign")
            if authorization.interaction_response_sha256 is not None:
                predecessor = authorization.interaction_predecessor_terminal_record_sha256
                if predecessor is None:
                    raise BatchStateError("response-bound terminal authorization lacks predecessor")
                self._validate_retry_predecessor(
                    head,
                    authorization.node_id,
                    predecessor,
                    authorization.retry_ordinal,
                    attempt=attempts_by_digest.get(predecessor),
                )
                self._load_and_validate_response(
                    authorization.interaction_response_sha256,
                    node_id=authorization.node_id,
                    predecessor=predecessor,
                    predecessor_attempt=attempts_by_digest.get(predecessor),
                )
            attempts[attempt_id] = attempt
        if set(head.node_status_by_id) != set(head.attempt_ids_by_node):
            raise BatchStateError("terminal node statuses disagree with attempt indexes")
        for node_id, attempt_ids in head.attempt_ids_by_node.items():
            if not attempt_ids:
                raise BatchStateError("terminal attempt lineage cannot be empty")
            lineage = [attempts[item] for item in attempt_ids]
            if any(item.node_id != node_id for item in lineage):
                raise BatchStateError("terminal attempt lineage names a foreign node")
            if head.node_status_by_id[node_id] is not lineage[-1].stage_result.status:
                raise BatchStateError("terminal node status contradicts its latest attempt")
            ordinals = tuple(
                authorization_record(item.authorization_sha256).retry_ordinal for item in lineage
            )
            if ordinals != tuple(range(len(ordinals))):
                raise BatchStateError("terminal retry lineage is not exact")

        if self._project_mutation_provider is not None:
            try:
                project_head = self._project_mutation_provider.load_authority_head(
                    head.project_identity_sha256
                )
            except Exception as exc:
                raise BatchStateError("project authority head disagrees with batch state") from exc
            if (
                project_head.project_identity_sha256 != head.project_identity_sha256
                or project_head.authority_head_sha256 != head.project_authority_head_sha256
                or project_head.materialization_state != head.project_materialization_state
                or project_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                or project_head.latest_mutation_attestation_sha256
                != head.project_mutation_attestation_sha256
            ):
                pending_upserts = tuple(
                    digest
                    for node_id, digest in head.pending_authorization_sha256_by_node.items()
                    if active.node_by_id.get(node_id, {}).get("kind") in {"project_upsert", "paired_image_preparation"}
                )
                try:
                    predecessor = self._project_mutation_provider.pending_upsert_predecessor(
                        project_path=Path(head.project_path),
                        project_identity_sha256=head.project_identity_sha256,
                        predecessor_head_sha256=head.project_authority_head_sha256,
                        pending_authorization_sha256s=pending_upserts,
                    )
                except Exception as exc:
                    raise BatchStateError(
                        "project authority head disagrees with batch state"
                    ) from exc
                if (
                    predecessor.materialization_state != head.project_materialization_state
                    or predecessor.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                    or predecessor.latest_mutation_attestation_sha256
                    != head.project_mutation_attestation_sha256
                ):
                    raise BatchStateError("pending upsert predecessor disagrees with batch state")

        self._validate_pending_interaction_requests(head, attempts)

        expected_failures: dict[str, str] = {}
        expected_generations: dict[str, tuple[str, ...]] = {}
        expected_durable_by_sample: dict[str, set[str]] = {}
        expected_durable_by_domain: dict[str, set[str]] = {}
        for node_id, attempt_ids in head.attempt_ids_by_node.items():
            lineage = [attempts[item] for item in attempt_ids]
            latest = lineage[-1].stage_result
            if latest.status is StageStatus.FAILED:
                if latest.error_code is None:
                    raise BatchStateError("failed terminal attempt lacks an error code")
                expected_failures[node_id] = latest.error_code
            generations = tuple(
                item.stage_result.generation_id
                for item in lineage
                if item.stage_result.generation_id is not None
            )
            if generations:
                expected_generations[node_id] = tuple(dict.fromkeys(generations))
            scope = head.node_scope_by_id[node_id]
            for sample_id in scope["sample_ids"]:
                expected_durable_by_sample.setdefault(str(sample_id), set()).add(node_id)
            domain_id = scope["validation_domain_id"]
            if domain_id is not None:
                expected_durable_by_domain.setdefault(
                    _text(domain_id, "node scope validation_domain_id"), set()
                ).add(node_id)
        if dict(head.failure_codes_by_node) != expected_failures:
            raise BatchStateError("failure index contradicts latest terminal attempts")
        if dict(head.generation_references_by_node) != expected_generations:
            raise BatchStateError("generation index contradicts terminal attempt history")
        durable_by_sample = {
            key: set(values) for key, values in head.last_durable_node_ids_by_sample.items()
        }
        durable_by_domain = {
            key: set(values) for key, values in head.last_durable_node_ids_by_domain.items()
        }
        if durable_by_sample != expected_durable_by_sample:
            raise BatchStateError("sample durable-node index contradicts terminal history")
        if durable_by_domain != expected_durable_by_domain:
            raise BatchStateError("domain durable-node index contradicts terminal history")

        terminal_authorizations = {item.authorization_sha256 for item in attempts.values()}
        current = active
        for node_id, authorization_digest in head.pending_authorization_sha256_by_node.items():
            if authorization_digest in terminal_authorizations:
                raise BatchStateError("terminal authorization cannot remain pending")
            authorization = authorization_record(authorization_digest)
            if authorization.node_id != node_id:
                raise BatchStateError("pending authorization names a foreign node")
            current_node = current.node_by_id.get(node_id)
            admission = plans_by_digest.get(authorization.admission_plan_sha256)
            if admission is None:
                admission = self._load_plan_snapshot(authorization.admission_plan_sha256)
            binding = authorization.dispatch_binding
            queued_image_response = (
                binding.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
                and authorization.interaction_response_sha256 is not None
            )
            # A queued image response is retained intent. Its live dispatch is
            # checked at admission after acquiring the actual project resources.
            self._validate_persisted_dispatch_binding(
                authorization, admission,
                head=head, check_current_binding=not queued_image_response,
                _retained_manifest=((original_manifest_bytes, retained_manifest)
                    if admission.normalized_manifest_sha256 == head.normalized_manifest_sha256 else None),
            )
            if queued_image_response and (
                binding.project_identity_sha256 != head.project_identity_sha256
                or binding.project_entry_projection_sha256
                != head.sample_project_entry_projection_sha256_by_sample.get(
                    binding.participant_ids[0]
                )
            ):
                raise BatchStateError("pending authorization image authority was superseded")
            admission_node = admission.node_by_id.get(node_id)
            if (
                current_node is None
                or admission_node is None
                or dict(current_node) != dict(admission_node)
                or authorization.admission_plan_sha256 not in head.plan_history_sha256s
                or authorization.admission_head_sha256
                not in {*head.ancestor_head_sha256s, head.sha256}
            ):
                raise BatchStateError("pending authorization material is stale")
            if authorization.interaction_response_sha256 is not None:
                predecessor = authorization.interaction_predecessor_terminal_record_sha256
                if predecessor is None:
                    raise BatchStateError("response-bound authorization lacks predecessor")
                self._validate_retry_predecessor(
                    head,
                    node_id,
                    predecessor,
                    authorization.retry_ordinal,
                    attempt=attempts_by_digest.get(predecessor),
                )
                self._load_and_validate_response(
                    authorization.interaction_response_sha256,
                    node_id=node_id,
                    predecessor=authorization.interaction_predecessor_terminal_record_sha256,
                    predecessor_attempt=attempts_by_digest.get(predecessor),
                )

            expected_inputs = self._expected_input_set_sha256(current, head, node_id)
            if authorization.input_artifact_set_sha256 != expected_inputs:
                raise BatchStateError("pending authorization input set is stale")
            expected_material = self._current_plan_node_material_sha256(current, node_id)
            if authorization.node_material_sha256 != expected_material:
                raise BatchStateError("pending authorization execution material is stale")
            lineage_length = len(head.attempt_ids_by_node.get(node_id, ()))
            if authorization.retry_ordinal != lineage_length:
                raise BatchStateError("pending authorization retry lineage is not exact")

        training_parent_kinds = {
            "primary_classifier_training_review",
            "secondary_classifier_training_review",
            "supervised_phenotype_training_review",
        }
        for node_id, checkpoint_digest in head.pending_training_fit_set_sha256_by_node.items():
            node = current.node_by_id.get(node_id)
            if (
                node is None
                or node_id in head.invalidated_node_ids
                or node["kind"] not in training_parent_kinds
                or head.node_status_by_id.get(node_id) is StageStatus.COMPLETED
                or checkpoint_digest in head.available_artifact_ref_sha256_by_key
            ):
                raise BatchStateError("selected native fit checkpoint names an ineligible node")
            checkpoint = self._load_training_fit_set(checkpoint_digest)
            originating = authorization_record(
                checkpoint.originating_attempt_authorization_sha256
            )
            scope = head.node_scope_by_id[node_id]
            origin_is_pending = (
                head.pending_authorization_sha256_by_node.get(node_id)
                == originating.authorization_sha256
            )
            origin_is_terminal = checkpoint.originating_attempt_id in set(
                head.attempt_ids_by_node.get(node_id, ())
            )
            if (
                checkpoint.checkpoint_set_sha256 != checkpoint_digest
                or checkpoint.batch_id != head.batch_id
                or checkpoint.node_id != node_id
                or checkpoint.admission_plan_sha256 not in head.plan_history_sha256s
                or checkpoint.validation_domain_id != scope["validation_domain_id"]
                or originating.node_id != node_id
                or originating.attempt_id != checkpoint.originating_attempt_id
                or originating.authorization_sha256
                != checkpoint.originating_attempt_authorization_sha256
                or originating.admission_plan_sha256 != checkpoint.admission_plan_sha256
                or originating.node_material_sha256 != checkpoint.node_material_sha256
                or originating.input_artifact_set_sha256 != checkpoint.input_artifact_set_sha256
                or not (origin_is_pending or origin_is_terminal)
                or any(
                    entry.project_identity_sha256 != head.project_identity_sha256
                    for entry in checkpoint.entries
                )
            ):
                raise BatchStateError("selected native fit checkpoint lineage is stale")

        for node_id, attempt_ids in head.attempt_ids_by_node.items():
            if (
                node_id not in current.node_by_id
                or node_id in head.invalidated_node_ids
                or node_id in head.pending_authorization_sha256_by_node
                or not attempt_ids
            ):
                continue
            latest_attempt = attempts[attempt_ids[-1]]
            authorization = authorization_record(latest_attempt.authorization_sha256)
            current_inputs = self._current_input_ref_sha256s(current, head, node_id)
            expected_material = self._current_plan_node_material_sha256(current, node_id)
            if (
                authorization.input_artifact_set_sha256
                != _digest({"artifact_ref_sha256s": list(current_inputs)})
                or authorization.node_material_sha256 != expected_material
            ):
                raise BatchStateError("latest terminal attempt execution material is stale")

        completed_outputs: dict[str, str] = {}
        for attempt in attempts.values():
            for output in attempt.output_artifacts:
                if (
                    attempt.stage_result.status is StageStatus.COMPLETED
                    and head.node_status_by_id.get(attempt.node_id) is StageStatus.COMPLETED
                    and attempt.node_id in active.node_by_id
                    and attempt.node_id not in head.invalidated_node_ids
                    and attempt.node_id not in head.pending_authorization_sha256_by_node
                ):
                    prior = completed_outputs.get(output.artifact_ref_sha256)
                    if prior is not None and prior != output.artifact_ref_sha256:
                        raise BatchStateError("completed terminal outputs conflict")
                    completed_outputs[output.artifact_ref_sha256] = output.artifact_ref_sha256
        referenced_external = {
            str(edge["accepted_artifact_ref_sha256"])
            for edge in active.edges
            if edge.get("accepted_artifact_ref_sha256") is not None
        }
        required_external = {
            digest
            for digest in active.external_input_sha256s
            if digest not in referenced_external
            or any(
                edge.get("accepted_artifact_ref_sha256") == digest
                and edge.get("consumer_node_id") not in head.invalidated_node_ids
                for edge in active.edges
            )
        }
        expected_inventory = {digest: digest for digest in required_external}
        expected_inventory.update(completed_outputs)
        if dict(head.available_artifact_ref_sha256_by_key) != expected_inventory:
            raise BatchStateError(
                "available inventory disagrees with current external and completed terminal authority"
            )
        if head.execution_state is BatchExecutionState.PAUSED:
            from .batch_pause import (
                _pending_interaction_inventory_sha256,
                _terminal_inventory_sha256,
            )

            record = self._read_quiescent_pause_record(head.quiescent_pause_sha256 or "")
            if (
                record.batch_id != head.batch_id
                or record.predecessor_state_head_sha256 != head.predecessor_head_sha256
                or record.scheduler_quiescence_state_head_sha256 != head.predecessor_head_sha256
                or record.active_plan_sha256 != head.active_plan_sha256
                or record.project_authority_head_sha256 != head.project_authority_head_sha256
                or record.terminal_attempt_inventory_sha256 != _terminal_inventory_sha256(head)
                or record.pending_interaction_inventory_sha256
                != _pending_interaction_inventory_sha256(head)
            ):
                raise BatchStateError("paused head does not bind its exact pause record")
        return attempts_by_digest, authorizations

    def _validate_head_transition(
        self,
        current: BatchStateHead,
        candidate: BatchStateHead,
        *,
        allow_terminal: bool,
        allow_inventory_projection: bool,
        allow_interaction_requests: bool,
        allow_execution_state_transition: bool = False,
        allow_native_fit_checkpoint: bool = False,
        classifier_setup_projection=None,
        secondary_definition_projection=None,
    ) -> None:
        if candidate.batch_id != current.batch_id:
            raise BatchStateError("state transition changes batch identity")
        if (candidate.project_path != current.project_path
                or candidate.operational_rebinding_sha256 != current.operational_rebinding_sha256):
            raise BatchStateError(
                "state transition changes immutable manifest/project/import authority"
            )
        if (candidate.original_manifest_sha256 != current.original_manifest_sha256
                or candidate.normalized_manifest_sha256 != current.normalized_manifest_sha256):
            if classifier_setup_projection is not None:
                from .batch_training import ValidatedTrainingPlanSetProjection
                from .batch_training.accepted_model_reuse import manifest_for_classifier_setup
                projection = classifier_setup_projection
                if (type(projection) is not ValidatedTrainingPlanSetProjection
                        or projection.state_head_sha256 != current.sha256
                        or projection.active_plan_sha256 != current.active_plan_sha256
                        or projection.artifact_ref.artifact_ref_sha256 not in current.available_artifact_ref_sha256_by_key
                        or candidate.active_plan_sha256 == current.active_plan_sha256
                        or current.pending_authorization_sha256_by_node):
                    raise BatchStateError("Saved classifier transition lacks its current accepted setup")
                selected = manifest_for_classifier_setup(self.retained_manifest(current.normalized_manifest_sha256), projection)
                if (candidate.normalized_manifest_sha256 != selected.normalized_manifest_sha256
                        or candidate.original_manifest_sha256 != selected.original_manifest_sha256
                        or candidate.source_sha256_by_sample != current.source_sha256_by_sample
                        or candidate.resolved_config_sha256_by_sample != current.resolved_config_sha256_by_sample
                        or candidate.accepted_domain_revision_sha256_by_id != current.accepted_domain_revision_sha256_by_id
                        or candidate.domain_set_sha256_by_sample != current.domain_set_sha256_by_sample):
                    raise BatchStateError("Saved classifier transition changes unrelated image material")
            elif secondary_definition_projection is not None:
                from .batch_secondary_definitions import (
                    ValidatedSecondaryDefinitionSelection, manifest_for_secondary_definitions,
                )
                projection = secondary_definition_projection
                if (type(projection) is not ValidatedSecondaryDefinitionSelection
                        or projection.state_head_sha256 != current.sha256
                        or projection.active_plan_sha256 != current.active_plan_sha256
                        or projection.artifact_ref.artifact_ref_sha256 not in current.available_artifact_ref_sha256_by_key
                        or candidate.active_plan_sha256 == current.active_plan_sha256
                        or current.pending_authorization_sha256_by_node):
                    raise BatchStateError("Criterion profile transition lacks its current accepted definition choice")
                before = self.retained_manifest(current.normalized_manifest_sha256)
                after = self.retained_manifest(candidate.normalized_manifest_sha256)
                selected = manifest_for_secondary_definitions(
                    before, projection)
                if (after.normalized_manifest_bytes != selected.normalized_manifest_bytes
                        or candidate.original_manifest_sha256 != after.original_manifest_sha256
                        or candidate.source_sha256_by_sample != current.source_sha256_by_sample
                        or candidate.resolved_config_sha256_by_sample != current.resolved_config_sha256_by_sample
                        or candidate.accepted_domain_revision_sha256_by_id != current.accepted_domain_revision_sha256_by_id
                        or candidate.domain_set_sha256_by_sample != current.domain_set_sha256_by_sample):
                    raise BatchStateError("Criterion profile transition changes unrelated image material")
            else:
                if current.operational_rebinding_sha256 is not None:
                    raise BatchStateError("Correct the image in the original analysis folder; relocated source correction is not available.")
                from .batch_source_revision import source_revision_sample
                sample_id = source_revision_sample(
                    self.retained_manifest(current.normalized_manifest_sha256),
                    self.retained_manifest(candidate.normalized_manifest_sha256), current.source_sha256_by_sample)
                if (candidate.active_plan_sha256 == current.active_plan_sha256
                        or current.pending_authorization_sha256_by_node):
                    raise BatchStateError("Source correction requires settled work and its exact successor plan.")
                from .batch_source_revision import source_processing_root
                plan = self._load_plan_snapshot(candidate.active_plan_sha256)
                inspection = next((raw for raw in plan.node_by_id.values() if raw["kind"] == "inspection" and tuple(raw["participant_ids"]) == (sample_id,)), None)
                selected_source = next(item for item in self.retained_manifest(candidate.normalized_manifest_sha256).manifest.samples if item.sample_id == sample_id)
                if inspection is None or "settled_review_revision" not in inspection or selected_source.processing_root != source_processing_root(
                        self.retained_manifest(candidate.normalized_manifest_sha256), sample_id,
                        SettledReviewRevision.from_mapping(inspection["settled_review_revision"]), self.layout.root):
                    raise BatchStateError("Source correction placement is not owned by this inspection return and workspace.")
                selected = self.retained_manifest(candidate.normalized_manifest_sha256)
                sample = next(item for item in selected.samples if item.sample_id == sample_id)
                sources = dict(current.source_sha256_by_sample)
                configurations = dict(current.resolved_config_sha256_by_sample)
                sources[sample_id] = sample.source_file_sha256
                configurations[sample_id] = sample.resolved_config_sha256
                if (candidate.original_manifest_sha256 != selected.original_manifest_sha256
                        or candidate.source_sha256_by_sample != sources
                        or candidate.resolved_config_sha256_by_sample != configurations):
                    raise BatchStateError("Source correction does not retain the exact unaffected material and new selection.")
        if (
            candidate.predecessor_head_sha256 != current.sha256
            or candidate.ancestor_head_sha256s
            != (
                *current.ancestor_head_sha256s,
                current.sha256,
            )
        ):
            raise BatchStateError("candidate head does not descend from exact predecessor")
        if (
            not allow_interaction_requests
            and candidate.pending_interaction_ids != current.pending_interaction_ids
        ):
            raise BatchStateError(
                "pending interactions may change only through their typed store authority"
            )
        if not allow_execution_state_transition and (
            candidate.execution_state != current.execution_state
            or candidate.quiescent_pause_sha256 != current.quiescent_pause_sha256
        ):
            raise BatchStateError(
                "execution state may change only through the pause transition authority"
            )
        if (
            not allow_native_fit_checkpoint
            and candidate.pending_training_fit_set_sha256_by_node
            != current.pending_training_fit_set_sha256_by_node
        ):
            raise BatchStateError(
                "native fit selection may change only through its typed state authority"
            )
        if not allow_terminal:
            protected_fields = [
                "node_status_by_id",
                "attempt_ids_by_node",
                "terminal_attempt_sha256_by_attempt_id",
                "failure_codes_by_node",
                "generation_references_by_node",
                "last_durable_node_ids_by_sample",
                "last_durable_node_ids_by_domain",
            ]
            if not allow_inventory_projection:
                protected_fields.append("available_artifact_ref_sha256_by_key")
            if any(getattr(candidate, name) != getattr(current, name) for name in protected_fields):
                raise BatchStateError(
                    "terminal state may change only through BatchTerminalCommitter"
                )

    def _replace_head_cas(
        self,
        candidate: BatchStateHead,
        expected_predecessor_sha256: str,
        lease: BatchWorkspaceLease,
        *,
        allow_terminal: bool = False,
        allow_inventory_projection: bool = False,
        allow_interaction_requests: bool = False,
        allow_execution_state_transition: bool = False,
        allow_native_fit_checkpoint: bool = False,
        classifier_setup_projection=None,
        secondary_definition_projection=None,
    ) -> BatchStateHead:
        with self._head_transition_lock:
            return self._replace_head_cas_locked(
                candidate,
                expected_predecessor_sha256,
                lease,
                allow_terminal=allow_terminal,
                allow_inventory_projection=allow_inventory_projection,
                allow_interaction_requests=allow_interaction_requests,
                allow_execution_state_transition=allow_execution_state_transition,
                allow_native_fit_checkpoint=allow_native_fit_checkpoint,
                classifier_setup_projection=classifier_setup_projection,
                secondary_definition_projection=secondary_definition_projection,
            )

    def _replace_head_cas_locked(
        self,
        candidate: BatchStateHead,
        expected_predecessor_sha256: str,
        lease: BatchWorkspaceLease,
        *,
        allow_terminal: bool = False,
        allow_inventory_projection: bool = False,
        allow_interaction_requests: bool = False,
        allow_execution_state_transition: bool = False,
        allow_native_fit_checkpoint: bool = False,
        classifier_setup_projection=None,
        secondary_definition_projection=None,
    ) -> BatchStateHead:
        self._assert_lease(lease)
        expected = _sha256(expected_predecessor_sha256, "expected predecessor head")
        current = self.load_head()
        if current.sha256 != expected:
            raise BatchHeadConflictError("state head compare-and-swap predecessor changed")
        if not isinstance(candidate, BatchStateHead):
            raise BatchStateError("candidate head must be BatchStateHead")
        self._validate_head_transition(
            current,
            candidate,
            allow_terminal=allow_terminal,
            allow_inventory_projection=allow_inventory_projection,
            allow_interaction_requests=allow_interaction_requests,
            allow_execution_state_transition=allow_execution_state_transition,
            allow_native_fit_checkpoint=allow_native_fit_checkpoint,
            classifier_setup_projection=classifier_setup_projection,
            secondary_definition_projection=secondary_definition_projection,
        )
        self._assert_lease(lease)
        latest = self.load_head()
        if latest.sha256 != expected:
            raise BatchHeadConflictError("state head changed before atomic replacement")
        self._assert_lease(lease)
        self._write_current_head(candidate)
        # Newly published records were checked by their owning mutator above.
        # A typed current transition does not reopen the retained history.
        self._validated_head_sha256 = candidate.sha256
        self._publish_current_progress(candidate)
        return candidate

    def _replace_project_head_cas(
        self,
        candidate: BatchStateHead,
        expected_predecessor_sha256: str,
        lease: BatchWorkspaceLease,
        *,
        allow_native_fit_checkpoint: bool = False,
    ) -> BatchStateHead:
        """Publish a prepared project head across its catalogue transition.

        Project material is prepared while the retained catalogue still names the
        predecessor.  Once that catalogue advances, the predecessor head is
        intentionally cross-authority stale, so authenticate its canonical bytes
        directly while validating only the successor against the new catalogue.
        """

        with self._head_transition_lock:
            self._assert_lease(lease)
            expected = _sha256(
                expected_predecessor_sha256,
                "expected predecessor head",
            )
            current, current_bytes = self._read_canonical_state_head()
            if current.sha256 != expected:
                raise BatchHeadConflictError(
                    "project state-head compare-and-swap predecessor changed"
                )
            if not isinstance(candidate, BatchStateHead):
                raise BatchStateError("candidate head must be BatchStateHead")
            self._validate_head_transition(
                current,
                candidate,
                allow_terminal=True,
                allow_inventory_projection=True,
                allow_interaction_requests=False,
                allow_native_fit_checkpoint=allow_native_fit_checkpoint,
            )
            self._assert_lease(lease)
            latest, latest_bytes = self._read_canonical_state_head()
            if latest.sha256 != expected or latest_bytes != current_bytes:
                raise BatchHeadConflictError("project state head changed before atomic replacement")
            self._assert_lease(lease)
            self._write_current_head(candidate)
            self._validated_head_sha256 = candidate.sha256
            self._publish_current_progress(candidate)
            return candidate

    @_guarded_batch_state_lease_operation
    def commit_head(
        self,
        head: BatchStateHead,
        expected_predecessor_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        self._assert_lease(lease)
        current = self.load_head()
        protected = (
            "active_plan_sha256",
            "plan_history_sha256s",
            "node_scope_by_id",
            "pending_authorization_sha256_by_node",
            "pending_training_fit_set_sha256_by_node",
            "invalidated_node_ids",
            "revision_reason_by_plan_sha256",
            "source_sha256_by_sample",
            "resolved_config_sha256_by_sample",
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_materialization_state",
            "project_qpproj_snapshot_sha256",
            "project_mutation_attestation_sha256",
            "project_catalogue_sha256",
            "sample_project_entry_id_by_sample",
            "sample_project_entry_projection_sha256_by_sample",
            "accepted_domain_revision_sha256_by_id",
            "domain_set_sha256_by_sample",
            "checkpoint_descriptor_sha256_by_node",
            "next_eligible_node_ids_by_sample",
            "next_eligible_node_ids_by_domain",
            "processed_image_paths_by_sample",
            "pending_interaction_ids",
            "domain_task_status_by_id",
            "execution_state",
            "quiescent_pause_sha256",
        )
        if any(getattr(head, name) != getattr(current, name) for name in protected):
            raise BatchStateError("public head commit cannot install plan or attempt authority")
        return self._replace_head_cas(
            head, expected_predecessor_sha256, lease, allow_terminal=False
        )

    @_guarded_batch_state_lease_operation
    def update_material_authority(
        self,
        update: BatchMaterialAuthorityUpdate,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        self._assert_lease(lease)
        if not isinstance(update, BatchMaterialAuthorityUpdate):
            raise BatchStateError("material authority update requires its exact closed type")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("material authority head changed")
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        sample_ids = set(head.source_sha256_by_sample)
        if (
            set(update.source_sha256_by_sample) != sample_ids
            or set(update.resolved_config_sha256_by_sample) != sample_ids
            or set(update.sample_project_entry_id_by_sample) != sample_ids
            or set(update.sample_project_entry_projection_sha256_by_sample) != sample_ids
        ):
            raise BatchStateError("material authority must exactly cover retained manifest samples")
        active_node_ids = set(snapshot.node_by_id)
        domain_ids = {
            str(binding["analysis_domain_id"])
            for node in snapshot.node_by_id.values()
            for binding in node["analysis_domain_bindings"]
        }
        domain_samples = {
            str(binding["sample_id"])
            for node in snapshot.node_by_id.values()
            for binding in node["analysis_domain_bindings"]
        }
        if (
            not set(update.checkpoint_descriptor_sha256_by_node) <= active_node_ids
            or not set(update.accepted_domain_revision_sha256_by_id) <= domain_ids
            or not set(update.domain_set_sha256_by_sample) <= domain_samples
        ):
            raise BatchStateError("material authority names nonexistent active keys")
        changed_source_samples = {
            sample_id
            for sample_id in sample_ids
            if head.source_sha256_by_sample[sample_id] != update.source_sha256_by_sample[sample_id]
        }
        changed_config_samples = {
            sample_id
            for sample_id in sample_ids
            if head.resolved_config_sha256_by_sample[sample_id]
            != update.resolved_config_sha256_by_sample[sample_id]
        }
        changed_project_entry_samples = {
            sample_id
            for sample_id in sample_ids
            if head.sample_project_entry_id_by_sample[sample_id]
            != update.sample_project_entry_id_by_sample[sample_id]
        }
        changed_project_entry_samples.update(
            sample_id
            for sample_id in sample_ids
            if head.sample_project_entry_projection_sha256_by_sample[sample_id]
            != update.sample_project_entry_projection_sha256_by_sample[sample_id]
        )
        changed_domain_ids = {
            domain_id
            for domain_id in set(head.accepted_domain_revision_sha256_by_id)
            | set(update.accepted_domain_revision_sha256_by_id)
            if head.accepted_domain_revision_sha256_by_id.get(domain_id)
            != update.accepted_domain_revision_sha256_by_id.get(domain_id)
        }
        changed_domain_set_samples = {
            sample_id
            for sample_id in set(head.domain_set_sha256_by_sample)
            | set(update.domain_set_sha256_by_sample)
            if head.domain_set_sha256_by_sample.get(sample_id)
            != update.domain_set_sha256_by_sample.get(sample_id)
        }
        changed_checkpoints = {
            node_id
            for node_id in set(head.checkpoint_descriptor_sha256_by_node)
            | set(update.checkpoint_descriptor_sha256_by_node)
            if head.checkpoint_descriptor_sha256_by_node.get(node_id)
            != update.checkpoint_descriptor_sha256_by_node.get(node_id)
        }
        old_external = set(snapshot.external_input_sha256s)
        new_external = set(update.external_artifact_ref_sha256_by_key)
        changed_external = old_external ^ new_external
        roots: set[str] = set(changed_checkpoints) & active_node_ids
        if head.project_identity_sha256 != update.project_identity_sha256:
            roots.update(
                node_id for node_id, node in snapshot.node_by_id.items() if _node_uses_project(node)
            )
        for node_id, node in snapshot.node_by_id.items():
            participants = set(str(item) for item in node["participant_ids"])
            if participants & (changed_source_samples | changed_config_samples):
                roots.add(node_id)
            if node["project_entry_ids"] and participants & changed_project_entry_samples:
                roots.add(node_id)
            if any(
                binding["analysis_domain_id"] in changed_domain_ids
                or binding["sample_id"] in changed_domain_set_samples
                for binding in node["analysis_domain_bindings"]
            ):
                roots.add(node_id)
        roots.update(
            str(edge["consumer_node_id"])
            for edge in snapshot.edges
            if edge.get("accepted_artifact_ref_sha256") in changed_external
        )
        old_incompatible = self._transitive_dependants(
            snapshot,
            {
                node_id
                for node_id, node in snapshot.node_by_id.items()
                if not self._node_matches_material_authority(node, head)
            },
        )
        new_incompatible = self._transitive_dependants(
            snapshot,
            {
                node_id
                for node_id, node in snapshot.node_by_id.items()
                if not self._node_matches_material_authority(
                    node,
                    head,
                    resolved_config_sha256_by_sample=(update.resolved_config_sha256_by_sample),
                    sample_project_entry_id_by_sample=(update.sample_project_entry_id_by_sample),
                    accepted_domain_revision_sha256_by_id=(
                        update.accepted_domain_revision_sha256_by_id
                    ),
                    domain_set_sha256_by_sample=(update.domain_set_sha256_by_sample),
                )
            },
        )
        resolved_without_attempt = (
            old_incompatible
            - new_incompatible
            - set(head.attempt_ids_by_node)
            - set(head.pending_authorization_sha256_by_node)
        )
        roots.difference_update(resolved_without_attempt)
        affected = self._transitive_dependants(snapshot, roots)
        pending = {
            node_id: digest
            for node_id, digest in head.pending_authorization_sha256_by_node.items()
            if node_id not in affected
        }
        fit_sets = {
            node_id: digest
            for node_id, digest in (head.pending_training_fit_set_sha256_by_node.items())
            if node_id not in affected
            and node_id not in new_incompatible
            and update.project_identity_sha256 == head.project_identity_sha256
        }
        available = dict(head.available_artifact_ref_sha256_by_key)
        for digest in old_external - new_external:
            available.pop(digest, None)
        for digest in old_external & new_external:
            available[digest] = digest
        for node_id in affected:
            attempt_ids = head.attempt_ids_by_node.get(node_id, ())
            if not attempt_ids:
                continue
            attempt = self.load_attempt(head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]])
            for output in attempt.output_artifacts:
                available.pop(output.artifact_ref_sha256, None)
        candidate = head.evolve(
            source_sha256_by_sample=update.source_sha256_by_sample,
            resolved_config_sha256_by_sample=(update.resolved_config_sha256_by_sample),
            project_identity_sha256=update.project_identity_sha256,
            project_authority_head_sha256=update.project_authority_head_sha256,
            project_materialization_state=update.project_materialization_state,
            project_qpproj_snapshot_sha256=update.project_qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=update.project_mutation_attestation_sha256,
            project_catalogue_sha256=update.project_catalogue_sha256,
            sample_project_entry_id_by_sample=(update.sample_project_entry_id_by_sample),
            sample_project_entry_projection_sha256_by_sample=(
                update.sample_project_entry_projection_sha256_by_sample
            ),
            accepted_domain_revision_sha256_by_id=(update.accepted_domain_revision_sha256_by_id),
            domain_set_sha256_by_sample=update.domain_set_sha256_by_sample,
            checkpoint_descriptor_sha256_by_node=(update.checkpoint_descriptor_sha256_by_node),
            pending_authorization_sha256_by_node=pending,
            pending_training_fit_set_sha256_by_node=fit_sets,
            available_artifact_ref_sha256_by_key=available,
            invalidated_node_ids=tuple(
                sorted(
                    (set(head.invalidated_node_ids) - resolved_without_attempt)
                    | affected
                    | new_incompatible
                )
            ),
        )
        return self._replace_head_cas(
            candidate,
            head.sha256,
            lease,
            allow_inventory_projection=True,
            allow_native_fit_checkpoint=True,
        )

    def _authorization_path(self, digest: str) -> Path:
        return self.layout.attempts / f"authorization-{digest}.json"

    def _authorization_request_material_path(self, digest: str) -> Path:
        return self.layout.attempts / (
            "authorization-request-material-" f"{_sha256(digest, 'authorization digest')}.json"
        )

    def _attempt_path(self, digest: str) -> Path:
        return self.layout.attempts / f"terminal-{digest}.json"

    def _request_path(self, request_id: str) -> Path:
        return self.layout.interactions / f"request-{_batch_request_id(request_id)}.json"

    def _response_path(self, digest: str) -> Path:
        return self.layout.interactions / f"response-{digest}.json"

    def _publish_authorization(self, authorization: BatchAttemptAuthorization) -> None:
        self._publish_immutable(
            self._authorization_path(authorization.authorization_sha256),
            _canonical_bytes(authorization.to_mapping()),
            self.layout.attempts,
        )

    def _publish_authorization_request_material(
        self,
        authorization: BatchAttemptAuthorization,
        request: BatchAttemptAuthorizationRequest,
    ) -> None:
        if (
            authorization.node_id != request.node.node_id
            or authorization.dispatch_binding.adapter_spec_sha256
            != request.adapter_spec.spec_sha256
        ):
            raise BatchStateError("authorization request material differs from authorization")
        material = {
            "schema_name": _AUTHORIZATION_REQUEST_SCHEMA_NAME,
            "schema_version": _AUTHORIZATION_REQUEST_SCHEMA_VERSION,
            "authorization_sha256": authorization.authorization_sha256,
            "adapter_spec": _adapter_spec_bundle(request.adapter_spec),
        }
        self._publish_immutable(
            self._authorization_request_material_path(authorization.authorization_sha256),
            _canonical_bytes(material),
            self.layout.attempts,
        )

    def _load_authorization_adapter_spec(
        self, authorization: BatchAttemptAuthorization
    ) -> StageAdapterSpec:
        path = self._authorization_request_material_path(authorization.authorization_sha256)
        self._owned_path(path, self.layout.attempts, "authorization request material")
        try:
            payload = path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("authorization request material is missing or invalid") from exc
        if _canonical_bytes(decoded) != payload:
            raise BatchStateError("authorization request material is not canonical")
        mapping = _closed_mapping(
            decoded,
            fields=frozenset(
                {
                    "schema_name",
                    "schema_version",
                    "authorization_sha256",
                    "adapter_spec",
                }
            ),
            field="authorization request material",
        )
        if (
            mapping["schema_name"] != _AUTHORIZATION_REQUEST_SCHEMA_NAME
            or mapping["schema_version"] != _AUTHORIZATION_REQUEST_SCHEMA_VERSION
            or mapping["authorization_sha256"] != authorization.authorization_sha256
        ):
            raise BatchStateError("authorization request material is foreign")
        spec = _adapter_spec_from_bundle(mapping["adapter_spec"])
        if spec.spec_sha256 != authorization.dispatch_binding.adapter_spec_sha256:
            raise BatchStateError("authorization adapter spec differs from dispatch binding")
        return spec

    def load_authorization(self, digest: str) -> BatchAttemptAuthorization:
        try:
            assert_current_writer(AUTHORIZATION_SCHEMA_NAME, str(AUTHORIZATION_SCHEMA_VERSION))
        except ValueError as exc:
            raise BatchStateError("attempt authorization schema is not current") from exc
        digest = _sha256(digest, "authorization digest")
        path = self._authorization_path(digest)
        self._owned_path(path, self.layout.attempts, "attempt authorization")
        try:
            payload = path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("attempt authorization bytes are missing or invalid") from exc
        if _canonical_bytes(decoded) != payload:
            raise BatchStateError("attempt authorization bytes are not canonical")
        try:
            authorization = BatchAttemptAuthorization.from_mapping(decoded)
        except BatchArtifactError as exc:
            raise BatchStateError("attempt authorization record is invalid") from exc
        if authorization.authorization_sha256 != digest:
            raise BatchStateError("authorization path and checksum disagree")
        return authorization

    def load_interaction_request(self, request_id: str) -> StageResult:
        """Load the exact existing StageResult wire payload for a queued request."""

        request_id = _batch_request_id(request_id)
        path = self._request_path(request_id)
        self._owned_path(path, self.layout.interactions, "interaction request")
        try:
            payload = path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("interaction request bytes are missing or invalid") from exc
        if _canonical_bytes(decoded) != payload:
            raise BatchStateError("interaction request bytes are not canonical")
        try:
            result = StageResult.from_mapping(decoded)
        except ROIAnalysisError as exc:
            raise BatchStateError("interaction request StageResult is invalid") from exc
        if result.status is not StageStatus.NEEDS_INTERACTION:
            raise BatchStateError("interaction request must be needs_interaction")
        _validate_compact_stage_result(result)
        return result

    def _validate_request_publication(
        self,
        publication: BatchInteractionRequestPublication,
        head: BatchStateHead,
    ) -> StageResult:
        """Read current durable authority before authenticating or publishing a request.

        These callers have not acquired the records in their operation, so each
        invocation reads and validates the plan, manifest, terminal and authorization.
        Reconstruction instead passes its own just-read records to the shared
        relationship checks without crossing a head or lease boundary.
        """

        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        try:
            retained = BatchManifest.from_json_bytes(
                self.retained_manifest(head.normalized_manifest_sha256).original_manifest_bytes
            )
        except (OSError, BatchManifestError) as exc:
            raise BatchStateError(
                "interaction request cannot authenticate retained manifest scope"
            ) from exc
        attempt = self.load_attempt(publication.terminal_record_sha256)
        authorization = self.load_authorization(publication.attempt_authorization_sha256)
        return self._validate_request_publication_records(
            publication,
            head,
            snapshot=snapshot,
            retained=retained,
            attempt=attempt,
            authorization=authorization,
        )

    def _validate_request_publication_records(
        self,
        publication: BatchInteractionRequestPublication,
        head: BatchStateHead,
        *,
        snapshot: _PlanSnapshot,
        retained: BatchManifest,
        attempt: BatchNodeAttempt,
        authorization: BatchAttemptAuthorization,
    ) -> StageResult:
        """Check request relationships using records read within this operation.

        Both callers own fresh durable decoding before invoking this method. The
        committed review retains exact material and image association across
        unrelated project saves. Its response receives a new live dispatch binding.
        """

        result = publication.stage_result
        node = snapshot.node_by_id.get(publication.node_id)
        if (
            node is None
            or publication.node_id in head.invalidated_node_ids
            or not self._node_matches_material_authority(node, head)
        ):
            raise BatchStateError("interaction request node is absent or stale in the active plan")
        sample_matches = tuple(
            item for item in retained.samples if item.sample_id == publication.sample_id
        )
        node_bindings = tuple(
            AnalysisDomainBinding.from_mapping(item) for item in node["analysis_domain_bindings"]
        )
        node_entries_by_sample = _node_project_entry_id_by_sample(node)
        current_project_entry_id = head.sample_project_entry_id_by_sample.get(publication.sample_id)
        if (
            len(sample_matches) != 1
            or sample_matches[0].slide_id != publication.slide_id
            or (sample_matches[0].validation_domain_id or retained.validation_domain_id)
            != publication.validation_domain_id
            or publication.sample_id not in node["participant_ids"]
            or publication.validation_domain_id != node["validation_domain_id"]
            or result.stage != node["kind"]
            or result.slide_id != publication.slide_id
            or publication.analysis_domain_bindings != node_bindings
            or publication.project_entry_id != node_entries_by_sample.get(publication.sample_id)
            or (
                publication.project_entry_id is not None
                and publication.project_entry_id != current_project_entry_id
            )
        ):
            raise BatchStateError("interaction request target scope contradicts retained authority")
        attempt_ids = head.attempt_ids_by_node.get(publication.node_id, ())
        if (
            not attempt_ids
            or attempt_ids[-1] != publication.attempt_id
            or head.node_status_by_id.get(publication.node_id) is not StageStatus.NEEDS_INTERACTION
            or head.terminal_attempt_sha256_by_attempt_id.get(publication.attempt_id)
            != publication.terminal_record_sha256
            or publication.node_id in head.pending_authorization_sha256_by_node
        ):
            raise BatchStateError(
                "interaction request lacks the exact committed needs_interaction terminal"
            )
        self._plan_material_is_compatible(head=head, authorization=authorization)
        dispatch = authorization.dispatch_binding
        if dispatch.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE and (
            dispatch.project_identity_sha256 != head.project_identity_sha256
            or dispatch.project_entry_projection_sha256
            != head.sample_project_entry_projection_sha256_by_sample.get(publication.sample_id)
        ):
            raise BatchStateError("interaction request image authority was superseded")
        if (
            attempt.node_id != publication.node_id
            or attempt.attempt_id != publication.attempt_id
            or attempt.authorization_sha256 != publication.attempt_authorization_sha256
            or attempt.stage_result != result
            or authorization.attempt_id != publication.attempt_id
            or authorization.node_id != publication.node_id
            or authorization.dispatch_binding.compatibility_signature_sha256
            != publication.compatibility_signature_sha256
        ):
            raise BatchStateError("interaction request publication contradicts terminal authority")
        return result

    @_guarded_batch_state_operation
    def authenticate_interaction_target(
        self,
        publication: BatchInteractionRequestPublication,
        expected_head_sha256: str,
    ) -> None:
        """Authenticate one queued target against the complete current authority."""

        if not isinstance(publication, BatchInteractionRequestPublication):
            raise BatchStateError("interaction target authentication requires a typed publication")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("interaction target authentication head changed")
        result = self._validate_request_publication(publication, head)
        if (
            publication.interaction_request_id not in head.pending_interaction_ids
            or self.load_interaction_request(publication.interaction_request_id) != result
        ):
            raise BatchStateError("interaction target lacks its exact queued request authority")

    def _reconstruct_interaction_request_publication(
        self,
        request_id: str,
        head: BatchStateHead,
    ) -> BatchInteractionRequestPublication:
        """Reopen one request and authenticate its locally acquired durable records.

        The matching terminal, authorization, active plan and manifest are reused
        only until this reconstruction returns; callers own their final head or
        lease checks before using the result.
        """

        request_id = _batch_request_id(request_id)
        if request_id not in head.pending_interaction_ids:
            raise BatchStateError("interaction request is not currently pending")
        result = self.load_interaction_request(request_id)
        matches: list[tuple[str, BatchNodeAttempt, BatchAttemptAuthorization, str]] = []
        for node_id, attempt_ids in head.attempt_ids_by_node.items():
            if not attempt_ids:
                continue
            attempt_id = attempt_ids[-1]
            terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(attempt_id)
            if terminal_digest is None:
                continue
            attempt = self.load_attempt(terminal_digest)
            authorization = self.load_authorization(attempt.authorization_sha256)
            context_sha256 = _digest(attempt.stage_result.to_mapping()["resume_context"])
            if attempt.stage_result == result and request_id == batch_interaction_request_id(
                node_id=node_id,
                attempt_id=attempt.attempt_id,
                attempt_authorization_sha256=(authorization.authorization_sha256),
                interaction_context_sha256=context_sha256,
            ):
                matches.append((node_id, attempt, authorization, context_sha256))
        if len(matches) != 1:
            raise BatchStateError("pending interaction request lacks one exact terminal authority")
        node_id, attempt, authorization, context_sha256 = matches[0]
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        node = snapshot.node_by_id.get(node_id)
        if node is None:
            raise BatchStateError("interaction request node is not current")
        try:
            retained = BatchManifest.from_json_bytes(
                self.retained_manifest(head.normalized_manifest_sha256).original_manifest_bytes
            )
        except (OSError, BatchManifestError) as exc:
            raise BatchStateError("interaction request retained manifest is unavailable") from exc
        samples = tuple(
            item
            for item in retained.samples
            if item.sample_id in node["participant_ids"] and item.slide_id == result.slide_id
        )
        if len(samples) != 1:
            raise BatchStateError("interaction request lacks one exact manifest sample")
        sample = samples[0]
        publication = BatchInteractionRequestPublication(
            stage_result=result,
            interaction_request_id=request_id,
            node_id=node_id,
            attempt_id=attempt.attempt_id,
            attempt_authorization_sha256=authorization.authorization_sha256,
            interaction_context_sha256=context_sha256,
            terminal_record_sha256=attempt.terminal_record_sha256,
            sample_id=sample.sample_id,
            slide_id=sample.slide_id,
            project_entry_id=_node_project_entry_id_by_sample(node).get(sample.sample_id),
            validation_domain_id=(sample.validation_domain_id or retained.validation_domain_id),
            analysis_domain_bindings=tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in node["analysis_domain_bindings"]
            ),
            compatibility_signature_sha256=(
                authorization.dispatch_binding.compatibility_signature_sha256
            ),
        )
        self._validate_request_publication_records(
            publication,
            head,
            snapshot=snapshot,
            retained=retained,
            attempt=attempt,
            authorization=authorization,
        )
        return publication

    @_guarded_batch_state_operation
    def reopen_interaction_request_publication(
        self,
        request_id: str,
        *,
        expected_head_sha256: str,
    ) -> BatchInteractionRequestPublication:
        """Reopen one pending request as its authenticated public publication."""

        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("interaction request head changed")
        publication = self._reconstruct_interaction_request_publication(request_id, head)
        final_head = self.load_head()
        if final_head.sha256 != expected:
            raise BatchHeadConflictError("interaction request head changed before return")
        return publication

    @_guarded_batch_state_operation
    def reopen_interaction_base_authorization_request(
        self,
        request_id: str,
        *,
        expected_head_sha256: str,
    ) -> BatchAttemptAuthorizationRequest:
        """Reconstruct the exact ordinary request behind one pending terminal."""

        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("interaction base authorization head changed")
        publication = self._reconstruct_interaction_request_publication(request_id, head)
        attempt = self.load_attempt(publication.terminal_record_sha256)
        authorization = self.load_authorization(publication.attempt_authorization_sha256)
        if (
            attempt.authorization_sha256 != authorization.authorization_sha256
            or attempt.attempt_id != authorization.attempt_id
            or authorization.node_id != publication.node_id
        ):
            raise BatchStateError("interaction terminal and authorization authority disagree")
        typed_plan = self._load_typed_plan(head.active_plan_sha256)
        nodes = {item.node_id: item for item in typed_plan.nodes}
        node = nodes.get(publication.node_id)
        if node is None:
            raise BatchStateError("interaction authorization node is not current")
        spec = self._load_authorization_adapter_spec(authorization)
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        input_digests = self._current_input_ref_sha256s(snapshot, head, node.node_id)
        artifacts, _attempts = self._current_artifact_material(head, typed_plan)
        artifacts_by_digest = {
            item.artifact_ref_sha256: item
            for item in artifacts
        }
        try:
            inputs = tuple(artifacts_by_digest[digest] for digest in input_digests)
        except KeyError as exc:
            raise BatchStateError(
                "interaction authorization input artifact is unavailable"
            ) from exc
        if (
            spec.spec_sha256 != node.adapter_spec_sha256
            or authorization.input_artifact_set_sha256 != artifact_ref_set_sha256(inputs)
        ):
            raise BatchStateError("interaction authorization material is stale or foreign")
        dispatch = authorization.dispatch_binding
        rebind_project = spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        request = BatchAttemptAuthorizationRequest(
            node=node,
            input_artifacts=inputs,
            adapter_spec=spec,
            dispatch_authority=BatchPreparedDispatchAuthority(
                compatibility_signature_sha256=(dispatch.compatibility_signature_sha256),
                current_image_identity_sha256=(dispatch.current_image_identity_sha256),
                script_sha256=dispatch.script_sha256,
                project_identity_sha256=(
                    head.project_identity_sha256
                    if rebind_project
                    else dispatch.project_identity_sha256
                ),
                project_authority_head_sha256=(
                    head.project_authority_head_sha256
                    if rebind_project
                    else dispatch.project_authority_head_sha256
                ),
                project_qpproj_snapshot_sha256=(
                    head.project_qpproj_snapshot_sha256
                    if rebind_project
                    else dispatch.project_qpproj_snapshot_sha256
                ),
                project_entry_projection_sha256=(
                    head.sample_project_entry_projection_sha256_by_sample.get(publication.sample_id)
                    if rebind_project
                    else dispatch.project_entry_projection_sha256
                ),
            ),
            retry_ordinal=0,
            interaction_response_sha256=None,
            interaction_predecessor_terminal_record_sha256=None,
        )
        final_head = self.load_head()
        if final_head.sha256 != expected:
            raise BatchHeadConflictError(
                "interaction base authorization head changed before return"
            )
        return request

    def _publish_request(self, publication: BatchInteractionRequestPublication) -> None:
        self._publish_immutable(
            self._request_path(publication.interaction_request_id),
            publication.canonical_request_bytes,
            self.layout.interactions,
        )

    @_guarded_batch_state_lease_operation
    def publish_interaction_requests(
        self,
        publications: tuple[BatchInteractionRequestPublication, ...],
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        """Publish a complete request set and install its IDs in one head CAS."""

        self._assert_lease(lease)
        values = tuple(publications)
        if not values or not all(
            isinstance(item, BatchInteractionRequestPublication) for item in values
        ):
            raise BatchStateError("publish_interaction_requests requires a non-empty typed tuple")
        request_ids = tuple(item.interaction_request_id for item in values)
        node_ids = tuple(item.node_id for item in values)
        if len(set(request_ids)) != len(request_ids) or len(set(node_ids)) != len(node_ids):
            raise BatchStateError("interaction request publication contains duplicate authority")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("interaction request publication head changed")
        if set(request_ids) & set(head.pending_interaction_ids):
            raise BatchStateError("interaction request is already pending")
        for publication in values:
            self._validate_request_publication(publication, head)
        for publication in values:
            self._assert_lease(lease)
            self._publish_request(publication)
        candidate = head.evolve(
            pending_interaction_ids=tuple(sorted((*head.pending_interaction_ids, *request_ids)))
        )
        return self._replace_head_cas(
            candidate,
            head.sha256,
            lease,
            allow_interaction_requests=True,
        )

    def _validate_pending_interaction_requests(
        self,
        head: BatchStateHead,
        attempts: Mapping[str, BatchNodeAttempt],
    ) -> None:
        """Rebind each pending ID to one exact latest committed terminal.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        seen_nodes: set[str] = set()
        for request_id in head.pending_interaction_ids:
            try:
                result = self.load_interaction_request(request_id)
            except BatchStateError:
                if self._is_pending_cluster_label_review_request(head, request_id):
                    continue
                raise
            matches: list[tuple[str, BatchNodeAttempt, BatchAttemptAuthorization]] = []
            for node_id, attempt_ids in head.attempt_ids_by_node.items():
                if not attempt_ids or attempt_ids[-1] not in attempts:
                    continue
                attempt = attempts[attempt_ids[-1]]
                if attempt.stage_result != result:
                    continue
                authorization = self.load_authorization(attempt.authorization_sha256)
                context_sha256 = _digest(result.to_mapping()["resume_context"])
                if request_id == batch_interaction_request_id(
                    node_id=node_id,
                    attempt_id=attempt.attempt_id,
                    attempt_authorization_sha256=authorization.authorization_sha256,
                    interaction_context_sha256=context_sha256,
                ):
                    matches.append((node_id, attempt, authorization))
            if len(matches) != 1:
                raise BatchStateError(
                    "pending interaction request lacks one exact terminal authority"
                )
            node_id, attempt, _ = matches[0]
            if (
                node_id in seen_nodes
                or head.node_status_by_id.get(node_id) is not StageStatus.NEEDS_INTERACTION
                or node_id in head.invalidated_node_ids
                or node_id in head.pending_authorization_sha256_by_node
                or head.terminal_attempt_sha256_by_attempt_id.get(attempt.attempt_id)
                != attempt.terminal_record_sha256
            ):
                raise BatchStateError(
                    "pending interaction request contradicts current node authority"
                )
            seen_nodes.add(node_id)

    def _is_pending_cluster_label_review_request(
        self, head: BatchStateHead, request_id: str
    ) -> bool:
        """Recognize one aggregate request without creating an ordinary file."""

        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        for node in snapshot.node_by_id.values():
            if node.get("kind") != "cluster_label_review":
                continue
            try:
                target = self._cluster_label_review_target_for_node(head, str(node["node_id"]))
            except BatchStateError:
                continue
            if target.interaction_request_id == request_id:
                return True
        return False

    def _publish_response(self, publication: BatchInteractionResponsePublication) -> None:
        self._validate_response_publication(publication)
        self._publish_immutable(
            self._response_path(publication.response_sha256),
            publication.canonical_response_bytes,
            self.layout.interactions,
        )

    def _validate_response_publication(
        self, publication: BatchInteractionResponsePublication
    ) -> Mapping[str, object]:
        """Validate response publication for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        try:
            decoded = json.loads(publication.canonical_response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("interaction response is invalid UTF-8 JSON") from exc
        if _canonical_bytes(decoded) != publication.canonical_response_bytes:
            raise BatchStateError("interaction response bytes are not canonical")
        expected = frozenset(
            {
                "schema_name",
                "schema_version",
                "target_id",
                "group_id",
                "group_sha256",
                "node_id",
                "prior_attempt_id",
                "prior_attempt_authorization_sha256",
                "prior_terminal_record_sha256",
                "interaction_request_id",
                "interaction_context_sha256",
                "action",
                "response",
                "response_validator_id",
                "response_validator_version",
                "response_sha256",
            }
        )
        mapping = _closed_mapping(decoded, fields=expected, field="interaction response")
        if (
            mapping["schema_name"] != INTERACTION_RESPONSE_SCHEMA_NAME
            or type(mapping["schema_version"]) is not int
            or mapping["schema_version"] != INTERACTION_RESPONSE_SCHEMA_VERSION
        ):
            raise BatchStateError("interaction response schema is not current")
        if (
            mapping["node_id"] != publication.node_id
            or mapping["prior_terminal_record_sha256"]
            != publication.predecessor_terminal_record_sha256
        ):
            raise BatchStateError("interaction response publication identity mismatch")
        unsigned = {key: item for key, item in mapping.items() if key != "response_sha256"}
        if (
            mapping["response_sha256"] != publication.response_sha256
            or _digest(unsigned) != publication.response_sha256
        ):
            raise BatchStateError("interaction response checksum mismatch")
        for field_name in (
            "group_sha256",
            "prior_attempt_authorization_sha256",
            "prior_terminal_record_sha256",
            "interaction_context_sha256",
        ):
            _sha256(mapping[field_name], field_name)
        for field_name in (
            "target_id",
            "group_id",
            "node_id",
            "prior_attempt_id",
            "interaction_request_id",
            "action",
            "response_validator_id",
            "response_validator_version",
        ):
            _text(mapping[field_name], field_name)
        _reject_prohibited_payload(mapping["response"], "interaction response")
        return mapping

    def _validate_request_against_snapshot(
        self,
        request: BatchAttemptAuthorizationRequest,
        snapshot: _PlanSnapshot,
        head: BatchStateHead,
    ) -> None:
        persisted = snapshot.node_by_id.get(request.node.node_id)
        if persisted is None or dict(persisted) != request.node.to_mapping():
            raise BatchStateError("authorization node is absent or materially stale")
        was_invalidated = request.node.node_id in head.invalidated_node_ids
        if was_invalidated and not self._node_matches_material_authority(persisted, head):
            raise BatchStateError("materially incompatible invalidated node cannot be authorized")
        if request.node.node_id in head.pending_authorization_sha256_by_node:
            raise BatchStateError("node already has a pending authorization")
        for dependency_id in request.node.dependency_ids:
            attempt_ids = head.attempt_ids_by_node.get(dependency_id, ())
            if (
                head.node_status_by_id.get(dependency_id) is not StageStatus.COMPLETED
                or dependency_id in head.invalidated_node_ids
                or dependency_id in head.pending_authorization_sha256_by_node
                or not attempt_ids
            ):
                raise BatchStateError("node dependency is not currently terminal-completed")
            terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(attempt_ids[-1])
            dependency_attempt = (
                None if terminal_digest is None else self.load_attempt(terminal_digest)
            )
            dependency_node = snapshot.node_by_id.get(dependency_id)
            if (
                dependency_attempt is None
                or dependency_node is None
                or dependency_attempt.stage_result.status is not StageStatus.COMPLETED
                or {item.artifact_kind for item in dependency_attempt.output_artifacts}
                != set(dependency_node["declared_output_kinds"])
                or any(
                    head.available_artifact_ref_sha256_by_key.get(item.artifact_ref_sha256)
                    != item.artifact_ref_sha256
                    for item in dependency_attempt.output_artifacts
                )
            ):
                raise BatchStateError(
                    "node dependency lacks exact completed typed output authority"
                )
        prior_status = head.node_status_by_id.get(request.node.node_id)
        if prior_status is StageStatus.COMPLETED and not was_invalidated:
            raise BatchStateError("completed material requires an explicit plan rerun generation")
        if (
            prior_status is StageStatus.NEEDS_INTERACTION
            and request.interaction_response_sha256 is None
        ):
            raise BatchStateError("needs_interaction retry requires an accepted response")
        if (
            prior_status is not StageStatus.NEEDS_INTERACTION
            and request.interaction_response_sha256 is not None
        ):
            raise BatchStateError("accepted response retry lacks needs_interaction authority")
        edges = [
            item for item in snapshot.edges if item.get("consumer_node_id") == request.node.node_id
        ]
        inputs = tuple(request.input_artifacts)
        if (
            len({item.artifact_ref_sha256 for item in inputs}) != len(inputs)
            or tuple(sorted(item.artifact_id for item in inputs)) != request.node.input_artifact_ids
        ):
            raise BatchStateError("authorization inputs do not exactly cover node inputs")
        if len(edges) != len(inputs):
            raise BatchStateError("authorization inputs disagree with active plan edges")
        unmatched = {item.artifact_ref_sha256: item for item in inputs}
        for edge in edges:
            artifact_id = _text(edge.get("artifact_id"), "edge artifact_id")
            candidates = tuple(
                item
                for item in unmatched.values()
                if item.artifact_id == artifact_id
                and item.artifact_kind == edge.get("artifact_kind")
                and _matches_persisted_requirement(item, edge.get("requirement"))
            )
            if len(candidates) != 1:
                raise BatchStateError("authorization input does not match typed plan edge")
            artifact = candidates[0]
            expected_external = edge.get("accepted_artifact_ref_sha256")
            if expected_external is not None:
                if artifact.artifact_ref_sha256 != expected_external:
                    raise BatchStateError("authorization external input projection is stale")
            else:
                if artifact.producer_node_id != edge.get("producer_node_id"):
                    raise BatchStateError("authorization internal input producer is stale")
                attempt_ids = head.attempt_ids_by_node.get(artifact.producer_node_id or "", ())
                if not attempt_ids:
                    raise BatchStateError("authorization internal input lacks terminal producer")
                terminal = self.load_attempt(
                    head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]]
                )
                if artifact.artifact_ref_sha256 not in {
                    item.artifact_ref_sha256 for item in terminal.output_artifacts
                }:
                    raise BatchStateError(
                        "authorization internal input is not the latest producer output"
                    )
            if head.available_artifact_ref_sha256_by_key.get(artifact.artifact_ref_sha256) != (
                artifact.artifact_ref_sha256
            ):
                raise BatchStateError("authorization input is not in available inventory")
            unmatched.pop(artifact.artifact_ref_sha256)
        if unmatched:
            raise BatchStateError("authorization input has no exact typed plan edge")

    def _committed_clustering_scope_selections(
        self,
        *,
        snapshot: _PlanSnapshot,
        manifest: ValidatedBatchManifest,
        head: BatchStateHead,
    ) -> Mapping[str, ClusteringMode]:
        """Read only preparation attempts needed to resolve late scope decisions.

        This runs while validating a head as well as while authorizing work, so
        it uses the supplied head and existing internal authority path rather
        than the public authenticator, which calls ``load_head`` recursively.
        """
        from .batch_phenotyping._common import BatchPhenotypingError
        from .batch_phenotyping.authority import decode_committed_preparation

        sample_by_id = {item.sample_id: item for item in manifest.enabled_samples}
        preparation_nodes = tuple(
            node
            for node in snapshot.node_by_id.values()
            if node["kind"] == "unsupervised_preparation"
        )
        requires_selection = {
            str(sample_id)
            for node in preparation_nodes
            for sample_id in node["participant_ids"]
            if str(sample_id) in sample_by_id
            and sample_by_id[str(sample_id)].config.clustering.mode is None
        }
        if not requires_selection:
            return MappingProxyType({})
        active = self._load_typed_plan(head.active_plan_sha256)
        active_by_id = {item.node_id: item for item in active.nodes}
        selected: dict[str, ClusteringMode] = {}
        seen: set[str] = set()
        for node in preparation_nodes:
            participants = tuple(str(item) for item in node["participant_ids"])
            if len(participants) != 1 or participants[0] not in requires_selection:
                continue
            sample_id = participants[0]
            node_id = str(node["node_id"])
            current_node = active_by_id.get(node_id)
            attempt_ids = head.attempt_ids_by_node.get(node_id, ())
            if (
                current_node is None
                or current_node.to_mapping() != dict(node)
                or node_id in head.invalidated_node_ids
                or head.node_status_by_id.get(node_id) is not StageStatus.COMPLETED
                or not attempt_ids
            ):
                raise BatchStateError(
                    "deferred clustering scope lacks current completed preparation"
                )
            terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(attempt_ids[-1])
            if terminal_digest is None:
                raise BatchStateError("deferred clustering preparation lacks terminal authority")
            attempt = self.load_attempt(terminal_digest)
            refs = tuple(
                ref
                for ref in attempt.output_artifacts
                if ref.artifact_kind == "unsupervised_preparation_result"
            )
            if (
                attempt.node_id != node_id
                or attempt.stage_result.status is not StageStatus.COMPLETED
                or len(refs) != 1
                or head.available_artifact_ref_sha256_by_key.get(refs[0].artifact_ref_sha256)
                != refs[0].artifact_ref_sha256
            ):
                raise BatchStateError("deferred clustering preparation is not available and exact")
            try:
                authorization = self.load_authorization(attempt.authorization_sha256)
                self._plan_material_is_compatible(head=head, authorization=authorization)
                authority = self._current_artifact_authority_from_records(
                    refs[0], head, current_node, attempt, authorization
                )
                preparation = decode_committed_preparation(
                    authority,
                    maximum_byte_count=_PREPARATION_SCOPE_MAX_BYTES,
                )
            except (BatchPhenotypingError, BatchArtifactError) as exc:
                raise BatchStateError(
                    "deferred clustering preparation authority is invalid"
                ) from exc
            planned_bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in node["analysis_domain_bindings"]
            )
            sample = sample_by_id[sample_id]
            planned_entries = tuple(node["project_entry_ids"])
            if (
                len(planned_entries) != 1
                or preparation.sample_id != sample_id
                or preparation.project_entry_id != planned_entries[0]
                or preparation.validation_domain_ids != (sample.validation_domain_id,)
                or preparation.analysis_domain_ids
                != tuple(sorted(item.analysis_domain_id for item in planned_bindings))
                or preparation.resolved_config_sha256 != sample.resolved_config_sha256
            ):
                raise BatchStateError("deferred clustering preparation scope contradicts plan")
            choice = preparation.clustering_mode
            prior = selected.setdefault(sample_id, choice)
            if prior != choice:
                raise BatchStateError("deferred clustering sections disagree on scope or group")
            seen.add(node_id)
        if set(selected) != requires_selection or not seen:
            raise BatchStateError(
                "deferred clustering participants lack complete preparation scope"
            )
        return MappingProxyType(selected)

    def _derive_dispatch_binding(
        self,
        request: BatchAttemptAuthorizationRequest,
        snapshot: _PlanSnapshot,
        head: BatchStateHead,
    ) -> BatchDispatchBinding:
        node = snapshot.node_by_id.get(request.node.node_id)
        if node is None:
            raise BatchStateError("dispatch binding node is absent from active plan")
        spec = request.adapter_spec
        if (
            spec.spec_sha256 != node["adapter_spec_sha256"]
            or spec.node_kind != node["kind"]
            or spec.node_version != node["version"]
            or spec.resource_kind != node["resource"]
            or spec.dispatch_mode.value != node["dispatch_mode"]
        ):
            raise BatchStateError("dispatch adapter spec contradicts exact active node authority")
        if spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE:
            expected_entries = _node_project_entry_id_by_sample(node)
            if len(expected_entries) != 1:
                raise BatchStateError(
                    "current-image QuPath dispatch requires one exact project entry"
                )
            expected_sample_id = next(iter(expected_entries))
            expected_projection = head.sample_project_entry_projection_sha256_by_sample.get(
                expected_sample_id
            )
            if (
                expected_projection is None
                or request.dispatch_authority.project_identity_sha256
                != head.project_identity_sha256
                or request.dispatch_authority.project_authority_head_sha256
                != head.project_authority_head_sha256
                or request.dispatch_authority.project_qpproj_snapshot_sha256
                != head.project_qpproj_snapshot_sha256
                or request.dispatch_authority.project_entry_projection_sha256 != expected_projection
                or any(
                    head.sample_project_entry_id_by_sample.get(sample_id) != project_entry_id
                    for sample_id, project_entry_id in expected_entries.items()
                )
            ):
                raise BatchStateError("QuPath dispatch project authority contradicts current head")
        try:
            # Initialization already retained and authenticated these exact bytes.
            # Re-running static admission here would incorrectly reject a
            # create_project_path after the authorized mutation materializes it.
            original_manifest_bytes = self.retained_manifest(snapshot.normalized_manifest_sha256).original_manifest_bytes
            retained = BatchManifest.from_json_bytes(original_manifest_bytes)
            if node.get("clustering_scope") is not None:
                validated_manifest = revalidate_retained_manifest(
                    original_manifest_bytes,
                    self.retained_manifest(snapshot.normalized_manifest_sha256).normalized_manifest_bytes,
                )
                _validate_clustering_scope_against_manifest(
                    node,
                    validated_manifest,
                    snapshot.node_by_id,
                    selected_scope_by_sample=self._committed_clustering_scope_selections(
                        snapshot=snapshot,
                        manifest=validated_manifest,
                        head=head,
                    ),
                )
            slide_by_sample = {item.sample_id: item.slide_id for item in retained.samples}
            participants = tuple(str(item) for item in node["participant_ids"])
            entries = tuple(str(item) for item in node["project_entry_ids"])
            domain_bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in node["analysis_domain_bindings"]
            )
            sample_id = participants[0] if node["scope"] == "sample" else None
            slide_id = None if sample_id is None else slide_by_sample[sample_id]
            project_entry_id = entries[0] if sample_id is not None and len(entries) == 1 else None
            scope = ArtifactScope(
                validation_domain_id=(
                    None
                    if node["validation_domain_id"] is None
                    else str(node["validation_domain_id"])
                ),
                sample_id=sample_id,
                slide_id=slide_id,
                project_entry_id=project_entry_id,
                analysis_domain_bindings=domain_bindings,
            )
            configuration_bindings = tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in node["configuration_bindings"]
            )
            projection_target_sha256 = _dispatch_projection_target_sha256(
                scope=scope,
                configuration_bindings=configuration_bindings,
                manifest_sha256=snapshot.normalized_manifest_sha256,
                participants=participants,
            )
            return BatchDispatchBinding.create(
                node_id=request.node.node_id,
                adapter_spec_sha256=spec.spec_sha256,
                dispatch_mode=spec.dispatch_mode,
                participant_ids=participants,
                project_entry_ids=entries,
                projection_target_sha256=projection_target_sha256,
                authority=request.dispatch_authority,
            )
        except (OSError, KeyError, BatchManifestError, BatchArtifactError) as exc:
            raise BatchStateError(
                "cannot derive exact dispatch binding from retained authority"
            ) from exc

    def _validate_persisted_dispatch_binding(
        self,
        authorization: BatchAttemptAuthorization,
        snapshot: _PlanSnapshot,
        *,
        head: BatchStateHead,
        check_current_binding: bool = False,
        _retained_manifest: tuple[bytes, BatchManifest] | None = None,
    ) -> None:
        """Check the original dispatch projection and its currently unchanged task.

        Another image's model choice may create a later
        manifest. The saved projection still uses its admission manifest; the
        current task must retain every original field and live project checks.
        Historical dispatches retain their exact admitted task and projection.
        Current dispatches also recheck clustering choices and the live project
        binding before work can use them.
        """

        node = snapshot.node_by_id.get(authorization.node_id)
        binding = authorization.dispatch_binding
        if node is None:
            raise BatchStateError("authorization dispatch node is absent")
        admission = (snapshot if snapshot.plan_sha256 == authorization.admission_plan_sha256
                     else self._load_plan_snapshot(authorization.admission_plan_sha256))
        if admission.node_by_id.get(authorization.node_id) != node:
            raise BatchStateError("authorization dispatch task changed since admission")
        if check_current_binding and snapshot.plan_sha256 != admission.plan_sha256:
            if (admission.plan_sha256 not in head.plan_history_sha256s
                    or snapshot.plan_sha256 not in head.plan_history_sha256s
                    or self._expected_input_set_sha256(snapshot, head, authorization.node_id)
                    != authorization.input_artifact_set_sha256):
                raise BatchStateError("authorization dispatch inputs or plan ancestry changed since admission")
        if (
            binding.node_id != authorization.node_id
            or binding.adapter_spec_sha256 != node["adapter_spec_sha256"]
            or binding.dispatch_mode.value != node["dispatch_mode"]
            or binding.participant_ids
            != tuple(sorted(str(item) for item in node["participant_ids"]))
            or binding.project_entry_ids
            != tuple(sorted(str(item) for item in node["project_entry_ids"]))
        ):
            raise BatchStateError("authorization dispatch binding contradicts persisted node")
        try:
            # Static validation was completed before initialization. The
            # retained create locator is expected to exist after a successful
            # project mutation and must remain usable for authority rederivation.
            original_manifest_bytes = self.retained_manifest(admission.normalized_manifest_sha256).original_manifest_bytes
            if _retained_manifest is not None:
                if original_manifest_bytes != _retained_manifest[0]:
                    raise BatchStateError("retained manifest changed during head validation")
                retained = _retained_manifest[1]
            else:
                retained = BatchManifest.from_json_bytes(original_manifest_bytes)
            if check_current_binding and node.get("clustering_scope") is not None:
                validated_manifest = revalidate_retained_manifest(
                    original_manifest_bytes,
                    self.retained_manifest(admission.normalized_manifest_sha256).normalized_manifest_bytes,
                )
                _validate_clustering_scope_against_manifest(
                    node,
                    validated_manifest,
                    snapshot.node_by_id,
                    selected_scope_by_sample=self._committed_clustering_scope_selections(
                        snapshot=snapshot,
                        manifest=validated_manifest,
                        head=head,
                    ),
                )
            slide_by_sample = {item.sample_id: item.slide_id for item in retained.samples}
            participants = binding.participant_ids
            domain_bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in node["analysis_domain_bindings"]
            )
            sample_id = participants[0] if node["scope"] == "sample" else None
            scope = ArtifactScope(
                validation_domain_id=(
                    None
                    if node["validation_domain_id"] is None
                    else str(node["validation_domain_id"])
                ),
                sample_id=sample_id,
                slide_id=(None if sample_id is None else slide_by_sample[sample_id]),
                project_entry_id=(
                    binding.project_entry_ids[0]
                    if sample_id is not None and len(binding.project_entry_ids) == 1
                    else None
                ),
                analysis_domain_bindings=domain_bindings,
            )
            configuration_bindings = tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in node["configuration_bindings"]
            )
            expected_target = _dispatch_projection_target_sha256(
                scope=scope,
                configuration_bindings=configuration_bindings,
                manifest_sha256=admission.normalized_manifest_sha256,
                participants=participants,
            )
        except (OSError, KeyError, BatchManifestError, BatchArtifactError) as exc:
            raise BatchStateError("authorization dispatch binding cannot be rederived") from exc
        if binding.projection_target_sha256 != expected_target:
            raise BatchStateError("authorization dispatch projection target is not exact")
        if (
            check_current_binding
            and binding.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        ):
            expected_entries = _node_project_entry_id_by_sample(node)
            if len(expected_entries) != 1:
                raise BatchStateError(
                    "current-image QuPath authorization requires one project entry"
                )
            expected_sample_id = next(iter(expected_entries))
            expected_projection = head.sample_project_entry_projection_sha256_by_sample.get(
                expected_sample_id
            )
            current_project_binding = (
                binding.project_identity_sha256 == head.project_identity_sha256
                and binding.project_authority_head_sha256
                == head.project_authority_head_sha256
                and binding.project_qpproj_snapshot_sha256
                == head.project_qpproj_snapshot_sha256
            )
            project_stage_predecessor_binding = (
                self._authenticated_project_stage_predecessor_binding(
                    authorization=authorization,
                    binding=binding,
                    head=head,
                )
            )
            native_fit_predecessor_binding = self._authenticated_native_fit_predecessor_binding(
                authorization=authorization,
                binding=binding,
                head=head,
            )
            if (
                expected_projection is None
                or not (
                    current_project_binding
                    or project_stage_predecessor_binding
                    or native_fit_predecessor_binding
                )
                or binding.project_entry_projection_sha256 != expected_projection
                or any(
                    head.sample_project_entry_id_by_sample.get(sample_id)
                    != project_entry_id
                    for sample_id, project_entry_id in expected_entries.items()
                )
            ):
                raise BatchStateError("authorization dispatch project authority is stale")

    def _validate_observed_project_stage_candidate(
        self,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
        candidate: BatchStateHead,
    ) -> None:
        """Require every observed save to name its exact terminal authority."""

        snapshot = self._load_plan_snapshot(candidate.active_plan_sha256)
        for context in contexts:
            authorization = context.authorization
            self._validate_persisted_dispatch_binding(
                authorization,
                snapshot,
                head=candidate, check_current_binding=True,
            )
            if not self._authenticated_project_stage_predecessor_binding(
                authorization=authorization,
                binding=authorization.dispatch_binding,
                head=candidate,
                require_named_authorization_edge=True,
            ):
                raise BatchStateError("authorization dispatch project authority is stale")

    def _replace_observed_project_stage_head_cas(
        self,
        candidate: BatchStateHead,
        expected_predecessor_sha256: str,
        lease: BatchWorkspaceLease,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
    ) -> BatchStateHead:
        """Strictly authenticate observed saves before entering project CAS."""

        self._validate_observed_project_stage_candidate(
            contexts=contexts,
            candidate=candidate,
        )
        return self._replace_project_head_cas(
            candidate,
            expected_predecessor_sha256,
            lease,
            allow_native_fit_checkpoint=True,
        )

    def _authenticated_native_fit_predecessor_binding(
        self,
        *,
        authorization: BatchAttemptAuthorization,
        binding: BatchDispatchBinding,
        head: BatchStateHead,
    ) -> bool:
        """Recognize only the exact state-selected classifier-only head chain."""

        provider = self._project_mutation_provider
        workspace_scope = self.workspace_storage_scope_sha256
        if provider is None or workspace_scope is None or binding.project_identity_sha256 != head.project_identity_sha256 or (binding.project_authority_head_sha256 is None) or (binding.project_qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256):
            return False
        try:
            entries = tuple(
                entry
                for checkpoint_sha256 in (head.pending_training_fit_set_sha256_by_node.values())
                for entry in self._load_training_fit_set(checkpoint_sha256).entries
            )
            by_successor = {
                entry.successor_project_authority_head_sha256: entry for entry in entries
            }
            if len(by_successor) != len(entries):
                return False
            current_provider_head = provider.load_authority_head(head.project_identity_sha256)
            if (
                current_provider_head.authority_head_sha256 != head.project_authority_head_sha256
                or current_provider_head.qpproj_snapshot_sha256
                != head.project_qpproj_snapshot_sha256
            ):
                return self._authenticated_completed_native_fit_predecessor_binding(
                    authorization=authorization,
                    binding=binding,
                    head=head,
                    starting_project_authority_head_sha256=(head.project_authority_head_sha256),
                )
            cursor = head.project_authority_head_sha256
            visited: set[str] = set()
            fit_edge_seen = False
            relocation_predecessor_sha256 = current_provider_head.predecessor_authority_head_sha256
            if relocation_predecessor_sha256 is not None:
                relocation_predecessor = provider._load_head_version(
                    head.project_identity_sha256,
                    relocation_predecessor_sha256,
                )
                if provider._is_exact_locator_relocation_edge(
                    current_provider_head, relocation_predecessor
                ):
                    visited.add(cursor)
                    cursor = relocation_predecessor_sha256
            while cursor != binding.project_authority_head_sha256:
                if cursor in visited:
                    return False
                visited.add(cursor)
                entry = by_successor.get(cursor)
                if entry is None:
                    break
                fit_edge_seen = True
                provider_head = provider._load_head_version(head.project_identity_sha256, cursor)
                attestation = provider.load_mutation_attestation(
                    head.project_identity_sha256,
                    entry.mutation_attestation_sha256,
                )
                material = provider.load_native_classifier_fit_material(
                    head.project_identity_sha256,
                    entry.runtime_fit_material_sha256,
                    workspace_storage_scope_sha256=workspace_scope,
                )
                provider.reopen_native_classifier_fit(
                    material,
                    workspace_storage_scope_sha256=workspace_scope,
                    mutation_attestation_sha256=(entry.mutation_attestation_sha256),
                    successor_project_authority_head_sha256=(
                        entry.successor_project_authority_head_sha256
                    ),
                )
                if (
                    entry.project_identity_sha256 != head.project_identity_sha256
                    or entry.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                    or provider_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                    or provider_head.latest_mutation_attestation_sha256
                    != entry.mutation_attestation_sha256
                    or attestation.mutation_kind != "annotation_classifier"
                    or attestation.predecessor_authority_head_sha256
                    != entry.predecessor_project_authority_head_sha256
                    or attestation.post_save_qpproj_snapshot_sha256
                    != head.project_qpproj_snapshot_sha256
                    or attestation.workspace_catalogue_sha256 is not None
                    or attestation.operation_evidence_sha256 != entry.runtime_fit_material_sha256
                    or material.predecessor_project_authority_head_sha256
                    != entry.predecessor_project_authority_head_sha256
                    or material.qpproj_snapshot_sha256 != entry.qpproj_snapshot_sha256
                ):
                    return False
                cursor = entry.predecessor_project_authority_head_sha256
            if cursor == binding.project_authority_head_sha256 and fit_edge_seen:
                return True
            # A current-image evidence save can be the exact predecessor of the
            # authenticated fit chain. Continue with the existing project-stage
            # verifier at that already-proven cursor instead of treating the two
            # independently valid mutation kinds as competing alternatives.
            if fit_edge_seen and self._authenticated_project_stage_predecessor_binding(
                authorization=authorization,
                binding=binding,
                head=head,
                starting_project_authority_head_sha256=cursor,
                batch_head_already_reached=True,
            ):
                return True
            return self._authenticated_completed_native_fit_predecessor_binding(
                authorization=authorization,
                binding=binding,
                head=head,
                starting_project_authority_head_sha256=cursor,
            )
        except Exception:
            return False

    def _authenticated_completed_native_fit_predecessor_binding(
        self,
        *,
        authorization: BatchAttemptAuthorization,
        binding: BatchDispatchBinding,
        head: BatchStateHead,
        starting_project_authority_head_sha256: str | None = None,
    ) -> bool:
        """Reopen an exact completed record-set terminal and its fit chain."""

        provider = self._project_mutation_provider
        workspace_scope = self.workspace_storage_scope_sha256
        if (
            provider is None
            or workspace_scope is None
            or binding.project_authority_head_sha256 is None
        ):
            return False
        try:
            current_provider_head = provider.load_authority_head(head.project_identity_sha256)
            cursor = (
                head.project_authority_head_sha256
                if starting_project_authority_head_sha256 is None
                else starting_project_authority_head_sha256
            )
            if cursor is None:
                return False
            selected_provider_head = provider._load_head_version(
                head.project_identity_sha256,
                cursor,
            )
            if (
                selected_provider_head.authority_head_sha256 != cursor
                or selected_provider_head.qpproj_snapshot_sha256
                != head.project_qpproj_snapshot_sha256
                or (
                    starting_project_authority_head_sha256 is None
                    and current_provider_head.authority_head_sha256
                    != head.project_authority_head_sha256
                )
            ):
                return False
            visited: set[str] = set()
            if starting_project_authority_head_sha256 is None:
                relocation_predecessor_sha256 = (
                    current_provider_head.predecessor_authority_head_sha256
                )
                if relocation_predecessor_sha256 is not None:
                    relocation_predecessor = provider._load_head_version(
                        head.project_identity_sha256,
                        relocation_predecessor_sha256,
                    )
                    if provider._is_exact_locator_relocation_edge(
                        current_provider_head, relocation_predecessor
                    ):
                        visited.add(cursor)
                        cursor = relocation_predecessor_sha256

            materials = []
            while cursor != binding.project_authority_head_sha256:
                if cursor in visited:
                    return False
                visited.add(cursor)
                provider_head = provider._load_head_version(head.project_identity_sha256, cursor)
                attestation_sha256 = provider_head.latest_mutation_attestation_sha256
                if attestation_sha256 is None:
                    return False
                attestation = provider.load_mutation_attestation(
                    head.project_identity_sha256,
                    attestation_sha256,
                )
                if attestation.predecessor_authority_head_sha256 is None:
                    return False
                if attestation.mutation_kind != "annotation_classifier":
                    if attestation.mutation_kind == "project_stage":
                        break
                    return False
                material = provider.load_native_classifier_fit_material(
                    head.project_identity_sha256,
                    attestation.operation_evidence_sha256,
                    workspace_storage_scope_sha256=workspace_scope,
                )
                provider.reopen_native_classifier_fit(
                    material,
                    workspace_storage_scope_sha256=workspace_scope,
                    mutation_attestation_sha256=attestation_sha256,
                    successor_project_authority_head_sha256=cursor,
                )
                if (
                    provider_head.authority_head_sha256 != cursor
                    or provider_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                    or provider_head.latest_mutation_attestation_sha256 != attestation_sha256
                    or attestation.operation_evidence_sha256 != material.fit_material_sha256
                    or attestation.mutation_authorization_sha256
                    != material.mutation_authorization_sha256
                    or attestation.predecessor_authority_head_sha256
                    != material.predecessor_project_authority_head_sha256
                    or attestation.post_save_qpproj_snapshot_sha256
                    != head.project_qpproj_snapshot_sha256
                    or attestation.workspace_catalogue_sha256 is not None
                    or material.project_identity_sha256 != head.project_identity_sha256
                    or material.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
                ):
                    return False
                materials.append(material)
                cursor = material.predecessor_project_authority_head_sha256
            if not materials:
                return False
            active = self._load_plan_snapshot(head.active_plan_sha256)
            for node_id, attempt_ids in head.attempt_ids_by_node.items():
                if (
                    not attempt_ids
                    or head.node_status_by_id.get(node_id) is not StageStatus.COMPLETED
                    or node_id in head.invalidated_node_ids
                ):
                    continue
                terminal_sha256 = head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]]
                attempt = self.load_attempt(terminal_sha256)
                outputs = tuple(attempt.output_artifacts)
                if len(outputs) != 1 or outputs[0].artifact_kind != "classifier_record_set":
                    continue
                output = outputs[0]
                if (
                    output.producer_node_id != node_id
                    or output.producer_attempt_id != attempt.attempt_id
                    or output.producer_attempt_authorization_sha256 != attempt.authorization_sha256
                    or head.available_artifact_ref_sha256_by_key.get(output.artifact_ref_sha256)
                    != output.artifact_ref_sha256
                ):
                    continue
                wanted = set(attempt.execution_context_binding.input_artifact_ref_sha256s)
                inputs: dict[str, BatchArtifactRef] = {}
                for candidate_sha256 in head.terminal_attempt_sha256_by_attempt_id.values():
                    candidate = self.load_attempt(candidate_sha256)
                    for artifact in candidate.output_artifacts:
                        digest = artifact.artifact_ref_sha256
                        if digest in wanted:
                            prior = inputs.get(digest)
                            if prior is not None and prior != artifact:
                                return False
                            inputs[digest] = artifact
                if set(inputs) != wanted:
                    continue
                input_refs = tuple(inputs[digest] for digest in sorted(inputs))
                plan_refs = tuple(
                    item for item in input_refs if item.artifact_kind == "training_plan_set"
                )
                evidence_refs = tuple(
                    item for item in input_refs if item.artifact_kind != "training_plan_set"
                )
                if len(plan_refs) != 1:
                    continue
                plan_identity = plan_refs[0].owning_identity
                record_identity = output.owning_identity
                classifier_ids = tuple(plan_identity.get("classifier_ids", ()))
                training_plan_ids = tuple(plan_identity.get("training_plan_ids", ()))
                output_inventory = tuple(record_identity.get("output_inventory", ()))
                for value, field in (
                    (
                        record_identity.get("classifier_record_set_sha256"),
                        "classifier_record_set_sha256",
                    ),
                    (
                        record_identity.get("source_training_plan_set_sha256"),
                        "source_training_plan_set_sha256",
                    ),
                    (
                        record_identity.get("evidence_child_inventory_sha256"),
                        "evidence_child_inventory_sha256",
                    ),
                    (
                        record_identity.get("material_evidence_sha256"),
                        "material_evidence_sha256",
                    ),
                    (
                        plan_identity.get("training_plan_set_sha256"),
                        "training_plan_set_sha256",
                    ),
                ):
                    _sha256(value, field)
                for training_plan_id in training_plan_ids:
                    _sha256(training_plan_id, "training_plan_id")
                if any(
                    not isinstance(item, (tuple, list))
                    or len(item) != 2
                    or not isinstance(item[0], str)
                    or _sha256(item[1], "classifier_record_sha256") != item[1]
                    for item in output_inventory
                ):
                    continue
                material_by_classifier = {item.classifier_id: item for item in materials}
                training_plan_by_classifier = dict(
                    zip(classifier_ids, training_plan_ids, strict=True)
                )
                material_projection_sha256 = _classifier_record_native_fit_inventory_sha256(
                    [
                        _classifier_record_native_fit_projection(
                            training_plan_id=(training_plan_by_classifier[material.classifier_id]),
                            classifier_id=material.classifier_id,
                            training_material_evidence_sha256=(
                                material.training_material_evidence_sha256
                            ),
                            fit_plan_sha256=material.fit_plan_sha256,
                            fit_execution_sha256=(material.fit_execution_sha256),
                            fit_material_sha256=(material.fit_material_sha256),
                            classifier_version=material.classifier_version,
                            project_relative_path=(material.project_relative_path),
                            native_classifier_sha256=(material.native_classifier_sha256),
                            native_classifier_byte_count=(material.native_classifier_byte_count),
                            project_identity_sha256=(material.project_identity_sha256),
                        )
                        for material in materials
                    ]
                )
                if (
                    len(material_by_classifier) != len(materials)
                    or len(classifier_ids) != len(training_plan_ids)
                    or set(material_by_classifier) != set(classifier_ids)
                    or record_identity.get("source_training_plan_set_sha256")
                    != plan_identity.get("training_plan_set_sha256")
                    or record_identity.get("classifier_kind")
                    != plan_identity.get("classifier_kind")
                    or record_identity.get("validation_domain_id")
                    != plan_identity.get("validation_domain_id")
                    or record_identity.get("evidence_child_inventory_sha256")
                    != _digest(sorted(item.artifact_ref_sha256 for item in evidence_refs))
                    or record_identity.get("material_evidence_sha256") != material_projection_sha256
                    or len(output_inventory) != len(classifier_ids)
                ):
                    continue
                # The completed record authenticates the intervening native-fit
                # chain.  The historical authorization may belong to any exact
                # earlier project-stage edge (for example tissue detection),
                # not only to the evidence artifact consumed by this record.
                lineage_matches = (
                    cursor == binding.project_authority_head_sha256
                    or self._authenticated_project_stage_predecessor_binding(
                        authorization=authorization,
                        binding=binding,
                        head=head,
                        starting_project_authority_head_sha256=cursor,
                        batch_head_already_reached=True,
                    )
                )
                if node_id in active.node_by_id and lineage_matches:
                    return True
            return False
        except Exception:
            return False

    def _authenticated_project_stage_predecessor_binding(
        self,
        *,
        authorization: BatchAttemptAuthorization,
        binding: BatchDispatchBinding,
        head: BatchStateHead,
        starting_project_authority_head_sha256: str | None = None,
        batch_head_already_reached: bool = False,
        require_named_authorization_edge: bool = False,
        require_unaffected_project_entry_id: str | None = None,
    ) -> bool:
        """Recognize a terminal named by one exact retained stage-attestation edge."""

        provider = self._project_mutation_provider
        if provider is None or head.project_catalogue_sha256 is None or head.project_identity_sha256 is None:
            return False
        try:
            current_project_head = provider.load_authority_head(head.project_identity_sha256)
            if binding.project_identity_sha256 != head.project_identity_sha256:
                return False
            if require_unaffected_project_entry_id is not None and (
                starting_project_authority_head_sha256 is not None
                or require_named_authorization_edge
                or current_project_head.authority_head_sha256 != head.project_authority_head_sha256
                or current_project_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
            ):
                return False
            from mif_pipeline.qupath_runtime.project_stage import (
                ProjectStageMutationEvidenceRecord,
            )

            if batch_head_already_reached and starting_project_authority_head_sha256 is None:
                return False
            cursor = (
                current_project_head
                if starting_project_authority_head_sha256 is None
                else provider._load_head_version(
                    head.project_identity_sha256,
                    starting_project_authority_head_sha256,
                )
            )
            visited: set[str] = set()
            batch_head_reached = batch_head_already_reached
            binding_qpproj_anchor_sha256 = head.project_qpproj_snapshot_sha256
            named_authorization_edge_seen = False
            while cursor.latest_mutation_attestation_sha256 is not None:
                if cursor.authority_head_sha256 in visited:
                    return False
                visited.add(cursor.authority_head_sha256)
                selected_candidate_edge = (
                    cursor.authority_head_sha256 == head.project_authority_head_sha256
                )
                if selected_candidate_edge:
                    if cursor.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256:
                        return False
                    batch_head_reached = True
                attestation = provider.load_mutation_attestation(
                    head.project_identity_sha256,
                    cursor.latest_mutation_attestation_sha256,
                )
                if attestation.mutation_kind == "annotation_classifier":
                    if require_unaffected_project_entry_id is not None:
                        return False
                    # Project-stage saves and native classifier fits form one
                    # immutable project-authority chain.  Once the selected
                    # batch head has been reached, cross a fit segment only
                    # through the completed record-set authenticator; it will
                    # validate the fit material and resume project-stage
                    # ancestry at the exact predecessor cursor.
                    if not batch_head_reached:
                        return False
                    if require_named_authorization_edge:
                        binding_head_sha256 = binding.project_authority_head_sha256
                        if not named_authorization_edge_seen or binding_head_sha256 is None:
                            return False
                        binding_head = provider._load_head_version(
                            head.project_identity_sha256,
                            binding_head_sha256,
                        )
                        if not (
                            binding.project_qpproj_snapshot_sha256
                            == binding_qpproj_anchor_sha256
                            == binding_head.qpproj_snapshot_sha256
                        ):
                            return False
                    return self._authenticated_completed_native_fit_predecessor_binding(
                        authorization=authorization,
                        binding=binding,
                        head=head,
                        starting_project_authority_head_sha256=(cursor.authority_head_sha256),
                    )
                predecessor_sha256 = attestation.predecessor_authority_head_sha256
                if (
                    attestation.mutation_kind != "project_stage"
                    or predecessor_sha256 is None
                    or cursor.predecessor_authority_head_sha256 != predecessor_sha256
                    or attestation.post_save_qpproj_snapshot_sha256 != cursor.qpproj_snapshot_sha256
                ):
                    return False
                evidence_mapping = self._read_batch_project_ref(
                    self._batch_project_ref(
                        "batch_project_runtime_evidence",
                        evidence_sha256=(attestation.operation_evidence_sha256),
                    ),
                    label="project-stage mutation evidence",
                )
                evidence = ProjectStageMutationEvidenceRecord.from_mapping(evidence_mapping)
                if (
                    evidence.mutation_evidence_sha256 != attestation.operation_evidence_sha256
                    or evidence.project_identity_sha256 != head.project_identity_sha256
                    or evidence.predecessor_project_authority_head_sha256 != predecessor_sha256
                    or evidence.mutation_request_sha256 != attestation.mutation_request_sha256
                    or evidence.mutation_authorization_sha256
                    != attestation.mutation_authorization_sha256
                ):
                    return False
                if require_unaffected_project_entry_id is not None and any(
                    item.project_entry_id == require_unaffected_project_entry_id
                    or item.saved_state_disposition == "unattestable"
                    for item in evidence.target_observations
                ):
                    return False
                names_authorization = batch_head_reached and any(
                    item.attempt_authorization_sha256 == authorization.authorization_sha256
                    and item.node_id == authorization.node_id
                    for item in evidence.target_observations
                )
                if (
                    require_named_authorization_edge
                    and selected_candidate_edge
                    and not names_authorization
                ):
                    return False
                if names_authorization and not named_authorization_edge_seen:
                    predecessor = provider._load_head_version(
                        head.project_identity_sha256, predecessor_sha256
                    )
                    if binding.project_authority_head_sha256 == predecessor.authority_head_sha256:
                        return (
                            binding.project_qpproj_snapshot_sha256
                            == predecessor.qpproj_snapshot_sha256
                        )
                    named_authorization_edge_seen = True
                    binding_qpproj_anchor_sha256 = predecessor.qpproj_snapshot_sha256
                    cursor = predecessor
                else:
                    cursor = provider._load_head_version(
                        head.project_identity_sha256, predecessor_sha256
                    )
                if require_unaffected_project_entry_id is not None:
                    binding_qpproj_anchor_sha256 = cursor.qpproj_snapshot_sha256
                if (
                    batch_head_reached
                    and cursor.authority_head_sha256 == binding.project_authority_head_sha256
                    and (named_authorization_edge_seen or not require_named_authorization_edge)
                ):
                    return (
                        binding.project_qpproj_snapshot_sha256
                        == binding_qpproj_anchor_sha256
                        == cursor.qpproj_snapshot_sha256
                    )
            return False
        except Exception:
            return False

    def _expected_input_set_sha256(
        self,
        snapshot: _PlanSnapshot,
        head: BatchStateHead,
        node_id: str,
    ) -> str:
        digests = self._current_input_ref_sha256s(snapshot, head, node_id)
        return _digest({"artifact_ref_sha256s": list(digests)})

    def _current_input_ref_sha256s(
        self,
        snapshot: _PlanSnapshot,
        head: BatchStateHead,
        node_id: str,
    ) -> tuple[str, ...]:
        node = snapshot.node_by_id.get(node_id)
        if node is None:
            raise BatchStateError("input-set validation node is absent from active plan")
        edges = [item for item in snapshot.edges if item.get("consumer_node_id") == node_id]
        artifact_ids = tuple(sorted(str(item["artifact_id"]) for item in edges))
        if artifact_ids != tuple(node["input_artifact_ids"]):
            raise BatchStateError("active plan input edges contradict node material")
        digests: list[str] = []
        for edge in edges:
            accepted = edge.get("accepted_artifact_ref_sha256")
            if accepted is not None:
                digest = head.available_artifact_ref_sha256_by_key.get(str(accepted))
                if digest != accepted:
                    raise BatchStateError(
                        "external input projection is absent from current inventory"
                    )
                digests.append(_sha256(digest, "current input artifact digest"))
                continue
            producer_node_id = _text(edge.get("producer_node_id"), "edge producer_node_id")
            attempt_ids = head.attempt_ids_by_node.get(producer_node_id, ())
            if (
                head.node_status_by_id.get(producer_node_id) is not StageStatus.COMPLETED
                or not attempt_ids
            ):
                raise BatchStateError("internal input producer is not terminal-completed")
            attempt = self.load_attempt(head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]])
            matches = tuple(
                output
                for output in attempt.output_artifacts
                if output.artifact_id == edge.get("artifact_id")
                and output.artifact_kind == edge.get("artifact_kind")
                and output.producer_node_id == producer_node_id
            )
            if len(matches) != 1:
                raise BatchStateError("internal input lacks one exact committed producer output")
            digest = matches[0].artifact_ref_sha256
            if head.available_artifact_ref_sha256_by_key.get(digest) != digest:
                raise BatchStateError("internal input projection is absent from current inventory")
            digests.append(digest)
        if len(set(digests)) != len(digests):
            raise BatchStateError("current input artifact set contains duplicate references")
        return tuple(sorted(digests))

    def _current_plan_node_material_sha256(self, snapshot: _PlanSnapshot, node_id: str) -> str:
        node = snapshot.node_by_id.get(node_id)
        if node is None:
            raise BatchStateError("plan node is absent from active plan")
        return _sha256(node["material_sha256"], "active plan node material")

    def _authoritative_retry_ordinals(self, head: BatchStateHead, node_id: str) -> set[int]:
        ordinals: set[int] = set()
        pending = head.pending_authorization_sha256_by_node.get(node_id)
        if pending is not None:
            ordinals.add(self.load_authorization(pending).retry_ordinal)
        for attempt_id in head.attempt_ids_by_node.get(node_id, ()):
            digest = head.terminal_attempt_sha256_by_attempt_id[attempt_id]
            attempt = self.load_attempt(digest)
            ordinals.add(self.load_authorization(attempt.authorization_sha256).retry_ordinal)
        return ordinals

    def _pending_response_needs_dispatch_authorization(
        self, authorization: BatchAttemptAuthorization, head: BatchStateHead,
    ) -> bool:
        """Distinguish queued responses from an original native recovery owner."""
        binding = authorization.dispatch_binding
        if (binding.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
                or authorization.interaction_response_sha256 is None
                or (binding.project_authority_head_sha256 == head.project_authority_head_sha256
                    and binding.project_qpproj_snapshot_sha256 == head.project_qpproj_snapshot_sha256)):
            return False
        provider = self._project_mutation_provider
        return provider is not None and not provider._unresolved_active_records_for_project(
            project_uri=Path(head.project_path).as_uri(),
            project_identity_sha256=head.project_identity_sha256,
        )

    def _validate_unlaunched_project_response_replacement(
        self, prior: BatchAttemptAuthorization, request: BatchAttemptAuthorizationRequest,
        head: BatchStateHead, saved_state_by_entry: Mapping[str, str],
    ) -> None:
        """Bind the unchanged saved response to its actual dispatch admission.

        An active native journal keeps its original attempt. Only authenticated
        saves of other image entries may precede a fresh response authorization.
        Common issuance checks own the current request's material and inputs.
        """
        binding, authority = prior.dispatch_binding, request.dispatch_authority
        if (head.execution_state is not BatchExecutionState.ACTIVE
                or head.pending_authorization_sha256_by_node.get(prior.node_id) != prior.authorization_sha256
                or self.load_authorization(prior.authorization_sha256) != prior
                or head.node_status_by_id.get(prior.node_id) is not StageStatus.NEEDS_INTERACTION
                or prior.attempt_id in head.terminal_attempt_sha256_by_attempt_id
                or prior.admission_head_sha256 not in head.ancestor_head_sha256s
                or prior.admission_plan_sha256 not in head.plan_history_sha256s
                or prior.node_id != request.node.node_id
                or prior.node_material_sha256 != request.node.material_sha256
                or prior.input_artifact_set_sha256 != artifact_ref_set_sha256(request.input_artifacts)
                or prior.retry_ordinal != request.retry_ordinal
                or prior.interaction_response_sha256 != request.interaction_response_sha256
                or prior.interaction_predecessor_terminal_record_sha256
                != request.interaction_predecessor_terminal_record_sha256
                or request.failed_retry is not None
                or len(binding.participant_ids) != 1 or len(binding.project_entry_ids) != 1
                or binding.adapter_spec_sha256 != request.adapter_spec.spec_sha256
                or not self._pending_response_needs_dispatch_authorization(prior, head)):
            raise BatchStateError("queued response cannot replace an executed or changed authorization")
        sample_id, entry_id = binding.participant_ids[0], binding.project_entry_ids[0]
        if (binding.project_identity_sha256 != head.project_identity_sha256
                or head.sample_project_entry_id_by_sample.get(sample_id) != entry_id
                or head.sample_project_entry_projection_sha256_by_sample.get(sample_id)
                != binding.project_entry_projection_sha256
                or any(getattr(authority, name) != getattr(binding, name) for name in (
                    "compatibility_signature_sha256", "current_image_identity_sha256",
                    "script_sha256", "project_identity_sha256", "project_entry_projection_sha256"))
                or not self._authenticated_project_stage_predecessor_binding(
                    authorization=prior, binding=binding, head=head,
                    require_unaffected_project_entry_id=entry_id)):
            raise BatchStateError("queued response image was changed or its project ancestry is uncertain")
        from mif_pipeline.qupath_runtime.catalogue import (
            _MAX_READ_ONLY_QPPROJ_BYTES, _ENTRY_HIERARCHY_MAX_BYTES,
            _bounded_registration_regular_file_digest,
        )
        project = Path(head.project_path)
        for path, expected, maximum in (
            (project, head.project_qpproj_snapshot_sha256, _MAX_READ_ONLY_QPPROJ_BYTES),
            (project.parent / "data" / entry_id / "data.qpdata",
             saved_state_by_entry.get(entry_id), _ENTRY_HIERARCHY_MAX_BYTES),
        ):
            observed, _size = _bounded_registration_regular_file_digest(
                path, max_bytes=maximum, label="queued response current project data",
            )
            if expected is None or observed != expected:
                raise BatchStateError("queued response project or image bytes changed before admission")

    @_guarded_batch_state_lease_operation
    def authorize_attempts(
        self,
        requests: tuple[BatchAttemptAuthorizationRequest, ...],
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        response_publications: tuple[BatchInteractionResponsePublication, ...] = (),
        consumed_interaction_request_ids: tuple[str, ...] = (),
        *,
        paused_response_authority: object | None = None,
        failed_retry_admissions: tuple[object, ...] = (),
        replaced_pending_authorizations: tuple[BatchAttemptAuthorization, ...] = (),
    ) -> tuple[BatchAttemptAuthorization, ...]:
        self._assert_lease(lease)
        values = tuple(requests)
        if not values or not all(
            isinstance(item, BatchAttemptAuthorizationRequest) for item in values
        ):
            raise BatchStateError("authorize_attempts requires a non-empty request tuple")
        node_ids = tuple(item.node.node_id for item in values)
        if len(set(node_ids)) != len(node_ids):
            raise BatchStateError("authorization batch contains duplicate node IDs")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("authorization admission head changed")
        from .batch_failed_retry import (
            is_failed_retry_node_kind,
            _FailedRetryAdmission,
        )

        if type(failed_retry_admissions) is not tuple:
            raise BatchStateError("failed-stage retry admissions must be a tuple")
        admissions = failed_retry_admissions
        if not all(
            type(item) is _FailedRetryAdmission
            and getattr(item, "_issuer", None) is not None
            and type(getattr(item, "request", None)) is BatchAttemptAuthorizationRequest
            for item in admissions
        ):
            raise BatchStateError("failed-stage retry admissions must be exact owner values")
        admission_by_node = {item.request.node.node_id: item for item in admissions}
        if len(admission_by_node) != len(admissions):
            raise BatchStateError("failed-stage retry admissions contain duplicates")
        required_admission_nodes = {
            item.node.node_id
            for item in values
            if head.node_status_by_id.get(item.node.node_id) is StageStatus.FAILED
            and item.node.node_id not in head.invalidated_node_ids
            and is_failed_retry_node_kind(item.node.kind)
        }
        if set(admission_by_node) != required_admission_nodes:
            raise BatchStateError("failed retry requests require exact owner admissions")
        if any(item.failed_retry is not None and item.node.node_id not in required_admission_nodes for item in values):
            raise BatchStateError("failed retry provenance requires its failed-stage owner admission")
        if head.execution_state is BatchExecutionState.PAUSED:
            from .batch_pause import _PausedResponseAuthority

            if type(paused_response_authority) is not _PausedResponseAuthority:
                raise BatchStateError("paused response requires pause-owner authority")
            paused_response_authority._validate_for_authorize(
                state_store=self,
                lease=lease,
                head=head,
            )
        elif paused_response_authority is not None:
            raise BatchStateError("paused response authority requires a paused head")
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        replaced = tuple(replaced_pending_authorizations)
        replacements = {item.node_id: item for item in replaced
                        if type(item) is BatchAttemptAuthorization}
        if len(replacements) != len(replaced) or not set(replacements) <= set(node_ids):
            raise BatchStateError("pending response replacements must be exact unique authorizations")
        saved_state_by_entry = ({} if not replaced else
            self.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=head.sha256, lease=lease))
        for request in values:
            prior = replacements.get(request.node.node_id)
            if prior is not None:
                self._validate_unlaunched_project_response_replacement(
                    prior, request, head, saved_state_by_entry
                )
        authorization_head = (head if not replaced else replace(
            head, pending_authorization_sha256_by_node={
                node_id: digest for node_id, digest in head.pending_authorization_sha256_by_node.items()
                if node_id not in replacements
            },
        ))
        retained_publications = []
        for item in replaced:
            response_path = self._owned_path(
                self._response_path(item.interaction_response_sha256),
                self.layout.interactions, "queued accepted response",
            )
            try:
                response_bytes = response_path.read_bytes()
            except OSError as exc:
                raise BatchStateError("queued accepted response cannot be reopened") from exc
            retained_publications.append(BatchInteractionResponsePublication(
                canonical_response_bytes=response_bytes,
                response_sha256=item.interaction_response_sha256,
                node_id=item.node_id,
                predecessor_terminal_record_sha256=item.interaction_predecessor_terminal_record_sha256,
            ))
        publications = tuple(response_publications) + tuple(retained_publications)
        if not all(isinstance(item, BatchInteractionResponsePublication) for item in publications):
            raise BatchStateError("response publications must be typed")
        by_digest = {item.response_sha256: item for item in publications}
        if len(by_digest) != len(publications):
            raise BatchStateError("response publication tuple contains duplicates")
        response_bound = {
            item.interaction_response_sha256
            for item in values
            if item.interaction_response_sha256 is not None
        }
        if set(by_digest) != response_bound:
            raise BatchStateError("response publications must match retry requests one-to-one")
        consumed = tuple(_batch_request_id(item) for item in consumed_interaction_request_ids)
        if len(set(consumed)) != len(consumed):
            raise BatchStateError("consumed interaction request IDs must be unique")
        if head.execution_state is BatchExecutionState.PAUSED and (
            not consumed or any(item.interaction_response_sha256 is None for item in values)
        ):
            raise BatchStateError("paused authorization requires accepted responses")
        validated_responses: dict[str, Mapping[str, object]] = {}
        dispatch_binding_by_node: dict[str, BatchDispatchBinding] = {}
        for request in values:
            admission = admission_by_node.get(request.node.node_id)
            if admission is not None:
                admission._validate_for_authorize(
                    state_store=self,
                    workspace_lease=lease,
                    request=request,
                    head=head,
                )
            self._validate_request_against_snapshot(request, snapshot, authorization_head)
            dispatch_binding_by_node[request.node.node_id] = self._derive_dispatch_binding(
                request, snapshot, head
            )
            ordinals = self._authoritative_retry_ordinals(authorization_head, request.node.node_id)
            if ordinals != set(range(len(ordinals))) or request.retry_ordinal != len(ordinals):
                raise BatchStateError("retry ordinal must continue exact attempt lineage")
            if request.interaction_response_sha256 is not None:
                publication = by_digest[request.interaction_response_sha256]
                if (
                    publication.node_id != request.node.node_id
                    or publication.predecessor_terminal_record_sha256
                    != request.interaction_predecessor_terminal_record_sha256
                ):
                    raise BatchStateError("retry request and response publication disagree")
                predecessor_attempt = self._validate_retry_predecessor(
                    head,
                    request.node.node_id,
                    publication.predecessor_terminal_record_sha256,
                    request.retry_ordinal,
                )
                response = self._validate_response_publication(publication)
                self._validate_response_predecessor(response, predecessor_attempt)
                if consumed:
                    predecessor_context_sha256 = _digest(
                        predecessor_attempt.stage_result.to_mapping()["resume_context"]
                    )
                    expected_request_id = batch_interaction_request_id(
                        node_id=request.node.node_id,
                        attempt_id=predecessor_attempt.attempt_id,
                        attempt_authorization_sha256=(predecessor_attempt.authorization_sha256),
                        interaction_context_sha256=predecessor_context_sha256,
                    )
                    if (
                        response["interaction_request_id"] != expected_request_id
                        or response["interaction_context_sha256"] != predecessor_context_sha256
                    ):
                        raise BatchStateError(
                            "accepted response contradicts its queued request authority"
                        )
                    try:
                        queued_result = self.load_interaction_request(expected_request_id)
                    except BatchStateError:
                        if not self._is_pending_cluster_label_review_request(
                            head, expected_request_id
                        ):
                            raise BatchStateError(
                                "accepted response contradicts its queued request authority"
                            ) from None
                    else:
                        if queued_result != predecessor_attempt.stage_result:
                            raise BatchStateError(
                                "accepted response contradicts its queued request authority"
                            )
                validated_responses[publication.response_sha256] = response
        response_request_ids = {
            _batch_request_id(item["interaction_request_id"])
            for item in validated_responses.values()
        }
        if consumed:
            if set(consumed) != response_request_ids:
                raise BatchStateError(
                    "consumed requests must match response publications one-to-one"
                )
            if not set(consumed) <= set(head.pending_interaction_ids):
                raise BatchStateError("accepted response request is not pending")
        elif response_request_ids & set(head.pending_interaction_ids):
            raise BatchStateError(
                "a pending interaction request must be consumed with its response"
            )
        authorizations: list[BatchAttemptAuthorization] = []
        for request in values:
            unsigned = {
                "schema_name": AUTHORIZATION_SCHEMA_NAME,
                "schema_version": AUTHORIZATION_SCHEMA_VERSION,
                "attempt_id": "attempt-" + uuid4().hex,
                "node_id": request.node.node_id,
                "node_material_sha256": self._current_plan_node_material_sha256(
                    snapshot, request.node.node_id
                ),
                "retry_ordinal": request.retry_ordinal,
                "was_invalidated_at_admission": (request.node.node_id in head.invalidated_node_ids),
                "admission_head_sha256": head.sha256,
                "admission_plan_sha256": head.active_plan_sha256,
                "input_artifact_set_sha256": artifact_ref_set_sha256(request.input_artifacts),
                "interaction_response_sha256": request.interaction_response_sha256,
                "interaction_predecessor_terminal_record_sha256": (
                    request.interaction_predecessor_terminal_record_sha256
                ),
                "dispatch_binding": dispatch_binding_by_node[request.node.node_id],
                **({"failed_retry": request.failed_retry}
                   if request.failed_retry is not None else {}),
            }
            try:
                authorizations.append(
                    BatchAttemptAuthorization(
                        **unsigned,
                        authorization_sha256=_digest(
                            {
                                **unsigned,
                                "dispatch_binding": unsigned["dispatch_binding"].to_mapping(),
                                **({"failed_retry": request.failed_retry.to_mapping()}
                                   if request.failed_retry is not None else {}),
                            }
                        ),
                    )
                )
            except BatchArtifactError as exc:
                raise BatchStateError("cannot construct attempt authorization") from exc
        for publication in publications:
            self._assert_lease(lease)
            self._publish_response(publication)
        for authorization, request in zip(authorizations, values, strict=True):
            self._assert_lease(lease)
            self._publish_authorization_request_material(authorization, request)
            self._publish_authorization(authorization)
        pending = dict(head.pending_authorization_sha256_by_node)
        pending.update({item.node_id: item.authorization_sha256 for item in authorizations})
        candidate = head.evolve(
            pending_authorization_sha256_by_node=pending,
            pending_interaction_ids=tuple(
                item for item in head.pending_interaction_ids if item not in set(consumed)
            ),
            invalidated_node_ids=tuple(sorted(set(head.invalidated_node_ids) - set(node_ids))),
        )
        if paused_response_authority is None:
            self._replace_head_cas(
                candidate,
                head.sha256,
                lease,
                allow_interaction_requests=bool(consumed),
            )
        else:
            paused_response_authority._publish_candidate(candidate)
        return tuple(authorizations)

    @_guarded_batch_state_lease_operation
    def authorize_attempt(
        self,
        request: BatchAttemptAuthorizationRequest,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchAttemptAuthorization:
        return self.authorize_attempts((request,), expected_head_sha256, lease)[0]

    def _cluster_label_review_predecessor_authorization(
        self,
        *,
        head: BatchStateHead,
        predecessor: BatchNodeAttempt,
        evidence_ref: BatchArtifactRef,
    ) -> BatchAttemptAuthorization:
        """Recover the immutable first-attempt request authority."""

        authorization = self.load_authorization(predecessor.authorization_sha256)
        if (
            authorization.authorization_sha256 != predecessor.authorization_sha256
            or authorization.node_id != predecessor.node_id
            or authorization.attempt_id != predecessor.attempt_id
            or authorization.retry_ordinal != 0
            or authorization.interaction_response_sha256 is not None
            or authorization.interaction_predecessor_terminal_record_sha256 is not None
            or authorization.admission_head_sha256 not in {*head.ancestor_head_sha256s, head.sha256}
            or authorization.admission_plan_sha256 not in head.plan_history_sha256s
            or authorization.input_artifact_set_sha256 != artifact_ref_set_sha256((evidence_ref,))
        ):
            raise BatchStateError("aggregate cluster review predecessor authorization is stale")
        return authorization

    def _cluster_label_review_target_from_state(
        self,
        *,
        head: BatchStateHead,
        node: Mapping[str, object],
        predecessor: BatchNodeAttempt,
        evidence_ref: BatchArtifactRef,
    ) -> "AggregateClusterReviewTarget":
        from mif_pipeline.orchestration.batch_phenotyping._common import (
            BatchPhenotypingError,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_authority import (
            reopen_cluster_review_evidence_authority,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_target import (
            AggregateClusterReviewTarget,
        )

        clustering_scope = node.get("clustering_scope")
        result = predecessor.stage_result
        try:
            predecessor_authorization = self._cluster_label_review_predecessor_authorization(
                head=head,
                predecessor=predecessor,
                evidence_ref=evidence_ref,
            )
            # This path validates a candidate head.  Re-entering the public
            # authenticator would reload and validate that same head, so issue
            # the exact current internal authority from the already supplied
            # head and typed active plan instead.
            evidence_authority = self._current_internal_artifact_authority(
                evidence_ref,
                head,
                self._load_typed_plan(head.active_plan_sha256),
            )
            evidence = reopen_cluster_review_evidence_authority(evidence_authority)
            node_bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in _sequence(
                    node["analysis_domain_bindings"],
                    "cluster review analysis-domain bindings",
                )
            )
            scope = _mapping(
                clustering_scope,
                "cluster review clustering scope",
            )
        except (
            BatchStateError,
            BatchPhenotypingError,
            BatchArtifactError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise BatchStateError("aggregate cluster review evidence cannot be reopened") from exc
        expected_context = {
            "schema_version": "mif-batch-cluster-label-review-interaction/v1",
            "evidence_sha256": evidence.evidence_sha256,
        }
        if (
            node.get("kind") != "cluster_label_review"
            or tuple(
                str(item)
                for item in _sequence(node["participant_ids"], "cluster review participant IDs")
            )
            != evidence.participant_ids
            or tuple(
                str(item)
                for item in _sequence(node["project_entry_ids"], "cluster review project-entry IDs")
            )
            != evidence.project_entry_ids
            or node_bindings != evidence.analysis_domain_bindings
            or scope.get("mode") != evidence.mode.value
            or scope.get("phenotyping_group_id") != evidence.phenotyping_group_id
            or predecessor.node_id != node.get("node_id")
            or predecessor.stage_result.status is not StageStatus.NEEDS_INTERACTION
            or result.stage != "cluster_label_review"
            or result.interaction_reason is not InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED
            or result.resume_context != expected_context
            or result.checkpoint_id != evidence.evidence_sha256
            or predecessor.execution_context_binding.input_artifact_ref_sha256s
            != (evidence_ref.artifact_ref_sha256,)
            or evidence.active_plan_sha256 not in head.plan_history_sha256s
        ):
            raise BatchStateError(
                "aggregate cluster review request differs from pooled graph authority"
            )
        interaction_context_sha256 = _digest(result.to_mapping()["resume_context"])
        target = AggregateClusterReviewTarget.create(
            node_id=str(node["node_id"]),
            attempt_id=predecessor.attempt_id,
            attempt_authorization_sha256=predecessor.authorization_sha256,
            needs_interaction_terminal_record_sha256=(predecessor.terminal_record_sha256),
            interaction_context_sha256=interaction_context_sha256,
            request_state_head_sha256=(predecessor_authorization.admission_head_sha256),
            evidence_artifact_ref_sha256=evidence_ref.artifact_ref_sha256,
            evidence=evidence,
        )
        return target

    def _cluster_label_review_target_for_node(
        self, head: BatchStateHead, selected_node_id: str
    ) -> "AggregateClusterReviewTarget":
        """Derive one exact aggregate target from one supplied current head."""

        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        node = snapshot.node_by_id.get(selected_node_id)
        attempt_ids = head.attempt_ids_by_node.get(selected_node_id, ())
        terminal_digest = (
            None
            if not attempt_ids
            else head.terminal_attempt_sha256_by_attempt_id.get(attempt_ids[-1])
        )
        if (
            node is None
            or selected_node_id in head.invalidated_node_ids
            or head.node_status_by_id.get(selected_node_id) is not StageStatus.NEEDS_INTERACTION
            or terminal_digest is None
        ):
            raise BatchStateError("aggregate cluster review request is not current and pending")
        predecessor = self.load_attempt(terminal_digest)
        current_input_sha256s = self._current_input_ref_sha256s(snapshot, head, selected_node_id)
        accepted, _attempts = self._current_artifact_material(
            head, self._load_typed_plan(head.active_plan_sha256)
        )
        artifacts = {
            item.artifact_ref_sha256: item
            for item in accepted
        }
        if len(current_input_sha256s) != 1:
            raise BatchStateError("aggregate cluster review requires one current evidence input")
        evidence_ref = artifacts.get(current_input_sha256s[0])
        if evidence_ref is None:
            raise BatchStateError("aggregate cluster review evidence is not currently available")
        target = self._cluster_label_review_target_from_state(
            head=head,
            node=node,
            predecessor=predecessor,
            evidence_ref=evidence_ref,
        )
        return target

    @_guarded_batch_state_operation
    def reopen_cluster_label_review_presentation(
        self,
        node_id: str,
        *,
        expected_head_sha256: str,
    ) -> "tuple[AggregateClusterReviewTarget, BatchClusterReviewEvidence]":
        """Reopen one current aggregate target and its exact retained profile."""

        from mif_pipeline.orchestration.batch_phenotyping._common import (
            BatchPhenotypingError,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_authority import (
            reopen_cluster_review_evidence_authority,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_target import (
            AggregateClusterReviewTarget,
        )

        if type(self) is not BatchStateStore:
            raise BatchStateError("aggregate cluster review requires the exact state owner")
        selected_node_id = _safe_segment(node_id, "cluster review node_id")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("aggregate cluster review request head changed")
        target = self._cluster_label_review_target_for_node(head, selected_node_id)
        plan = self._load_typed_plan(head.active_plan_sha256)
        artifacts, _attempts = self._current_artifact_material(head, plan)
        evidence_ref = next(
            (
                item
                for item in artifacts
                if item.artifact_ref_sha256 == target.evidence_artifact_ref_sha256
            ),
            None,
        )
        if evidence_ref is None:
            raise BatchStateError("aggregate cluster review evidence is not current")
        try:
            authority = self._current_internal_artifact_authority(evidence_ref, head, plan)
            evidence = reopen_cluster_review_evidence_authority(authority)
            projected = AggregateClusterReviewTarget.create(
                node_id=target.node_id,
                attempt_id=target.attempt_id,
                attempt_authorization_sha256=target.attempt_authorization_sha256,
                needs_interaction_terminal_record_sha256=target.needs_interaction_terminal_record_sha256,
                interaction_context_sha256=target.interaction_context_sha256,
                request_state_head_sha256=target.request_state_head_sha256,
                evidence_artifact_ref_sha256=evidence_ref.artifact_ref_sha256,
                evidence=evidence,
            )
        except (BatchPhenotypingError, BatchArtifactError) as exc:
            raise BatchStateError("aggregate cluster review evidence cannot be reopened") from exc
        if projected != target:
            raise BatchStateError("aggregate cluster review evidence differs from target")
        if self.load_head().sha256 != expected:
            raise BatchHeadConflictError(
                "aggregate cluster review request head changed before return"
            )
        return target, evidence

    @_guarded_batch_state_operation
    def reopen_cluster_label_review_target(
        self,
        node_id: str,
        *,
        expected_head_sha256: str,
    ) -> "AggregateClusterReviewTarget":
        """Reopen one current pending aggregate-review request target."""

        if type(self) is not BatchStateStore:
            raise BatchStateError("aggregate cluster review requires the exact state owner")
        selected_node_id = _safe_segment(node_id, "cluster review node_id")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("aggregate cluster review request head changed")
        target = self._cluster_label_review_target_for_node(head, selected_node_id)
        if self.load_head().sha256 != expected:
            raise BatchHeadConflictError(
                "aggregate cluster review request head changed before return"
            )
        return target

    @_guarded_batch_state_lease_operation
    def publish_cluster_label_review_requests(
        self,
        node_ids: tuple[str, ...],
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        """Atomically queue exact state-derived aggregate review targets."""

        self._assert_lease(lease)
        values = tuple(_safe_segment(item, "cluster review node_id") for item in node_ids)
        if not values or len(set(values)) != len(values):
            raise BatchStateError("aggregate cluster review nodes must be non-empty and unique")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("aggregate cluster review request head changed")
        targets = tuple(
            self._cluster_label_review_target_for_node(head, node_id) for node_id in values
        )
        request_ids = tuple(item.interaction_request_id for item in targets)
        if len(set(request_ids)) != len(request_ids) or set(request_ids) & set(
            head.pending_interaction_ids
        ):
            raise BatchStateError("aggregate cluster review request is already pending")
        candidate = head.evolve(
            pending_interaction_ids=tuple(sorted((*head.pending_interaction_ids, *request_ids)))
        )
        return self._replace_head_cas(
            candidate,
            head.sha256,
            lease,
            allow_interaction_requests=True,
        )

    @_guarded_batch_state_lease_operation
    def authorize_cluster_label_review_retry(
        self,
        request: BatchAttemptAuthorizationRequest,
        *,
        target: object,
        canonical_response_bytes: bytes,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        paused_response_authority: object | None = None,
    ) -> BatchAttemptAuthorization:
        """Validate and atomically publish one aggregate-review retry.

        Aggregate review deliberately bypasses the image-scoped interaction
        target validator.  Its target instead binds the complete clustering
        graph, while the existing response publication and head-CAS machinery
        remains the sole persistence authority.
        """

        from mif_pipeline.orchestration.batch_phenotyping.review_authority import (
            _response_from_mapping,
            _validate_response_against_target,
            validate_cluster_review_evidence_authority,
        )
        from mif_pipeline.orchestration.batch_phenotyping._common import (
            BatchPhenotypingError,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_target import (
            AggregateClusterReviewTarget,
        )

        self._assert_lease(lease)
        if type(request) is not BatchAttemptAuthorizationRequest:
            raise BatchStateError("aggregate cluster review requires an exact retry request")
        if type(target) is not AggregateClusterReviewTarget:
            raise BatchStateError("aggregate cluster review requires an exact aggregate target")
        if type(canonical_response_bytes) is not bytes:
            raise BatchStateError("aggregate cluster review response bytes must be exact bytes")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("aggregate cluster review admission head changed")
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        node = snapshot.node_by_id.get(request.node.node_id)
        node_clustering_scope = None if node is None else node.get("clustering_scope")
        predecessor_sha256 = request.interaction_predecessor_terminal_record_sha256
        if (
            node is None
            or node["kind"] != "cluster_label_review"
            or request.retry_ordinal <= 0
            or request.interaction_response_sha256 is None
            or predecessor_sha256 is None
            or target.node_id != request.node.node_id
            or target.active_plan_sha256 not in head.plan_history_sha256s
            or target.needs_interaction_terminal_record_sha256 != predecessor_sha256
            or target.participant_ids != tuple(str(item) for item in node["participant_ids"])
            or target.project_entry_ids != tuple(str(item) for item in node["project_entry_ids"])
            or target.analysis_domain_bindings
            != tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in node["analysis_domain_bindings"]
            )
            or len(request.input_artifacts) != 1
            or request.input_artifacts[0].artifact_ref_sha256 != target.evidence_artifact_ref_sha256
            or not isinstance(node_clustering_scope, Mapping)
            or node_clustering_scope.get("mode") != target.mode.value
            or node_clustering_scope.get("phenotyping_group_id") != target.phenotyping_group_id
        ):
            raise BatchStateError(
                "aggregate cluster review target differs from current graph authority"
            )
        predecessor = self._validate_retry_predecessor(
            head,
            request.node.node_id,
            predecessor_sha256,
            request.retry_ordinal,
        )
        interaction_context_sha256 = _digest(
            predecessor.stage_result.to_mapping()["resume_context"]
        )
        predecessor_authorization = self._cluster_label_review_predecessor_authorization(
            head=head,
            predecessor=predecessor,
            evidence_ref=request.input_artifacts[0],
        )
        if (
            predecessor.attempt_id != target.attempt_id
            or predecessor.authorization_sha256 != target.attempt_authorization_sha256
            or interaction_context_sha256 != target.interaction_context_sha256
            or target.request_state_head_sha256 != predecessor_authorization.admission_head_sha256
            or target.interaction_request_id
            != batch_interaction_request_id(
                node_id=request.node.node_id,
                attempt_id=predecessor.attempt_id,
                attempt_authorization_sha256=predecessor.authorization_sha256,
                interaction_context_sha256=interaction_context_sha256,
            )
            or predecessor.stage_result.interaction_reason
            is not InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED
        ):
            raise BatchStateError(
                "aggregate cluster review target differs from predecessor lineage"
            )
        try:
            evidence_authority = self.authenticate_available_artifact(request.input_artifacts[0])
            validate_cluster_review_evidence_authority(
                evidence_authority,
                target=target,
            )
        except (BatchStateError, BatchPhenotypingError) as exc:
            raise BatchStateError(
                "aggregate cluster review evidence is not current and authentic"
            ) from exc
        try:
            decoded = json.loads(canonical_response_bytes.decode("utf-8"))
            if _canonical_bytes(decoded) != canonical_response_bytes:
                raise BatchStateError("aggregate cluster review response is not canonical")
            validated_response = _response_from_mapping(decoded)
            _validate_response_against_target(validated_response, target)
        except (UnicodeDecodeError, json.JSONDecodeError, BatchStateError) as exc:
            raise BatchStateError("aggregate cluster review response is malformed") from exc
        except (BatchPhenotypingError, TypeError, ValueError) as exc:
            raise BatchStateError(
                "aggregate cluster review response contradicts its target"
            ) from exc
        if validated_response["response_sha256"] != request.interaction_response_sha256:
            raise BatchStateError("aggregate cluster review request and response digest differ")
        publication = BatchInteractionResponsePublication(
            canonical_response_bytes=canonical_response_bytes,
            response_sha256=request.interaction_response_sha256,
            node_id=request.node.node_id,
            predecessor_terminal_record_sha256=predecessor_sha256,
        )
        return self.authorize_attempts(
            (request,),
            head.sha256,
            lease,
            (publication,),
            (target.interaction_request_id,),
            paused_response_authority=paused_response_authority,
        )[0]

    @_guarded_batch_state_lease_operation
    def resolve_pending_authorization(
        self,
        node: BatchNode,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchAttemptAuthorization | None:
        self._assert_lease(lease)
        if not isinstance(node, BatchNode):
            raise BatchStateError("pending authorization resolution requires BatchNode")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("pending authorization head changed")
        digest = head.pending_authorization_sha256_by_node.get(node.node_id)
        if digest is None:
            return None
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        persisted = snapshot.node_by_id.get(node.node_id)
        if persisted is None or dict(persisted) != node.to_mapping():
            raise BatchStateError("pending authorization node is materially stale")
        authorization = self.load_authorization(digest)
        self._validate_persisted_dispatch_binding(authorization, snapshot, head=head, check_current_binding=True)
        if (
            authorization.node_id != node.node_id
            or authorization.admission_plan_sha256 not in head.plan_history_sha256s
            or authorization.admission_head_sha256 not in {*head.ancestor_head_sha256s, head.sha256}
            or node.node_id in head.invalidated_node_ids
        ):
            raise BatchStateError("pending authorization has stale admission authority")
        admission = self._load_plan_snapshot(authorization.admission_plan_sha256)
        admitted_node = admission.node_by_id.get(node.node_id)
        if admitted_node is None or dict(admitted_node) != node.to_mapping():
            raise BatchStateError("pending authorization material no longer matches admission")
        if authorization.interaction_response_sha256 is not None:
            predecessor = authorization.interaction_predecessor_terminal_record_sha256
            if predecessor is None:
                raise BatchStateError("response-bound authorization lacks predecessor")
            self._validate_retry_predecessor(
                head,
                node.node_id,
                predecessor,
                authorization.retry_ordinal,
            )
            self._load_and_validate_response(
                authorization.interaction_response_sha256,
                node_id=node.node_id,
                predecessor=authorization.interaction_predecessor_terminal_record_sha256,
            )
        if authorization.input_artifact_set_sha256 != self._expected_input_set_sha256(
            snapshot, head, node.node_id
        ):
            raise BatchStateError("pending authorization input set is no longer current")
        if authorization.node_material_sha256 != self._current_plan_node_material_sha256(
            snapshot, node.node_id
        ):
            raise BatchStateError("pending authorization execution material is no longer current")
        return authorization

    def _load_and_validate_response(
        self,
        digest: str,
        *,
        node_id: str,
        predecessor: str | None,
        predecessor_attempt: BatchNodeAttempt | None = None,
    ) -> Mapping[str, object]:
        digest = _sha256(digest, "response digest")
        path = self._response_path(digest)
        self._owned_path(path, self.layout.interactions, "interaction response")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise BatchStateError("interaction response bytes are missing") from exc
        publication = BatchInteractionResponsePublication(
            canonical_response_bytes=payload,
            response_sha256=digest,
            node_id=node_id,
            predecessor_terminal_record_sha256=(
                predecessor
                if predecessor is not None
                else (_ for _ in ()).throw(
                    BatchStateError("interaction response lacks predecessor")
                )
            ),
        )
        response = self._validate_response_publication(publication)
        # A head read may supply the terminal record it has already authenticated.
        if predecessor_attempt is None:
            predecessor_attempt = self.load_attempt(publication.predecessor_terminal_record_sha256)
        if (
            type(predecessor_attempt) is not BatchNodeAttempt
            or predecessor_attempt.terminal_record_sha256
            != publication.predecessor_terminal_record_sha256
            or predecessor_attempt.node_id != node_id
            or predecessor_attempt.stage_result.status is not StageStatus.NEEDS_INTERACTION
        ):
            raise BatchStateError("interaction response predecessor is foreign or non-interactive")
        self._validate_response_predecessor(response, predecessor_attempt)
        return response

    @_guarded_batch_state_operation
    def load_validated_interaction_response(
        self, authorization: BatchAttemptAuthorization
    ) -> Mapping[str, object] | None:
        """Read one exact validated response as an immutable worker mapping.

        The returned immutable mapping is suitable for a bounded worker wire;
        this method exposes neither the private response loader nor any ability
        to bypass predecessor and publication validation. An explicit failed
        segmentation or measurement retry may reuse its original setup response
        while retaining that response's original attempt and publication identity.
        The setup projector still checks it against the current inputs and section.
        """

        if not isinstance(authorization, BatchAttemptAuthorization):
            raise BatchStateError("authorized interaction response requires exact authorization")
        digest = authorization.interaction_response_sha256
        expected_setup_action = None
        while digest is None and authorization.failed_retry is not None:
            terminal = self.load_attempt(authorization.failed_retry.terminal_record_sha256)
            setup_action = {
                "segmentation": "configure_segmentation",
                "measurements": "configure_measurements",
            }.get(terminal.stage_result.stage)
            if setup_action is None:
                return None
            previous = self.load_authorization(terminal.authorization_sha256)
            if (
                self.load_authorization(authorization.authorization_sha256) != authorization
                or terminal.stage_result.status is not StageStatus.FAILED
                or terminal.node_id != authorization.node_id
                or previous.node_id != authorization.node_id
                or terminal.attempt_id != previous.attempt_id
                or previous.node_material_sha256 != authorization.node_material_sha256
                or previous.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
                or previous.retry_ordinal + 1 != authorization.retry_ordinal
                or authorization.failed_retry.native_abort_archive_ref is not None
                or (expected_setup_action is not None and setup_action != expected_setup_action)
            ):
                raise BatchStateError("failed-stage setup response lineage changed")
            expected_setup_action = setup_action
            authorization = previous
            digest = authorization.interaction_response_sha256
        if digest is None:
            return None
        predecessor = authorization.interaction_predecessor_terminal_record_sha256
        response = self._load_and_validate_response(
            digest,
            node_id=authorization.node_id,
            predecessor=predecessor,
        )
        if expected_setup_action is not None and response.get("action") != expected_setup_action:
            raise BatchStateError("failed-stage retry does not retain its setup action")

        def freeze(value: object) -> object:
            if isinstance(value, Mapping):
                return MappingProxyType(
                    {str(key): freeze(item) for key, item in sorted(value.items())}
                )
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            return value

        frozen = freeze(response)
        assert isinstance(frozen, Mapping)
        return frozen

    @_guarded_batch_state_operation
    def load_authorized_interaction_response(
        self,
        context: BatchNodeExecutionContext,
    ) -> "AuthorizedTrainingInteractionResponse | None":
        """Issue one opaque training response for an exact current retry context."""

        if type(self) is not BatchStateStore:
            raise BatchStateError("authorized training response requires the exact state owner")
        if type(context) is not BatchNodeExecutionContext:
            raise BatchStateError("authorized training response requires exact execution context")
        self.authenticate_current_execution_context(context)
        authorization = context.authorization
        response_sha256 = authorization.interaction_response_sha256
        predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
        if response_sha256 is None:
            if predecessor_sha256 is not None:
                raise BatchStateError(
                    "training interaction authorization has incomplete retry lineage"
                )
            return None
        if predecessor_sha256 is None or authorization.retry_ordinal <= 0:
            raise BatchStateError("training interaction authorization lacks its predecessor")
        response = self._load_and_validate_response(
            response_sha256,
            node_id=context.node_id,
            predecessor=predecessor_sha256,
        )
        head = self.load_head()
        predecessor = self._validate_retry_predecessor(
            head,
            context.node_id,
            predecessor_sha256,
            authorization.retry_ordinal,
        )
        interaction_context_sha256 = _digest(
            predecessor.stage_result.to_mapping()["resume_context"]
        )
        request_id = batch_interaction_request_id(
            node_id=context.node_id,
            attempt_id=predecessor.attempt_id,
            attempt_authorization_sha256=predecessor.authorization_sha256,
            interaction_context_sha256=interaction_context_sha256,
        )
        if (
            response["interaction_request_id"] != request_id
            or response["interaction_context_sha256"] != interaction_context_sha256
        ):
            raise BatchStateError("training interaction response contradicts predecessor request")
        request = self.load_interaction_request(request_id)
        if (
            request != predecessor.stage_result
            or request.checkpoint_id != predecessor.stage_result.checkpoint_id
            or request.checkpoint_id is None
        ):
            raise BatchStateError("training interaction response checkpoint request differs")
        current_execution = self.authenticate_current_execution_context(context)
        try:
            return _issue_authorized_training_interaction_response(
                current_execution=current_execution,
                accepted_response=response,
                checkpoint_id=request.checkpoint_id,
                expected_context=context,
            )
        except BatchInteractionAuthorityError as exc:
            raise BatchStateError(
                "authorized training interaction response issuance failed"
            ) from exc

    @_guarded_batch_state_operation
    def load_authorized_cluster_label_review_response(
        self,
        context: BatchNodeExecutionContext,
        *,
        target: object,
    ) -> "AuthorizedClusterLabelReviewResponse":
        """Issue one opaque aggregate-review response from current state."""

        from mif_pipeline.orchestration.batch_phenotyping._common import (
            BatchPhenotypingError,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_authority import (
            _issue_authorized_cluster_label_review_response,
        )
        from mif_pipeline.orchestration.batch_phenotyping.review_target import (
            AggregateClusterReviewTarget,
        )

        if type(self) is not BatchStateStore:
            raise BatchStateError(
                "aggregate cluster review response requires the exact state owner"
            )
        if type(context) is not BatchNodeExecutionContext:
            raise BatchStateError(
                "aggregate cluster review response requires exact execution context"
            )
        if type(target) is not AggregateClusterReviewTarget:
            raise BatchStateError("aggregate cluster review response requires exact target")
        current_execution = self.authenticate_current_execution_context(context)
        authorization = context.authorization
        response_sha256 = authorization.interaction_response_sha256
        predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
        if (
            response_sha256 is None
            or predecessor_sha256 is None
            or authorization.retry_ordinal <= 0
            or current_execution.node_kind != "cluster_label_review"
            or current_execution.participant_ids != target.participant_ids
            or current_execution.project_entry_ids != target.project_entry_ids
            or current_execution.analysis_domain_bindings != target.analysis_domain_bindings
            or len(context.inputs) != 1
            or context.inputs[0].artifact_ref_sha256 != target.evidence_artifact_ref_sha256
        ):
            raise BatchStateError("aggregate cluster review retry scope is incomplete or stale")
        response = self._load_and_validate_response(
            response_sha256,
            node_id=context.node_id,
            predecessor=predecessor_sha256,
        )
        head = self.load_head()
        predecessor = self._validate_retry_predecessor(
            head,
            context.node_id,
            predecessor_sha256,
            authorization.retry_ordinal,
        )
        interaction_context_sha256 = _digest(
            predecessor.stage_result.to_mapping()["resume_context"]
        )
        if (
            target.node_id != context.node_id
            or target.active_plan_sha256 not in head.plan_history_sha256s
            or target.attempt_id != predecessor.attempt_id
            or target.attempt_authorization_sha256 != predecessor.authorization_sha256
            or target.needs_interaction_terminal_record_sha256 != predecessor.terminal_record_sha256
            or target.interaction_context_sha256 != interaction_context_sha256
            or target.interaction_request_id
            != batch_interaction_request_id(
                node_id=context.node_id,
                attempt_id=predecessor.attempt_id,
                attempt_authorization_sha256=predecessor.authorization_sha256,
                interaction_context_sha256=interaction_context_sha256,
            )
            or target.request_state_head_sha256 not in {*head.ancestor_head_sha256s, head.sha256}
            or predecessor.stage_result.interaction_reason
            is not InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED
        ):
            raise BatchStateError(
                "aggregate cluster review target is not the current retry predecessor"
            )
        evidence_authority = self.authenticate_available_artifact(context.inputs[0])
        current_execution = self.authenticate_current_execution_context(context)
        try:
            return _issue_authorized_cluster_label_review_response(
                current_execution=current_execution,
                target=target,
                evidence_authority=evidence_authority,
                accepted_response=response,
                expected_context=context,
            )
        except BatchPhenotypingError as exc:
            raise BatchStateError("aggregate cluster review response issuance failed") from exc

    @_guarded_batch_state_operation
    def reopen_authorized_cluster_label_review(
        self,
        context: BatchNodeExecutionContext,
    ) -> tuple[
        "AggregateClusterReviewTarget",
        "AuthorizedClusterLabelReviewResponse",
    ]:
        """Reopen the exact aggregate target and opaque response for a retry."""

        if type(self) is not BatchStateStore:
            raise BatchStateError("aggregate cluster review retry requires the exact state owner")
        current = self.authenticate_current_execution_context(context)
        authorization = context.authorization
        predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
        if (
            current.node_kind != "cluster_label_review"
            or authorization.interaction_response_sha256 is None
            or predecessor_sha256 is None
            or authorization.retry_ordinal <= 0
            or len(context.inputs) != 1
        ):
            raise BatchStateError("aggregate cluster review retry lineage is incomplete")
        head = self.load_head()
        predecessor = self._validate_retry_predecessor(
            head,
            context.node_id,
            predecessor_sha256,
            authorization.retry_ordinal,
        )
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        node = snapshot.node_by_id.get(context.node_id)
        if node is None or context.node_id in head.invalidated_node_ids:
            raise BatchStateError("aggregate cluster review retry node is not current")
        target = self._cluster_label_review_target_from_state(
            head=head,
            node=node,
            predecessor=predecessor,
            evidence_ref=context.inputs[0],
        )
        authority = self.load_authorized_cluster_label_review_response(
            context,
            target=target,
        )
        if self.load_head().sha256 != head.sha256:
            raise BatchHeadConflictError(
                "aggregate cluster review retry head changed before return"
            )
        return target, authority

    @_guarded_batch_state_operation
    def load_current_transfer_review_response(
        self,
        context: BatchNodeExecutionContext,
    ) -> Mapping[str, object]:
        """Resolve one transfer review from the exact current retry authority."""

        if type(self) is not BatchStateStore:
            raise BatchStateError("transfer review resolution requires the exact state owner")
        if type(context) is not BatchNodeExecutionContext:
            raise BatchStateError("transfer review resolution requires exact execution context")
        self.authenticate_current_execution_context(context)
        authorization = context.authorization
        response_sha256 = authorization.interaction_response_sha256
        predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
        if (
            response_sha256 is None
            or predecessor_sha256 is None
            or authorization.retry_ordinal <= 0
        ):
            raise BatchStateError("transfer review resolution lacks exact retry lineage")
        response = self._load_and_validate_response(
            response_sha256,
            node_id=context.node_id,
            predecessor=predecessor_sha256,
        )
        head = self.load_head()
        predecessor = self._validate_retry_predecessor(
            head,
            context.node_id,
            predecessor_sha256,
            authorization.retry_ordinal,
        )
        request = predecessor.stage_result
        resume_context = request.resume_context
        interaction_context_sha256 = _digest(request.to_mapping()["resume_context"])
        request_id = batch_interaction_request_id(
            node_id=context.node_id,
            attempt_id=predecessor.attempt_id,
            attempt_authorization_sha256=predecessor.authorization_sha256,
            interaction_context_sha256=interaction_context_sha256,
        )
        if (
            request.interaction_reason is not InteractionReason.TRANSFER_REVIEW_REQUIRED
            or request.allowed_actions != ("accept_transfers", "reject_transfers")
            or request.checkpoint_id is None
            or request.checkpoint_id != resume_context.get("proposal_sha256")
            or response["interaction_request_id"] != request_id
            or response["interaction_context_sha256"] != interaction_context_sha256
            or response["response_validator_id"] != "roi-interaction-validator"
            or response["response_validator_version"] != "roi-interaction-actions/v2"
            or response["action"] not in {"accept_transfers", "reject_transfers"}
        ):
            raise BatchStateError("transfer review response contradicts its exact request")
        persisted_request = self.load_interaction_request(request_id)
        if persisted_request != request:
            raise BatchStateError("transfer review persisted request differs from predecessor")
        validated = _mapping(response["response"], "transfer review response")
        expected_validated_fields = frozenset(resume_context) | {
            "action",
            "response",
            "reviewed_by",
        }
        nested = _mapping(
            validated.get("response"),
            "transfer review response body",
        )
        if (
            frozenset(validated) != expected_validated_fields
            or frozenset(nested) != {"reviewed_by"}
            or validated.get("action") != response["action"]
            or validated.get("reviewed_by") != nested.get("reviewed_by")
            or any(validated.get(key) != value for key, value in resume_context.items())
        ):
            raise BatchStateError("transfer review validated response changed owner context")
        reviewer = _text(
            nested["reviewed_by"],
            "transfer review reviewer",
        )
        if reviewer != nested["reviewed_by"]:
            raise BatchStateError("transfer review reviewer is not canonical")
        self.authenticate_current_execution_context(context)

        def freeze(value: object) -> object:
            if isinstance(value, Mapping):
                return MappingProxyType(
                    {str(key): freeze(item) for key, item in sorted(value.items())}
                )
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            return value

        return MappingProxyType(
            {
                "action": str(response["action"]),
                "reviewed_by": reviewer,
                "accepted_response_sha256": response_sha256,
                "interaction_context": freeze(resume_context),
            }
        )

    def _resolve_current_transfer_review_response(
        self,
        context: BatchNodeExecutionContext,
        *,
        project_file: str,
        moving_project_entry_id: str,
        fixed_project_entry_id: str,
        tissue_generation_id: str,
    ) -> Mapping[str, object]:
        expected = {
            "project_file": _text(project_file, "transfer review project_file"),
            "moving_project_entry_id": _text(
                moving_project_entry_id,
                "transfer review moving_project_entry_id",
            ),
            "fixed_project_entry_id": _text(
                fixed_project_entry_id,
                "transfer review fixed_project_entry_id",
            ),
        }
        _text(tissue_generation_id, "transfer review tissue_generation_id")
        resolved = self.load_current_transfer_review_response(context)
        interaction_context = _mapping(
            resolved["interaction_context"],
            "resolved transfer review interaction context",
        )
        if any(interaction_context.get(key) != value for key, value in expected.items()):
            raise BatchStateError("resolved transfer review project scope changed")
        self.authenticate_current_execution_context(context)
        return resolved

    @_guarded_batch_state_operation
    def authenticate_current_execution_context(
        self,
        context: BatchNodeExecutionContext,
    ) -> BatchCurrentExecutionAuthority:
        """Reopen one exact current pending context into an issuer-only capability."""

        if type(self) is not BatchStateStore:
            raise BatchStateError("current execution authentication requires the exact state owner")
        if type(context) is not BatchNodeExecutionContext:
            raise BatchStateError("current execution authentication requires exact node context")
        head = self.load_head()
        authorization = context.authorization
        if head.pending_authorization_sha256_by_node.get(context.node_id) != (
            authorization.authorization_sha256
        ):
            raise BatchStateError("current execution context lacks exact pending authorization")
        reopened = self.load_authorization(authorization.authorization_sha256)
        if reopened != authorization:
            raise BatchStateError("current execution authorization bytes changed")
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        node = snapshot.node_by_id.get(context.node_id)
        if node is None or context.node_id in head.invalidated_node_ids:
            raise BatchStateError("current execution node is absent or invalidated")
        self._plan_material_is_compatible(
            head=head,
            authorization=authorization,
        )
        self._validate_persisted_dispatch_binding(authorization, snapshot, head=head, check_current_binding=True)
        current_inputs = self._current_input_ref_sha256s(snapshot, head, context.node_id)
        context_inputs = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        if (
            context.adapter_spec.spec_sha256 != node["adapter_spec_sha256"]
            or context.adapter_spec.node_kind != node["kind"]
            or context.adapter_spec.node_version != node["version"]
            or context.adapter_spec.resource_kind != node["resource"]
            or context.adapter_spec.dispatch_mode.value != node["dispatch_mode"]
            or context_inputs != current_inputs
            or artifact_ref_set_sha256(context.inputs) != authorization.input_artifact_set_sha256
        ):
            raise BatchStateError("current execution context differs from active node or inputs")
        try:
            bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in node["analysis_domain_bindings"]
            )
            configurations = tuple(
                ArtifactConfigurationBinding.from_mapping(item)
                for item in node["configuration_bindings"]
            )
            return _issue_batch_current_execution_authority(
                context=context,
                state_head_sha256=head.sha256,
                active_plan_sha256=head.active_plan_sha256,
                node_kind=str(node["kind"]),
                node_scope=str(node["scope"]),
                validation_domain_id=(
                    None
                    if node["validation_domain_id"] is None
                    else str(node["validation_domain_id"])
                ),
                clustering_scope=(
                    None
                    if node.get("clustering_scope") is None
                    else _mapping(
                        node["clustering_scope"],
                        "persisted clustering scope",
                    )
                ),
                participant_ids=tuple(str(item) for item in node["participant_ids"]),
                project_entry_ids=tuple(str(item) for item in node["project_entry_ids"]),
                analysis_domain_bindings=bindings,
                configuration_bindings=configurations,
                input_artifact_ref_sha256s=current_inputs,
            )
        except (BatchArtifactError, KeyError, TypeError, ValueError) as exc:
            raise BatchStateError("current execution authority issuance failed") from exc

    @staticmethod
    def _validate_response_predecessor(
        response: Mapping[str, object], predecessor: BatchNodeAttempt
    ) -> None:
        if (
            response["prior_attempt_id"] != predecessor.attempt_id
            or response["prior_attempt_authorization_sha256"] != predecessor.authorization_sha256
            or response["prior_terminal_record_sha256"] != predecessor.terminal_record_sha256
        ):
            raise BatchStateError(
                "interaction response does not bind the exact predecessor attempt"
            )

    def _validate_retry_predecessor(
        self,
        head: BatchStateHead,
        node_id: str,
        terminal_digest: str,
        retry_ordinal: int,
        *,
        attempt: BatchNodeAttempt | None = None,
    ) -> BatchNodeAttempt:
        attempt_ids = head.attempt_ids_by_node.get(node_id, ())
        if retry_ordinal <= 0 or retry_ordinal > len(attempt_ids):
            raise BatchStateError("retry predecessor ordinal is not authoritative")
        expected_attempt_id = attempt_ids[retry_ordinal - 1]
        if head.terminal_attempt_sha256_by_attempt_id.get(expected_attempt_id) != terminal_digest:
            raise BatchStateError("retry predecessor is not the exact prior attempt")
        if attempt is None:
            attempt = self.load_attempt(terminal_digest)
        if (
            type(attempt) is not BatchNodeAttempt
            or attempt.terminal_record_sha256 != terminal_digest
            or attempt.attempt_id != expected_attempt_id
            or attempt.node_id != node_id
            or attempt.stage_result.status is not StageStatus.NEEDS_INTERACTION
        ):
            raise BatchStateError("retry predecessor must be needs_interaction for the same node")
        return attempt

    @_guarded_batch_state_operation
    def prepare_node_result(
        self,
        context: BatchNodeExecutionContext,
        stage_result: StageResult,
        execution_result: BatchNodeExecutionResult,
        *,
        execution_exchange_sha256: str | None,
    ) -> BatchPreparedNodeResult:
        return self._prepare_node_result_at_head(
            context,
            stage_result,
            execution_result,
            execution_exchange_sha256=execution_exchange_sha256,
            head=self.load_head(),
        )

    def _project_mutation_predecessor_head(
        self,
        context: BatchNodeExecutionContext,
        *,
        project_lease: object,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchStateHead:
        """Authenticate the exact batch predecessor held by one live mutation."""

        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLease

        self._assert_lease(lease)
        head, _head_bytes = self._read_canonical_state_head()
        provider = self._project_mutation_provider
        if (
            not isinstance(context, BatchNodeExecutionContext)
            or not isinstance(project_lease, ProjectMutationLease)
            or getattr(project_lease, "_provider", None) is not provider
            or head.sha256 != _sha256(expected_head_sha256, "expected_head_sha256")
        ):
            raise BatchStateError("project mutation predecessor authority is foreign")
        validate = getattr(provider, "_validate_live_lease_authority", None)
        if not callable(validate):
            raise BatchStateError("project mutation provider cannot authenticate its live lease")
        try:
            active = validate(project_lease, require_project=True)
        except Exception as exc:
            raise BatchStateError("project mutation live lease authority is invalid") from exc
        designation = project_lease.designation
        predecessor = designation.authority_head
        if (
            active.project_identity_sha256 != head.project_identity_sha256
            or designation.identity.project_identity_sha256 != head.project_identity_sha256
            or str(designation.project_path) != head.project_path
            or predecessor.project_identity_sha256 != head.project_identity_sha256
            or predecessor.authority_head_sha256 != head.project_authority_head_sha256
            or predecessor.materialization_state != head.project_materialization_state
            or predecessor.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
            or predecessor.latest_mutation_attestation_sha256
            != head.project_mutation_attestation_sha256
            or head.pending_authorization_sha256_by_node.get(context.node_id)
            != context.authorization.authorization_sha256
        ):
            raise BatchStateError("project mutation predecessor disagrees with its live lease")
        return head

    @_guarded_batch_state_lease_operation
    def prepare_project_mutation_node_result(
        self,
        context: BatchNodeExecutionContext,
        stage_result: StageResult,
        execution_result: BatchNodeExecutionResult,
        *,
        execution_exchange_sha256: str | None,
        project_lease: object,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchPreparedNodeResult:
        """Prepare a result against one authenticated in-flight predecessor."""

        head = self._project_mutation_predecessor_head(
            context,
            project_lease=project_lease,
            expected_head_sha256=expected_head_sha256,
            lease=lease,
        )
        return self._prepare_node_result_at_head(
            context,
            stage_result,
            execution_result,
            execution_exchange_sha256=execution_exchange_sha256,
            head=head,
        )

    def _validate_native_fit_completion(self, context, execution_result, head) -> None:
        """A completed classifier must bind the fit that it retires from pending state.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""
        selected = head.pending_training_fit_set_sha256_by_node.get(context.node_id)
        if selected is None:
            return
        checkpoint = self._load_training_fit_set(selected)
        outputs = tuple(
            item
            for item in execution_result.outputs
            if item.artifact_kind == "classifier_record_set"
        )
        plans = tuple(item for item in context.inputs if item.artifact_kind == "training_plan_set")
        if len(outputs) != 1 or len(plans) != 1 or checkpoint.node_id != context.node_id:
            raise BatchStateError("completed training lacks the selected native fit material")
        plan_identity, record_identity = plans[0].owning_identity, outputs[0].owning_identity
        try:
            classifier_ids = tuple(plan_identity["classifier_ids"])
            training_plan_ids = tuple(plan_identity["training_plan_ids"])
            plan_by_classifier = dict(zip(classifier_ids, training_plan_ids, strict=True))
            if (
                not classifier_ids
                or len(plan_by_classifier) != len(classifier_ids)
                or len(checkpoint.entries) != len(classifier_ids)
                or {item.classifier_id for item in checkpoint.entries} != set(classifier_ids)
            ):
                raise ValueError("native fit coverage differs from the accepted plans")
            projections = []
            for entry in checkpoint.entries:
                projections.append(
                    _classifier_record_native_fit_projection(
                        training_plan_id=plan_by_classifier[entry.classifier_id],
                        classifier_id=entry.classifier_id,
                        training_material_evidence_sha256=entry.training_material_evidence_sha256,
                        fit_plan_sha256=entry.fit_plan_sha256,
                        fit_execution_sha256=entry.fit_execution_sha256,
                        fit_material_sha256=entry.runtime_fit_material_sha256,
                        classifier_version=entry.classifier_version,
                        project_relative_path=entry.project_relative_path,
                        native_classifier_sha256=entry.native_classifier_sha256,
                        native_classifier_byte_count=entry.native_classifier_byte_count,
                        project_identity_sha256=entry.project_identity_sha256,
                    )
                )
            expected_material = _classifier_record_native_fit_inventory_sha256(projections)
            expected_children = _digest(
                sorted(
                    item.artifact_ref_sha256
                    for item in context.inputs
                    if item.artifact_kind != "training_plan_set"
                )
            )
            if (
                record_identity["material_evidence_sha256"] != expected_material
                or record_identity["source_training_plan_set_sha256"]
                != checkpoint.training_plan_set_sha256
                or plan_identity["training_plan_set_sha256"] != checkpoint.training_plan_set_sha256
                or record_identity["evidence_child_inventory_sha256"] != expected_children
                or record_identity["classifier_kind"] != plan_identity["classifier_kind"]
                or any(
                    item.classifier_kind != record_identity["classifier_kind"]
                    for item in checkpoint.entries
                )
                or record_identity["validation_domain_id"] != checkpoint.validation_domain_id
                or len(record_identity["output_inventory"]) != len(classifier_ids)
            ):
                raise ValueError("classifier output differs from the selected fit")
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchStateError("completed classifier has stale native fit material") from exc

    def _prepare_node_result_at_head(
        self,
        context: BatchNodeExecutionContext,
        stage_result: StageResult,
        execution_result: BatchNodeExecutionResult,
        *,
        execution_exchange_sha256: str | None,
        head: BatchStateHead,
    ) -> BatchPreparedNodeResult:
        if not isinstance(context, BatchNodeExecutionContext):
            raise BatchStateError("prepared result requires exact execution context")
        if not isinstance(stage_result, StageResult):
            raise BatchStateError("prepared result requires exact StageResult")
        _validate_compact_stage_result(stage_result)
        if not isinstance(execution_result, BatchNodeExecutionResult):
            raise BatchStateError("prepared result requires exact execution output")
        authorization = context.authorization
        if (
            head.pending_authorization_sha256_by_node.get(context.node_id)
            != authorization.authorization_sha256
            or execution_result.authorization_sha256 != authorization.authorization_sha256
        ):
            raise BatchStateError("prepared result lacks exact pending execution authority")
        reopened = self.load_authorization(authorization.authorization_sha256)
        if reopened != authorization:
            raise BatchStateError("prepared result authorization bytes changed")
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        node = snapshot.node_by_id.get(context.node_id)
        if node is None or context.adapter_spec.spec_sha256 != node["adapter_spec_sha256"]:
            raise BatchStateError("prepared result adapter is foreign to active node")
        self._plan_is_compatible(head=head, authorization=authorization)
        if stage_result.stage != node["kind"]:
            raise BatchStateError("prepared StageResult names a foreign node stage")
        try:
            retained = BatchManifest.from_json_bytes(
                self.retained_manifest(snapshot.normalized_manifest_sha256).original_manifest_bytes
            )
        except (OSError, BatchManifestError) as exc:
            raise BatchStateError(
                "cannot validate prepared result against retained manifest"
            ) from exc
        slide_by_sample = {item.sample_id: item.slide_id for item in retained.samples}
        participant_slides = {
            slide_by_sample[str(sample_id)] for sample_id in node["participant_ids"]
        }
        if len(participant_slides) == 1 and stage_result.slide_id not in participant_slides:
            raise BatchStateError("prepared StageResult names a foreign slide")
        if stage_result.status is StageStatus.COMPLETED:
            self._validate_native_fit_completion(context, execution_result, head)
        return BatchPreparedNodeResult._issue(
            stage_result=stage_result,
            execution_result=execution_result,
            execution_exchange_sha256=execution_exchange_sha256,
            execution_context_binding=(_BatchExecutionContextBinding.from_context(context)),
            issuer=self._prepared_result_capability,
        )

    @_guarded_batch_state_lease_operation
    def stage_project_terminal_attempt(
        self,
        context: BatchNodeExecutionContext,
        prepared: BatchPreparedNodeResult,
        *,
        project_lease: object,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> str:
        """Publish exact immutable terminal bytes without advancing state."""

        self._assert_lease(lease)
        registration = (
            isinstance(context, BatchNodeExecutionContext)
            and context.adapter_spec.node_kind == "registration"
            and getattr(project_lease, "operation", None) == "registration_project_write"
        )
        if (
            not isinstance(context, BatchNodeExecutionContext)
            or not isinstance(prepared, BatchPreparedNodeResult)
            or prepared._issuer is not self._prepared_result_capability
            or prepared.execution_context_sha256 != _execution_context_sha256(context)
            or (prepared.stage_result.status is not StageStatus.COMPLETED and not registration)
            or (context.adapter_spec.node_kind == "registration" and not registration)
            or prepared.stage_result.stage != context.adapter_spec.node_kind
            or prepared.execution_result.authorization_sha256
            != context.authorization.authorization_sha256
            or self.load_authorization(context.authorization.authorization_sha256)
            != context.authorization
        ):
            raise BatchStateError("project terminal staging authority is invalid")
        head = self._project_mutation_predecessor_head(
            context,
            project_lease=project_lease,
            expected_head_sha256=expected_head_sha256,
            lease=lease,
        )
        if head.pending_authorization_sha256_by_node.get(context.node_id) != (
            context.authorization.authorization_sha256
        ):
            raise BatchStateError("project terminal staging lost pending authority")
        if registration and prepared.stage_result.status is not StageStatus.COMPLETED:
            if prepared.execution_result.outputs:
                raise BatchStateError("registration non-completion cannot publish artifacts")
            outputs = ()
        else:
            try:
                outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
                    context.adapter_spec,
                    prepared.execution_result.outputs,
                    authorization=context.authorization,
                )
            except BatchArtifactError as exc:
                if not registration:
                    raise
                raise BatchStateError(
                    "project terminal outputs violate the authorized contract"
                ) from exc
            if len(outputs) != 1:
                raise BatchStateError(
                    "completed registration needs one result"
                    if registration
                    else "project upsert must emit exactly one primary artifact"
                )
        attempt = BatchNodeAttempt.create(
            authorization=context.authorization,
            stage_result=prepared.stage_result,
            output_artifacts=outputs,
            execution_exchange_sha256=prepared.execution_exchange_sha256,
            execution_context_binding=prepared.execution_context_binding,
        )
        self._publish_attempt(attempt)
        return attempt.terminal_record_sha256

    @staticmethod
    def _prepared_project_result_binding(
        prepared: BatchPreparedNodeResult,
    ) -> str:
        return _digest(
            {
                "stage_result": prepared.stage_result.to_mapping(),
                "outputs": [_artifact_bundle(item) for item in prepared.execution_result.outputs],
                "execution_exchange_sha256": prepared.execution_exchange_sha256,
                "execution_context_sha256": prepared.execution_context_sha256,
            }
        )

    @_guarded_batch_state_lease_operation
    def prepare_project_material_successor(
        self,
        update: BatchMaterialAuthorityUpdate,
        *,
        context: BatchNodeExecutionContext,
        prepared_result: BatchPreparedNodeResult,
        project_lease: object,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchProjectMaterialSuccessor:
        """Validate one project result without publishing batch state."""
        self._assert_lease(lease)
        if (
            not isinstance(update, BatchMaterialAuthorityUpdate)
            or not isinstance(context, BatchNodeExecutionContext)
            or not isinstance(prepared_result, BatchPreparedNodeResult)
            or prepared_result._issuer is not self._prepared_result_capability
            or not getattr(project_lease, "is_live", False)
        ):
            raise BatchStateError("project material preparation lacks live authority")
        head = self._project_mutation_predecessor_head(
            context,
            project_lease=project_lease,
            expected_head_sha256=expected_head_sha256,
            lease=lease,
        )
        if (
            head.pending_authorization_sha256_by_node.get(context.node_id)
            != context.authorization.authorization_sha256
            or prepared_result.execution_result.authorization_sha256
            != context.authorization.authorization_sha256
            or prepared_result.execution_context_sha256 != _execution_context_sha256(context)
            or prepared_result.stage_result.status is not StageStatus.COMPLETED
        ):
            raise BatchStateError("project result is not the exact pending completion")
        scope = head.node_scope_by_id[context.node_id]
        sample_ids = tuple(scope["sample_ids"])
        if len(sample_ids) != 1:
            raise BatchStateError("project upsert must own exactly one sample")
        sample_id = sample_ids[0]
        project_entry_id = update.sample_project_entry_id_by_sample.get(sample_id)
        projection = update.sample_project_entry_projection_sha256_by_sample.get(sample_id)
        project_head = getattr(project_lease, "current_authority_head", None)
        designation = getattr(project_lease, "designation", None)
        if (
            update.project_materialization_state != "materialized"
            or project_entry_id is None
            or projection is None
            or update.project_identity_sha256
            != getattr(getattr(designation, "identity", None), "project_identity_sha256", None)
            or update.project_authority_head_sha256
            != getattr(project_head, "authority_head_sha256", None)
            or update.project_qpproj_snapshot_sha256
            != getattr(project_head, "qpproj_snapshot_sha256", None)
            or update.project_mutation_attestation_sha256
            != getattr(project_head, "latest_mutation_attestation_sha256", None)
            or getattr(project_lease, "mutation_authorization_sha256", None)
            != context.authorization.authorization_sha256
        ):
            raise BatchStateError("project material disagrees with live project lease")
        unchanged_maps = (
            update.source_sha256_by_sample == head.source_sha256_by_sample,
            update.resolved_config_sha256_by_sample == head.resolved_config_sha256_by_sample,
            update.accepted_domain_revision_sha256_by_id
            == head.accepted_domain_revision_sha256_by_id,
            update.domain_set_sha256_by_sample == head.domain_set_sha256_by_sample,
            update.checkpoint_descriptor_sha256_by_node
            == head.checkpoint_descriptor_sha256_by_node,
            update.external_artifact_ref_sha256_by_key
            == {
                key: key
                for key in self._load_plan_snapshot(head.active_plan_sha256).external_input_sha256s
            },
        )
        if not all(unchanged_maps):
            raise BatchStateError("project upsert cannot change unrelated material authority")
        return BatchProjectMaterialSuccessor._issue(
            expected_state_head_sha256=head.sha256,
            execution_context_sha256=prepared_result.execution_context_sha256,
            prepared_result_binding_sha256=self._prepared_project_result_binding(prepared_result),
            acquisition_record_sha256=project_lease.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(
                project_lease.acquisition_session_challenge_sha256
            ),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=project_lease.mutation_request_sha256,
            mutation_authorization_sha256=project_lease.mutation_authorization_sha256,
            artifact_inventory_sha256=project_lease.artifact_inventory_sha256,
            project_fencing_token_sha256=project_lease.fencing_token_sha256,
            project_identity_sha256=update.project_identity_sha256,
            project_authority_head_sha256=update.project_authority_head_sha256,
            project_materialization_state="materialized",
            project_qpproj_snapshot_sha256=update.project_qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=(update.project_mutation_attestation_sha256),
            project_catalogue_sha256=update.project_catalogue_sha256,
            sample_id=sample_id,
            project_entry_id=project_entry_id,
            project_entry_projection_sha256=projection,
            _issuer=self._project_durability_capability,
        )

    @_guarded_batch_state_lease_operation
    def prepare_project_stage_material_successor(
        self,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
        prepared_results: tuple[BatchPreparedNodeResult, ...],
        project_lease: object,
        expected_head_sha256: str,
        disposition: Literal["observed_mutation", "unchanged_project"],
        project_catalogue_sha256: str,
        project_stage_evidence_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchProjectStageMaterialSuccessor:
        """Issue the exact grouped successor after project authority is known."""

        return self._prepare_project_stage_material_successor(
            contexts=contexts,
            prepared_results=prepared_results,
            project_lease=project_lease,
            expected_head_sha256=expected_head_sha256,
            disposition=disposition,
            project_catalogue_sha256=project_catalogue_sha256,
            project_stage_evidence_sha256=project_stage_evidence_sha256,
            expected_operation="project_stage",
            lease=lease,
        )

    @_guarded_batch_state_lease_operation
    def prepare_registration_material_successor(
        self,
        context: BatchNodeExecutionContext,
        prepared_result: BatchPreparedNodeResult,
        *,
        project_lease: object,
        session: object,
        completion: object,
        resolution: object,
        terminal_record_sha256: str,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchProjectStageMaterialSuccessor:
        """Bind one staged registration terminal to provider-resolved material."""

        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationArtifactResolutionEvidence,
            ProjectMutationLease,
            ProjectMutationRecoveryLease,
        )
        from mif_pipeline.qupath_runtime.registration_session import (
            ExistingRegistrationProjectSession,
        )
        from mif_pipeline.qupath_runtime.runner import (
            QuPathRegistrationProcessCompletion,
        )

        self._assert_lease(lease)
        provider = self._project_mutation_provider
        if (
            not isinstance(context, BatchNodeExecutionContext)
            or context.adapter_spec.node_kind != "registration"
            or not isinstance(prepared_result, BatchPreparedNodeResult)
            or prepared_result._issuer is not self._prepared_result_capability
            or type(project_lease) not in (ProjectMutationLease, ProjectMutationRecoveryLease)
            or project_lease._provider is not provider
            or not project_lease.is_live
            or project_lease.operation != "registration_project_write"
            or type(session) is not ExistingRegistrationProjectSession
            or type(completion) is not QuPathRegistrationProcessCompletion
            or type(resolution) is not ProjectMutationArtifactResolutionEvidence
            or resolution.resolution_evidence_sha256
            != project_lease.artifact_resolution_evidence_sha256
            or resolution.acquisition_record_sha256 != project_lease.acquisition_record_sha256
            or resolution.artifact_inventory_sha256 != project_lease.artifact_inventory_sha256
            or resolution.project_identity_sha256
            != project_lease.designation.identity.project_identity_sha256
        ):
            raise BatchStateError("registration material lacks exact live authority")
        head = self._project_mutation_predecessor_head(
            context,
            project_lease=project_lease,
            expected_head_sha256=expected_head_sha256,
            lease=lease,
        )
        if (
            prepared_result.execution_context_sha256 != _execution_context_sha256(context)
            or prepared_result.execution_result.authorization_sha256
            != context.authorization.authorization_sha256
            or prepared_result.stage_result.stage != "registration"
            or self.load_authorization(context.authorization.authorization_sha256)
            != context.authorization
            or head.pending_authorization_sha256_by_node.get(context.node_id)
            != context.authorization.authorization_sha256
        ):
            raise BatchStateError("registration material lost pending terminal authority")
        if prepared_result.stage_result.status is StageStatus.COMPLETED:
            try:
                outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
                    context.adapter_spec,
                    prepared_result.execution_result.outputs,
                    authorization=context.authorization,
                )
            except BatchArtifactError as exc:
                raise BatchStateError("registration terminal output is invalid") from exc
            if len(outputs) != 1:
                raise BatchStateError("completed registration needs one result")
        elif (
            prepared_result.stage_result.status
            in {
                StageStatus.NEEDS_INTERACTION,
                StageStatus.FAILED,
            }
            and not prepared_result.execution_result.outputs
        ):
            outputs = ()
        else:
            raise BatchStateError("registration terminal status or outputs are invalid")
        expected_attempt = BatchNodeAttempt.create(
            authorization=context.authorization,
            stage_result=prepared_result.stage_result,
            output_artifacts=outputs,
            execution_exchange_sha256=prepared_result.execution_exchange_sha256,
            execution_context_binding=prepared_result.execution_context_binding,
        )
        if (
            _sha256(terminal_record_sha256, "registration terminal digest")
            != expected_attempt.terminal_record_sha256
            or self.load_attempt(terminal_record_sha256) != expected_attempt
        ):
            raise BatchStateError("registration material has a foreign staged terminal")
        material_sha256 = provider.authenticate_registration_terminal_resolution(
            session,
            completion,
            terminal_record_sha256=terminal_record_sha256,
            resolution=resolution,
        )
        return self._prepare_project_stage_material_successor(
            contexts=(context,),
            prepared_results=(prepared_result,),
            project_lease=project_lease,
            expected_head_sha256=expected_head_sha256,
            disposition="unchanged_project",
            project_catalogue_sha256=head.project_catalogue_sha256,
            project_stage_evidence_sha256=material_sha256,
            expected_operation="registration_project_write",
            lease=lease,
        )

    def _prepare_project_stage_material_successor(
        self,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
        prepared_results: tuple[BatchPreparedNodeResult, ...],
        project_lease: object,
        expected_head_sha256: str,
        disposition: Literal["observed_mutation", "unchanged_project"],
        project_catalogue_sha256: str,
        project_stage_evidence_sha256: str,
        expected_operation: Literal["project_stage", "registration_project_write"],
        lease: BatchWorkspaceLease,
    ) -> BatchProjectStageMaterialSuccessor:
        """Validate common predecessor facts without broadening public issuance."""

        self._assert_lease(lease)
        values = tuple(contexts)
        prepared = tuple(prepared_results)
        if (
            not values
            or len(values) != len(prepared)
            or not all(isinstance(item, BatchNodeExecutionContext) for item in values)
            or not all(
                isinstance(item, BatchPreparedNodeResult)
                and item._issuer is self._prepared_result_capability
                for item in prepared
            )
            or not getattr(project_lease, "is_live", False)
            or disposition not in {"observed_mutation", "unchanged_project"}
            or (
                expected_operation == "project_stage"
                and any(item.adapter_spec.node_kind == "registration" for item in values)
            )
            or (
                expected_operation == "registration_project_write"
                and (
                    len(values) != 1
                    or values[0].adapter_spec.node_kind != "registration"
                    or disposition != "unchanged_project"
                )
            )
        ):
            raise BatchStateError("project-stage material lacks exact live authority")
        head, _head_bytes = self._read_canonical_state_head()
        expected_head = _sha256(expected_head_sha256, "expected_head_sha256")
        catalogue_sha256 = _sha256(project_catalogue_sha256, "project_catalogue_sha256")
        evidence_sha256 = _sha256(project_stage_evidence_sha256, "project_stage_evidence_sha256")
        if head.sha256 != expected_head:
            raise BatchHeadConflictError("project-stage material predecessor changed")
        context_digests = tuple(_execution_context_sha256(item) for item in values)
        prepared_digests = tuple(self._prepared_project_result_binding(item) for item in prepared)
        if (
            len(set(context_digests)) != len(context_digests)
            or any(
                result.execution_context_sha256 != context_digest
                or result.execution_result.authorization_sha256
                != context.authorization.authorization_sha256
                or head.pending_authorization_sha256_by_node.get(context.node_id)
                != context.authorization.authorization_sha256
                or self.load_authorization(context.authorization.authorization_sha256)
                != context.authorization
                for context, result, context_digest in zip(
                    values, prepared, context_digests, strict=True
                )
            )
            or head.project_catalogue_sha256 != catalogue_sha256
        ):
            raise BatchStateError("project-stage material differs from pending batch authority")
        designation = getattr(project_lease, "designation", None)
        project_head = getattr(project_lease, "current_authority_head", None)
        project_identity = getattr(
            getattr(designation, "identity", None),
            "project_identity_sha256",
            None,
        )
        attestation_sha256 = getattr(project_head, "latest_mutation_attestation_sha256", None)
        if (
            project_identity != head.project_identity_sha256
            or getattr(project_lease, "operation", None) != expected_operation
            or getattr(project_lease, "mutation_request_sha256", None) is None
            or getattr(project_lease, "mutation_authorization_sha256", None) is None
        ):
            raise BatchStateError("project-stage lease differs from batch authority")
        if disposition == "observed_mutation":
            attestation = project_lease.current_mutation_attestation()
            if (
                attestation is None
                or attestation.mutation_kind != "project_stage"
                or attestation.mutation_attestation_sha256 != attestation_sha256
                or attestation.predecessor_authority_head_sha256
                != head.project_authority_head_sha256
                or attestation.workspace_catalogue_sha256 != catalogue_sha256
                or attestation.operation_evidence_sha256 != evidence_sha256
            ):
                raise BatchStateError(
                    "project-stage attestation differs from its batch predecessor"
                )
        elif (
            getattr(project_head, "authority_head_sha256", None)
            != head.project_authority_head_sha256
            or getattr(project_head, "qpproj_snapshot_sha256", None)
            != head.project_qpproj_snapshot_sha256
            or attestation_sha256 != head.project_mutation_attestation_sha256
        ):
            raise BatchStateError("unchanged project-stage material changed project authority")
        return BatchProjectStageMaterialSuccessor._issue(
            expected_state_head_sha256=head.sha256,
            execution_context_sha256s=context_digests,
            prepared_result_binding_sha256s=prepared_digests,
            disposition=disposition,
            acquisition_record_sha256=project_lease.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(
                project_lease.acquisition_session_challenge_sha256
            ),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=project_lease.mutation_request_sha256,
            mutation_authorization_sha256=(project_lease.mutation_authorization_sha256),
            artifact_inventory_sha256=project_lease.artifact_inventory_sha256,
            project_fencing_token_sha256=project_lease.fencing_token_sha256,
            project_identity_sha256=project_identity,
            project_authority_head_sha256=project_head.authority_head_sha256,
            project_qpproj_snapshot_sha256=project_head.qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=attestation_sha256,
            project_catalogue_sha256=catalogue_sha256,
            project_stage_evidence_sha256=evidence_sha256,
            _issuer=self._project_durability_capability,
            _project_lease=project_lease,
        )

    @_guarded_batch_state_lease_operation
    def terminal_committer(
        self,
        contexts: tuple[BatchNodeExecutionContext, ...],
        lease: BatchWorkspaceLease,
    ) -> BatchTerminalCommitter:
        self._assert_lease(lease)
        values = tuple(contexts)
        if not values or not all(isinstance(item, BatchNodeExecutionContext) for item in values):
            raise BatchStateError("terminal committer requires exact execution contexts")
        digests = tuple(item.authorization.authorization_sha256 for item in values)
        if len(set(digests)) != len(digests):
            raise BatchStateError("terminal committer contexts must be unique")
        head = self.load_head()
        for context in values:
            if head.pending_authorization_sha256_by_node.get(context.node_id) != (
                context.authorization.authorization_sha256
            ):
                raise BatchStateError("execution context lacks exact pending authorization")
            reopened = self.load_authorization(context.authorization.authorization_sha256)
            if reopened != context.authorization:
                raise BatchStateError("execution context authorization bytes changed")
        return BatchTerminalCommitter(
            store=self,
            contexts=values,
            lease=lease,
            _capability=self._terminal_committer_capability,
        )

    @_guarded_batch_state_lease_operation
    def native_fit_checkpoint_writer(
        self,
        context: BatchNodeExecutionContext,
        lease: BatchWorkspaceLease,
    ) -> BatchNativeFitCheckpointWriter:
        """Issue a reusable fit-checkpoint writer for one current aggregate."""

        from .batch_training.stage_adapters import TRAINING_PARENT_NODE_KINDS

        self._assert_lease(lease)
        if (
            type(context) is not BatchNodeExecutionContext
            or context.adapter_spec.node_kind not in TRAINING_PARENT_NODE_KINDS
        ):
            raise BatchStateError("native fit checkpoint writer requires one aggregate context")
        self.authenticate_current_execution_context(context)
        return BatchNativeFitCheckpointWriter(
            store=self,
            context=context,
            lease=lease,
            _capability=self._native_fit_checkpoint_writer_capability,
        )

    @_guarded_batch_state_lease_operation
    def validate_native_fit_checkpoint_writer(
        self,
        writer: BatchNativeFitCheckpointWriter,
        *,
        context: BatchNodeExecutionContext,
        lease: BatchWorkspaceLease,
    ) -> BatchNativeFitCheckpointWriter:
        """Authenticate a checkpoint writer against its exact aggregate owners."""

        self._assert_lease(lease)
        if (
            type(writer) is not BatchNativeFitCheckpointWriter
            or writer._store is not self
            or writer._context is not context
            or writer._lease is not lease
        ):
            raise BatchStateError("native fit checkpoint writer ownership is foreign")
        self.authenticate_current_execution_context(context)
        return writer

    @_guarded_batch_state_lease_operation
    def validate_terminal_committer(
        self,
        committer: BatchTerminalCommitter,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
        lease: BatchWorkspaceLease,
    ) -> BatchTerminalCommitter:
        """Authenticate one still-open committer against its exact owners."""

        self._assert_lease(lease)
        values = tuple(contexts)
        if (
            type(committer) is not BatchTerminalCommitter
            or committer._store is not self
            or committer._lease is not lease
            or len(committer._contexts) != len(values)
            or any(
                actual is not expected
                for actual, expected in zip(committer._contexts, values, strict=True)
            )
        ):
            raise BatchStateError("terminal committer ownership is foreign")
        with committer._condition:
            if committer._state != "open":
                raise BatchStateError("terminal committer is not open")
        return committer

    @property
    def _training_fit_set_root(self) -> Path:
        root = self.layout.state / "training-fit-sets"
        _reject_symlink_path(root, self.layout.state, "training fit-set root")
        try:
            root.mkdir(exist_ok=True, mode=0o700)
        except OSError as exc:
            raise BatchStateError("training fit-set root is unavailable") from exc
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise BatchStateError("training fit-set root is unsafe")
        return root

    def _retained_training_fit_set_digests(self, head: BatchStateHead) -> tuple[str, ...]:
        """List the bounded retained checkpoints, including completed fits.

        Pending pointers identify work still in progress. Completed native fits
        remain part of the accepted project history after those pointers clear,
        so saved-state reconstruction and capsule export use this same inventory.
        """
        digests = set(head.pending_training_fit_set_sha256_by_node.values())
        paths = tuple(self._training_fit_set_root.iterdir())
        if len(paths) > 4096:
            raise BatchStateError("retained fit checkpoint inventory is unbounded")
        for path in paths:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix != ".json"
                or not re.fullmatch(r"[0-9a-f]{64}", path.stem)
            ):
                raise BatchStateError("retained fit checkpoint inventory is unsafe")
            digests.add(path.stem)
        return tuple(sorted(digests))

    def _publish_training_fit_set(self, record: object) -> None:
        from .batch_training_checkpoint import BatchNativeFitCheckpointSet

        if type(record) is not BatchNativeFitCheckpointSet:
            raise BatchStateError("training fit-set publication requires its exact record")
        root = self._training_fit_set_root
        self._publish_immutable(
            root / f"{record.checkpoint_set_sha256}.json",
            _canonical_bytes(record.to_mapping()),
            root,
        )

    def _load_training_fit_set(self, digest: str) -> object:
        from .batch_training_checkpoint import BatchNativeFitCheckpointSet

        checkpoint_sha256 = _sha256(digest, "training fit-set digest")
        root = self._training_fit_set_root
        path = root / f"{checkpoint_sha256}.json"
        self._owned_path(path, root, "training fit-set")
        try:
            payload, mapping = read_canonical_record(
                path,
                max_bytes=_PROJECT_STABLE_AUTHORITY_MAX_BYTES,
                label="training fit-set",
            )
        except DurableRecordError as exc:
            raise BatchStateError("training fit-set bytes are unavailable") from exc
        record = BatchNativeFitCheckpointSet.from_mapping(mapping)
        if (
            record.checkpoint_set_sha256 != checkpoint_sha256
            or _canonical_bytes(record.to_mapping()) != payload
        ):
            raise BatchStateError("training fit-set path or bytes differ")
        return record

    def _validate_native_fit_predecessor_parent(
        self,
        *,
        current_parent: object,
        context: BatchNodeExecutionContext,
        head: BatchStateHead,
    ) -> None:
        """Validate the sealed pre-fit parent against the current batch head.

        A durable native fit has already advanced the live project authority at
        this boundary.  The parent must therefore be validated as the exact
        sealed predecessor, while the caller separately proves the project
        predecessor/successor mutation lineage before the batch-head CAS.
        """

        from .batch_training.parent_execution import (
            validate_current_training_parent_execution,
        )

        validate_current_training_parent_execution(
            current_parent,
            expected_context=context,
            state_store=None,
        )
        if (
            current_parent.state_head_sha256 != head.sha256
            or current_parent.active_plan_sha256 != head.active_plan_sha256
            or current_parent.node_id != context.node_id
            or current_parent.authorization_sha256 != context.authorization.authorization_sha256
            or head.pending_authorization_sha256_by_node.get(context.node_id)
            != context.authorization.authorization_sha256
        ):
            raise BatchStateError("native fit predecessor parent differs from current batch head")

    @_guarded_batch_state_lease_operation
    def prepare_native_fit_checkpoint(
        self,
        *,
        context: BatchNodeExecutionContext,
        current_parent: object,
        registry_authority: object,
        project_commit: object,
        lease: BatchWorkspaceLease,
    ) -> BatchNativeFitCheckpointSuccessor:
        """Prepare one immutable native-fit progress checkpoint before its CAS."""

        from mif_pipeline.qupath_runtime.native_classifier_fit import (
            QuPathNativeClassifierFitCommit,
        )
        from .batch_training.engines import (
            NativeFitRegistryCommitAuthority,
            TrainingFitPlanProjection,
            TrainingEngineProviderRegistry,
        )
        from .batch_training.parent_execution import CurrentTrainingParentExecution
        from .batch_training_checkpoint import (
            BatchNativeFitCheckpointEntry,
            BatchNativeFitCheckpointSet,
        )

        self._assert_lease(lease)
        if (
            type(context) is not BatchNodeExecutionContext
            or type(current_parent) is not CurrentTrainingParentExecution
            or type(registry_authority) is not NativeFitRegistryCommitAuthority
            or type(project_commit) is not QuPathNativeClassifierFitCommit
            or type(registry_authority._registry) is not TrainingEngineProviderRegistry
        ):
            raise BatchStateError("native fit checkpoint authorities are invalid")
        # The durable native fit has intentionally advanced project authority
        # one step ahead of batch state. Read the exact self-authenticating
        # predecessor bytes; the sealed parent and project lineage below then
        # authenticate the specialized project-head CAS.
        head, _head_bytes = self._read_canonical_state_head()
        try:
            self._validate_native_fit_predecessor_parent(
                current_parent=current_parent,
                context=context,
                head=head,
            )
            registry = registry_authority._registry
            registry.validate_native_fit_commit_authority(
                registry_authority,
                project_commit=project_commit,
            )
        except Exception as exc:
            raise BatchStateError(
                "native fit checkpoint parent or registry authority is stale"
            ) from exc
        material = project_commit.material_record
        project_lease = project_commit._lease
        project_head = project_lease.current_authority_head
        if (
            head.project_materialization_state != "materialized"
            or head.project_qpproj_snapshot_sha256 is None
            or head.project_catalogue_sha256 is None
            or material.project_identity_sha256 != head.project_identity_sha256
            or material.predecessor_project_authority_head_sha256
            != head.project_authority_head_sha256
            or material.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
            or project_commit._provider is not self._project_mutation_provider
            or not project_commit._lease.is_live
            or project_head.authority_head_sha256
            != project_commit.successor_project_authority_head_sha256
            or project_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
            or project_head.latest_mutation_attestation_sha256
            != project_commit.mutation_attestation_sha256
            or material.acquisition_record_sha256 != project_lease.acquisition_record_sha256
            or material.acquisition_session_challenge_sha256
            != project_lease.acquisition_session_challenge_sha256
            or material.mutation_request_sha256 != project_lease.mutation_request_sha256
            or material.mutation_authorization_sha256 != project_lease.mutation_authorization_sha256
            or material.artifact_inventory_sha256 != project_lease.artifact_inventory_sha256
            or material.fencing_token_sha256 != project_lease.fencing_token_sha256
            or project_lease.artifact_resolution_evidence_sha256 is None
        ):
            raise BatchStateError("native fit checkpoint project lineage differs")
        plans = tuple(
            plan
            for plan in current_parent.training_plan_set.plan_set.plans
            if plan.request.classifier_id == material.classifier_id
        )
        if len(plans) != 1:
            raise BatchStateError("native fit checkpoint classifier is not planned")
        plan = plans[0]
        expected_runtime_kind = (
            "supervised_phenotype"
            if plan.request.classifier_kind.value == "supervised_phenotype"
            else "pixel"
        )
        if (
            TrainingFitPlanProjection.create(plan).fit_plan_sha256 != material.fit_plan_sha256
            or registry_authority.fit_plan_sha256 != material.fit_plan_sha256
        ):
            raise BatchStateError("native fit checkpoint plan differs")
        if material.classifier_kind != expected_runtime_kind:
            raise BatchStateError("native fit checkpoint classifier family differs")
        entry = BatchNativeFitCheckpointEntry.create(
            classifier_id=material.classifier_id,
            classifier_kind=plan.request.classifier_kind.value,
            fit_plan_sha256=material.fit_plan_sha256,
            fit_execution_sha256=material.fit_execution_sha256,
            training_material_evidence_sha256=(material.training_material_evidence_sha256),
            engine_id=registry_authority.engine_id,
            engine_version=registry_authority.engine_version,
            engine_capabilities_sha256=(registry_authority.engine_capabilities_sha256),
            native_classifier_type=material.native_classifier_type,
            classifier_version=material.classifier_version,
            project_relative_path=material.project_relative_path,
            native_classifier_sha256=material.native_classifier_sha256,
            native_classifier_byte_count=material.native_classifier_byte_count,
            project_identity_sha256=material.project_identity_sha256,
            predecessor_project_authority_head_sha256=(
                material.predecessor_project_authority_head_sha256
            ),
            successor_project_authority_head_sha256=(
                project_commit.successor_project_authority_head_sha256
            ),
            qpproj_snapshot_sha256=material.qpproj_snapshot_sha256,
            mutation_attestation_sha256=project_commit.mutation_attestation_sha256,
            runtime_fit_material_sha256=material.fit_material_sha256,
        )
        prior_digest = head.pending_training_fit_set_sha256_by_node.get(context.node_id)
        if prior_digest is None:
            entries = (entry,)
            origin_attempt_id = context.authorization.attempt_id
            origin_authorization = context.authorization.authorization_sha256
            admission_plan = context.authorization.admission_plan_sha256
            node_material = context.authorization.node_material_sha256
            input_set = context.authorization.input_artifact_set_sha256
        else:
            prior = self._load_training_fit_set(prior_digest)
            reopened_prior = self._validate_native_fit_checkpoint_against_predecessor(
                head=head,
                context=context,
                current_parent=current_parent,
                engine_registry=registry,
            )
            if reopened_prior is None or reopened_prior[0] != prior:
                raise BatchStateError("existing native fit checkpoint is stale")
            if any(
                item.classifier_id == entry.classifier_id
                or item.fit_execution_sha256 == entry.fit_execution_sha256
                for item in prior.entries
            ):
                raise BatchStateError("native fit checkpoint duplicate is rejected")
            entries = (*prior.entries, entry)
            origin_attempt_id = prior.originating_attempt_id
            origin_authorization = prior.originating_attempt_authorization_sha256
            admission_plan = prior.admission_plan_sha256
            node_material = prior.node_material_sha256
            input_set = prior.input_artifact_set_sha256
        checkpoint_set = BatchNativeFitCheckpointSet.create(
            batch_id=head.batch_id,
            node_id=context.node_id,
            originating_attempt_id=origin_attempt_id,
            originating_attempt_authorization_sha256=origin_authorization,
            admission_plan_sha256=admission_plan,
            node_material_sha256=node_material,
            input_artifact_set_sha256=input_set,
            validation_domain_id=current_parent.validation_domain_id,
            training_plan_set_sha256=(
                current_parent.training_plan_set.plan_set.training_plan_set_sha256
            ),
            entries=entries,
            completed_lease_archive_ref_by_fit_material_sha256=(
                {}
                if prior_digest is None
                else prior.completed_lease_archive_ref_by_fit_material_sha256
            ),
        )
        return BatchNativeFitCheckpointSuccessor._issue(
            expected_state_head_sha256=head.sha256,
            execution_context_sha256=current_parent.execution_context_sha256,
            checkpoint_set=checkpoint_set,
            project_identity_sha256=material.project_identity_sha256,
            project_authority_head_sha256=(project_commit.successor_project_authority_head_sha256),
            project_qpproj_snapshot_sha256=material.qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=(project_commit.mutation_attestation_sha256),
            project_catalogue_sha256=head.project_catalogue_sha256,
            acquisition_record_sha256=material.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(material.acquisition_session_challenge_sha256),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=material.mutation_request_sha256,
            mutation_authorization_sha256=(material.mutation_authorization_sha256),
            artifact_inventory_sha256=material.artifact_inventory_sha256,
            project_fencing_token_sha256=material.fencing_token_sha256,
            _project_commit=project_commit,
            _registry_authority=registry_authority,
            _registry=registry,
            _context=context,
            _current_parent=current_parent,
            _issuer=self._native_fit_checkpoint_capability,
        )

    def _reopen_native_fit_checkpoint_entries(
        self,
        record: object,
        *,
        current_parent: object,
        engine_registry: object,
    ) -> tuple[object, ...]:
        """Reopen runtime-owned fit material selected by one exact state record."""

        from .batch_training.engines import TrainingEngineProviderRegistry
        from .batch_training.parent_execution import CurrentTrainingParentExecution
        from .batch_training_checkpoint import BatchNativeFitCheckpointSet

        if type(record) is not BatchNativeFitCheckpointSet or type(current_parent) is not CurrentTrainingParentExecution or type(engine_registry) is not TrainingEngineProviderRegistry or (self._project_mutation_provider is None):
            raise BatchStateError("native fit checkpoint reopen authority is invalid")
        provider = self._project_mutation_provider
        reopened: list[object] = []
        for entry in record.entries:
            try:
                engine_registry.authenticate_checkpointed_native_fit(
                    entry,
                    current_parent=current_parent,
                )
                material = provider.load_native_classifier_fit_material(
                    entry.project_identity_sha256,
                    entry.runtime_fit_material_sha256,
                    workspace_storage_scope_sha256=(self.workspace_storage_scope_sha256),
                )
                authority = provider.reopen_native_classifier_fit(
                    material,
                    workspace_storage_scope_sha256=(self.workspace_storage_scope_sha256),
                    mutation_attestation_sha256=(entry.mutation_attestation_sha256),
                    successor_project_authority_head_sha256=(
                        entry.successor_project_authority_head_sha256
                    ),
                )
            except Exception as exc:
                raise BatchStateError(
                    "native fit checkpoint runtime authority cannot be reopened"
                ) from exc
            observed = (
                material.fit_plan_sha256,
                material.fit_execution_sha256,
                material.training_material_evidence_sha256,
                material.classifier_id,
                material.classifier_kind,
                material.native_classifier_type,
                material.classifier_version,
                material.project_relative_path,
                material.native_classifier_sha256,
                material.native_classifier_byte_count,
                material.project_identity_sha256,
                material.predecessor_project_authority_head_sha256,
                material.qpproj_snapshot_sha256,
                material.fit_material_sha256,
            )
            expected = (
                entry.fit_plan_sha256,
                entry.fit_execution_sha256,
                entry.training_material_evidence_sha256,
                entry.classifier_id,
                (
                    "supervised_phenotype"
                    if entry.classifier_kind == "supervised_phenotype"
                    else "pixel"
                ),
                entry.native_classifier_type,
                entry.classifier_version,
                entry.project_relative_path,
                entry.native_classifier_sha256,
                entry.native_classifier_byte_count,
                entry.project_identity_sha256,
                entry.predecessor_project_authority_head_sha256,
                entry.qpproj_snapshot_sha256,
                entry.runtime_fit_material_sha256,
            )
            if observed != expected:
                raise BatchStateError("native fit checkpoint differs from runtime-owned material")
            reopened.append(authority)
        return tuple(reopened)

    def _validate_current_native_fit_checkpoint(
        self,
        *,
        head: BatchStateHead,
        context: BatchNodeExecutionContext,
        current_parent: object,
        engine_registry: object,
    ) -> tuple[object, tuple[object, ...]] | None:
        from .batch_training.parent_execution import (
            CurrentTrainingParentExecution,
            validate_current_training_parent_execution,
        )

        if type(current_parent) is not CurrentTrainingParentExecution:
            raise BatchStateError("native fit checkpoint parent is invalid")
        try:
            validate_current_training_parent_execution(
                current_parent,
                expected_context=context,
                state_store=self,
            )
        except Exception as exc:
            raise BatchStateError("native fit checkpoint parent is no longer current") from exc
        return self._validate_native_fit_checkpoint_record(
            head=head,
            context=context,
            current_parent=current_parent,
            engine_registry=engine_registry,
        )

    def _validate_native_fit_checkpoint_against_predecessor(
        self,
        *,
        head: BatchStateHead,
        context: BatchNodeExecutionContext,
        current_parent: object,
        engine_registry: object,
    ) -> tuple[object, tuple[object, ...]] | None:
        """Reopen the prior fit set during the exact post-project transition."""

        from .batch_training.parent_execution import CurrentTrainingParentExecution

        if type(current_parent) is not CurrentTrainingParentExecution:
            raise BatchStateError("native fit checkpoint parent is invalid")
        self._validate_native_fit_predecessor_parent(
            current_parent=current_parent,
            context=context,
            head=head,
        )
        return self._validate_native_fit_checkpoint_record(
            head=head,
            context=context,
            current_parent=current_parent,
            engine_registry=engine_registry,
        )

    def _validate_native_fit_checkpoint_record(
        self,
        *,
        head: BatchStateHead,
        context: BatchNodeExecutionContext,
        current_parent: object,
        engine_registry: object,
    ) -> tuple[object, tuple[object, ...]] | None:
        """Validate and reopen the fit set selected by a supplied state head."""

        from .batch_training_checkpoint import BatchNativeFitCheckpointSet

        selected = head.pending_training_fit_set_sha256_by_node.get(context.node_id)
        if selected is None:
            return None
        record = self._load_training_fit_set(selected)
        if type(record) is not BatchNativeFitCheckpointSet:
            raise BatchStateError("native fit checkpoint record is invalid")
        try:
            originating = self.load_authorization(record.originating_attempt_authorization_sha256)
        except Exception as exc:
            raise BatchStateError("native fit checkpoint origin cannot be reopened") from exc
        current_authorization = context.authorization
        if (
            record.checkpoint_set_sha256 != selected
            or record.batch_id != head.batch_id
            or record.node_id != context.node_id
            or record.validation_domain_id != current_parent.validation_domain_id
            or record.training_plan_set_sha256
            != current_parent.training_plan_set.plan_set.training_plan_set_sha256
            or record.admission_plan_sha256 not in head.plan_history_sha256s
            or record.input_artifact_set_sha256 != current_authorization.input_artifact_set_sha256
            or originating.node_id != context.node_id
            or originating.attempt_id != record.originating_attempt_id
            or originating.authorization_sha256 != record.originating_attempt_authorization_sha256
            or originating.admission_plan_sha256 != record.admission_plan_sha256
            or originating.node_material_sha256 != record.node_material_sha256
            or originating.input_artifact_set_sha256 != record.input_artifact_set_sha256
            or any(
                entry.project_identity_sha256 != head.project_identity_sha256
                for entry in record.entries
            )
        ):
            raise BatchStateError("native fit checkpoint differs from its current state lineage")
        origin_is_pending = (
            head.pending_authorization_sha256_by_node.get(context.node_id)
            == originating.authorization_sha256
        )
        origin_is_terminal = record.originating_attempt_id in set(
            head.attempt_ids_by_node.get(context.node_id, ())
        )
        if not (origin_is_pending or origin_is_terminal):
            raise BatchStateError("native fit checkpoint origin is outside exact retry lineage")
        runtime = self._reopen_native_fit_checkpoint_entries(
            record,
            current_parent=current_parent,
            engine_registry=engine_registry,
        )
        return record, runtime

    @_guarded_batch_state_operation
    def reopen_current_native_fit_set(
        self,
        context: BatchNodeExecutionContext,
        *,
        current_parent: object,
        engine_registry: object,
    ) -> CurrentNativeFitSetAuthority | None:
        """Reopen only the exact fit set selected by the current state head."""

        if type(context) is not BatchNodeExecutionContext:
            raise BatchStateError("native fit checkpoint requires exact context")
        head = self.load_head()
        reopened = self._validate_current_native_fit_checkpoint(
            head=head,
            context=context,
            current_parent=current_parent,
            engine_registry=engine_registry,
        )
        if reopened is None:
            return None
        record, runtime = reopened
        return CurrentNativeFitSetAuthority._issue(
            checkpoint_set=record,
            runtime_authorities=runtime,
            state_head_sha256=head.sha256,
            active_plan_sha256=head.active_plan_sha256,
            _current_execution=self.authenticate_current_execution_context(context),
            _issuer=self._native_fit_checkpoint_capability,
        )

    @staticmethod
    def _native_fit_lease_authority(
        successor: BatchNativeFitCheckpointSuccessor,
    ) -> dict[str, object]:
        return {
            "acquisition_record_sha256": successor.acquisition_record_sha256,
            "acquisition_session_challenge_sha256": (
                successor.acquisition_session_challenge_sha256
            ),
            "artifact_resolution_evidence_sha256": (successor.artifact_resolution_evidence_sha256),
            "mutation_request_sha256": successor.mutation_request_sha256,
            "mutation_authorization_sha256": (successor.mutation_authorization_sha256),
            "artifact_inventory_sha256": successor.artifact_inventory_sha256,
            "fencing_token_sha256": successor.project_fencing_token_sha256,
            "project_identity_sha256": successor.project_identity_sha256,
            "project_authority_head_sha256": (successor.project_authority_head_sha256),
            "project_qpproj_snapshot_sha256": (successor.project_qpproj_snapshot_sha256),
            "project_mutation_attestation_sha256": (successor.project_mutation_attestation_sha256),
        }

    def _commit_native_fit_checkpoint(
        self,
        *,
        context: BatchNodeExecutionContext,
        successor: BatchNativeFitCheckpointSuccessor,
        lease: BatchWorkspaceLease,
    ) -> tuple[
        CurrentNativeFitSetAuthority,
        BatchProjectMutationDurabilityReceipt,
    ]:
        """Publish and select one fit-set successor without terminalizing."""

        from mif_pipeline.qupath_runtime.native_classifier_fit import (
            QuPathNativeClassifierFitCommit,
        )
        from .batch_training_checkpoint import BatchNativeFitCheckpointSet

        self._assert_lease(lease)
        if (
            type(context) is not BatchNodeExecutionContext
            or type(successor) is not BatchNativeFitCheckpointSuccessor
            or successor._issuer is not self._native_fit_checkpoint_capability
            or successor._context is not context
            or type(successor.checkpoint_set) is not BatchNativeFitCheckpointSet
            or type(successor._project_commit) is not QuPathNativeClassifierFitCommit
        ):
            raise BatchStateError("native fit checkpoint successor is foreign")
        # Cross-authority validation is intentionally stale until this exact
        # checkpoint CAS installs the already-durable project successor.
        head, _head_bytes = self._read_canonical_state_head()
        project_commit = successor._project_commit
        project_lease = project_commit._lease
        try:
            self._validate_native_fit_predecessor_parent(
                current_parent=successor._current_parent,
                context=context,
                head=head,
            )
            successor._registry.validate_native_fit_commit_authority(
                successor._registry_authority,
                project_commit=project_commit,
            )
        except Exception as exc:
            raise BatchStateError("native fit checkpoint successor authority is stale") from exc
        prior_digest = head.pending_training_fit_set_sha256_by_node.get(context.node_id)
        prior_record = None if prior_digest is None else self._load_training_fit_set(prior_digest)
        canonical_successor = successor.checkpoint_set
        successor_entries = canonical_successor.entries
        retained_entries = () if prior_record is None else prior_record.entries
        added_entries = tuple(item for item in successor_entries if item not in retained_entries)
        material = project_commit.material_record
        if (
            canonical_successor.completed_lease_archive_ref_by_fit_material_sha256
            != (
                {}
                if prior_record is None
                else prior_record.completed_lease_archive_ref_by_fit_material_sha256
            )
            or head.sha256 != successor.expected_state_head_sha256
            or successor.execution_context_sha256
            != successor._current_parent.execution_context_sha256
            or canonical_successor.batch_id != head.batch_id
            or canonical_successor.node_id != context.node_id
            or canonical_successor.validation_domain_id
            != successor._current_parent.validation_domain_id
            or canonical_successor.training_plan_set_sha256
            != successor._current_parent.training_plan_set.plan_set.training_plan_set_sha256
            or canonical_successor.input_artifact_set_sha256
            != context.authorization.input_artifact_set_sha256
            or head.pending_authorization_sha256_by_node.get(context.node_id)
            != context.authorization.authorization_sha256
            or successor.project_identity_sha256 != head.project_identity_sha256
            or successor.project_qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
            or successor.project_catalogue_sha256 != head.project_catalogue_sha256
            or successor.project_authority_head_sha256
            != project_commit.successor_project_authority_head_sha256
            or successor.project_mutation_attestation_sha256
            != project_commit.mutation_attestation_sha256
            or project_commit._provider is not self._project_mutation_provider
            or not project_lease.is_live
            or material.predecessor_project_authority_head_sha256
            != head.project_authority_head_sha256
            or (prior_digest is not None and not set(retained_entries) < set(successor_entries))
            or len(successor_entries) != len(retained_entries) + 1
            or len(added_entries) != 1
        ):
            raise BatchStateError("native fit checkpoint successor differs")
        added = added_entries[0]
        if (
            added.fit_plan_sha256,
            added.fit_execution_sha256,
            added.training_material_evidence_sha256,
            added.classifier_id,
            (
                "supervised_phenotype"
                if added.classifier_kind == "supervised_phenotype"
                else "pixel"
            ),
            added.native_classifier_type,
            added.classifier_version,
            added.project_relative_path,
            added.native_classifier_sha256,
            added.native_classifier_byte_count,
            added.project_identity_sha256,
            added.predecessor_project_authority_head_sha256,
            added.qpproj_snapshot_sha256,
            added.runtime_fit_material_sha256,
        ) != (
            material.fit_plan_sha256,
            material.fit_execution_sha256,
            material.training_material_evidence_sha256,
            material.classifier_id,
            material.classifier_kind,
            material.native_classifier_type,
            material.classifier_version,
            material.project_relative_path,
            material.native_classifier_sha256,
            material.native_classifier_byte_count,
            material.project_identity_sha256,
            material.predecessor_project_authority_head_sha256,
            material.qpproj_snapshot_sha256,
            material.fit_material_sha256,
        ) or (
            added.successor_project_authority_head_sha256
            != project_commit.successor_project_authority_head_sha256
            or added.mutation_attestation_sha256 != project_commit.mutation_attestation_sha256
        ):
            raise BatchStateError("native fit checkpoint entry differs from runtime material")
        retain = getattr(
            self._project_mutation_provider,
            "_retain_native_fit_batch_commit_authority",
            None,
        )
        if not callable(retain):
            raise BatchStateError("native fit project authority cannot be retained")
        try:
            with retain(
                project_lease,
                **self._native_fit_lease_authority(successor),
            ) as retained_project_lease:
                if retained_project_lease is not project_lease:
                    raise BatchStateError("native fit retained a foreign project lease")
                self._publish_training_fit_set(successor.checkpoint_set)
                fit_sets = dict(head.pending_training_fit_set_sha256_by_node)
                fit_sets[context.node_id] = successor.checkpoint_set.checkpoint_set_sha256
                candidate = head.evolve(
                    project_authority_head_sha256=(successor.project_authority_head_sha256),
                    project_qpproj_snapshot_sha256=(successor.project_qpproj_snapshot_sha256),
                    project_mutation_attestation_sha256=(
                        successor.project_mutation_attestation_sha256
                    ),
                    project_catalogue_sha256=successor.project_catalogue_sha256,
                    pending_training_fit_set_sha256_by_node=fit_sets,
                )
                committed = self._replace_project_head_cas(
                    candidate,
                    head.sha256,
                    lease,
                    allow_native_fit_checkpoint=True,
                )
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError(
                "native fit project authority changed before batch commit"
            ) from exc
        runtime = self._reopen_native_fit_checkpoint_entries(
            successor.checkpoint_set,
            current_parent=successor._current_parent,
            engine_registry=successor._registry,
        )
        current = CurrentNativeFitSetAuthority._issue(
            checkpoint_set=successor.checkpoint_set,
            runtime_authorities=runtime,
            state_head_sha256=committed.sha256,
            active_plan_sha256=committed.active_plan_sha256,
            _current_execution=self.authenticate_current_execution_context(context),
            _issuer=self._native_fit_checkpoint_capability,
        )
        receipt = BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="classifier_fit",
            workspace_lease_binding_sha256=(self._workspace_lease_binding_sha256(lease)),
            acquisition_record_sha256=successor.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(successor.acquisition_session_challenge_sha256),
            artifact_resolution_evidence_sha256=(successor.artifact_resolution_evidence_sha256),
            mutation_request_sha256=successor.mutation_request_sha256,
            mutation_authorization_sha256=(successor.mutation_authorization_sha256),
            artifact_inventory_sha256=successor.artifact_inventory_sha256,
            project_identity_sha256=successor.project_identity_sha256,
            project_fencing_token_sha256=(successor.project_fencing_token_sha256),
            project_authority_head_sha256=(successor.project_authority_head_sha256),
            mutation_attestation_sha256=(successor.project_mutation_attestation_sha256),
            terminal_attempt_record_sha256s=(),
            committed_batch_state_head_sha256=committed.sha256,
        )
        return current, receipt

    @_guarded_batch_state_lease_operation
    def record_selected_native_fit_completion(
        self,
        *,
        context: BatchNodeExecutionContext,
        current_parent: object,
        current_fit_set: CurrentNativeFitSetAuthority,
        lease: BatchWorkspaceLease,
    ) -> CurrentNativeFitSetAuthority:
        """Retain only an owner-proven completion of the unchanged selected fit."""
        from .batch_training.parent_execution import validate_current_training_parent_execution
        from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseRecord

        self._assert_lease(lease)
        if (
            type(current_fit_set) is not CurrentNativeFitSetAuthority
            or current_fit_set._issuer is not self._native_fit_checkpoint_capability
        ):
            raise BatchStateError("native fit completion requires current state authority")
        validate_current_training_parent_execution(
            current_parent, expected_context=context, state_store=self
        )
        head = self.load_head()
        current_execution = self.authenticate_current_execution_context(context)
        record = current_fit_set.checkpoint_set
        if (
            head.pending_training_fit_set_sha256_by_node.get(context.node_id)
            != record.checkpoint_set_sha256
            or current_fit_set.state_head_sha256 != head.sha256
            or current_fit_set.active_plan_sha256 != head.active_plan_sha256
            or current_fit_set._current_execution != current_execution
            or self._load_training_fit_set(record.checkpoint_set_sha256) != record
        ):
            raise BatchStateError("native fit completion authority is stale")
        selected = _selected_native_fit_runtime_for_current_attestation(
            current_fit_set,
            current_mutation_attestation_sha256=head.project_mutation_attestation_sha256,
        )
        if selected is None:
            raise BatchStateError("native fit completion lacks its current scientific attestation")
        digest = selected.material_record.fit_material_sha256
        refs = dict(record.completed_lease_archive_ref_by_fit_material_sha256)
        try:
            archive_bytes, _active_bytes = (
                self._project_mutation_provider.read_native_fit_completion_records(
                    selected,
                    completed_archive_ref=refs.get(digest),
                )
            )
            archived = ProjectMutationLeaseRecord.from_mapping(json.loads(archive_bytes))
        except Exception as exc:
            raise BatchStateError("native fit completion proof is invalid") from exc
        if digest in refs:
            if refs[digest] != archived.lease_archive_ref:
                raise BatchStateError("native fit completion cannot replace retained proof")
            return current_fit_set
        successor = record.with_completed_lease_archive_reference(
            digest, archived.lease_archive_ref
        )
        self._publish_training_fit_set(successor)
        pending = dict(head.pending_training_fit_set_sha256_by_node)
        pending[context.node_id] = successor.checkpoint_set_sha256
        committed = self._replace_head_cas(
            head.evolve(pending_training_fit_set_sha256_by_node=pending),
            head.sha256,
            lease,
            allow_native_fit_checkpoint=True,
        )
        return CurrentNativeFitSetAuthority._issue(
            checkpoint_set=successor,
            runtime_authorities=current_fit_set.runtime_authorities,
            state_head_sha256=committed.sha256,
            active_plan_sha256=committed.active_plan_sha256,
            _current_execution=self.authenticate_current_execution_context(context),
            _issuer=self._native_fit_checkpoint_capability,
        )

    @_guarded_batch_state_lease_operation
    def complete_selected_native_fit_durability(
        self,
        *,
        context: BatchNodeExecutionContext,
        current_parent: object,
        current_fit_set: CurrentNativeFitSetAuthority,
        lease: BatchWorkspaceLease,
    ) -> NativeFitDurabilityCleanupOutcome:
        """Idempotently archive the active fit selected by current batch state."""

        from mif_pipeline.qupath_runtime.native_classifier_fit import (
            QuPathNativeClassifierFitAuthority,
        )
        from .batch_training.parent_execution import (
            CurrentTrainingParentExecution,
            validate_current_training_parent_execution,
        )

        self._assert_lease(lease)
        if (
            type(context) is not BatchNodeExecutionContext
            or type(current_parent) is not CurrentTrainingParentExecution
            or type(current_fit_set) is not CurrentNativeFitSetAuthority
            or current_fit_set._issuer is not self._native_fit_checkpoint_capability
            or self._project_mutation_provider is None
        ):
            raise BatchStateError("native fit durability recovery authority is foreign")
        try:
            validate_current_training_parent_execution(
                current_parent,
                expected_context=context,
                state_store=self,
            )
        except Exception as exc:
            raise BatchStateError("native fit durability recovery parent is stale") from exc
        head = self.load_head()
        selected = head.pending_training_fit_set_sha256_by_node.get(context.node_id)
        fresh_execution = self.authenticate_current_execution_context(context)
        if (
            selected != current_fit_set.checkpoint_set.checkpoint_set_sha256
            or current_fit_set.state_head_sha256 != head.sha256
            or current_fit_set.active_plan_sha256 != head.active_plan_sha256
            or current_fit_set._current_execution != fresh_execution
        ):
            raise BatchStateError(
                "native fit durability recovery is not state-selected current material"
            )
        try:
            catalogue = self.project_catalogue_store(lease)
            if not current_fit_set.runtime_authorities:
                raise BatchStateError(
                    "native fit durability recovery lacks selected runtime authority"
                )
            # Checkpoint entries are canonically sorted by classifier ID, not
            # execution time.  The state-selected current project attestation
            # identifies the only fit lease that may still need archival; a
            # different attestation proves a later authenticated descendant.
            runtime_fit = _selected_native_fit_runtime_for_current_attestation(
                current_fit_set,
                current_mutation_attestation_sha256=(head.project_mutation_attestation_sha256),
            )
            if runtime_fit is None:
                return NativeFitDurabilityCleanupOutcome(
                    disposition="already_completed",
                    receipt=None,
                )
            if type(runtime_fit) is not QuPathNativeClassifierFitAuthority:
                raise BatchStateError("native fit durability recovery authority is foreign")
            material = runtime_fit.material_record
            entry_matches = tuple(
                entry
                for entry in current_fit_set.checkpoint_set.entries
                if entry.runtime_fit_material_sha256 == material.fit_material_sha256
            )
            if len(entry_matches) != 1:
                raise BatchStateError("native fit durability recovery lacks its selected entry")
            entry = entry_matches[0]
            if (
                entry.runtime_fit_material_sha256 != material.fit_material_sha256
                or entry.successor_project_authority_head_sha256
                != runtime_fit.successor_project_authority_head_sha256
                or entry.mutation_attestation_sha256 != runtime_fit.mutation_attestation_sha256
                or material.project_identity_sha256 != head.project_identity_sha256
            ):
                raise BatchStateError(
                    "native fit durability recovery lacks its latest selected entry"
                )
            request_record, request = catalogue._reopen_annotation_classifier_request(
                material.mutation_request_sha256,
                workspace_lease=lease,
                state_store_issuer=self._project_durability_capability,
            )
            inventory = self.reopen_project_mutation_artifact_inventory(
                material.artifact_inventory_sha256,
                lease=lease,
            )
            acquisition = self.reopen_project_mutation_acquisition_record(
                material.acquisition_record_sha256,
                lease=lease,
            )
            if (
                request_record.authorizing_record_sha256 != material.mutation_authorization_sha256
                or acquisition.mutation_request_sha256 != material.mutation_request_sha256
                or acquisition.mutation_authorization_sha256
                != material.mutation_authorization_sha256
                or acquisition.artifact_inventory_sha256 != material.artifact_inventory_sha256
                or inventory.inventory.artifact_inventory_sha256
                != material.artifact_inventory_sha256
            ):
                raise BatchStateError("native fit durability recovery input authority differs")
            durability = self.project_mutation_durability_authority(lease)
            acquisition_session = self._project_mutation_provider.issue_acquisition_session(
                acquisition,
                request=request,
                inventory=inventory,
                durability_authority=durability,
            )
            with self._project_mutation_provider.recover_checkpointed_native_fit_lease(
                runtime_fit,
                acquisition_session=acquisition_session,
                completed_archive_ref=current_fit_set.checkpoint_set.completed_lease_archive_ref_by_fit_material_sha256.get(
                    material.fit_material_sha256
                ),
            ) as recovered:
                if recovered is None:
                    return NativeFitDurabilityCleanupOutcome(
                        disposition="already_completed",
                        receipt=None,
                    )
                receipt = BatchProjectMutationDurabilityReceipt._issue(
                    issuer=self._project_durability_capability,
                    durability_kind="classifier_fit",
                    workspace_lease_binding_sha256=(self._workspace_lease_binding_sha256(lease)),
                    acquisition_record_sha256=(recovered.acquisition_record_sha256),
                    acquisition_session_challenge_sha256=(
                        recovered.acquisition_session_challenge_sha256
                    ),
                    artifact_resolution_evidence_sha256=(
                        recovered.artifact_resolution_evidence_sha256
                    ),
                    mutation_request_sha256=recovered.mutation_request_sha256,
                    mutation_authorization_sha256=(recovered.mutation_authorization_sha256),
                    artifact_inventory_sha256=(recovered.artifact_inventory_sha256),
                    project_identity_sha256=head.project_identity_sha256,
                    project_fencing_token_sha256=(recovered.fencing_token_sha256),
                    project_authority_head_sha256=(head.project_authority_head_sha256),
                    mutation_attestation_sha256=(head.project_mutation_attestation_sha256),
                    terminal_attempt_record_sha256s=(),
                    committed_batch_state_head_sha256=head.sha256,
                )
                recovered.complete(receipt)
                return NativeFitDurabilityCleanupOutcome(
                    disposition="completed_now",
                    receipt=receipt,
                )
        except BatchStateError:
            raise
        except Exception as exc:
            raise BatchStateError("native fit durability receipt cannot be reconstructed") from exc

    def _publish_attempt(self, attempt: BatchNodeAttempt) -> None:
        self._publish_immutable(
            self._attempt_path(attempt.terminal_record_sha256),
            _canonical_bytes(attempt.to_mapping()),
            self.layout.attempts,
        )

    def load_attempt(self, digest: str) -> BatchNodeAttempt:
        digest = _sha256(digest, "terminal attempt digest")
        path = self._attempt_path(digest)
        self._owned_path(path, self.layout.attempts, "terminal attempt")
        try:
            payload = path.read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchStateError("terminal attempt bytes are missing or invalid") from exc
        if _canonical_bytes(decoded) != payload:
            raise BatchStateError("terminal attempt bytes are not canonical")
        attempt = BatchNodeAttempt.from_mapping(decoded)
        if attempt.terminal_record_sha256 != digest:
            raise BatchStateError("terminal attempt path and checksum disagree")
        return attempt

    def _authenticate_registration_terminal_for_provider(
        self,
        acquisition_session: object,
        terminal_record_sha256: str,
    ) -> tuple[BatchNodeAttempt, Mapping[str, object] | None]:
        """Reopen one pending registration request and its exact staged terminal."""

        from mif_pipeline.qupath_runtime.catalogue import (
            ProjectMutationAcquisitionSession,
            ProjectMutationArtifactInventoryCapability,
            ProjectMutationRequestCapability,
        )
        from mif_pipeline.registration import registration_record_sha256

        from .batch_project import BatchRegistrationProjectRequest
        from .batch_registration import (
            reopen_accepted_warpy_record,
            validate_registration_result_artifact_ref,
        )
        from .batch_stage_adapters import build_registration_stage_spec
        from .batch_tissue_artifacts import (
            decode_committed_tissue_acceptance_result,
        )

        if type(acquisition_session) is not ProjectMutationAcquisitionSession:
            raise BatchStateError("registration terminal requires exact acquisition")
        request_capability = acquisition_session._mutation_request_capability
        inventory_capability = acquisition_session._artifact_inventory_capability
        if (
            type(request_capability) is not ProjectMutationRequestCapability
            or type(inventory_capability) is not ProjectMutationArtifactInventoryCapability
            or inventory_capability._store_issuer is not self
        ):
            raise BatchStateError("registration terminal acquisition is foreign")
        issued = self._issued_project_inventory_capabilities.get(id(inventory_capability))
        if issued is None or issued[0] is not inventory_capability:
            raise BatchStateError("registration terminal inventory was not issued")
        workspace_lease = issued[1]
        with self._runtime_state_operation():
            with self._lease_operation(workspace_lease):
                self._assert_lease(workspace_lease)
                self._authenticate_project_acquisition_material(
                    record=acquisition_session.record,
                    request=request_capability,
                    inventory=inventory_capability,
                    workspace_lease_binding_sha256=(
                        acquisition_session.workspace_lease_binding_sha256
                    ),
                )
                if (
                    acquisition_session.record.operation != "registration_project_write"
                    or acquisition_session.record.mutation_request_sha256
                    != request_capability.mutation_request_sha256
                    or acquisition_session.record.mutation_authorization_sha256
                    != request_capability.mutation_authorization_sha256
                ):
                    raise BatchStateError("registration acquisition operation differs")
                request_store = request_capability._store_issuer
                self._authenticate_project_stage_catalogue_store(request_store, workspace_lease)
                request, reopened_capability = request_store._reopen_registration_project_request(
                    request_capability.mutation_request_sha256,
                    workspace_lease=workspace_lease,
                    state_store_issuer=self._project_durability_capability,
                )
                self._authenticate_project_request_capability(
                    reopened_capability,
                    expected_kind="registration_project",
                )
                if (
                    type(request) is not BatchRegistrationProjectRequest
                    or request.intent != "open_existing"
                    or request.ordered_roles != ("registration_import",)
                    or request.request_sha256 != request_capability.mutation_request_sha256
                    or request.authorizing_record_sha256
                    != request_capability.mutation_authorization_sha256
                ):
                    raise BatchStateError("registration retained request differs")
                head, _head_bytes = self._read_canonical_state_head()
                digest = _sha256(terminal_record_sha256, "registration terminal record")
                terminal = self.load_attempt(digest)
                authorization = self.load_authorization(request.attempt_authorization_sha256)
                if (
                    head.sha256 != request.state_head_sha256
                    or head.batch_id != request.batch_id
                    or head.active_plan_sha256 != request.active_plan_sha256
                    or head.project_identity_sha256 != request.project_identity_sha256
                    or head.project_authority_head_sha256 != request.project_authority_head_sha256
                    or head.project_qpproj_snapshot_sha256 != request.project_qpproj_snapshot_sha256
                    or head.project_catalogue_sha256 != request.project_catalogue_sha256
                    or self.workspace_storage_scope_sha256 != request.workspace_storage_scope_sha256
                    or head.pending_authorization_sha256_by_node.get(request.node_id)
                    != request.attempt_authorization_sha256
                    or authorization.authorization_sha256 != request.attempt_authorization_sha256
                    or authorization.node_id != request.node_id
                    or authorization.attempt_id != request.attempt_id
                    or authorization.interaction_response_sha256
                    != request.interaction_response_sha256
                    or authorization.interaction_predecessor_terminal_record_sha256
                    != request.predecessor_terminal_record_sha256
                    or terminal.authorization_sha256 != authorization.authorization_sha256
                    or terminal.node_id != request.node_id
                    or terminal.attempt_id != request.attempt_id
                    or terminal.execution_context_sha256 != request.execution_context_sha256
                    or terminal.stage_result.stage != "registration"
                ):
                    raise BatchStateError("registration terminal lost pending request authority")
                self._plan_is_compatible(head=head, authorization=authorization)
                if terminal.stage_result.status is not StageStatus.COMPLETED:
                    if (
                        terminal.stage_result.status
                        not in {
                            StageStatus.NEEDS_INTERACTION,
                            StageStatus.FAILED,
                        }
                        or terminal.output_artifacts
                    ):
                        raise BatchStateError("registration noncompletion has outputs")
                    return terminal, None
                if (
                    request.attempt_kind != "review_registration"
                    or authorization.interaction_response_sha256 is None
                ):
                    raise BatchStateError("registration completion lacks accepted review")
                spec = build_registration_stage_spec()
                try:
                    outputs = StageAdapterRegistry((spec,)).validate_outputs(
                        spec,
                        terminal.output_artifacts,
                        authorization=authorization,
                    )
                    if len(outputs) != 1:
                        raise BatchArtifactError("registration needs one result")
                    projection = validate_registration_result_artifact_ref(outputs[0])
                except BatchArtifactError as exc:
                    raise BatchStateError("completed registration output is invalid") from exc
                record = reopen_accepted_warpy_record(self, authorization)
                current = self.reopen_execution_snapshot(expected_head_sha256=head.sha256)
                tissue_authorities = tuple(
                    item
                    for item in current.committed_artifact_authorities
                    if item.artifact_ref.artifact_ref_sha256
                    == request.tissue_acceptance_result_ref_sha256
                )
                if len(tissue_authorities) != 1:
                    raise BatchStateError("accepted registration tissue authority is absent")
                tissue = decode_committed_tissue_acceptance_result(tissue_authorities[0])
                record_rows = tuple(
                    sorted(
                        zip(
                            record.tissue_section_ids,
                            record.tissue_roi_ids,
                            record.tissue_path_object_ids,
                            strict=True,
                        )
                    )
                )
                accepted_rows = tuple(
                    sorted(
                        (
                            str(item["section_id"]),
                            str(item["roi_id"]),
                            str(item["path_object_id"]),
                        )
                        for item in tissue.section_inventory
                    )
                )
                if (
                    tissue.generation_id != request.tissue_generation_id
                    or tissue.generation_content_sha256 != request.tissue_generation_content_sha256
                    or tissue.section_inventory_sha256 != request.tissue_section_inventory_sha256
                    or tissue.project_entry_id != request.fixed_project_entry_id
                    or tissue.canonical_image_sha256 != request.fixed_image_sha256
                    or record_rows != accepted_rows
                ):
                    raise BatchStateError("registration tissue rows differ from acceptance")
                expected_identity = {
                    "fixed_project_entry_id": record.fixed_project_entry_id,
                    "moving_project_entry_id": record.moving_project_entry_id,
                    "fixed_image_sha256": record.fixed_image_sha256,
                    "moving_image_sha256": record.moving_image_sha256,
                    "direction": record.direction,
                    "transform_qc_review_sha256": _digest(record.registration_qc.to_mapping()),
                    "original_tissue_pathobject_ids_sha256": _digest(
                        list(record.tissue_path_object_ids)
                    ),
                }
                identity = outputs[0].owning_identity
                current_saved = self.current_project_entry_saved_state_sha256_by_id(
                    expected_head_sha256=head.sha256,
                    lease=workspace_lease,
                )
                if (
                    request.fixed_project_entry_id not in current_saved
                    or request.moving_project_entry_id not in current_saved
                ):
                    raise BatchStateError("registration current entry saved state is missing")
                expected_outer = {
                    "active_plan_sha256": head.active_plan_sha256,
                    "producing_state_head_sha256": head.sha256,
                    "project_identity_sha256": request.project_identity_sha256,
                    "project_authority_head_sha256": (request.project_authority_head_sha256),
                    "project_qpproj_snapshot_sha256": (request.project_qpproj_snapshot_sha256),
                    "project_catalogue_sha256": request.project_catalogue_sha256,
                    "primary_project_entry_id": request.fixed_project_entry_id,
                    "primary_saved_state_attestation_sha256": (
                        current_saved[request.fixed_project_entry_id]
                    ),
                    "primary_image_sha256": request.fixed_image_sha256,
                    "primary_project_entry_projection_sha256": (
                        request.fixed_entry_projection_sha256
                    ),
                    "moving_project_entry_id": request.moving_project_entry_id,
                    "moving_saved_state_attestation_sha256": (
                        current_saved[request.moving_project_entry_id]
                    ),
                    "moving_image_sha256": request.moving_image_sha256,
                    "moving_project_entry_projection_sha256": (
                        request.moving_entry_projection_sha256
                    ),
                    "moving_reference_modality": (request.moving_reference_modality),
                    "analysis_frame_ref_sha256": (request.analysis_frame_ref_sha256),
                    "tissue_acceptance_result_ref_sha256": (
                        request.tissue_acceptance_result_ref_sha256
                    ),
                    "tissue_generation_id": request.tissue_generation_id,
                    "tissue_generation_content_sha256": (request.tissue_generation_content_sha256),
                    "tissue_section_inventory_sha256": (request.tissue_section_inventory_sha256),
                    "tissue_section_ids_sha256": request.tissue_section_ids_sha256,
                    "tissue_roi_ids_sha256": request.tissue_roi_ids_sha256,
                    "tissue_path_object_ids_sha256": (request.tissue_path_object_ids_sha256),
                }
                node_scope = head.node_scope_by_id.get(request.node_id)
                if (
                    projection.mode != "warpy"
                    or dict(projection.accepted_warpy_identity or {}) != expected_identity
                    or any(identity.get(name) != value for name, value in expected_outer.items())
                    or node_scope is None
                    or len(node_scope["sample_ids"]) != 1
                    or identity.get("sample_id") != node_scope["sample_ids"][0]
                    or identity.get("validation_domain_id") != node_scope["validation_domain_id"]
                    or identity["accepted_warpy_record_sha256"]
                    != registration_record_sha256(record)
                    or identity["accepted_registration_review_sha256"]
                    != authorization.interaction_response_sha256
                    or record.fixed_project_entry_id != request.fixed_project_entry_id
                    or record.moving_project_entry_id != request.moving_project_entry_id
                    or record.fixed_image_sha256 != request.fixed_image_sha256
                    or record.moving_image_sha256 != request.moving_image_sha256
                    or record.transform_file_sha256 != request.transform_sha256
                    or record.tissue_generation_id != request.tissue_generation_id
                    or _digest(sorted(record.tissue_section_ids))
                    != request.tissue_section_ids_sha256
                    or _digest(sorted(record.tissue_roi_ids)) != request.tissue_roi_ids_sha256
                    or _digest(sorted(record.tissue_path_object_ids))
                    != request.tissue_path_object_ids_sha256
                ):
                    raise BatchStateError("accepted registration differs from request")
                return terminal, record.to_mapping()

    @_guarded_batch_state_operation
    def authenticate_available_artifact(
        self, ref: BatchArtifactRef
    ) -> BatchCommittedArtifactAuthority:
        """Reconstruct current state membership and issue an opaque capability."""

        try:
            validated = validate_artifact_ref(ref)
        except (BatchArtifactError, TypeError, ValueError) as exc:
            raise BatchStateError("available artifact reference is invalid") from exc
        if validated.origin is not ArtifactOrigin.INTERNAL_NODE:
            raise BatchStateError("committed artifact authentication requires an internal producer")
        producer_node_id = validated.producer_node_id
        producer_attempt_id = validated.producer_attempt_id
        producer_authorization_sha256 = validated.producer_attempt_authorization_sha256
        if (
            producer_node_id is None
            or producer_attempt_id is None
            or producer_authorization_sha256 is None
        ):
            raise BatchStateError("available artifact lacks exact producer identity")

        head = self.load_head()
        digest = validated.artifact_ref_sha256
        if head.available_artifact_ref_sha256_by_key.get(digest) != digest:
            raise BatchStateError("artifact is not available in the current state head")
        active = self._load_plan_snapshot(head.active_plan_sha256)
        node = active.node_by_id.get(producer_node_id)
        if (
            node is None
            or producer_node_id in head.invalidated_node_ids
            or head.node_status_by_id.get(producer_node_id) is not StageStatus.COMPLETED
            or validated.artifact_kind not in node["declared_output_kinds"]
        ):
            raise BatchStateError("artifact producer is not active and completed")
        attempt_ids = head.attempt_ids_by_node.get(producer_node_id, ())
        if not attempt_ids or attempt_ids[-1] != producer_attempt_id:
            raise BatchStateError("artifact does not belong to the latest producer attempt")
        terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(producer_attempt_id)
        if terminal_digest is None:
            raise BatchStateError("artifact attempt lacks a committed terminal record")
        attempt = self.load_attempt(terminal_digest)
        if (
            attempt.node_id != producer_node_id
            or attempt.attempt_id != producer_attempt_id
            or attempt.authorization_sha256 != producer_authorization_sha256
            or attempt.stage_result.status is not StageStatus.COMPLETED
            or sum(item == validated for item in attempt.output_artifacts) != 1
        ):
            raise BatchStateError("artifact does not match its completed terminal attempt")
        authorization = self.load_authorization(producer_authorization_sha256)
        if (
            authorization.node_id != producer_node_id
            or authorization.attempt_id != producer_attempt_id
            or authorization.authorization_sha256 != producer_authorization_sha256
        ):
            raise BatchStateError("artifact does not match its reopened authorization")
        self._plan_material_is_compatible(
            head=head,
            authorization=authorization,
        )
        try:
            return _issue_batch_committed_artifact_authority(
                artifact_ref=validated,
                state_head_sha256=head.sha256,
                active_plan_sha256=head.active_plan_sha256,
                producer_node_kind=str(node["kind"]),
                terminal_record_sha256=attempt.terminal_record_sha256,
                execution_exchange_sha256=attempt.execution_exchange_sha256,
            )
        except BatchArtifactError as exc:
            raise BatchStateError("committed artifact authority issuance failed") from exc

    @_guarded_batch_state_operation
    def authenticate_available_manifest_external_artifact(
        self, ref: BatchArtifactRef
    ) -> BatchCommittedArtifactAuthority:
        """Issue current-head authority for one retained manifest input."""

        try:
            validated = validate_artifact_ref(ref)
        except (BatchArtifactError, TypeError, ValueError) as exc:
            raise BatchStateError("manifest external artifact reference is invalid") from exc
        if validated.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
            raise BatchStateError(
                "manifest external authentication requires manifest_external origin"
            )
        receipt_sha256 = validated.external_validation_receipt_sha256
        if receipt_sha256 is None:
            raise BatchStateError("manifest external artifact lacks its validation receipt")

        head = self.load_head()
        digest = validated.artifact_ref_sha256
        active = self._load_plan_snapshot(head.active_plan_sha256)
        if digest not in active.external_input_sha256s:
            raise BatchStateError(
                "manifest external artifact is absent from the current active plan"
            )
        if head.available_artifact_ref_sha256_by_key.get(digest) != digest:
            raise BatchStateError(
                "manifest external artifact is not available in the current state head"
            )
        try:
            return _issue_batch_committed_artifact_authority(
                artifact_ref=validated,
                state_head_sha256=head.sha256,
                active_plan_sha256=head.active_plan_sha256,
                producer_node_kind="manifest_external",
                terminal_record_sha256=receipt_sha256,
                execution_exchange_sha256=None,
            )
        except BatchArtifactError as exc:
            raise BatchStateError("manifest external artifact authority issuance failed") from exc

    @_guarded_batch_state_operation
    def authenticate_available_artifact_producer_spec(
        self,
        ref: BatchArtifactRef,
        *,
        expected_adapter_spec: StageAdapterSpec,
    ) -> BatchCommittedArtifactAuthority:
        """Prove that a current artifact came from one exact production spec."""

        if type(expected_adapter_spec) is not StageAdapterSpec:
            raise BatchStateError(
                "artifact production authentication requires an exact adapter spec"
            )
        authority = self.authenticate_available_artifact(ref)
        head = self.load_head()
        snapshot = self._load_plan_snapshot(head.active_plan_sha256)
        producer_node_id = authority.artifact_ref.producer_node_id
        node = snapshot.node_by_id.get(producer_node_id or "")
        if (
            node is None
            or authority.producer_node_kind != expected_adapter_spec.node_kind
            or node["kind"] != expected_adapter_spec.node_kind
            or node["version"] != expected_adapter_spec.node_version
            or node["adapter_spec_sha256"] != expected_adapter_spec.spec_sha256
            or node["resource"] != expected_adapter_spec.resource_kind
            or node["dispatch_mode"] != expected_adapter_spec.dispatch_mode.value
            or tuple(node["declared_output_kinds"]) != expected_adapter_spec.declared_outputs
            or authority.artifact_ref.artifact_kind not in expected_adapter_spec.declared_outputs
        ):
            raise BatchStateError(
                "artifact producer does not use its exact production adapter spec"
            )
        return authority

    def _plan_material_is_compatible(
        self,
        *,
        head: BatchStateHead,
        authorization: BatchAttemptAuthorization,
    ) -> None:
        if authorization.admission_head_sha256 not in {
            *head.ancestor_head_sha256s,
            head.sha256,
        }:
            raise BatchStateError("terminal result does not descend from admission head")
        if authorization.admission_plan_sha256 not in head.plan_history_sha256s:
            raise BatchStateError("terminal admission plan is not active plan ancestry")
        active = self._load_plan_snapshot(head.active_plan_sha256)
        admission = self._load_plan_snapshot(authorization.admission_plan_sha256)
        self._validate_persisted_dispatch_binding(authorization, admission, head=head)
        active_node = active.node_by_id.get(authorization.node_id)
        admission_node = admission.node_by_id.get(authorization.node_id)
        if (
            active_node is None
            or admission_node is None
            or dict(active_node) != dict(admission_node)
            or authorization.node_id in head.invalidated_node_ids
        ):
            raise BatchStateError("terminal result was materially superseded")
        if authorization.input_artifact_set_sha256 != self._expected_input_set_sha256(
            active, head, authorization.node_id
        ):
            raise BatchStateError("terminal result input set was superseded")
        if authorization.node_material_sha256 != self._current_plan_node_material_sha256(
            active, authorization.node_id
        ):
            raise BatchStateError("terminal result execution material was superseded")
        cursor = active
        seen: set[str] = set()
        while cursor.plan_sha256 != authorization.admission_plan_sha256:
            if cursor.plan_sha256 in seen or cursor.predecessor_plan_sha256 is None:
                raise BatchStateError("plan history is not an unbroken descendant chain")
            seen.add(cursor.plan_sha256)
            cursor = self._load_plan_snapshot(cursor.predecessor_plan_sha256)

    def _plan_is_compatible(
        self,
        *,
        head: BatchStateHead,
        authorization: BatchAttemptAuthorization,
    ) -> None:
        self._plan_material_is_compatible(
            head=head,
            authorization=authorization,
        )
        active = self._load_plan_snapshot(head.active_plan_sha256)
        self._validate_persisted_dispatch_binding(
            authorization,
            active,
            head=head, check_current_binding=True,
        )

    def _commit_terminal_result(
        self,
        *,
        context: BatchNodeExecutionContext,
        prepared: BatchPreparedNodeResult,
        lease: BatchWorkspaceLease,
    ) -> ScheduledNodeResult:
        authorization = context.authorization
        reopened = self.load_authorization(authorization.authorization_sha256)
        if reopened != authorization:
            raise BatchStateError("terminal authorization bytes are not exact")
        attempt = BatchNodeAttempt.create(
            authorization=authorization,
            stage_result=prepared.stage_result,
            output_artifacts=prepared.execution_result.outputs,
            execution_exchange_sha256=prepared.execution_exchange_sha256,
            execution_context_binding=prepared.execution_context_binding,
        )
        self._assert_lease(lease)
        self._publish_attempt(attempt)
        for _ in range(8):
            self._assert_lease(lease)
            head = self.load_head()
            self._plan_is_compatible(head=head, authorization=authorization)
            if head.pending_authorization_sha256_by_node.get(context.node_id) != (
                authorization.authorization_sha256
            ):
                raise BatchStateError("terminal attempt lost its exact pending authority")
            if authorization.attempt_id in head.terminal_attempt_sha256_by_attempt_id:
                raise BatchStateError("attempt already has a competing terminal result")
            if authorization.interaction_response_sha256 is not None:
                self._load_and_validate_response(
                    authorization.interaction_response_sha256,
                    node_id=authorization.node_id,
                    predecessor=authorization.interaction_predecessor_terminal_record_sha256,
                )
            pending = dict(head.pending_authorization_sha256_by_node)
            pending.pop(context.node_id)
            statuses = dict(head.node_status_by_id)
            statuses[context.node_id] = prepared.stage_result.status
            attempt_ids = {key: tuple(item) for key, item in head.attempt_ids_by_node.items()}
            attempt_ids[context.node_id] = (
                *attempt_ids.get(context.node_id, ()),
                authorization.attempt_id,
            )
            terminal = dict(head.terminal_attempt_sha256_by_attempt_id)
            terminal[authorization.attempt_id] = attempt.terminal_record_sha256
            available = dict(head.available_artifact_ref_sha256_by_key)
            fit_sets = dict(head.pending_training_fit_set_sha256_by_node)
            if prepared.stage_result.status is StageStatus.COMPLETED:
                self._validate_native_fit_completion(context, prepared.execution_result, head)
                fit_sets.pop(context.node_id, None)
                for output in attempt.output_artifacts:
                    key = output.artifact_ref_sha256
                    existing = available.get(key)
                    if existing is not None and existing != output.artifact_ref_sha256:
                        raise BatchStateError("completed artifact inventory key conflicts")
                    available[key] = output.artifact_ref_sha256
            failures = dict(head.failure_codes_by_node)
            if prepared.stage_result.status is StageStatus.FAILED:
                if prepared.stage_result.error_code is None:
                    raise BatchStateError("failed terminal result lacks an error code")
                failures[context.node_id] = prepared.stage_result.error_code
            else:
                failures.pop(context.node_id, None)
            generations = {
                key: tuple(item) for key, item in head.generation_references_by_node.items()
            }
            if prepared.stage_result.generation_id is not None:
                prior_generations = generations.get(context.node_id, ())
                generations[context.node_id] = (
                    prior_generations
                    if prepared.stage_result.generation_id in prior_generations
                    else (*prior_generations, prepared.stage_result.generation_id)
                )
            node_scope = head.node_scope_by_id[context.node_id]
            by_sample = {
                key: tuple(item) for key, item in head.last_durable_node_ids_by_sample.items()
            }
            for sample_id in node_scope["sample_ids"]:
                prior = by_sample.get(sample_id, ())
                by_sample[sample_id] = (
                    prior if context.node_id in prior else (*prior, context.node_id)
                )
            by_domain = {
                key: tuple(item) for key, item in head.last_durable_node_ids_by_domain.items()
            }
            domain = node_scope["validation_domain_id"]
            if domain is not None:
                domain_id = _text(domain, "node scope validation_domain_id")
                prior_domain = by_domain.get(domain_id, ())
                by_domain[domain_id] = (
                    prior_domain
                    if context.node_id in prior_domain
                    else (*prior_domain, context.node_id)
                )
            candidate = head.evolve(
                node_status_by_id=statuses,
                pending_authorization_sha256_by_node=pending,
                available_artifact_ref_sha256_by_key=available,
                failure_codes_by_node=failures,
                generation_references_by_node=generations,
                attempt_ids_by_node=attempt_ids,
                terminal_attempt_sha256_by_attempt_id=terminal,
                last_durable_node_ids_by_sample=by_sample,
                last_durable_node_ids_by_domain=by_domain,
                pending_training_fit_set_sha256_by_node=fit_sets,
            )
            try:
                committed = self._replace_head_cas(
                    candidate,
                    head.sha256,
                    lease,
                    allow_terminal=True,
                    allow_native_fit_checkpoint=True,
                )
            except BatchHeadConflictError:
                continue
            return ScheduledNodeResult(
                node_id=context.node_id,
                attempt_id=authorization.attempt_id,
                attempt_authorization_sha256=authorization.authorization_sha256,
                stage_result=prepared.stage_result,
                terminal_attempt_record_sha256=attempt.terminal_record_sha256,
                committed_state_head_sha256=committed.sha256,
            )
        raise BatchHeadConflictError("terminal commit lost repeated state-head races")

    def _commit_project_terminal_result(
        self,
        *,
        context: BatchNodeExecutionContext,
        prepared: BatchPreparedNodeResult,
        material: BatchProjectMaterialSuccessor,
        lease: BatchWorkspaceLease,
    ) -> tuple[ScheduledNodeResult, BatchProjectMutationDurabilityReceipt]:
        self._assert_lease(lease)
        if (
            not isinstance(material, BatchProjectMaterialSuccessor)
            or material._issuer is not self._project_durability_capability
            or not isinstance(prepared, BatchPreparedNodeResult)
            or prepared._issuer is not self._prepared_result_capability
            or material.execution_context_sha256 != _execution_context_sha256(context)
            or material.prepared_result_binding_sha256
            != self._prepared_project_result_binding(prepared)
            or prepared.stage_result.status is not StageStatus.COMPLETED
        ):
            raise BatchStateError("project terminal capabilities are foreign")
        try:
            outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
                context.adapter_spec,
                prepared.execution_result.outputs,
                authorization=context.authorization,
            )
        except BatchArtifactError as exc:
            raise BatchStateError(
                "project terminal outputs violate the authorized stage contract"
            ) from exc
        if len(outputs) != 1:
            raise BatchStateError("project upsert must emit exactly one primary artifact")
        authorization = context.authorization
        reopened = self.load_authorization(authorization.authorization_sha256)
        if reopened != authorization:
            raise BatchStateError("project terminal authorization bytes changed")
        head, _ = self._read_canonical_state_head()
        if head.sha256 != material.expected_state_head_sha256:
            raise BatchHeadConflictError("project terminal predecessor changed")
        self._plan_is_compatible(head=head, authorization=authorization)
        if head.pending_authorization_sha256_by_node.get(context.node_id) != (
            authorization.authorization_sha256
        ):
            raise BatchStateError("project terminal lost pending authority")
        attempt = BatchNodeAttempt.create(
            authorization=authorization,
            stage_result=prepared.stage_result,
            output_artifacts=outputs,
            execution_exchange_sha256=prepared.execution_exchange_sha256,
            execution_context_binding=prepared.execution_context_binding,
        )
        self._publish_attempt(attempt)
        pending = dict(head.pending_authorization_sha256_by_node)
        pending.pop(context.node_id)
        statuses = dict(head.node_status_by_id)
        statuses[context.node_id] = StageStatus.COMPLETED
        attempt_ids = {key: tuple(value) for key, value in head.attempt_ids_by_node.items()}
        attempt_ids[context.node_id] = (
            *attempt_ids.get(context.node_id, ()),
            authorization.attempt_id,
        )
        terminal = dict(head.terminal_attempt_sha256_by_attempt_id)
        terminal[authorization.attempt_id] = attempt.terminal_record_sha256
        available = dict(head.available_artifact_ref_sha256_by_key)
        available[outputs[0].artifact_ref_sha256] = outputs[0].artifact_ref_sha256
        fit_sets = dict(head.pending_training_fit_set_sha256_by_node)
        fit_sets.pop(context.node_id, None)
        entry_ids = dict(head.sample_project_entry_id_by_sample)
        projections = dict(head.sample_project_entry_projection_sha256_by_sample)
        entry_ids[material.sample_id] = material.project_entry_id
        projections[material.sample_id] = material.project_entry_projection_sha256
        node_scope = head.node_scope_by_id[context.node_id]
        by_sample = {
            key: tuple(value) for key, value in head.last_durable_node_ids_by_sample.items()
        }
        for sample_id in node_scope["sample_ids"]:
            prior = by_sample.get(sample_id, ())
            by_sample[sample_id] = prior if context.node_id in prior else (*prior, context.node_id)
        by_domain = {
            key: tuple(value) for key, value in head.last_durable_node_ids_by_domain.items()
        }
        domain = node_scope["validation_domain_id"]
        if domain is None:
            raise BatchStateError("project mutation cannot use a pooled validation-domain scope")
        domain_id = _text(domain, "node scope validation_domain_id")
        prior_domain = by_domain.get(domain_id, ())
        by_domain[domain_id] = (
            prior_domain if context.node_id in prior_domain else (*prior_domain, context.node_id)
        )
        candidate = head.evolve(
            project_identity_sha256=material.project_identity_sha256,
            project_authority_head_sha256=material.project_authority_head_sha256,
            project_materialization_state=material.project_materialization_state,
            project_qpproj_snapshot_sha256=material.project_qpproj_snapshot_sha256,
            project_mutation_attestation_sha256=(material.project_mutation_attestation_sha256),
            project_catalogue_sha256=material.project_catalogue_sha256,
            sample_project_entry_id_by_sample=entry_ids,
            sample_project_entry_projection_sha256_by_sample=projections,
            node_status_by_id=statuses,
            pending_authorization_sha256_by_node=pending,
            available_artifact_ref_sha256_by_key=available,
            attempt_ids_by_node=attempt_ids,
            terminal_attempt_sha256_by_attempt_id=terminal,
            last_durable_node_ids_by_sample=by_sample,
            last_durable_node_ids_by_domain=by_domain,
            pending_training_fit_set_sha256_by_node=fit_sets,
        )
        committed = self._replace_project_head_cas(
            candidate,
            head.sha256,
            lease,
            allow_native_fit_checkpoint=True,
        )
        receipt = BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="project_node",
            workspace_lease_binding_sha256=self._workspace_lease_binding_sha256(lease),
            acquisition_record_sha256=material.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(material.acquisition_session_challenge_sha256),
            artifact_resolution_evidence_sha256=(material.artifact_resolution_evidence_sha256),
            mutation_request_sha256=material.mutation_request_sha256,
            mutation_authorization_sha256=material.mutation_authorization_sha256,
            artifact_inventory_sha256=material.artifact_inventory_sha256,
            project_identity_sha256=material.project_identity_sha256,
            project_fencing_token_sha256=material.project_fencing_token_sha256,
            project_authority_head_sha256=material.project_authority_head_sha256,
            mutation_attestation_sha256=material.project_mutation_attestation_sha256,
            terminal_attempt_record_sha256s=(attempt.terminal_record_sha256,),
            committed_batch_state_head_sha256=committed.sha256,
        )
        return (
            ScheduledNodeResult(
                node_id=context.node_id,
                attempt_id=authorization.attempt_id,
                attempt_authorization_sha256=authorization.authorization_sha256,
                stage_result=prepared.stage_result,
                terminal_attempt_record_sha256=attempt.terminal_record_sha256,
                committed_state_head_sha256=committed.sha256,
            ),
            receipt,
        )

    @staticmethod
    def _project_stage_lease_authority(
        material: BatchProjectStageMaterialSuccessor,
    ) -> dict[str, object]:
        return {
            "acquisition_record_sha256": material.acquisition_record_sha256,
            "acquisition_session_challenge_sha256": (material.acquisition_session_challenge_sha256),
            "artifact_resolution_evidence_sha256": (material.artifact_resolution_evidence_sha256),
            "mutation_request_sha256": material.mutation_request_sha256,
            "mutation_authorization_sha256": (material.mutation_authorization_sha256),
            "artifact_inventory_sha256": material.artifact_inventory_sha256,
            "fencing_token_sha256": material.project_fencing_token_sha256,
            "project_identity_sha256": material.project_identity_sha256,
            "project_authority_head_sha256": (material.project_authority_head_sha256),
            "project_qpproj_snapshot_sha256": (material.project_qpproj_snapshot_sha256),
            "project_mutation_attestation_sha256": (material.project_mutation_attestation_sha256),
        }

    def _commit_project_stage_results(
        self,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
        prepared: tuple[BatchPreparedNodeResult, ...],
        material: BatchProjectStageMaterialSuccessor,
        lease: BatchWorkspaceLease,
    ) -> tuple[
        tuple[ScheduledNodeResult, ...],
        BatchProjectMutationDurabilityReceipt,
    ]:
        """Hold exact live project authority across grouped batch commit."""

        if (
            type(material) is not BatchProjectStageMaterialSuccessor
            or material._issuer is not self._project_durability_capability
            or self._project_mutation_provider is None
            or not getattr(material._project_lease, "is_live", False)
        ):
            raise BatchStateError("project-stage terminal capabilities are foreign")
        operation = getattr(material._project_lease, "operation", None)
        registration = operation == "registration_project_write"
        if (
            operation not in {"project_stage", "registration_project_write"}
            or (
                not registration
                and any(item.adapter_spec.node_kind == "registration" for item in contexts)
            )
            or (
                registration
                and (
                    len(contexts) != 1
                    or len(prepared) != 1
                    or contexts[0].adapter_spec.node_kind != "registration"
                    or material.disposition != "unchanged_project"
                )
            )
        ):
            raise BatchStateError("project-stage terminal operation is invalid")
        retain = getattr(
            self._project_mutation_provider,
            (
                "_retain_registration_batch_commit_authority"
                if registration
                else "_retain_project_stage_batch_commit_authority"
            ),
            None,
        )
        if not callable(retain):
            raise BatchStateError("project-stage project authority cannot be retained")
        with retain(
            material._project_lease,
            **self._project_stage_lease_authority(material),
        ) as project_lease:
            return self._commit_project_stage_results_under_project_authority(
                contexts=contexts,
                prepared=prepared,
                material=material,
                lease=lease,
                project_lease=project_lease,
            )

    def _commit_project_stage_results_under_project_authority(
        self,
        *,
        contexts: tuple[BatchNodeExecutionContext, ...],
        prepared: tuple[BatchPreparedNodeResult, ...],
        material: BatchProjectStageMaterialSuccessor,
        lease: BatchWorkspaceLease,
        project_lease: object,
    ) -> tuple[
        tuple[ScheduledNodeResult, ...],
        BatchProjectMutationDurabilityReceipt,
    ]:
        """Commit one authenticated project-stage group through one state CAS."""

        self._assert_lease(lease)
        if (
            type(material) is not BatchProjectStageMaterialSuccessor
            or material._issuer is not self._project_durability_capability
            or material._project_lease is not project_lease
            or len(contexts) != len(prepared)
            or material.execution_context_sha256s
            != tuple(_execution_context_sha256(item) for item in contexts)
            or material.prepared_result_binding_sha256s
            != tuple(self._prepared_project_result_binding(item) for item in prepared)
            or any(item._issuer is not self._prepared_result_capability for item in prepared)
        ):
            raise BatchStateError("project-stage terminal capabilities are foreign")
        registration = getattr(project_lease, "operation", None) == "registration_project_write"
        if (
            getattr(project_lease, "operation", None)
            not in {"project_stage", "registration_project_write"}
            or (
                not registration
                and any(item.adapter_spec.node_kind == "registration" for item in contexts)
            )
            or (
                registration
                and (
                    len(contexts) != 1
                    or contexts[0].adapter_spec.node_kind != "registration"
                    or material.disposition != "unchanged_project"
                )
            )
        ):
            raise BatchStateError("project-stage terminal operation is invalid")
        validate_project_authority = getattr(
            self._project_mutation_provider,
            (
                "_validate_registration_batch_commit_authority"
                if registration
                else "_validate_project_stage_batch_commit_authority"
            ),
            None,
        )
        if not callable(validate_project_authority):
            raise BatchStateError("project-stage terminal authority cannot be validated")
        head, _ = self._read_canonical_state_head()
        if head.sha256 != material.expected_state_head_sha256:
            raise BatchHeadConflictError("project-stage terminal predecessor changed")

        attempts: list[BatchNodeAttempt] = []
        for context, result in zip(contexts, prepared, strict=True):
            authorization = context.authorization
            if (
                self.load_authorization(authorization.authorization_sha256) != authorization
                or head.pending_authorization_sha256_by_node.get(context.node_id)
                != authorization.authorization_sha256
                or result.execution_result.authorization_sha256
                != authorization.authorization_sha256
                or result.execution_context_sha256 != _execution_context_sha256(context)
                or result.stage_result.stage != context.adapter_spec.node_kind
            ):
                raise BatchStateError("project-stage terminal result lost exact pending authority")
            self._plan_is_compatible(head=head, authorization=authorization)
            if registration and result.stage_result.status in {
                StageStatus.NEEDS_INTERACTION,
                StageStatus.FAILED,
            }:
                if result.execution_result.outputs:
                    raise BatchStateError("registration non-completion cannot publish artifacts")
                outputs = ()
            else:
                try:
                    outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
                        context.adapter_spec,
                        result.execution_result.outputs,
                        authorization=authorization,
                    )
                except BatchArtifactError as exc:
                    if result.stage_result.status is StageStatus.COMPLETED:
                        raise BatchStateError(
                            "project-stage outputs violate the authorized contract"
                        ) from exc
                    outputs = result.execution_result.outputs
                    declared = set(context.adapter_spec.declared_outputs)
                    output_contracts = {
                        item.artifact_kind: item
                        for item in context.adapter_spec.output_identity_contracts
                    }
                    if len({item.artifact_kind for item in outputs}) != len(outputs) or any(
                        item.artifact_kind not in declared for item in outputs
                    ):
                        raise BatchStateError(
                            "project-stage non-completion has foreign outputs"
                        ) from exc
                    for output in outputs:
                        try:
                            validate_artifact_ref(output, authorization=authorization)
                        except BatchArtifactError as output_exc:
                            raise BatchStateError(
                                "project-stage non-completion output is invalid"
                            ) from output_exc
                        if (
                            output.owning_identity_contract_sha256
                            != output_contracts[output.artifact_kind].contract_sha256
                        ):
                            raise BatchStateError(
                                "project-stage output uses foreign identity contract"
                            )
            if (
                registration
                and result.stage_result.status is StageStatus.COMPLETED
                and len(outputs) != 1
            ):
                raise BatchStateError("completed registration needs one result")
            attempts.append(
                BatchNodeAttempt.create(
                    authorization=authorization,
                    stage_result=result.stage_result,
                    output_artifacts=outputs,
                    execution_exchange_sha256=result.execution_exchange_sha256,
                    execution_context_binding=result.execution_context_binding,
                )
            )

        # Every rejection above is effect-free. Immutable attempt publication is
        # allowed only after the entire group and predecessor are authenticated.
        for attempt in attempts:
            self._publish_attempt(attempt)

        pending = dict(head.pending_authorization_sha256_by_node)
        statuses = dict(head.node_status_by_id)
        attempt_ids = {key: tuple(value) for key, value in head.attempt_ids_by_node.items()}
        terminal = dict(head.terminal_attempt_sha256_by_attempt_id)
        available = dict(head.available_artifact_ref_sha256_by_key)
        fit_sets = dict(head.pending_training_fit_set_sha256_by_node)
        failures = dict(head.failure_codes_by_node)
        generations = {
            key: tuple(value) for key, value in head.generation_references_by_node.items()
        }
        by_sample = {
            key: tuple(value) for key, value in head.last_durable_node_ids_by_sample.items()
        }
        by_domain = {
            key: tuple(value) for key, value in head.last_durable_node_ids_by_domain.items()
        }
        for context, result, attempt in zip(contexts, prepared, attempts, strict=True):
            authorization = context.authorization
            pending.pop(context.node_id)
            statuses[context.node_id] = result.stage_result.status
            attempt_ids[context.node_id] = (
                *attempt_ids.get(context.node_id, ()),
                authorization.attempt_id,
            )
            terminal[authorization.attempt_id] = attempt.terminal_record_sha256
            if result.stage_result.status is StageStatus.COMPLETED:
                fit_sets.pop(context.node_id, None)
                for output in attempt.output_artifacts:
                    available[output.artifact_ref_sha256] = output.artifact_ref_sha256
            if result.stage_result.status is StageStatus.FAILED:
                if result.stage_result.error_code is None:
                    raise BatchStateError("failed project-stage terminal lacks an error code")
                failures[context.node_id] = result.stage_result.error_code
            else:
                failures.pop(context.node_id, None)
            if result.stage_result.generation_id is not None:
                prior_generations = generations.get(context.node_id, ())
                generations[context.node_id] = (
                    prior_generations
                    if result.stage_result.generation_id in prior_generations
                    else (*prior_generations, result.stage_result.generation_id)
                )
            scope = head.node_scope_by_id[context.node_id]
            for sample_id in scope["sample_ids"]:
                prior = by_sample.get(sample_id, ())
                by_sample[sample_id] = (
                    prior if context.node_id in prior else (*prior, context.node_id)
                )
            domain = scope["validation_domain_id"]
            if domain is None:
                raise BatchStateError(
                    "project-stage terminal cannot use pooled validation-domain scope"
                )
            domain_id = _text(domain, "node scope validation_domain_id")
            prior_domain = by_domain.get(domain_id, ())
            by_domain[domain_id] = (
                prior_domain
                if context.node_id in prior_domain
                else (*prior_domain, context.node_id)
            )

        candidate = head.evolve(
            project_identity_sha256=material.project_identity_sha256,
            project_authority_head_sha256=(material.project_authority_head_sha256),
            project_qpproj_snapshot_sha256=(material.project_qpproj_snapshot_sha256),
            project_mutation_attestation_sha256=(material.project_mutation_attestation_sha256),
            project_catalogue_sha256=material.project_catalogue_sha256,
            node_status_by_id=statuses,
            pending_authorization_sha256_by_node=pending,
            available_artifact_ref_sha256_by_key=available,
            failure_codes_by_node=failures,
            generation_references_by_node=generations,
            attempt_ids_by_node=attempt_ids,
            terminal_attempt_sha256_by_attempt_id=terminal,
            last_durable_node_ids_by_sample=by_sample,
            last_durable_node_ids_by_domain=by_domain,
            pending_training_fit_set_sha256_by_node=fit_sets,
        )
        validate_project_authority(
            project_lease,
            **self._project_stage_lease_authority(material),
        )
        try:
            committed = (
                self._replace_observed_project_stage_head_cas(
                    candidate,
                    head.sha256,
                    lease,
                    contexts=contexts,
                )
                if material.disposition == "observed_mutation"
                else self._replace_head_cas(
                    candidate,
                    head.sha256,
                    lease,
                    allow_terminal=True,
                    allow_native_fit_checkpoint=True,
                )
            )
        except BaseException:
            # Exact same-session recovery only: an atomic replacement may have
            # completed before its publication primitive reported failure.
            reopened, _ = self._read_canonical_state_head()
            if reopened != candidate:
                raise
            committed = reopened
        validate_project_authority(
            project_lease,
            **self._project_stage_lease_authority(material),
        )
        scheduled = tuple(
            ScheduledNodeResult(
                node_id=context.node_id,
                attempt_id=context.authorization.attempt_id,
                attempt_authorization_sha256=(context.authorization.authorization_sha256),
                stage_result=result.stage_result,
                terminal_attempt_record_sha256=attempt.terminal_record_sha256,
                committed_state_head_sha256=committed.sha256,
            )
            for context, result, attempt in zip(contexts, prepared, attempts, strict=True)
        )
        receipt = BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="project_stage",
            workspace_lease_binding_sha256=(self._workspace_lease_binding_sha256(lease)),
            acquisition_record_sha256=material.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(material.acquisition_session_challenge_sha256),
            artifact_resolution_evidence_sha256=(material.artifact_resolution_evidence_sha256),
            mutation_request_sha256=material.mutation_request_sha256,
            mutation_authorization_sha256=(material.mutation_authorization_sha256),
            artifact_inventory_sha256=material.artifact_inventory_sha256,
            project_identity_sha256=material.project_identity_sha256,
            project_fencing_token_sha256=material.project_fencing_token_sha256,
            project_authority_head_sha256=(material.project_authority_head_sha256),
            mutation_attestation_sha256=(material.project_mutation_attestation_sha256),
            terminal_attempt_record_sha256s=tuple(item.terminal_record_sha256 for item in attempts),
            committed_batch_state_head_sha256=committed.sha256,
        )
        return scheduled, receipt

    @_guarded_batch_state_lease_operation
    def reissue_project_terminal_durability_receipt(
        self,
        authorization: BatchAttemptAuthorization,
        *,
        project_lease: object,
        lease: BatchWorkspaceLease,
    ) -> BatchProjectMutationDurabilityReceipt:
        """Return completion authority for the original, already-committed upsert.

        Its saved authorization identifies the completed terminal attempt; no
        pending execution context or new scientific result is required.
        """

        self._assert_lease(lease)
        if not isinstance(authorization, BatchAttemptAuthorization) or not getattr(
            project_lease, "is_live", False
        ):
            raise BatchStateError("project receipt recovery lacks live authority")
        head = self.load_head()
        if (
            head.node_status_by_id.get(authorization.node_id) is not StageStatus.COMPLETED
            or authorization.node_id in head.pending_authorization_sha256_by_node
            or self.load_authorization(authorization.authorization_sha256) != authorization
        ):
            raise BatchStateError("project terminal successor is not current")
        attempt_ids = head.attempt_ids_by_node.get(authorization.node_id, ())
        if not attempt_ids or attempt_ids[-1] != authorization.attempt_id:
            raise BatchStateError("project terminal attempt lineage is not exact")
        terminal_digest = head.terminal_attempt_sha256_by_attempt_id.get(authorization.attempt_id)
        if terminal_digest is None:
            raise BatchStateError("project terminal record is unavailable")
        attempt = self.load_attempt(terminal_digest)
        scope = head.node_scope_by_id.get(authorization.node_id)
        sample_ids = () if scope is None else tuple(scope["sample_ids"])
        project_head = getattr(project_lease, "current_authority_head", None)
        designation = getattr(project_lease, "designation", None)
        current_attestation = project_lease.current_mutation_attestation()
        if (
            len(sample_ids) != 1
            or attempt.authorization_sha256 != authorization.authorization_sha256
            or attempt.stage_result.status is not StageStatus.COMPLETED
            or head.sample_project_entry_id_by_sample.get(sample_ids[0]) is None
            or head.sample_project_entry_projection_sha256_by_sample.get(sample_ids[0]) is None
            or head.project_identity_sha256
            != getattr(
                getattr(designation, "identity", None),
                "project_identity_sha256",
                None,
            )
            or head.project_authority_head_sha256
            != getattr(project_head, "authority_head_sha256", None)
            or head.project_qpproj_snapshot_sha256
            != getattr(project_head, "qpproj_snapshot_sha256", None)
            or head.project_mutation_attestation_sha256
            != getattr(project_head, "latest_mutation_attestation_sha256", None)
            or current_attestation is None
            or current_attestation.workspace_catalogue_sha256 != head.project_catalogue_sha256
            or getattr(project_lease, "mutation_authorization_sha256", None)
            != authorization.authorization_sha256
        ):
            raise BatchStateError("project terminal successor authority disagrees")
        return BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="project_node",
            workspace_lease_binding_sha256=self._workspace_lease_binding_sha256(lease),
            acquisition_record_sha256=project_lease.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(
                project_lease.acquisition_session_challenge_sha256
            ),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=project_lease.mutation_request_sha256,
            mutation_authorization_sha256=(project_lease.mutation_authorization_sha256),
            artifact_inventory_sha256=project_lease.artifact_inventory_sha256,
            project_identity_sha256=head.project_identity_sha256,
            project_fencing_token_sha256=project_lease.fencing_token_sha256,
            project_authority_head_sha256=head.project_authority_head_sha256,
            mutation_attestation_sha256=(head.project_mutation_attestation_sha256),
            terminal_attempt_record_sha256s=(terminal_digest,),
            committed_batch_state_head_sha256=head.sha256,
        )

    @_guarded_batch_state_lease_operation
    def issue_capsule_import_durability_receipt(
        self,
        *,
        project_lease: object,
        authenticated_graph_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> BatchProjectMutationDurabilityReceipt:
        """Bind one imported graph's durable head to its live project lease."""

        self._assert_lease(lease)
        _sha256(authenticated_graph_sha256, "authenticated_graph_sha256")
        head = self.load_head()
        project_head = getattr(project_lease, "current_authority_head", None)
        if (
            not getattr(project_lease, "is_live", False)
            or getattr(project_lease, "operation", None) != "capsule_import"
            or head.project_identity_sha256
            != getattr(
                getattr(getattr(project_lease, "designation", None), "identity", None),
                "project_identity_sha256",
                None,
            )
            or head.project_authority_head_sha256
            != getattr(project_head, "authority_head_sha256", None)
        ):
            raise BatchStateError("capsule import project authority disagrees")
        return BatchProjectMutationDurabilityReceipt._issue(
            issuer=self._project_durability_capability,
            durability_kind="capsule_import",
            workspace_lease_binding_sha256=self._workspace_lease_binding_sha256(lease),
            acquisition_record_sha256=project_lease.acquisition_record_sha256,
            acquisition_session_challenge_sha256=(
                project_lease.acquisition_session_challenge_sha256
            ),
            artifact_resolution_evidence_sha256=(project_lease.artifact_resolution_evidence_sha256),
            mutation_request_sha256=project_lease.mutation_request_sha256,
            mutation_authorization_sha256=(project_lease.mutation_authorization_sha256),
            artifact_inventory_sha256=project_lease.artifact_inventory_sha256,
            project_identity_sha256=head.project_identity_sha256,
            project_fencing_token_sha256=project_lease.fencing_token_sha256,
            project_authority_head_sha256=head.project_authority_head_sha256,
            mutation_attestation_sha256=head.project_mutation_attestation_sha256,
            terminal_attempt_record_sha256s=(),
            committed_batch_state_head_sha256=head.sha256,
        )

    @_guarded_batch_state_lease_operation
    def pending_training_settings_revision(
        self,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
    ) -> TrainingSettingsRevision | None:
        """Project one current accepted settings change before any new admission."""

        from mif_pipeline.pipeline_core.stage_result import INTERACTION_ACTION_SCHEMA_VERSION
        from .batch_training.record_publication import validate_training_fit_review_context

        self._assert_lease(lease)
        head = self.load_head()
        if head.sha256 != expected_head_sha256:
            raise BatchHeadConflictError("classifier settings revision head changed")
        previous = self._load_plan_snapshot(head.active_plan_sha256)
        review_kinds = {
            "primary_classifier_training_review": "primary_classifier_training_plan_review",
            "secondary_classifier_training_review": "secondary_classifier_training_plan_review",
            "supervised_phenotype_training_review": "supervised_phenotype_training_plan_review",
        }
        for node_id in sorted(head.pending_authorization_sha256_by_node):
            raw = previous.node_by_id.get(node_id)
            if raw is None or raw["kind"] not in review_kinds:
                continue
            authorization = self.resolve_pending_authorization(
                _plan_node_from_mapping(raw),
                head.sha256,
                lease,
            )
            if authorization is None:
                raise BatchStateError("classifier settings authorization is no longer pending")
            response = self.load_validated_interaction_response(authorization)
            if response is None or response["action"] != "retrain_classifier":
                continue
            predecessor = self.load_attempt(
                authorization.interaction_predecessor_terminal_record_sha256
            )
            review = validate_training_fit_review_context(predecessor.stage_result.resume_context)
            body = _mapping(response["response"], "classifier settings response")
            if (
                response["response_validator_id"] != "mif-training-fit-review-response"
                or response["response_validator_version"] != INTERACTION_ACTION_SCHEMA_VERSION
                or review["phase"] not in {"freeze_settings", "configure_outputs"}
                or body.get("schema_version") != "classifier-settings-change/v1"
                or body.get("fit_review_sha256") != review["fit_review_sha256"]
                or (
                    head.pending_training_fit_set_sha256_by_node.get(node_id)
                    != review.get("checkpoint_set_sha256")
                )
            ):
                raise BatchStateError(
                    "classifier settings change differs from the current reviewed fit"
                )
            if review["phase"] == "configure_outputs":
                from .batch_training.output_review import validate_output_policy_change

                validate_output_policy_change(body, review=review)
            elif "output_policy_by_classifier_id" in body:
                raise BatchStateError("A fitted classifier review cannot amend output classes")
            plan_nodes = [
                previous.node_by_id[item]
                for item in raw["dependency_ids"]
                if previous.node_by_id[item]["kind"] == review_kinds[raw["kind"]]
            ]
            if len(plan_nodes) != 1:
                raise BatchStateError(
                    "classifier settings change lacks one predecessor plan review"
                )
            old = plan_nodes[0]
            old_id = old["node_id"]
            if head.node_status_by_id.get(old_id) is not StageStatus.COMPLETED:
                raise BatchStateError("classifier settings predecessor plan is not completed")
            attempt = self.load_attempt(
                head.terminal_attempt_sha256_by_attempt_id[head.attempt_ids_by_node[old_id][-1]]
            )
            outputs = tuple(
                item
                for item in attempt.output_artifacts
                if item.artifact_kind == "training_plan_set"
            )
            if (
                len(outputs) != 1
                or outputs[0].owning_identity["training_plan_set_sha256"]
                != review["training_plan_set_sha256"]
                or outputs[0].artifact_ref_sha256 not in head.available_artifact_ref_sha256_by_key
            ):
                raise BatchStateError("classifier settings predecessor plan is not current")
            prior_revision = old.get("training_settings_revision")
            adjusted = bool(prior_revision and prior_revision["adjusted_after_validation"]) or any(
                metric["split"] == "validation"
                for classifier in review["classifiers"]
                for metric in classifier.get("split_metrics", ())
            )
            return TrainingSettingsRevision(
                predecessor_plan_review_node_id=old_id,
                predecessor_training_plan_set_sha256=review["training_plan_set_sha256"],
                accepted_response_sha256=response["response_sha256"],
                response_authorization_sha256=authorization.authorization_sha256,
                predecessor_review_sha256=review["fit_review_sha256"],
                adjusted_after_validation=adjusted,
            )
        return None

    def _validate_training_settings_revision_transition(
        self,
        previous: _PlanSnapshot,
        plan: BatchPlan,
        head: BatchStateHead,
        lease: BatchWorkspaceLease,
    ) -> None:
        prior = {
            (raw["kind"], raw["validation_domain_id"]): raw
            for raw in previous.node_by_id.values()
            if "training_settings_revision" in raw
        }
        introduced = []
        for node in plan.nodes:
            old = prior.get((node.kind, node.validation_domain_id))
            binding = node.training_settings_revision
            old_mapping = None if old is None else old["training_settings_revision"]
            if binding is None:
                if old_mapping is not None:
                    raise BatchStateError(
                        "plan revision cannot discard classifier settings history"
                    )
                continue
            if binding.to_mapping() == old_mapping:
                continue
            if old is None or binding.predecessor_plan_review_node_id != old["node_id"]:
                raise BatchStateError("classifier settings revision has a foreign predecessor")
            introduced.append(binding)
        if introduced and (
            len(introduced) != 1
            or introduced[0] != self.pending_training_settings_revision(head.sha256, lease)
        ):
            raise BatchStateError(
                "classifier settings revision lacks its exact current accepted response"
            )

    def _measurement_revision_after_correction(self, previous, node, head):
        """Authenticate choices restored after this section's correction deferred measurement."""
        from .batch_cell_corrections import (CellCorrectionRequest,
            read_correction_submission, measurement_revision_for_correction)

        refs, _ = self._current_artifact_material(head, self._load_typed_plan(previous.plan_sha256))
        bases = {ref.artifact_ref_sha256:ref for ref in refs if ref.artifact_kind == 'segmentation_result'}
        corrections = [ref for ref in refs if ref.artifact_kind == 'segmentation_correction_result'
            and ref.producer_node_id in node.dependency_ids and ref.artifact_id in node.input_artifact_ids]
        if len(corrections) != 1:
            return None
        correction = corrections[0]
        raw = previous.node_by_id[correction.producer_node_id]
        if (raw['kind'] != 'segmentation_correction'
                or raw['section_target'] != node.section_target.to_mapping()
                or tuple(raw['participant_ids']) != node.participant_ids
                or tuple(raw['project_entry_ids']) != node.project_entry_ids
                or raw['validation_domain_id'] != node.validation_domain_id):
            return None
        selected = CellCorrectionRequest.from_mapping(raw['cell_correction'])
        base = bases.get(selected.segmentation_result_ref_sha256)
        if (base is None or base.artifact_id not in node.input_artifact_ids
                or CellCorrectionRequest.from_mapping(correction.owning_identity['correction_request']) != selected):
            return None
        return measurement_revision_for_correction(self, selected,
            read_correction_submission(self, selected), base)

    def _validate_section_settings_revision_transition(self, previous, plan, head) -> None:
        """Keep selection history and require a settled current predecessor."""
        for kind, field in (("segmentation", "segmentation_settings_revision"),
                            ("measurements", "measurement_settings_revision")):
            old_sections = {
                (tuple(raw["participant_ids"]), raw["section_target"]["section_id"]): raw
                for raw in previous.node_by_id.values() if raw["kind"] == kind
            }
            for node in plan.nodes:
                if node.kind != kind:
                    continue
                old = old_sections.get((node.participant_ids, node.section_target.section_id))
                binding = getattr(node, field)
                if binding is None:
                    if old is not None and old.get(field) is not None:
                        raise BatchStateError(f"plan revision cannot discard {kind} settings history")
                    continue
                if old is not None and old.get(field) == binding.to_mapping():
                    continue
                if (old is None and kind == 'measurements'
                        and binding == self._measurement_revision_after_correction(previous, node, head)):
                    continue
                if old is None or old["node_id"] != binding.predecessor_node_id:
                    raise BatchStateError(f"{kind} revision lacks its exact current predecessor")
                prior = old.get(field)
                ordinal = 1 if prior is None else prior["revision"] + 1
                if binding.revision != ordinal or head.node_status_by_id.get(old["node_id"]) not in {
                    StageStatus.COMPLETED, StageStatus.FAILED
                } or old["node_id"] in head.pending_authorization_sha256_by_node:
                    raise BatchStateError(f"{kind} revision requires a settled completed or failed task")
                if kind == "measurements" and binding.supervised_reference_predecessor is not None:
                    reference = binding.supervised_reference_predecessor
                    if prior is not None and prior.get("supervised_reference_predecessor") == reference.to_mapping():
                        continue
                    source = previous.node_by_id.get(reference.predecessor_node_id)
                    attempts = head.attempt_ids_by_node.get(reference.predecessor_node_id, ())
                    previous_reference = None if source is None else source.get("settled_review_revision")
                    if (source is None or source["kind"] != "supervised_phenotype_training_plan_review"
                            or source["validation_domain_id"] != node.validation_domain_id
                            or not set(node.participant_ids) <= set(source["participant_ids"])
                            or source["node_id"] not in self._transitive_dependants(previous, {old["node_id"]})
                            or reference.revision != (1 if previous_reference is None else previous_reference["revision"] + 1)
                            or head.node_status_by_id.get(source["node_id"]) is not StageStatus.COMPLETED
                            or source["node_id"] in head.invalidated_node_ids
                            or source["node_id"] in head.pending_authorization_sha256_by_node
                            or not attempts or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
                            != reference.predecessor_terminal_record_sha256):
                        raise BatchStateError("Measurement references require the exact current completed affected setup")
                    terminal = self.load_attempt(reference.predecessor_terminal_record_sha256)
                    if terminal.node_id != source["node_id"] or len(tuple(
                            ref for ref in terminal.output_artifacts if ref.artifact_kind == "training_plan_set")) != 1:
                        raise BatchStateError("Measurement references lack their completed training plan")

    def settled_review_predecessor(self, context, *, execution, node_kind=None):
        """Reopen existing records admitted by a current settled-review return.

        A dependent stage may reuse its prior review material after an ancestor
        decision is reopened. Retired outputs remain history, never available
        scientific inputs. Current task, lineage and original scope are checked
        here; the native owner then reopens its ordinary recorded exchange.
        """
        from .batch_artifacts import validate_current_execution_authority

        execution = validate_current_execution_authority(execution, expected_context=context)
        head = self.load_head()
        if head.sha256 != execution.state_head_sha256:
            raise BatchStateError("settled review state changed after current admission")
        current = self._load_plan_snapshot(execution.active_plan_sha256)
        target = current.node_by_id[context.node_id]
        source_kind = target["kind"] if node_kind is None else node_kind
        if source_kind not in SETTLED_REVIEW_KINDS:
            raise BatchStateError("settled review predecessor has no supported stage owner")
        from .batch_settled_review_revision import REGION_SCOPED_REVIEW_KINDS, revision_root
        root = revision_root(current, context.node_id)
        if root is None:
            return None
        lineage = current
        visited_plans, visited_roots = set(), set()
        while True:
            binding = SettledReviewRevision.from_mapping(root["settled_review_revision"])
            if binding.predecessor_node_id in visited_roots:
                raise BatchStateError("settled review predecessor history contains a cycle")
            visited_roots.add(binding.predecessor_node_id)
            root_terminal = self.load_attempt(binding.predecessor_terminal_record_sha256)
            root_attempts = head.attempt_ids_by_node.get(binding.predecessor_node_id, ())
            if (root_terminal.node_id != binding.predecessor_node_id
                    or (root_terminal.stage_result.status not in {StageStatus.COMPLETED, StageStatus.FAILED}
                        and not (source_kind == "supervised_phenotype_inference"
                                 and _pending_supervised_prediction(root_terminal)))
                    or not root_attempts
                    or head.terminal_attempt_sha256_by_attempt_id.get(root_attempts[-1])
                    != binding.predecessor_terminal_record_sha256):
                raise BatchStateError("settled review lacks its exact retained current predecessor")
            while binding.predecessor_node_id not in lineage.node_by_id:
                if lineage.plan_sha256 in visited_plans or lineage.predecessor_plan_sha256 is None:
                    raise BatchStateError("settled review predecessor is absent from admitted plan history")
                visited_plans.add(lineage.plan_sha256)
                lineage = self._load_plan_snapshot(lineage.predecessor_plan_sha256)
            original_root = lineage.node_by_id[binding.predecessor_node_id]
            if review_key(original_root) != review_key(root):
                raise BatchStateError("settled review original scope differs")
            sources = tuple(raw for raw in lineage.node_by_id.values()
                if raw["kind"] == source_kind
                and raw["validation_domain_id"] == target["validation_domain_id"]
                and tuple(raw["participant_ids"]) == tuple(target["participant_ids"])
                and (source_kind not in REGION_SCOPED_REVIEW_KINDS or review_key(raw) == review_key(target)))
            if len(sources) > 1:
                raise BatchStateError("settled review source is ambiguous in its admitted predecessor plan")
            terminal, authorization = None, None
            if sources:
                source = sources[0]
                attempts = head.attempt_ids_by_node.get(source["node_id"], ())
                if attempts:
                    terminal_sha = head.terminal_attempt_sha256_by_attempt_id[attempts[-1]]
                    terminal = root_terminal if terminal_sha == binding.predecessor_terminal_record_sha256 else self.load_attempt(terminal_sha)
                    authorization = self.load_authorization(terminal.authorization_sha256)
                    unfinished_tissue_review = (
                        source_kind == "tissue_candidate"
                        and source["node_id"] != binding.predecessor_node_id
                        and terminal.stage_result.status is StageStatus.NEEDS_INTERACTION
                        and not terminal.output_artifacts
                    )
                    pending_prediction = (source_kind == "supervised_phenotype_inference"
                                          and _pending_supervised_prediction(terminal))
                    if (terminal.node_id != source["node_id"] or authorization.node_id != source["node_id"]
                            or terminal.attempt_id != attempts[-1]
                            or (terminal.stage_result.status not in {StageStatus.COMPLETED, StageStatus.FAILED}
                                and not unfinished_tissue_review and not pending_prediction)
                            or authorization.dispatch_binding.participant_ids != tuple(source["participant_ids"])
                            or authorization.dispatch_binding.project_entry_ids != tuple(source["project_entry_ids"])
                            or source["adapter_spec_sha256"] != context.adapter_spec.spec_sha256):
                        raise BatchStateError("settled review retained producer association differs")
                    if terminal.stage_result.status is StageStatus.COMPLETED or pending_prediction:
                        return terminal, authorization
                    if unfinished_tissue_review:
                        # Returning to setup or resolution can supersede a pending
                        # candidate review. It supplies no accepted tissue to reuse.
                        terminal, authorization = None, None
            if "settled_review_revision" not in original_root:
                return None if terminal is None else (terminal, authorization)
            # A failed or unstarted replacement has no new accepted tissue. Its
            # earlier reviewed manifest remains the exact admitted history.
            root = original_root

    def _validate_settled_review_revision_transition(self, previous, plan, head) -> None:
        """Keep redo lineage tied to its exact latest completed or failed review."""
        old_reviews = {review_key(raw): raw
            for raw in previous.node_by_id.values() if raw["kind"] in SETTLED_REVIEW_KINDS}
        tissue_kinds = {"tissue_setup", "tissue_resolution", "tissue_candidate"}
        tissue_roots = {
            node.settled_review_revision.predecessor_node_id
            for node in plan.nodes
            if node.kind in tissue_kinds and node.settled_review_revision is not None
            and (old_reviews.get(review_key(node), {}).get("settled_review_revision")
                 != node.settled_review_revision.to_mapping())
        }
        superseded_tissue = self._transitive_dependants(previous, tissue_roots) - tissue_roots
        for node in plan.nodes:
            if node.kind not in SETTLED_REVIEW_KINDS:
                continue
            old = old_reviews.get(review_key(node))
            binding = node.settled_review_revision
            if binding is None:
                if (old is not None and old.get("settled_review_revision") is not None
                        and not (node.kind in tissue_kinds and old["node_id"] in superseded_tissue)
                        and (previous.normalized_manifest_sha256 == plan.normalized_manifest_sha256
                             or old["configuration_bindings"] == [item.to_mapping() for item in node.configuration_bindings])):
                    raise BatchStateError("plan revision cannot discard settled review history")
                continue
            if old is not None and old.get("settled_review_revision") == binding.to_mapping():
                continue
            if node.kind == "supervised_phenotype_training_plan_review" and any(
                    raw["kind"] == "measurements"
                    and raw["validation_domain_id"] == node.validation_domain_id
                    and set(raw["participant_ids"]) <= set(node.participant_ids)
                    and raw.get("measurement_settings_revision", {}).get("supervised_reference_predecessor") == binding.to_mapping()
                    for raw in previous.node_by_id.values()):
                # This exact pointer was admitted with the measurement change.
                # It survives a deferred setup without reopening earlier plans.
                attempts = head.attempt_ids_by_node.get(binding.predecessor_node_id, ())
                terminal = self.load_attempt(binding.predecessor_terminal_record_sha256)
                if (not attempts or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
                        != binding.predecessor_terminal_record_sha256
                        or terminal.node_id != binding.predecessor_node_id
                        or terminal.stage_result.status is not StageStatus.COMPLETED
                        or len(tuple(ref for ref in terminal.output_artifacts
                                     if ref.artifact_kind == "training_plan_set")) != 1):
                    raise BatchStateError("Carried references require their exact completed setup terminal")
                continue
            attempts = () if old is None else head.attempt_ids_by_node.get(old["node_id"], ())
            prior = None if old is None else old.get("settled_review_revision")
            pending_prediction = False
            if (old is not None and node.kind == "supervised_phenotype_inference"
                    and head.node_status_by_id.get(old["node_id"]) is StageStatus.NEEDS_INTERACTION
                    and attempts and head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
                    == binding.predecessor_terminal_record_sha256):
                terminal = self.load_attempt(binding.predecessor_terminal_record_sha256)
                pending_prediction = terminal.node_id == old["node_id"] and _pending_supervised_prediction(terminal)
            if (old is None or old["node_id"] != binding.predecessor_node_id
                    or binding.revision != (1 if prior is None else prior["revision"] + 1)
                    or (head.node_status_by_id.get(old["node_id"]) not in {StageStatus.COMPLETED, StageStatus.FAILED}
                        and not pending_prediction
                        and not (node.kind == "reference_unmixing" and head.node_status_by_id.get(old["node_id"]) is StageStatus.NEEDS_INTERACTION))
                    or old["node_id"] in head.pending_authorization_sha256_by_node
                    or old["node_id"] in head.invalidated_node_ids
                    or not attempts or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
                    != binding.predecessor_terminal_record_sha256):
                raise BatchStateError("settled review revision requires its exact settled latest predecessor")

    def _validate_supervised_cutoff_transition(self, previous, plan, head) -> None:
        """Keep one scoped assignment choice and its settled section returns together."""
        before, after = previous.supervised_cutoff_selections, plan.supervised_cutoff_selections
        if before == after:
            return
        choices = tuple(item for item in after if merge_selection(before, item) == after)
        if len(choices) != 1:
            raise BatchStateError("Apply one image or processing-group assignment choice at a time.")
        selection = choices[0]
        manifest = self.retained_manifest(head.normalized_manifest_sha256)
        samples = {sample.sample_id: sample for sample in manifest.enabled_samples
                   if sample.validation_domain_id == selection.processing_group_id}
        if not samples:
            raise BatchStateError("The cutoff processing group is not current.")
        if selection.scope == "image":
            sample_id, entry_id, slide_id = selection.image_identity
            sample = samples.get(sample_id)
            if (sample is None or sample.slide_id != slide_id
                    or head.sample_project_entry_id_by_sample.get(sample_id) != entry_id):
                raise BatchStateError("The cutoff image does not belong to its current processing group.")
            samples = {sample_id: sample}
        targets = {raw["node_id"] for raw in previous.node_by_id.values()
                   if raw["kind"] == "supervised_phenotype_inference"
                   and raw["validation_domain_id"] == selection.processing_group_id
                   and len(raw["participant_ids"]) == 1 and raw["participant_ids"][0] in samples}
        affected = self._transitive_dependants(previous, targets)
        if affected.intersection(head.pending_authorization_sha256_by_node):
            raise BatchStateError("Finish retained work before changing these assignments.")
        required = {key for key in targets if head.node_status_by_id.get(key) in {
                    StageStatus.COMPLETED, StageStatus.NEEDS_INTERACTION}}
        revised = {node.settled_review_revision.predecessor_node_id
                   for node in plan.nodes if node.kind == "supervised_phenotype_inference"
                   and node.settled_review_revision is not None}
        if not required <= revised:
            raise BatchStateError("The cutoff must revise every current assignment in its selected scope.")

    def _validate_saved_result_retirement_transition(self, previous, plan, head) -> None:
        """Cover every affected saved result before accepting a revised root."""
        from .batch_saved_result_retirement import retirement_kinds, revision_predecessor

        prior_nodes = {key: raw for key, raw in previous.node_by_id.items() if "saved_result_retirement" in raw}
        planned = {node.node_id: node for node in plan.nodes}
        origins = {revision_predecessor(node) for node in plan.nodes}
        for key, old in prior_nodes.items():
            origin = old["saved_result_retirement"]["predecessor_node_id"]
            if origin in origins and (key not in planned or planned[key].to_mapping() != dict(old)):
                raise BatchStateError("plan revision cannot discard its unfinished or completed saved result retirement")
        introduced = {}
        for node in plan.nodes:
            if node.saved_result_retirement is None or node.node_id in prior_nodes:
                continue
            binding = node.saved_result_retirement
            if binding.predecessor_plan_sha256 != previous.plan_sha256:
                raise BatchStateError("saved result retirement lacks its exact current predecessor plan")
            introduced.setdefault(binding.predecessor_node_id, []).append(node)
        revised = {revision_predecessor(node): node for node in plan.nodes
                   if revision_predecessor(node) in previous.node_by_id
                   and (node.cell_correction is None or node.node_id not in previous.node_by_id)}
        if set(introduced) - set(revised):
            raise BatchStateError("saved result retirement is not bound to an introduced scientific revision")
        for origin, root in revised.items():
            affected = self._transitive_dependants(previous, {origin})
            expected = []
            for key in sorted(affected):
                old = previous.node_by_id[key]
                if old["kind"] not in retirement_kinds(root.kind) or head.node_status_by_id.get(key) is not StageStatus.COMPLETED:
                    continue
                attempts = head.attempt_ids_by_node.get(key, ())
                if not attempts:
                    raise BatchStateError("affected saved result lacks its completed attempt")
                attempt = self.load_attempt(head.terminal_attempt_sha256_by_attempt_id[attempts[-1]])
                if len(attempt.output_artifacts) != 1:
                    raise BatchStateError("affected saved result lacks one saved-result artifact")
                ref = attempt.output_artifacts[0]
                expected.append(ref)
            targets = introduced.get(origin, ())
            actual = tuple(ref for target in targets for ref in target.saved_result_retirement.sources)
            required_retirements = {target.node_id for target in targets}
            if root.cell_correction is not None:
                # Joint corrections can share a completed phenotype fit. Its
                # per-section applications are retired once across the group;
                # each correction still waits for its own retirement tasks.
                expected_refs = {ref.artifact_ref_sha256 for ref in expected}
                actual += tuple(ref for other, other_targets in introduced.items()
                    if other != origin and revised[other].cell_correction is not None
                    for target in other_targets for ref in target.saved_result_retirement.sources
                    if ref.artifact_ref_sha256 in expected_refs)
                required_retirements.update(target.node_id for other_targets in introduced.values()
                    for target in other_targets if any(
                        ref.scope.project_entry_id in root.project_entry_ids
                        and ref.owning_identity.get("section_id") == root.section_target.section_id
                        for ref in target.saved_result_retirement.sources))
            if (tuple(sorted(actual, key=lambda ref: ref.artifact_ref_sha256))
                    != tuple(sorted(expected, key=lambda ref: ref.artifact_ref_sha256))
                    or len({target.project_entry_ids for target in targets}) != len(targets)):
                raise BatchStateError("saved result retirement must cover every affected saved result once per entry")
            if not required_retirements <= set(root.dependency_ids):
                raise BatchStateError("revised task must wait for every affected entry saved result retirement")

    def _validate_cell_correction_transition(self, previous, plan, head):
        """Supersede the reviewed section's idle measurement task or completion."""
        from .batch_cell_corrections import read_correction_submission
        introduced = [node for node in plan.nodes
                      if node.cell_correction is not None and node.node_id not in previous.node_by_id]
        if (len({node.cell_correction.segmentation_result_ref_sha256 for node in introduced}) != len(introduced)
                or len({node.cell_correction.predecessor_node_id for node in introduced}) != len(introduced)):
            raise BatchStateError("Review each section's corrections once before accepting them")
        planned = {node.node_id for node in plan.nodes}
        for old in previous.node_by_id.values():
            if "cell_correction" not in old:
                continue
            base_nodes = {item for item in old["dependency_ids"]
                          if previous.node_by_id[item]["kind"] == "segmentation"}
            if base_nodes & planned and old["node_id"] not in planned:
                raise BatchStateError("A current segmentation must retain its accepted correction chain")
        roots = set()
        for node in introduced:
            selected = node.cell_correction
            old = previous.node_by_id.get(selected.predecessor_node_id)
            body = read_correction_submission(self, selected)
            if (old is None or old["kind"] not in {"segmentation", "measurements"}
                    or old.get("section_target") != node.section_target.to_mapping()
                    or tuple(old["participant_ids"]) != node.participant_ids
                    or body["plan_sha256"] != previous.plan_sha256
                    or body["state_head_sha256"] not in (*head.ancestor_head_sha256s, head.sha256)
                    or selected.segmentation_result_ref_sha256 not in head.available_artifact_ref_sha256_by_key
                    or (selected.previous_correction_ref_sha256 is not None
                        and selected.previous_correction_ref_sha256 not in head.available_artifact_ref_sha256_by_key)
                    or (old["kind"] == "segmentation" and head.node_status_by_id.get(old["node_id"]) is not StageStatus.COMPLETED)
                    or (old["kind"] == "measurements" and head.node_status_by_id.get(old["node_id"]) is StageStatus.FAILED)):
                raise BatchStateError("Cell correction requires its current accepted draft; finish stopped measurements first")
            source = body['scope'].get('measurement_source')
            if old['kind'] == 'measurements' and head.node_status_by_id.get(old['node_id']) is StageStatus.COMPLETED:
                attempts = head.attempt_ids_by_node.get(old['node_id'], ())
                terminal_sha = head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) if attempts else None
                terminal = self.load_attempt(terminal_sha) if terminal_sha else None
                if (terminal is None or len(terminal.output_artifacts) != 1
                        or terminal.output_artifacts[0].artifact_kind != 'measurements_result'
                        or terminal.output_artifacts[0].artifact_ref_sha256 not in head.available_artifact_ref_sha256_by_key
                        or source != dict(terminal_record_sha256=terminal_sha,
                            artifact_ref_sha256=terminal.output_artifacts[0].artifact_ref_sha256)):
                    raise BatchStateError('Cell correction must retain its exact current completed measurement checkpoint')
            elif source is not None:
                raise BatchStateError('Cell correction has an unselected measurement predecessor')
            affected = self._transitive_dependants(previous, {old["node_id"]})
            if set(head.pending_authorization_sha256_by_node) & affected:
                raise BatchStateError("Wait for affected native work before accepting the cell correction")
            roots.add(old["node_id"])
        return roots



    def _validate_memory_limit_plan(self, previous_sha256, current_sha256, manifest, *, typed_plans=None):
        """Verify the exact same graph and saved inputs at a resource-only transition."""
        from .batch_dag import plan_with_memory_limit
        previous = self._load_typed_plan(previous_sha256) if typed_plans is None else typed_plans[previous_sha256]
        current = self._load_typed_plan(current_sha256) if typed_plans is None else typed_plans[current_sha256]
        if current != plan_with_memory_limit(previous, manifest):
            raise BatchStateError("Memory-limit change must retain the exact analysis plan")


    @classmethod
    def _revision_node_sets(
        cls, previous: _PlanSnapshot, successor: _PlanSnapshot, head: BatchStateHead, *,
        resolved_config_sha256_by_sample: Mapping[str, str] | None = None,
    ) -> tuple[set[str], set[str], tuple[str, ...]]:
        """Find changed work and reusable completed nodes for a plan revision.

        We follow changed nodes through the old dependencies, then check the
        successor's recorded image, region and settings inputs against the saved
        state. Preview and commit use this same calculation without reading images
        or changing either plan. Source replacement can supply its new settings
        digests; declaration-only revisions retain the existing ones.
        """
        changed_roots = {
            node_id
            for node_id, raw in previous.node_by_id.items()
            if node_id not in successor.node_by_id or dict(successor.node_by_id[node_id]) != dict(raw)
        }
        invalidated = cls._transitive_dependants(previous, changed_roots)
        incompatible_roots = {
            node_id
            for node_id, node in successor.node_by_id.items()
            if not cls._node_matches_material_authority(node, head,
                resolved_config_sha256_by_sample=resolved_config_sha256_by_sample)
        }
        incompatible = cls._transitive_dependants(successor, incompatible_roots)
        invalidated.update(incompatible)
        active_invalidated = (
            set(head.invalidated_node_ids) & set(successor.node_by_id)
        ) | incompatible
        reusable = tuple(
            sorted(
                node_id
                for node_id, status in head.node_status_by_id.items()
                if status is StageStatus.COMPLETED
                and node_id in successor.node_by_id
                and node_id not in invalidated
            )
        )
        return invalidated, active_invalidated, reusable

    def preview_reusable_completed_nodes(self, plan: BatchPlan, *,
                                         expected_head_sha256: str) -> tuple[str, ...]:
        """Identify completed work retained by the next plan without publishing it.

        Software checks use these IDs to skip operations that will reuse saved
        results. The caller must still submit the revision through revise_plan,
        which checks its authorization and inputs again before updating progress.
        """
        head = self.load_head()
        if head.sha256 != expected_head_sha256:
            raise BatchHeadConflictError("plan revision head changed")
        if not isinstance(plan, BatchPlan):
            raise BatchStateError("plan preview requires a BatchPlan")
        previous = self._load_plan_snapshot(head.active_plan_sha256)
        successor = _plan_snapshot_from_bytes(_canonical_bytes(plan.to_mapping()))
        if (successor.predecessor_plan_sha256 != previous.plan_sha256
                or successor.revision != previous.revision + 1
                or successor.batch_id != previous.batch_id):
            raise BatchStateError("plan preview is not the exact next successor")
        return self._revision_node_sets(previous, successor, head)[2]

    @_guarded_batch_state_lease_operation
    def revise_plan(
        self,
        plan: BatchPlan,
        reason: str,
        expected_head_sha256: str,
        lease: BatchWorkspaceLease,
        *,
        manifest: ValidatedBatchManifest | None = None,
        source_selection=None,
        classifier_setup_projection=None,
        secondary_definition_projection=None,
    ) -> BatchPlanRevisionResult:
        self._assert_lease(lease)
        reason = _text(reason, "plan revision reason")
        expected = _sha256(expected_head_sha256, "expected_head_sha256")
        head = self.load_head()
        if head.sha256 != expected:
            raise BatchHeadConflictError("plan revision head changed")
        previous = self._load_plan_snapshot(head.active_plan_sha256)
        source_updates = {}
        declaration_updates = {}
        if classifier_setup_projection is not None:
            from .batch_training import ValidatedTrainingPlanSetProjection
            from .batch_training.accepted_model_reuse import manifest_for_classifier_setup, validate_classifier_manifest_transition
            if type(classifier_setup_projection) is not ValidatedTrainingPlanSetProjection:
                raise BatchStateError("Saved classifier selection requires its accepted setup record")
            projection = classifier_setup_projection
            authority = self.authenticate_available_artifact(projection.artifact_ref)
            if (projection.state_head_sha256 != head.sha256 or projection.active_plan_sha256 != head.active_plan_sha256
                    or projection.terminal_record_sha256 != authority.terminal_record_sha256
                    or projection.producer_node_kind != "primary_classifier_training_plan_review"
                    or manifest is None or manifest.normalized_manifest_sha256 != plan.normalized_manifest_sha256):
                raise BatchStateError("Saved classifier setup changed before applying its model selection")
            expected_manifest = manifest_for_classifier_setup(self.retained_manifest(head.normalized_manifest_sha256), projection)
            validate_classifier_manifest_transition(self.retained_manifest(head.normalized_manifest_sha256), manifest)
            if expected_manifest.normalized_manifest_bytes != manifest.normalized_manifest_bytes:
                raise BatchStateError("Saved classifier declaration differs from the accepted setup")
            if head.pending_authorization_sha256_by_node:
                raise BatchStateError("Finish retained native work before applying a saved classifier selection")
            self._retain_manifest(manifest)
            declaration_updates = dict(original_manifest_sha256=manifest.original_manifest_sha256,
                                      normalized_manifest_sha256=manifest.normalized_manifest_sha256)
        if secondary_definition_projection is not None:
            from .batch_secondary_definitions import (
                ValidatedSecondaryDefinitionSelection, decode_committed_secondary_definition_selection,
                manifest_for_secondary_definitions,
            )
            projection = secondary_definition_projection
            if (classifier_setup_projection is not None or source_selection is not None
                    or type(projection) is not ValidatedSecondaryDefinitionSelection):
                raise BatchStateError("Criterion profile publication requires only its accepted definition choice")
            authority = self.authenticate_available_artifact(projection.artifact_ref)
            if (decode_committed_secondary_definition_selection(authority, state_store=self) != projection
                    or projection.state_head_sha256 != head.sha256
                    or projection.active_plan_sha256 != head.active_plan_sha256
                    or manifest is None or manifest.normalized_manifest_sha256 != plan.normalized_manifest_sha256):
                raise BatchStateError("Secondary definitions changed before publishing their criterion profiles")
            expected_manifest = manifest_for_secondary_definitions(
                self.retained_manifest(head.normalized_manifest_sha256), projection)
            if expected_manifest.normalized_manifest_bytes != manifest.normalized_manifest_bytes:
                raise BatchStateError("Criterion profile declarations differ from the accepted definitions")
            if head.pending_authorization_sha256_by_node:
                raise BatchStateError("Finish retained native work before publishing criterion profiles")
            # Returning to an earlier choice can recreate its normalized settings
            # from differently formatted JSON. Keep that generation's original
            # source bytes instead of trying to replace its immutable record.
            if self.layout.manifest_paths(manifest.normalized_manifest_sha256)[1].exists():
                manifest = self.retained_manifest(manifest.normalized_manifest_sha256)
            self._retain_manifest(manifest)
            declaration_updates = dict(original_manifest_sha256=manifest.original_manifest_sha256,
                                       normalized_manifest_sha256=manifest.normalized_manifest_sha256)
        if plan.normalized_manifest_sha256 != head.normalized_manifest_sha256 and not declaration_updates:
            from .batch_source_revision import source_revision_sample
            if (head.operational_rebinding_sha256 is not None
                    or self.layout.root != self.retained_manifest(head.normalized_manifest_sha256).manifest.output_root / head.batch_id):
                raise BatchStateError("Correct the image in the original analysis folder; relocated source correction is not available.")
            if manifest is None or manifest.normalized_manifest_sha256 != plan.normalized_manifest_sha256:
                raise BatchStateError("Source correction lacks its exact new manifest.")
            sample_id = source_revision_sample(self.retained_manifest(head.normalized_manifest_sha256), manifest, head.source_sha256_by_sample)
            if head.pending_authorization_sha256_by_node:
                raise BatchStateError("Source correction requires all retained native attempts to be settled.")
            from .batch_source_revision import source_processing_root
            inspection = next((node for node in plan.nodes if node.kind == "inspection" and node.participant_ids == (sample_id,)), None)
            selected_sample = next(item for item in manifest.manifest.samples if item.sample_id == sample_id)
            from .batch_source_revision import SourceSelection
            if (type(source_selection) is not SourceSelection or source_selection.to_mapping() != {
                    key: selected_sample.to_mapping()[key] for key in source_selection.to_mapping()}):
                raise BatchStateError("Source correction requires the exact inspected image selection.")
            if inspection is None or inspection.settled_review_revision is None or selected_sample.processing_root != source_processing_root(
                    manifest, sample_id, inspection.settled_review_revision, self.layout.root):
                raise BatchStateError("Source correction must use its inspection return below this analysis folder.")
            self._retain_manifest(manifest)
            sources, configurations = dict(head.source_sha256_by_sample), dict(head.resolved_config_sha256_by_sample)
            sample = next(item for item in manifest.samples if item.sample_id == sample_id)
            sources[sample_id], configurations[sample_id] = sample.source_file_sha256, sample.resolved_config_sha256
            source_updates = dict(original_manifest_sha256=manifest.original_manifest_sha256,
                normalized_manifest_sha256=manifest.normalized_manifest_sha256,
                source_sha256_by_sample=sources, resolved_config_sha256_by_sample=configurations)
        self._assert_lease(lease)
        self._validate_training_settings_revision_transition(previous, plan, head, lease)
        self._validate_section_settings_revision_transition(previous, plan, head)
        self._validate_settled_review_revision_transition(previous, plan, head)
        self._validate_supervised_cutoff_transition(previous, plan, head)
        corrected_roots = self._validate_cell_correction_transition(previous, plan, head)
        self._validate_saved_result_retirement_transition(previous, plan, head)
        snapshot = self._install_plan(plan)
        if (
            snapshot.predecessor_plan_sha256 != previous.plan_sha256
            or snapshot.revision != previous.revision + 1
            or snapshot.batch_id != previous.batch_id
            or snapshot.normalized_manifest_sha256 != declaration_updates.get("normalized_manifest_sha256", source_updates.get("normalized_manifest_sha256", previous.normalized_manifest_sha256))
        ):
            raise BatchStateError("plan revision is not the exact next successor")
        if source_updates:
            domain_ids = {binding["analysis_domain_id"] for raw in snapshot.node_by_id.values()
                          for binding in raw["analysis_domain_bindings"]}
            domain_samples = {binding["sample_id"] for raw in snapshot.node_by_id.values()
                              for binding in raw["analysis_domain_bindings"]}
            source_updates.update(
                accepted_domain_revision_sha256_by_id={key: value for key, value in head.accepted_domain_revision_sha256_by_id.items() if key in domain_ids},
                domain_set_sha256_by_sample={key: value for key, value in head.domain_set_sha256_by_sample.items() if key in domain_samples})
        invalidated, active_invalidated, reusable = self._revision_node_sets(
            previous, snapshot, head,
            resolved_config_sha256_by_sample=source_updates.get("resolved_config_sha256_by_sample"),
        )
        # A validated return to a saved decision supersedes its idle dependent
        # reviews. Only newly introduced revisions may replace those reviews.
        revised_reviews = {node.settled_review_revision.predecessor_node_id
            for node in plan.nodes if node.kind in SETTLED_REVIEW_KINDS
            and node.node_id not in previous.node_by_id
            and node.settled_review_revision is not None
            and node.settled_review_revision.predecessor_node_id in previous.node_by_id}
        superseded_reviews = self._transitive_dependants(previous, revised_reviews)
        superseded_reviews |= self._transitive_dependants(previous, corrected_roots)
        pending_interactions = list(head.pending_interaction_ids)
        for request_id in head.pending_interaction_ids:
            try:
                pending_node_id = self._reconstruct_interaction_request_publication(
                    request_id, head
                ).node_id
            except BatchStateError as ordinary_error:
                aggregate_nodes: list[str] = []
                for node_id, node in previous.node_by_id.items():
                    if node["kind"] != "cluster_label_review":
                        continue
                    try:
                        target = self._cluster_label_review_target_for_node(head, node_id)
                    except BatchStateError:
                        continue
                    if target.interaction_request_id == request_id:
                        aggregate_nodes.append(node_id)
                if len(aggregate_nodes) != 1:
                    raise ordinary_error
                pending_node_id = aggregate_nodes[0]
            if (
                pending_node_id in invalidated
                or pending_node_id in active_invalidated
                or pending_node_id not in snapshot.node_by_id
            ):
                if source_updates or pending_node_id in superseded_reviews:
                    pending_interactions.remove(request_id)
                else:
                    raise BatchStateError("plan revision would retire an unresolved pending interaction")
        pending = {
            node_id: digest
            for node_id, digest in head.pending_authorization_sha256_by_node.items()
            if node_id in snapshot.node_by_id and node_id not in invalidated
        }
        fit_sets = {
            node_id: digest
            for node_id, digest in (head.pending_training_fit_set_sha256_by_node.items())
            if node_id in snapshot.node_by_id and node_id not in invalidated
        }
        scopes = dict(head.node_scope_by_id)
        for node_id, node in snapshot.node_by_id.items():
            scopes[node_id] = {
                "sample_ids": list(node["participant_ids"]),
                "validation_domain_id": node["validation_domain_id"],
                "analysis_domain_ids": [
                    item["analysis_domain_id"] for item in node["analysis_domain_bindings"]
                ],
            }
        reasons = dict(head.revision_reason_by_plan_sha256)
        reasons[plan.plan_sha256] = reason
        available = {digest: digest for digest in snapshot.external_input_sha256s}
        for node_id in reusable:
            latest_attempt_id = head.attempt_ids_by_node[node_id][-1]
            latest_attempt = self.load_attempt(
                head.terminal_attempt_sha256_by_attempt_id[latest_attempt_id]
            )
            for output in latest_attempt.output_artifacts:
                prior = available.get(output.artifact_ref_sha256)
                if prior is not None and prior != output.artifact_ref_sha256:
                    raise BatchStateError("reusable completed artifact inventory conflicts")
                available[output.artifact_ref_sha256] = output.artifact_ref_sha256
        candidate = head.evolve(
            active_plan_sha256=plan.plan_sha256,
            plan_history_sha256s=(*head.plan_history_sha256s, plan.plan_sha256),
            node_scope_by_id=scopes,
            pending_authorization_sha256_by_node=pending,
            pending_training_fit_set_sha256_by_node=fit_sets,
            pending_interaction_ids=tuple(pending_interactions),
            available_artifact_ref_sha256_by_key=available,
            invalidated_node_ids=tuple(sorted(active_invalidated)),
            revision_reason_by_plan_sha256=reasons,
            checkpoint_descriptor_sha256_by_node={key: value for key, value in head.checkpoint_descriptor_sha256_by_node.items()
                if key in snapshot.node_by_id and key not in invalidated},
            **source_updates,
            **declaration_updates,
        )
        committed = self._replace_head_cas(
            candidate,
            head.sha256,
            lease,
            allow_inventory_projection=True,
            allow_native_fit_checkpoint=True,
            allow_interaction_requests=bool(source_updates or superseded_reviews),
            classifier_setup_projection=classifier_setup_projection,
            secondary_definition_projection=secondary_definition_projection,
        )
        return BatchPlanRevisionResult(
            head=committed,
            reusable_node_ids=reusable,
            invalidated_node_ids=tuple(sorted(invalidated)),
        )

    @staticmethod
    def _transitive_dependants(snapshot: _PlanSnapshot, roots: set[str]) -> set[str]:
        """Follow the saved plan's dependency edges from all changed roots."""
        return dependent_node_ids(
            ((node_id, node["dependency_ids"]) for node_id, node in snapshot.node_by_id.items()),
            roots,
        )


def BatchStateTransferReviewResponseResolver(
    state_store: BatchStateStore,
    context: BatchNodeExecutionContext,
) -> TransferReviewResponseResolver:
    """Issue a retained resolver from one exact current batch retry."""

    if type(state_store) is not BatchStateStore:
        raise BatchStateError("transfer review resolver requires the exact state owner")
    if type(context) is not BatchNodeExecutionContext:
        raise BatchStateError("transfer review resolver requires exact execution context")
    state_store.authenticate_current_execution_context(context)
    try:
        return _issue_transfer_review_response_resolver(
            state_owner=state_store,
            execution_context=context,
            resolve=state_store._resolve_current_transfer_review_response,
        )
    except TransferReviewAuthorityError as exc:
        raise BatchStateError("transfer review resolver issuance failed") from exc


__all__ = [
    "BatchAttemptAuthorizationRequest",
    "BatchExecutionSnapshot",
    "BatchExecutionState",
    "BatchEffectiveLocatorAuthority",
    "BatchHeadConflictError",
    "BatchInteractionResponsePublication",
    "BatchMaterialAuthorityUpdate",
    "BatchNativeFitCheckpointWriter",
    "BatchNodeAttempt",
    "BatchPlanRevisionResult",
    "BatchProjectMaterialSuccessor",
    "BatchProjectControlContext",
    "BatchProjectStageMaterialSuccessor",
    "BatchNativeFitCheckpointSuccessor",
    "BatchProjectMutationDurabilityReceipt",
    "BatchPreparedNodeResult",
    "BatchRawNodeOutcome",
    "BatchSecurePublicationProvider",
    "BatchStateError",
    "BatchStateHead",
    "BatchStateStore",
    "BatchStateTransferReviewResponseResolver",
    "BatchTerminalCommitter",
    "BatchWorkspaceLayout",
    "BatchWorkspaceLease",
    "BatchWorkspaceLeaseReleaseError",
    "BatchWorkspaceLeaseUse",
    "BatchWorkspaceLockProvider",
    "CurrentNativeFitSetAuthority",
    "NativeFitDurabilityCleanupOutcome",
    "PosixBatchWorkspaceLockProvider",
    "PosixBatchSecurePublicationProvider",
    "NativeWindowsBatchSecurePublicationPrimitives",
    "ScheduledNodeResult",
    "WindowsBatchWorkspaceLockProvider",
    "WindowsBatchSecurePublicationProvider",
]
