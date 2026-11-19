"""Grouped presentation and atomic retry admission for batch interactions.

This module owns no scientific validation or execution.  It groups immutable
request authorities, delegates each selected target to an injected validator,
and asks :class:`BatchStateStore` for one atomic retry-authorization CAS.

The `BatchTargetApplication` record carries `target_id`, `node_id`, `prior_attempt_id`,
`prior_attempt_authorization_sha256`, `prior_terminal_record_sha256` and its remaining
declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Protocol

from mif_pipeline.orchestration.batch_artifacts import AnalysisDomainBinding
from mif_pipeline.orchestration.batch_phenotyping.review_records import (
    BatchClusterReviewEvidence,
)
from mif_pipeline.orchestration.batch_phenotyping.review_target import (
    AggregateClusterReviewTarget,
)
from mif_pipeline.orchestration.batch_state import (
    BatchAttemptAuthorizationRequest,
    BatchInteractionRequestPublication,
    BatchInteractionResponsePublication,
    BatchStateError,
    BatchStateStore,
    BatchWorkspaceLease,
    batch_interaction_request_id,
)
from mif_pipeline.pipeline_core import (
    InteractionReason,
    ROIAnalysisError,
    StageResult,
    StageStatus,
    assert_current_writer,
)

INTERACTION_RESPONSE_SCHEMA_NAME = "mif-batch-interaction-response"
INTERACTION_RESPONSE_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                               Batch interactions
# ==============================================================================


class BatchInteractionError(ROIAnalysisError):
    """Raised when grouped interaction authority is malformed or stale."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchInteractionError(f"{field} must be non-empty canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise BatchInteractionError(f"{field} must be a lowercase SHA-256")
    return digest


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise BatchInteractionError("batch interaction mappings must have string keys")
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
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
        raise BatchInteractionError(
            "batch interaction material must be finite canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _freeze_json(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchInteractionError(f"{field} must be a string-keyed mapping")
    try:
        decoded = json.loads(_canonical_bytes(value).decode("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - canonical encoder owns this
        raise BatchInteractionError(f"{field} is not canonical JSON") from exc

    def freeze(item: object) -> object:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in sorted(item.items())})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    frozen = freeze(decoded)
    assert isinstance(frozen, Mapping)
    return frozen


def _unique_text_tuple(value: Sequence[object], field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise BatchInteractionError(f"{field} must be a sequence")
    parsed = tuple(sorted(_text(item, field) for item in value))
    if len(set(parsed)) != len(parsed):
        raise BatchInteractionError(f"{field} must be unique")
    return parsed


def _binding_key(
    binding: AnalysisDomainBinding,
) -> tuple[str, str, str, int, str, str, str]:
    return (
        binding.sample_id,
        binding.slide_id,
        binding.analysis_domain_id,
        binding.analysis_domain_revision,
        binding.analysis_domain_revision_sha256,
        binding.domain_set_sha256,
        binding.project_entry_id,
    )


def _bindings(
    value: Sequence[AnalysisDomainBinding], field: str
) -> tuple[AnalysisDomainBinding, ...]:
    values = tuple(value)
    if not all(isinstance(item, AnalysisDomainBinding) for item in values):
        raise BatchInteractionError(f"{field} must contain typed domain bindings")
    ordered = tuple(sorted(values, key=_binding_key))
    if len({_binding_key(item) for item in ordered}) != len(ordered):
        raise BatchInteractionError(f"{field} must be unique")
    return ordered


def _context_version(result: StageResult) -> str:
    context = result.resume_context or {}
    if "schema_version" not in context:
        raise BatchInteractionError(
            "interaction request requires its owning producer schema_version"
        )
    return _text(context["schema_version"], "resume_context.schema_version")


def _context_sha256(result: StageResult) -> str:
    return _digest(result.to_mapping()["resume_context"])


_BULK_SAFE_REASONS = frozenset(
    {
        InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED.value,
        InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED.value,
        InteractionReason.BIOMARKER_THRESHOLD_REQUIRED.value,
        InteractionReason.PHENOTYPING_MODE_REQUIRED.value,
        InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED.value,
        InteractionReason.PHENOTYPE_CLASSES_REQUIRED.value,
        InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED.value,
        InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED.value,
        InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED.value,
        InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED.value,
    }
)


def _bulk_application_allowed(reason: str) -> bool:
    return reason in _BULK_SAFE_REASONS


@dataclass(frozen=True, slots=True)
class ReferenceOnlyQuPathView:
    """Current owner-issued guidance; never a scientific invocation input."""

    target_id: str
    state_head_sha256: str
    active_plan_sha256: str
    project_path: str
    project_identity_sha256: str
    project_qpproj_snapshot_sha256: str
    registration_result_ref_sha256: str
    primary_project_entry_id: str
    primary_image_sha256: str
    moving_project_entry_id: str
    moving_image_sha256: str
    moving_reference_modality: str
    registration_mode: str = "side_by_side"

    def __post_init__(self):
        if self.registration_mode not in {"side_by_side", "warpy"}:
            raise BatchInteractionError("Reference view mode is invalid")

    @property
    def warning(self) -> str:
        return (
            "Visual reference only; no coordinate transfer. Quantitative pixels "
            "and saved annotations remain on the primary mIF entry."
        )


@dataclass(frozen=True, slots=True)
class BatchInteractionTarget:
    target_id: str
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    needs_interaction_terminal_record_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    project_entry_id: str | None
    sample_id: str
    slide_id: str
    validation_domain_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    compatibility_signature_sha256: str
    stage_result: StageResult
    reference_view: ReferenceOnlyQuPathView | None = None
    tissue_section_summary_error: str | None = None
    saved_spectral_source: Mapping[str, object] | None = None
    region_review: Mapping[str, object] | None = None
    saved_supervised_setup: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the BatchInteractionTarget fields when the record is constructed.

        The checks cover ``project_entry_id``, ``stage_result``,
        ``interaction_context_sha256``, ``interaction_request_id``,
        ``analysis_domain_bindings``, ``reference_view``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.reference_view is not None and (
            not isinstance(self.reference_view, ReferenceOnlyQuPathView)
            or self.reference_view.target_id != self.target_id
        ):
            raise BatchInteractionError("reference view must belong to this exact target")
        for field_name in (
            "target_id",
            "node_id",
            "attempt_id",
            "interaction_request_id",
            "sample_id",
            "slide_id",
            "validation_domain_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if self.project_entry_id is not None:
            object.__setattr__(
                self,
                "project_entry_id",
                _text(self.project_entry_id, "project_entry_id"),
            )
        for field_name in (
            "attempt_authorization_sha256",
            "needs_interaction_terminal_record_sha256",
            "interaction_context_sha256",
            "compatibility_signature_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        if (
            not isinstance(self.stage_result, StageResult)
            or self.stage_result.status is not StageStatus.NEEDS_INTERACTION
            or self.stage_result.slide_id != self.slide_id
        ):
            raise BatchInteractionError("target must carry its exact needs_interaction StageResult")
        _context_version(self.stage_result)
        if _context_sha256(self.stage_result) != self.interaction_context_sha256:
            raise BatchInteractionError("interaction context checksum mismatch")
        try:
            expected_request_id = batch_interaction_request_id(
                node_id=self.node_id,
                attempt_id=self.attempt_id,
                attempt_authorization_sha256=self.attempt_authorization_sha256,
                interaction_context_sha256=self.interaction_context_sha256,
            )
        except BatchStateError as exc:
            raise BatchInteractionError("interaction request identity is invalid") from exc
        if self.interaction_request_id != expected_request_id:
            raise BatchInteractionError("interaction request identity is not canonical")
        request_bytes = _canonical_bytes(self.stage_result.to_mapping())
        if self.needs_interaction_terminal_record_sha256.encode("ascii") in request_bytes:
            raise BatchInteractionError(
                "terminal predecessor cannot be embedded in its own request"
            )
        bindings = _bindings(self.analysis_domain_bindings, "analysis_domain_bindings")
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        if bindings:
            matching = tuple(
                item
                for item in bindings
                if item.sample_id == self.sample_id and item.slide_id == self.slide_id
            )
            if not matching:
                raise BatchInteractionError(
                    "analysis-domain bindings do not cover the target sample and slide"
                )
            if self.project_entry_id is None or any(
                item.project_entry_id != self.project_entry_id for item in matching
            ):
                raise BatchInteractionError(
                    "project entry and analysis-domain binding authority disagree"
                )


def project_batch_interaction_target(
    publication: BatchInteractionRequestPublication,
) -> BatchInteractionTarget:
    """Project the admitted immutable request without decoding its saved representation.

    Current plan and terminal authority remain the state store's responsibility;
    this projection applies the target's supported-interaction policy.
    """

    if not isinstance(publication, BatchInteractionRequestPublication):
        raise BatchInteractionError("interaction target projection requires a typed publication")
    result = publication.stage_result
    if result.interaction_reason is InteractionReason.TRANSFER_COMMIT_RECOVERY_REQUIRED:
        raise BatchInteractionError("interaction publication facts are inconsistent or unsupported")
    return BatchInteractionTarget(
        target_id=publication.interaction_request_id,
        node_id=publication.node_id,
        attempt_id=publication.attempt_id,
        attempt_authorization_sha256=(publication.attempt_authorization_sha256),
        needs_interaction_terminal_record_sha256=(publication.terminal_record_sha256),
        interaction_request_id=publication.interaction_request_id,
        interaction_context_sha256=publication.interaction_context_sha256,
        project_entry_id=publication.project_entry_id,
        sample_id=publication.sample_id,
        slide_id=publication.slide_id,
        validation_domain_id=publication.validation_domain_id,
        analysis_domain_bindings=publication.analysis_domain_bindings,
        compatibility_signature_sha256=(publication.compatibility_signature_sha256),
        stage_result=result,
    )


def _target_identity(target: BatchInteractionTarget) -> dict[str, object]:
    return {
        "target_id": target.target_id,
        "node_id": target.node_id,
        "attempt_id": target.attempt_id,
        "attempt_authorization_sha256": target.attempt_authorization_sha256,
        "needs_interaction_terminal_record_sha256": (
            target.needs_interaction_terminal_record_sha256
        ),
        "interaction_request_id": target.interaction_request_id,
        "interaction_context_sha256": target.interaction_context_sha256,
        "project_entry_id": target.project_entry_id,
        "sample_id": target.sample_id,
        "slide_id": target.slide_id,
        "validation_domain_id": target.validation_domain_id,
        "analysis_domain_bindings": [item.to_mapping() for item in target.analysis_domain_bindings],
        "compatibility_signature_sha256": target.compatibility_signature_sha256,
    }


@dataclass(frozen=True, slots=True)
class BatchInteractionGroup:
    group_id: str
    group_sha256: str
    stage: str
    interaction_reason: str
    action_schema_version: str
    context_version: str
    compatibility_signature_sha256: str
    context_signature_sha256: str | None
    bulk_application_allowed: bool
    targets: tuple[BatchInteractionTarget, ...]

    def __post_init__(self) -> None:
        """Validate the BatchInteractionGroup fields when the record is constructed.

        The checks cover ``context_signature_sha256``, ``group_sha256``,
        ``compatibility_signature_sha256``, ``bulk_application_allowed``, ``targets``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        for field_name in (
            "group_id",
            "stage",
            "interaction_reason",
            "action_schema_version",
            "context_version",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "group_sha256",
            _sha256(self.group_sha256, "group_sha256"),
        )
        object.__setattr__(
            self,
            "compatibility_signature_sha256",
            _sha256(
                self.compatibility_signature_sha256,
                "compatibility_signature_sha256",
            ),
        )
        if self.context_signature_sha256 is not None:
            object.__setattr__(
                self,
                "context_signature_sha256",
                _sha256(self.context_signature_sha256, "context_signature_sha256"),
            )
        if type(self.bulk_application_allowed) is not bool:
            raise BatchInteractionError("bulk_application_allowed must be boolean")
        targets = tuple(sorted(self.targets, key=lambda item: item.target_id))
        if not targets or not all(isinstance(item, BatchInteractionTarget) for item in targets):
            raise BatchInteractionError("interaction group requires typed targets")
        if len({item.target_id for item in targets}) != len(targets):
            raise BatchInteractionError("interaction group target IDs must be unique")
        object.__setattr__(self, "targets", targets)


@dataclass(frozen=True, slots=True)
class BatchAggregateInteractionGroup:
    """Truthful presentation of one pooled cluster-review authority.

    Aggregate review deliberately retains its complete graph target rather
    than inventing a singular image, slide, or validation-domain target.
    """

    target: AggregateClusterReviewTarget
    validation_domain_ids: tuple[str, ...]
    evidence: BatchClusterReviewEvidence

    def __post_init__(self) -> None:
        if type(self.target) is not AggregateClusterReviewTarget:
            raise BatchInteractionError(
                "aggregate interaction group requires its exact graph target"
            )
        if type(self.evidence) is not BatchClusterReviewEvidence:
            raise BatchInteractionError("aggregate interaction group requires exact evidence")
        target = self.target
        projected = AggregateClusterReviewTarget.create(
            node_id=target.node_id,
            attempt_id=target.attempt_id,
            attempt_authorization_sha256=target.attempt_authorization_sha256,
            needs_interaction_terminal_record_sha256=target.needs_interaction_terminal_record_sha256,
            interaction_context_sha256=target.interaction_context_sha256,
            request_state_head_sha256=target.request_state_head_sha256,
            evidence_artifact_ref_sha256=target.evidence_artifact_ref_sha256,
            evidence=self.evidence,
        )
        if projected != target:
            raise BatchInteractionError("aggregate interaction evidence differs from target")
        domains = _unique_text_tuple(self.validation_domain_ids, "validation_domain_ids")
        if not domains:
            raise BatchInteractionError(
                "aggregate interaction group requires validation-domain scope"
            )
        object.__setattr__(self, "validation_domain_ids", domains)

    @property
    def group_id(self) -> str:
        return self.target.group_id

    @property
    def group_sha256(self) -> str:
        return self.target.group_sha256

    @property
    def stage(self) -> str:
        return "cluster_label_review"

    @property
    def interaction_reason(self) -> str:
        return InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED.value

    @property
    def action_schema_version(self) -> str:
        return "roi-interaction-actions/v2"

    @property
    def context_version(self) -> str:
        return "mif-batch-cluster-label-review-interaction/v1"

    @property
    def compatibility_signature_sha256(self) -> str:
        return self.target.target_sha256

    @property
    def context_signature_sha256(self) -> str:
        return self.target.interaction_context_sha256

    @property
    def bulk_application_allowed(self) -> bool:
        return False

    @property
    def targets(self) -> tuple[AggregateClusterReviewTarget, ...]:
        return (self.target,)


def _group_key(target: BatchInteractionTarget) -> dict[str, object]:
    result = target.stage_result
    assert result.interaction_reason is not None
    context = result.resume_context or {}
    context_signature = None
    if result.interaction_reason.value == "tissue_native_level_required":
        context_signature = context.get("native_pyramid_inventory_sha256")
        if context_signature is None:
            raise BatchInteractionError(
                "tissue resolution grouping requires exact native-pyramid authority"
            )
        context_signature = _sha256(context_signature, "native_pyramid_inventory_sha256")
        if "compatible_native_resolution_indices" in context:
            context_signature = _digest({
                "native_pyramid_inventory_sha256": context_signature,
                "compatible_native_resolution_indices": context["compatible_native_resolution_indices"],
                "allow_level0_selection": context.get("allow_level0_selection"),
            })
    elif result.interaction_reason is InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED:
        from .batch_training.plan_selection import validate_training_plan_selection_request

        try:
            validate_training_plan_selection_request(context)
        except ValueError as exc:
            raise BatchInteractionError(
                "training plan grouping requires an authenticated candidate inventory"
            ) from exc
        context_signature = _digest(context["candidate_entry_ids"])
    compatibility = target.compatibility_signature_sha256
    if (result.interaction_reason is InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED
            and _context_version(result) == "primary-count-review/v1"
            and set(result.allowed_actions) == {"accept_all_primary_structures", "exclude_analysis_domain"}):
        # These decisions carry no image-specific values. Each target still binds
        # its own count inventory, limit and domain when its response is validated.
        compatibility = _digest({"whole_region_decisions": sorted(result.allowed_actions)})
    return {
        "stage": result.stage,
        "interaction_reason": result.interaction_reason.value,
        "action_schema_version": result.interaction_action_schema_version,
        "allowed_actions": list(result.allowed_actions),
        "context_version": _context_version(result),
        "compatibility_signature_sha256": compatibility,
        "context_signature_sha256": context_signature,
    }


def group_interactions(
    targets: tuple[BatchInteractionTarget, ...],
) -> tuple[BatchInteractionGroup, ...]:
    """Group only targets with identical declared interaction semantics."""

    values = tuple(targets)
    if not all(isinstance(item, BatchInteractionTarget) for item in values):
        raise BatchInteractionError("group_interactions requires typed targets")
    if len({item.target_id for item in values}) != len(values):
        raise BatchInteractionError("interaction target IDs must be unique")
    buckets: dict[bytes, list[BatchInteractionTarget]] = {}
    keys: dict[bytes, dict[str, object]] = {}
    for target in values:
        key = _group_key(target)
        encoded = _canonical_bytes(key)
        keys[encoded] = key
        buckets.setdefault(encoded, []).append(target)
    groups: list[BatchInteractionGroup] = []
    for encoded, members in buckets.items():
        key = keys[encoded]
        group_id = "batch-interaction-group-" + hashlib.sha256(encoded).hexdigest()
        ordered = tuple(sorted(members, key=lambda item: item.target_id))
        group_sha256 = _digest(
            {
                "group_id": group_id,
                "group_key": key,
                "targets": [_target_identity(item) for item in ordered],
            }
        )
        reason = str(key["interaction_reason"])
        groups.append(
            BatchInteractionGroup(
                group_id=group_id,
                group_sha256=group_sha256,
                stage=str(key["stage"]),
                interaction_reason=reason,
                action_schema_version=str(key["action_schema_version"]),
                context_version=str(key["context_version"]),
                compatibility_signature_sha256=str(key["compatibility_signature_sha256"]),
                context_signature_sha256=(
                    None
                    if key["context_signature_sha256"] is None
                    else str(key["context_signature_sha256"])
                ),
                bulk_application_allowed=(_bulk_application_allowed(reason) or (
                    reason == InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED.value
                    and key["context_version"] == "primary-count-review/v1"
                    and set(key["allowed_actions"]) == {"accept_all_primary_structures", "exclude_analysis_domain"})),
                targets=ordered,
            )
        )
    return tuple(sorted(groups, key=lambda item: item.group_id))


@dataclass(frozen=True, slots=True)
class BatchTargetSelection:
    target_ids: tuple[str, ...] = ()
    validation_domain_ids: tuple[str, ...] = ()
    all_compatible: bool = False

    def __post_init__(self) -> None:
        targets = _unique_text_tuple(self.target_ids, "target_ids")
        domains = _unique_text_tuple(self.validation_domain_ids, "validation_domain_ids")
        if type(self.all_compatible) is not bool:
            raise BatchInteractionError("all_compatible must be boolean")
        if sum((bool(targets), bool(domains), self.all_compatible)) != 1:
            raise BatchInteractionError("selection requires exactly one selection mode")
        object.__setattr__(self, "target_ids", targets)
        object.__setattr__(self, "validation_domain_ids", domains)


@dataclass(frozen=True, slots=True)
class BatchInteractionQuery:
    offset: int = 0
    limit: int = 50
    validation_domain_ids: tuple[str, ...] = ()
    interaction_reasons: tuple[str, ...] = ()
    node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_ids", _unique_text_tuple(self.node_ids, "node_ids"))
        if type(self.offset) is not int or self.offset < 0:
            raise BatchInteractionError("offset must be a nonnegative integer")
        if type(self.limit) is not int or not 1 <= self.limit <= 200:
            raise BatchInteractionError("limit must be between 1 and 200")
        object.__setattr__(
            self,
            "validation_domain_ids",
            _unique_text_tuple(self.validation_domain_ids, "validation_domain_ids"),
        )
        object.__setattr__(
            self,
            "interaction_reasons",
            _unique_text_tuple(self.interaction_reasons, "interaction_reasons"),
        )


@dataclass(frozen=True, slots=True)
class BatchInteractionPage:
    groups: tuple[BatchInteractionGroup | BatchAggregateInteractionGroup, ...]
    total_count: int
    offset: int
    limit: int
    next_offset: int | None
    qupath_project_path: str | None = None

    def __post_init__(self) -> None:
        if self.qupath_project_path is not None:
            from pathlib import Path, PurePosixPath

            if (
                type(self.qupath_project_path) is not str
                or not (
                    Path(self.qupath_project_path).is_absolute()
                    or PurePosixPath(self.qupath_project_path).is_absolute()
                )
            ):
                raise BatchInteractionError(
                    "The current QuPath project must be an absolute path on its processing computer"
                )
        groups = tuple(self.groups)
        if not all(
            type(item) in {BatchInteractionGroup, BatchAggregateInteractionGroup} for item in groups
        ):
            raise BatchInteractionError("page groups must be typed")
        if (
            type(self.total_count) is not int
            or self.total_count < len(groups)
            or type(self.offset) is not int
            or self.offset < 0
            or type(self.limit) is not int
            or self.limit < 1
            or (
                self.next_offset is not None
                and (
                    type(self.next_offset) is not int
                    or self.next_offset <= self.offset
                    or self.next_offset >= self.total_count
                )
            )
        ):
            raise BatchInteractionError("interaction page bounds are inconsistent")
        object.__setattr__(self, "groups", groups)


def page_interactions(
    groups: tuple[BatchInteractionGroup | BatchAggregateInteractionGroup, ...],
    query: BatchInteractionQuery,
) -> BatchInteractionPage:
    if not isinstance(query, BatchInteractionQuery) or not all(
        type(item) in {BatchInteractionGroup, BatchAggregateInteractionGroup} for item in groups
    ):
        raise BatchInteractionError("pagination requires typed groups and query")
    ordered = tuple(sorted(groups, key=lambda item: item.group_id))
    if query.validation_domain_ids:
        selected_domains = set(query.validation_domain_ids)
        ordered = tuple(
            item
            for item in ordered
            if (
                bool(selected_domains & set(item.validation_domain_ids))
                if type(item) is BatchAggregateInteractionGroup
                else any(target.validation_domain_id in selected_domains for target in item.targets)
            )
        )
    if query.interaction_reasons:
        selected_reasons = set(query.interaction_reasons)
        ordered = tuple(item for item in ordered if item.interaction_reason in selected_reasons)
    if query.node_ids:
        selected_nodes = set(query.node_ids)
        ordered = tuple(
            item
            for item in ordered
            if (
                item.target.node_id in selected_nodes
                if type(item) is BatchAggregateInteractionGroup
                else any(target.node_id in selected_nodes for target in item.targets)
            )
        )
    total = len(ordered)
    page = ordered[query.offset : query.offset + query.limit]
    end = query.offset + len(page)
    return BatchInteractionPage(
        groups=page,
        total_count=total,
        offset=query.offset,
        limit=query.limit,
        next_offset=end if end < total else None,
    )


@dataclass(frozen=True, slots=True)
class BatchInteractionResponseRequest:
    group_id: str
    group_sha256: str
    selection: BatchTargetSelection
    action: str
    response: Mapping[str, object]
    expected_state_head_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _text(self.group_id, "group_id"))
        object.__setattr__(self, "group_sha256", _sha256(self.group_sha256, "group_sha256"))
        if not isinstance(self.selection, BatchTargetSelection):
            raise BatchInteractionError("response request requires typed selection")
        object.__setattr__(self, "action", _text(self.action, "action"))
        object.__setattr__(self, "response", _freeze_json(self.response, "response"))
        object.__setattr__(
            self,
            "expected_state_head_sha256",
            _sha256(
                self.expected_state_head_sha256,
                "expected_state_head_sha256",
            ),
        )


@dataclass(frozen=True, slots=True)
class BatchTargetResponseValidation:
    """Closed outcome from one target's owning pure response validator."""

    target_id: str
    node_id: str
    status: str
    response: Mapping[str, object] | None
    authorization_request: BatchAttemptAuthorizationRequest | None
    response_validator_id: str | None
    response_validator_version: str | None
    rejection_code: str | None

    def __post_init__(self) -> None:
        """Validate the BatchTargetResponseValidation fields when the record is constructed.

        The checks cover ``status``, ``target_id``, ``node_id``, ``response``,
        ``response_validator_id``, ``response_validator_version``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        object.__setattr__(self, "target_id", _text(self.target_id, "target_id"))
        object.__setattr__(self, "node_id", _text(self.node_id, "node_id"))
        if self.status == "accepted":
            if (
                self.response is None
                or not isinstance(self.authorization_request, BatchAttemptAuthorizationRequest)
                or self.response_validator_id is None
                or self.response_validator_version is None
                or self.rejection_code is not None
            ):
                raise BatchInteractionError(
                    "accepted validation lacks its canonical response authority"
                )
            object.__setattr__(self, "response", _freeze_json(self.response, "validated response"))
            object.__setattr__(
                self,
                "response_validator_id",
                _text(self.response_validator_id, "response_validator_id"),
            )
            object.__setattr__(
                self,
                "response_validator_version",
                _text(
                    self.response_validator_version,
                    "response_validator_version",
                ),
            )
        elif self.status == "rejected":
            if (
                self.response is not None
                or self.authorization_request is not None
                or self.response_validator_id is not None
                or self.response_validator_version is not None
                or self.rejection_code is None
            ):
                raise BatchInteractionError("rejected validation may carry only a rejection code")
            object.__setattr__(
                self,
                "rejection_code",
                _text(self.rejection_code, "rejection_code"),
            )
        else:
            raise BatchInteractionError("validation status must be accepted or rejected")


class BatchTargetResponseValidator(Protocol):
    def __call__(
        self,
        target: BatchInteractionTarget,
        action: str,
        response: Mapping[str, object],
    ) -> BatchTargetResponseValidation: ...


@dataclass(frozen=True, slots=True)
class BatchInteractionOwnerBinding:
    """One stage-owned interaction response validator registration."""

    stage: str
    interaction_reason: str
    action_schema_version: str
    validate_target: BatchTargetResponseValidator

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", _text(self.stage, "stage"))
        object.__setattr__(
            self,
            "interaction_reason",
            _text(self.interaction_reason, "interaction_reason"),
        )
        object.__setattr__(
            self,
            "action_schema_version",
            _text(self.action_schema_version, "action_schema_version"),
        )
        if not callable(self.validate_target):
            raise BatchInteractionError("interaction owner validator must be callable")


@dataclass(frozen=True, slots=True)
class BatchInteractionOwnerRegistry:
    """Closed resolver for validators owned by scientific stage packages."""

    bindings: tuple[BatchInteractionOwnerBinding, ...]

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        if not all(isinstance(item, BatchInteractionOwnerBinding) for item in bindings):
            raise BatchInteractionError("interaction owner registry requires typed bindings")
        keys = tuple(
            (item.stage, item.interaction_reason, item.action_schema_version) for item in bindings
        )
        if len(set(keys)) != len(keys):
            raise BatchInteractionError("interaction owner registry has duplicate keys")
        object.__setattr__(
            self,
            "bindings",
            tuple(
                sorted(
                    bindings,
                    key=lambda item: (
                        item.stage,
                        item.interaction_reason,
                        item.action_schema_version,
                    ),
                )
            ),
        )

    def resolve(self, target: BatchInteractionTarget, /) -> BatchTargetResponseValidator:
        if not isinstance(target, BatchInteractionTarget):
            raise BatchInteractionError("interaction owner resolution requires a typed target")
        stage_result = target.stage_result
        reason = stage_result.interaction_reason
        if reason is None:
            raise BatchInteractionError("interaction target has no authenticated reason")
        key = (
            stage_result.stage,
            reason.value,
            stage_result.interaction_action_schema_version,
        )
        for binding in self.bindings:
            if (
                binding.stage,
                binding.interaction_reason,
                binding.action_schema_version,
            ) == key:
                return binding.validate_target
        raise BatchInteractionError(
            "no interaction owner validator for authenticated target key "
            f"{key[0]}/{key[1]}/{key[2]}"
        )


@dataclass(frozen=True, slots=True)
class AcceptedBatchInteractionResponse:
    schema_name: str
    schema_version: int
    target_id: str
    group_id: str
    group_sha256: str
    node_id: str
    prior_attempt_id: str
    prior_attempt_authorization_sha256: str
    prior_terminal_record_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    action: str
    response: Mapping[str, object]
    response_validator_id: str
    response_validator_version: str
    response_sha256: str

    def __post_init__(self) -> None:
        """Validate the AcceptedBatchInteractionResponse fields when the record is constructed.

        The checks cover ``response_sha256``, ``schema_name``, ``schema_version``,
        ``response``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        if (self.schema_name, self.schema_version) != (
            INTERACTION_RESPONSE_SCHEMA_NAME,
            INTERACTION_RESPONSE_SCHEMA_VERSION,
        ):
            raise BatchInteractionError(
                "accepted response must use mif-batch-interaction-response/v1"
            )
        try:
            assert_current_writer(self.schema_name, str(self.schema_version))
        except ValueError as exc:
            raise BatchInteractionError("accepted response schema is not current") from exc
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
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        for field_name in (
            "group_sha256",
            "prior_attempt_authorization_sha256",
            "prior_terminal_record_sha256",
            "interaction_context_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        object.__setattr__(self, "response", _freeze_json(self.response, "accepted response"))
        response_sha256 = _sha256(self.response_sha256, "response_sha256")
        if response_sha256 != _digest(self._unsigned_mapping()):
            raise BatchInteractionError("accepted response checksum mismatch")
        object.__setattr__(self, "response_sha256", response_sha256)

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "target_id": self.target_id,
            "group_id": self.group_id,
            "group_sha256": self.group_sha256,
            "node_id": self.node_id,
            "prior_attempt_id": self.prior_attempt_id,
            "prior_attempt_authorization_sha256": (self.prior_attempt_authorization_sha256),
            "prior_terminal_record_sha256": self.prior_terminal_record_sha256,
            "interaction_request_id": self.interaction_request_id,
            "interaction_context_sha256": self.interaction_context_sha256,
            "action": self.action,
            "response": _export_json(self.response),
            "response_validator_id": self.response_validator_id,
            "response_validator_version": self.response_validator_version,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "response_sha256": self.response_sha256}

    @classmethod
    def create(
        cls,
        *,
        target: BatchInteractionTarget,
        group: BatchInteractionGroup,
        action: str,
        response: Mapping[str, object],
        response_validator_id: str,
        response_validator_version: str,
    ) -> "AcceptedBatchInteractionResponse":
        unsigned = {
            "schema_name": INTERACTION_RESPONSE_SCHEMA_NAME,
            "schema_version": INTERACTION_RESPONSE_SCHEMA_VERSION,
            "target_id": target.target_id,
            "group_id": group.group_id,
            "group_sha256": group.group_sha256,
            "node_id": target.node_id,
            "prior_attempt_id": target.attempt_id,
            "prior_attempt_authorization_sha256": (target.attempt_authorization_sha256),
            "prior_terminal_record_sha256": (target.needs_interaction_terminal_record_sha256),
            "interaction_request_id": target.interaction_request_id,
            "interaction_context_sha256": target.interaction_context_sha256,
            "action": action,
            "response": _freeze_json(response, "accepted response"),
            "response_validator_id": response_validator_id,
            "response_validator_version": response_validator_version,
        }
        return cls(**unsigned, response_sha256=_digest(unsigned))


@dataclass(frozen=True, slots=True)
class BatchTargetApplication:
    target_id: str
    node_id: str
    prior_attempt_id: str
    prior_attempt_authorization_sha256: str
    prior_terminal_record_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    project_entry_id: str | None
    sample_id: str
    slide_id: str
    validation_domain_id: str
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    compatibility_signature_sha256: str
    status: str
    accepted_response_sha256: str | None
    retry_attempt_id: str | None
    retry_authorization_sha256: str | None
    rejection_code: str | None

    def __post_init__(self) -> None:
        """Validate the BatchTargetApplication fields when the record is constructed.

        The checks cover ``project_entry_id``, ``status``, ``analysis_domain_bindings``,
        ``accepted_response_sha256``, ``retry_attempt_id``, ``retry_authorization_sha256``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        for field_name in (
            "target_id",
            "node_id",
            "prior_attempt_id",
            "interaction_request_id",
            "sample_id",
            "slide_id",
            "validation_domain_id",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if self.project_entry_id is not None:
            object.__setattr__(
                self,
                "project_entry_id",
                _text(self.project_entry_id, "project_entry_id"),
            )
        for field_name in (
            "prior_attempt_authorization_sha256",
            "prior_terminal_record_sha256",
            "interaction_context_sha256",
            "compatibility_signature_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "analysis_domain_bindings",
            _bindings(self.analysis_domain_bindings, "analysis_domain_bindings"),
        )
        if self.status == "accepted_for_retry":
            if (
                self.accepted_response_sha256 is None
                or self.retry_attempt_id is None
                or self.retry_authorization_sha256 is None
                or self.rejection_code is not None
            ):
                raise BatchInteractionError("accepted application lacks exact retry authority")
            object.__setattr__(
                self,
                "accepted_response_sha256",
                _sha256(
                    self.accepted_response_sha256,
                    "accepted_response_sha256",
                ),
            )
            object.__setattr__(
                self,
                "retry_attempt_id",
                _text(self.retry_attempt_id, "retry_attempt_id"),
            )
            object.__setattr__(
                self,
                "retry_authorization_sha256",
                _sha256(
                    self.retry_authorization_sha256,
                    "retry_authorization_sha256",
                ),
            )
        elif self.status == "rejected":
            if (
                self.accepted_response_sha256 is not None
                or self.retry_attempt_id is not None
                or self.retry_authorization_sha256 is not None
                or self.rejection_code is None
            ):
                raise BatchInteractionError(
                    "rejected application may carry only its rejection code"
                )
            object.__setattr__(
                self,
                "rejection_code",
                _text(self.rejection_code, "rejection_code"),
            )
        else:
            raise BatchInteractionError("application status must be accepted_for_retry or rejected")


@dataclass(frozen=True, slots=True)
class BatchAggregateTargetApplication:
    """Retry admission result for one complete pooled cluster graph."""

    target: AggregateClusterReviewTarget
    status: str
    accepted_response_sha256: str | None
    retry_attempt_id: str | None
    retry_authorization_sha256: str | None
    rejection_code: str | None

    def __post_init__(self) -> None:
        """Validate the BatchAggregateTargetApplication fields when the record is constructed.

        The checks cover ``status``, ``target``, ``accepted_response_sha256``,
        ``retry_attempt_id``, ``retry_authorization_sha256``, ``rejection_code``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if type(self.target) is not AggregateClusterReviewTarget:
            raise BatchInteractionError("aggregate application requires its exact graph target")
        if self.status == "accepted_for_retry":
            if (
                self.accepted_response_sha256 is None
                or self.retry_attempt_id is None
                or self.retry_authorization_sha256 is None
                or self.rejection_code is not None
            ):
                raise BatchInteractionError("accepted aggregate application lacks retry authority")
            object.__setattr__(
                self,
                "accepted_response_sha256",
                _sha256(
                    self.accepted_response_sha256,
                    "accepted_response_sha256",
                ),
            )
            object.__setattr__(
                self,
                "retry_attempt_id",
                _text(self.retry_attempt_id, "retry_attempt_id"),
            )
            object.__setattr__(
                self,
                "retry_authorization_sha256",
                _sha256(
                    self.retry_authorization_sha256,
                    "retry_authorization_sha256",
                ),
            )
        elif self.status == "rejected":
            if (
                self.accepted_response_sha256 is not None
                or self.retry_attempt_id is not None
                or self.retry_authorization_sha256 is not None
                or self.rejection_code is None
            ):
                raise BatchInteractionError(
                    "rejected aggregate application may carry only its rejection code"
                )
            object.__setattr__(
                self,
                "rejection_code",
                _text(self.rejection_code, "rejection_code"),
            )
        else:
            raise BatchInteractionError(
                "aggregate application status must be accepted_for_retry or rejected"
            )

    @property
    def target_id(self) -> str:
        return self.target.target_id

    @property
    def node_id(self) -> str:
        return self.target.node_id

    @property
    def participant_ids(self) -> tuple[str, ...]:
        return self.target.participant_ids

    @property
    def project_entry_ids(self) -> tuple[str, ...]:
        return self.target.project_entry_ids

    @property
    def analysis_domain_bindings(self) -> tuple[AnalysisDomainBinding, ...]:
        return self.target.analysis_domain_bindings


@dataclass(frozen=True, slots=True)
class BatchInteractionApplicationResult:
    group_id: str
    group_sha256: str
    applications: tuple[BatchTargetApplication | BatchAggregateTargetApplication, ...]
    committed_state_head_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _text(self.group_id, "group_id"))
        object.__setattr__(self, "group_sha256", _sha256(self.group_sha256, "group_sha256"))
        object.__setattr__(
            self,
            "committed_state_head_sha256",
            _sha256(
                self.committed_state_head_sha256,
                "committed_state_head_sha256",
            ),
        )
        applications = tuple(self.applications)
        if not all(
            type(item) in {BatchTargetApplication, BatchAggregateTargetApplication}
            for item in applications
        ):
            raise BatchInteractionError("applications must be typed")
        if len({item.target_id for item in applications}) != len(applications):
            raise BatchInteractionError("application target IDs must be unique")
        object.__setattr__(self, "applications", applications)


def _application(
    target: BatchInteractionTarget,
    *,
    status: str,
    accepted_response_sha256: str | None = None,
    retry_attempt_id: str | None = None,
    retry_authorization_sha256: str | None = None,
    rejection_code: str | None = None,
) -> BatchTargetApplication:
    return BatchTargetApplication(
        target_id=target.target_id,
        node_id=target.node_id,
        prior_attempt_id=target.attempt_id,
        prior_attempt_authorization_sha256=target.attempt_authorization_sha256,
        prior_terminal_record_sha256=(target.needs_interaction_terminal_record_sha256),
        interaction_request_id=target.interaction_request_id,
        interaction_context_sha256=target.interaction_context_sha256,
        project_entry_id=target.project_entry_id,
        sample_id=target.sample_id,
        slide_id=target.slide_id,
        validation_domain_id=target.validation_domain_id,
        analysis_domain_bindings=target.analysis_domain_bindings,
        compatibility_signature_sha256=target.compatibility_signature_sha256,
        status=status,
        accepted_response_sha256=accepted_response_sha256,
        retry_attempt_id=retry_attempt_id,
        retry_authorization_sha256=retry_authorization_sha256,
        rejection_code=rejection_code,
    )


def publish_interaction_requests(
    *,
    targets: tuple[BatchInteractionTarget, ...],
    expected_state_head_sha256: str,
    state_store: BatchStateStore,
    workspace_lease: BatchWorkspaceLease,
) -> str:
    """Persist exact StageResult request bytes and atomically queue their IDs."""

    values = tuple(sorted(targets, key=lambda item: item.target_id))
    if not values or not all(isinstance(item, BatchInteractionTarget) for item in values):
        raise BatchInteractionError("request publication requires typed targets")
    publications = tuple(_request_publication(target) for target in values)
    return state_store.publish_interaction_requests(
        publications,
        expected_state_head_sha256,
        workspace_lease,
    ).sha256


def _selected_targets(
    group: BatchInteractionGroup, selection: BatchTargetSelection
) -> tuple[BatchInteractionTarget, ...]:
    if selection.all_compatible:
        return group.targets
    if selection.target_ids:
        by_id = {item.target_id: item for item in group.targets}
        if not set(selection.target_ids) <= set(by_id):
            raise BatchInteractionError("selection names a foreign target")
        return tuple(by_id[item] for item in selection.target_ids)
    domains = set(selection.validation_domain_ids)
    selected = tuple(item for item in group.targets if item.validation_domain_id in domains)
    if not selected or domains != {item.validation_domain_id for item in selected}:
        raise BatchInteractionError("selection names a foreign validation domain")
    return selected


def _request_publication(
    target: BatchInteractionTarget,
) -> BatchInteractionRequestPublication:
    return BatchInteractionRequestPublication(
        stage_result=target.stage_result,
        interaction_request_id=target.interaction_request_id,
        node_id=target.node_id,
        attempt_id=target.attempt_id,
        attempt_authorization_sha256=target.attempt_authorization_sha256,
        interaction_context_sha256=target.interaction_context_sha256,
        terminal_record_sha256=target.needs_interaction_terminal_record_sha256,
        sample_id=target.sample_id,
        slide_id=target.slide_id,
        project_entry_id=target.project_entry_id,
        validation_domain_id=target.validation_domain_id,
        analysis_domain_bindings=target.analysis_domain_bindings,
        compatibility_signature_sha256=target.compatibility_signature_sha256,
    )


def _revalidate_target(
    target: BatchInteractionTarget,
    expected_head_sha256: str,
    state_store: BatchStateStore,
) -> None:
    try:
        state_store.authenticate_interaction_target(
            _request_publication(target), expected_head_sha256
        )
    except BatchStateError as exc:
        raise BatchInteractionError(
            "interaction target contradicts current store authority"
        ) from exc


def apply_group_response(
    group: BatchInteractionGroup,
    request: BatchInteractionResponseRequest,
    validate_target: BatchTargetResponseValidator,
    state_store: BatchStateStore,
    workspace_lease: BatchWorkspaceLease,
    *,
    paused_response_authority: object | None = None,
) -> BatchInteractionApplicationResult:
    """Validate selected targets independently and atomically authorize retries."""

    if not isinstance(group, BatchInteractionGroup) or not isinstance(
        request, BatchInteractionResponseRequest
    ):
        raise BatchInteractionError("response application requires typed inputs")
    canonical_groups = group_interactions(group.targets)
    if len(canonical_groups) != 1 or canonical_groups[0] != group:
        raise BatchInteractionError("interaction group identity is not canonical")
    if (request.group_id, request.group_sha256) != (
        group.group_id,
        group.group_sha256,
    ):
        raise BatchInteractionError("response request names a foreign group")
    selected = _selected_targets(group, request.selection)
    if len(selected) > 1 and not group.bulk_application_allowed:
        raise BatchInteractionError("image-space interactions require per-entry submission")
    head = state_store.load_head()
    if head.sha256 != request.expected_state_head_sha256:
        raise BatchInteractionError("interaction response head authority changed")
    for target in selected:
        _revalidate_target(target, head.sha256, state_store)

    staged: list[
        tuple[
            BatchInteractionTarget,
            AcceptedBatchInteractionResponse,
            BatchAttemptAuthorizationRequest,
            BatchInteractionResponsePublication,
        ]
    ] = []
    applications: list[BatchTargetApplication] = []
    for target in selected:
        try:
            validation = validate_target(target, request.action, request.response)
        except BatchInteractionError:
            raise
        except Exception as exc:
            raise BatchInteractionError(
                f"target response validation failed for {target.target_id}"
            ) from exc
        if not isinstance(validation, BatchTargetResponseValidation):
            raise BatchInteractionError("target validator returned malformed output")
        if (validation.target_id, validation.node_id) != (
            target.target_id,
            target.node_id,
        ):
            raise BatchInteractionError("target validator returned foreign authority")
        if validation.status == "rejected":
            applications.append(
                _application(
                    target,
                    status="rejected",
                    rejection_code=validation.rejection_code,
                )
            )
            continue
        base = validation.authorization_request
        assert base is not None
        if (
            base.node.node_id != target.node_id
            or base.retry_ordinal != 0
            or base.interaction_response_sha256 is not None
            or base.interaction_predecessor_terminal_record_sha256 is not None
        ):
            raise BatchInteractionError(
                "accepted target requires an ordinary base authorization request"
            )
        if (
            target.sample_id not in base.node.participant_ids
            or base.node.validation_domain_id != target.validation_domain_id
            or tuple(base.node.analysis_domain_bindings) != target.analysis_domain_bindings
            or (
                target.project_entry_id is not None
                and target.project_entry_id not in base.node.project_entry_ids
            )
        ):
            raise BatchInteractionError("base authorization request has foreign target scope")
        assert validation.response is not None
        assert validation.response_validator_id is not None
        assert validation.response_validator_version is not None
        accepted = AcceptedBatchInteractionResponse.create(
            target=target,
            group=group,
            action=request.action,
            response=validation.response,
            response_validator_id=validation.response_validator_id,
            response_validator_version=validation.response_validator_version,
        )
        retry = replace(
            base,
            retry_ordinal=len(head.attempt_ids_by_node[target.node_id]),
            interaction_response_sha256=accepted.response_sha256,
            interaction_predecessor_terminal_record_sha256=(
                target.needs_interaction_terminal_record_sha256
            ),
        )
        publication = BatchInteractionResponsePublication(
            canonical_response_bytes=_canonical_bytes(accepted.to_mapping()),
            response_sha256=accepted.response_sha256,
            node_id=target.node_id,
            predecessor_terminal_record_sha256=(target.needs_interaction_terminal_record_sha256),
        )
        staged.append((target, accepted, retry, publication))

    if staged:
        authorizations = state_store.authorize_attempts(
            tuple(item[2] for item in staged),
            head.sha256,
            workspace_lease,
            tuple(item[3] for item in staged),
            consumed_interaction_request_ids=tuple(
                item[0].interaction_request_id for item in staged
            ),
            paused_response_authority=paused_response_authority,
        )
        authorization_by_node = {item.node_id: item for item in authorizations}
        accepted_application_by_target = {
            target.target_id: _application(
                target,
                status="accepted_for_retry",
                accepted_response_sha256=accepted.response_sha256,
                retry_attempt_id=authorization_by_node[target.node_id].attempt_id,
                retry_authorization_sha256=(
                    authorization_by_node[target.node_id].authorization_sha256
                ),
            )
            for target, accepted, _, _ in staged
        }
        applications.extend(accepted_application_by_target.values())
        committed = state_store.load_head().sha256
    else:
        committed = head.sha256
    return BatchInteractionApplicationResult(
        group_id=group.group_id,
        group_sha256=group.group_sha256,
        applications=tuple(sorted(applications, key=lambda item: item.target_id)),
        committed_state_head_sha256=committed,
    )


__all__ = [
    "AcceptedBatchInteractionResponse",
    "BatchAggregateInteractionGroup",
    "BatchAggregateTargetApplication",
    "BatchInteractionApplicationResult",
    "BatchInteractionError",
    "BatchInteractionGroup",
    "BatchInteractionOwnerBinding",
    "BatchInteractionOwnerRegistry",
    "BatchInteractionPage",
    "BatchInteractionQuery",
    "BatchInteractionResponseRequest",
    "BatchInteractionTarget",
    "ReferenceOnlyQuPathView",
    "BatchTargetApplication",
    "BatchTargetResponseValidation",
    "BatchTargetResponseValidator",
    "BatchTargetSelection",
    "apply_group_response",
    "group_interactions",
    "page_interactions",
    "project_batch_interaction_target",
    "publish_interaction_requests",
]
