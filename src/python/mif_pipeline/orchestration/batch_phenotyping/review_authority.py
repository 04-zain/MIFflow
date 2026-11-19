"""Opaque state-issued authority for one aggregate cluster-label review retry.

The `AuthorizedClusterLabelReviewResponse` record carries `current_execution`,
`evidence_authority`, `target`, `accepted_response`.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactIdentityContract,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from ._common import (
    BatchPhenotypingError,
    canonical_bytes,
    closed_mapping,
    digest,
    read_bounded_file,
    sha256,
    text,
)
from .review_codec import loads_batch_cluster_review_evidence
from .accepted_review import (
    AcceptedClusterLabelReview,
    _cluster_label_mapping,
    _excluded_cluster_ids,
    _create_accepted_cluster_label_review,
)
from .review_records import BatchClusterReviewEvidence
from .review_target import AggregateClusterReviewTarget

CLUSTER_REVIEW_RESPONSE_VALIDATOR_ID = "aggregate-cluster-label-review-validator"
CLUSTER_REVIEW_RESPONSE_VALIDATOR_VERSION = "1"
CLUSTER_REVIEW_EVIDENCE_ARTIFACT_KIND = "cluster_review_evidence"
CLUSTER_REVIEW_EVIDENCE_SCHEMA_NAME = "mif-batch-cluster-review-evidence"
CLUSTER_REVIEW_EVIDENCE_SCHEMA_VERSION = 1
_AUTHORIZED_CLUSTER_LABEL_REVIEW_RESPONSE_ISSUER = object()
_RESPONSE_FIELDS = frozenset(
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


# ==============================================================================
#                                Review authority
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedClusterLabelReviewResponse:
    """Opaque capability for one exact state-authenticated review retry."""

    current_execution: BatchCurrentExecutionAuthority
    evidence_authority: BatchCommittedArtifactAuthority
    target: AggregateClusterReviewTarget
    accepted_response: Mapping[str, object]
    _seal_sha256: str
    _issuer: object
    _current_execution_binding: object = field(repr=False, compare=False)
    _evidence_authority_binding: object = field(repr=False, compare=False)
    _target_binding: object = field(repr=False, compare=False)
    _accepted_response_binding: object = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("AuthorizedClusterLabelReviewResponse is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("AuthorizedClusterLabelReviewResponse cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("AuthorizedClusterLabelReviewResponse is nonserializable")

    @property
    def action(self) -> str:
        return str(self.accepted_response["action"])

    @property
    def response_sha256(self) -> str:
        return str(self.accepted_response["response_sha256"])

    @property
    def cluster_labels(self) -> Mapping[str, str]:
        response = self.accepted_response["response"]
        assert isinstance(response, Mapping)
        labels = response["cluster_labels"]
        assert isinstance(labels, Mapping)
        return labels  # type: ignore[return-value]


@runtime_checkable
class AuthorizedClusterLabelReviewResponseResolver(Protocol):
    """Presentation-neutral state-owner resolver for a current retry."""

    def __call__(
        self, context: BatchNodeExecutionContext
    ) -> AuthorizedClusterLabelReviewResponse | None: ...


def _response_from_mapping(
    value: object,
) -> Mapping[str, object]:
    try:
        mapping = closed_mapping(
            value,
            _RESPONSE_FIELDS,
            "authorized cluster-label review response",
        )
        decoded = json.loads(canonical_bytes(mapping).decode("utf-8"))
    except (BatchPhenotypingError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError(
            "authorized cluster review requires one canonical response mapping"
        ) from exc

    def freeze(item: object) -> object:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in sorted(item.items())})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    frozen = freeze(decoded)
    assert isinstance(frozen, Mapping)
    if (
        frozen["schema_name"] != "mif-batch-interaction-response"
        or type(frozen["schema_version"]) is not int
        or frozen["schema_version"] != 1
    ):
        raise BatchPhenotypingError("cluster-label review response schema is not current")
    for name in (
        "target_id",
        "group_id",
        "node_id",
        "prior_attempt_id",
        "interaction_request_id",
        "action",
        "response_validator_id",
        "response_validator_version",
    ):
        text(frozen[name], name)
    for name in (
        "group_sha256",
        "prior_attempt_authorization_sha256",
        "prior_terminal_record_sha256",
        "interaction_context_sha256",
        "response_sha256",
    ):
        sha256(frozen[name], name)
    unsigned = {key: item for key, item in frozen.items() if key != "response_sha256"}
    if frozen["response_sha256"] != digest(unsigned):
        raise BatchPhenotypingError("cluster-label review response digest differs")
    return frozen


def _authority_seal_material(
    *,
    authority: AuthorizedClusterLabelReviewResponse,
    current_execution: BatchCurrentExecutionAuthority,
    evidence_authority: BatchCommittedArtifactAuthority,
    target: AggregateClusterReviewTarget,
    accepted_response: Mapping[str, object],
) -> dict[str, object]:
    return {
        "authority_object_identity": id(authority),
        "current_execution_object_identity": id(current_execution),
        "current_execution_seal_sha256": current_execution._seal_sha256,
        "evidence_authority_object_identity": id(evidence_authority),
        "evidence_authority_seal_sha256": evidence_authority._seal_sha256,
        "evidence_artifact_ref_sha256": (evidence_authority.artifact_ref.artifact_ref_sha256),
        "target_object_identity": id(target),
        "target_sha256": target.target_sha256,
        "accepted_response_object_identity": id(accepted_response),
        "accepted_response": accepted_response,
    }


def cluster_review_evidence_owning_identity(
    evidence: BatchClusterReviewEvidence,
) -> dict[str, object]:
    """Return the exact owning identity for one persisted review summary."""

    if type(evidence) is not BatchClusterReviewEvidence:
        raise BatchPhenotypingError("cluster review evidence identity requires exact evidence")
    return {
        "mode": evidence.mode.value,
        "phenotyping_group_id": evidence.phenotyping_group_id,
        "participant_ids": list(evidence.participant_ids),
        "project_entry_ids": list(evidence.project_entry_ids),
        "analysis_domain_binding_set_sha256": digest(
            [item.to_mapping() for item in evidence.analysis_domain_bindings]
        ),
        "active_plan_sha256": evidence.active_plan_sha256,
        "source_state_head_sha256": evidence.source_state_head_sha256,
        "source_authority_sha256s": list(evidence.source_authority_sha256s),
        "clustering_result_ref_sha256": evidence.clustering_result_ref_sha256,
        "clustering_result_sha256": evidence.clustering_result_sha256,
        "clustering_request_sha256": evidence.clustering_request_sha256,
        "clustering_generation_sha256": (evidence.clustering_generation_sha256),
        "input_population_sha256": evidence.input_population_sha256,
        "assignment_inventory_sha256": evidence.assignment_inventory_sha256,
        "feature_schema_sha256": evidence.feature_schema_sha256,
        "cluster_inventory_sha256": evidence.cluster_inventory_sha256,
        "cell_count": evidence.cell_count,
        "cluster_count": evidence.cluster_count,
        "evidence_sha256": evidence.evidence_sha256,
    }


def cluster_review_evidence_identity_contract() -> ArtifactIdentityContract:
    """Return the closed identity contract for persisted aggregate evidence."""

    return ArtifactIdentityContract(
        contract_id="batch-cluster-review-evidence/v1",
        artifact_kind=CLUSTER_REVIEW_EVIDENCE_ARTIFACT_KIND,
        schema_name=CLUSTER_REVIEW_EVIDENCE_SCHEMA_NAME,
        schema_version=CLUSTER_REVIEW_EVIDENCE_SCHEMA_VERSION,
        required_fields=(
            "mode",
            "phenotyping_group_id",
            "participant_ids",
            "project_entry_ids",
            "analysis_domain_binding_set_sha256",
            "active_plan_sha256",
            "source_state_head_sha256",
            "source_authority_sha256s",
            "clustering_result_ref_sha256",
            "clustering_result_sha256",
            "clustering_request_sha256",
            "clustering_generation_sha256",
            "input_population_sha256",
            "assignment_inventory_sha256",
            "feature_schema_sha256",
            "cluster_inventory_sha256",
            "cell_count",
            "cluster_count",
            "evidence_sha256",
        ),
        optional_fields=(),
        required_runtime_names=("python",),
    )


def accepted_cluster_label_review_owning_identity(
    value: AcceptedClusterLabelReview,
) -> dict[str, object]:
    """Return compact exact identity for one accepted aggregate decision."""

    if type(value) is not AcceptedClusterLabelReview:
        raise BatchPhenotypingError("accepted cluster-label review identity requires exact record")
    return {
        "mode": value.mode.value,
        "phenotyping_group_id": value.phenotyping_group_id,
        "participant_ids": list(value.participant_ids),
        "project_entry_ids": list(value.project_entry_ids),
        "analysis_domain_binding_set_sha256": digest(
            [item.to_mapping() for item in value.analysis_domain_bindings]
        ),
        "active_plan_sha256": value.active_plan_sha256,
        "source_state_head_sha256": value.source_state_head_sha256,
        "request_state_head_sha256": value.request_state_head_sha256,
        "accepted_state_head_sha256": value.accepted_state_head_sha256,
        "source_authority_sha256s": list(value.source_authority_sha256s),
        "clustering_result_ref_sha256": value.clustering_result_ref_sha256,
        "clustering_result_sha256": value.clustering_result_sha256,
        "clustering_request_sha256": value.clustering_request_sha256,
        "clustering_generation_sha256": value.clustering_generation_sha256,
        "input_population_sha256": value.input_population_sha256,
        "assignment_inventory_sha256": value.assignment_inventory_sha256,
        "cluster_inventory_sha256": value.cluster_inventory_sha256,
        "cluster_ids": list(value.cluster_ids),
        "cluster_labels_sha256": digest(dict(value.cluster_labels)),
        "evidence_artifact_ref_sha256": value.evidence_artifact_ref_sha256,
        "evidence_artifact_sha256": value.evidence_artifact_sha256,
        "evidence_sha256": value.evidence_sha256,
        "review_target_sha256": value.review_target_sha256,
        "interaction_request_id": value.interaction_request_id,
        "interaction_context_sha256": value.interaction_context_sha256,
        "response_sha256": value.response_sha256,
        "prior_attempt_id": value.prior_attempt_id,
        "prior_attempt_authorization_sha256": (value.prior_attempt_authorization_sha256),
        "prior_terminal_record_sha256": value.prior_terminal_record_sha256,
        "retry_attempt_id": value.retry_attempt_id,
        "retry_authorization_sha256": value.retry_authorization_sha256,
        "retry_ordinal": value.retry_ordinal,
        "result_sha256": value.result_sha256,
    }


def accepted_cluster_label_review_identity_contract() -> ArtifactIdentityContract:
    """Return the closed identity contract for accepted label decisions."""

    return ArtifactIdentityContract(
        contract_id="batch-accepted-cluster-label-review/v1",
        artifact_kind="accepted_cluster_label_review",
        schema_name="mif-batch-accepted-cluster-label-review",
        schema_version=1,
        required_fields=(
            "mode",
            "phenotyping_group_id",
            "participant_ids",
            "project_entry_ids",
            "analysis_domain_binding_set_sha256",
            "active_plan_sha256",
            "source_state_head_sha256",
            "request_state_head_sha256",
            "accepted_state_head_sha256",
            "source_authority_sha256s",
            "clustering_result_ref_sha256",
            "clustering_result_sha256",
            "clustering_request_sha256",
            "clustering_generation_sha256",
            "input_population_sha256",
            "assignment_inventory_sha256",
            "cluster_inventory_sha256",
            "cluster_ids",
            "cluster_labels_sha256",
            "evidence_artifact_ref_sha256",
            "evidence_artifact_sha256",
            "evidence_sha256",
            "review_target_sha256",
            "interaction_request_id",
            "interaction_context_sha256",
            "response_sha256",
            "prior_attempt_id",
            "prior_attempt_authorization_sha256",
            "prior_terminal_record_sha256",
            "retry_attempt_id",
            "retry_authorization_sha256",
            "retry_ordinal",
            "result_sha256",
        ),
        optional_fields=(),
        required_runtime_names=("python",),
    )


def reopen_cluster_review_evidence_authority(
    authority: BatchCommittedArtifactAuthority,
    *,
    maximum_byte_count: int = 64 * 1024 * 1024,
) -> BatchClusterReviewEvidence:
    """Reopen one exact state-authenticated aggregate-review evidence artifact."""

    try:
        (validated,) = validate_committed_artifact_authority_set(
            (authority,),
            expected_active_plan_sha256=authority.active_plan_sha256,
        )
    except (BatchArtifactError, AttributeError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "cluster review evidence authority is not state-authenticated"
        ) from exc
    artifact = validated.artifact_ref
    scope = artifact.scope
    public_contract = cluster_review_evidence_identity_contract()
    if (
        validated.producer_node_kind != CLUSTER_REVIEW_EVIDENCE_ARTIFACT_KIND
        or artifact.artifact_kind != CLUSTER_REVIEW_EVIDENCE_ARTIFACT_KIND
        or artifact.schema_name != CLUSTER_REVIEW_EVIDENCE_SCHEMA_NAME
        or artifact.schema_version != CLUSTER_REVIEW_EVIDENCE_SCHEMA_VERSION
        or artifact.locator is None
        or artifact.byte_count is None
        or artifact.owning_identity_contract_sha256 != public_contract.contract_sha256
        or artifact.identity_contract != public_contract
    ):
        raise BatchPhenotypingError("cluster review evidence artifact identity is not current")
    payload = read_bounded_file(
        artifact.locator,
        expected_byte_count=artifact.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise BatchPhenotypingError("cluster review evidence bytes differ")
    evidence = loads_batch_cluster_review_evidence(payload, maximum_byte_count=maximum_byte_count)
    binding_slides = {item.slide_id for item in evidence.analysis_domain_bindings}
    if evidence.mode.value == "per_slide":
        scope_matches_mode = (
            scope.validation_domain_id is not None
            and scope.sample_id == evidence.participant_ids[0]
            and len(binding_slides) == 1
            and scope.slide_id == next(iter(binding_slides))
            and scope.project_entry_id == evidence.project_entry_ids[0]
        )
    else:
        scope_matches_mode = scope.validation_domain_id is not None and all(
            item is None
            for item in (
                scope.sample_id,
                scope.slide_id,
                scope.project_entry_id,
            )
        )
    if (
        canonical_bytes(artifact.owning_identity)
        != canonical_bytes(cluster_review_evidence_owning_identity(evidence))
        or not scope_matches_mode
        or scope.analysis_domain_bindings != evidence.analysis_domain_bindings
        or tuple(item.sample_id for item in artifact.configuration_bindings)
        != evidence.participant_ids
    ):
        raise BatchPhenotypingError(
            "cluster review evidence content differs from artifact identity"
        )
    return evidence


def validate_cluster_review_evidence_authority(
    authority: BatchCommittedArtifactAuthority,
    *,
    target: AggregateClusterReviewTarget,
    maximum_byte_count: int = 64 * 1024 * 1024,
) -> BatchClusterReviewEvidence:
    """Reopen one exact committed evidence artifact for an aggregate target.

    This validator is intentionally independent of orchestration state.  The
    state owner must reconstruct and supply the committed authority for its
    current head before review-response authority can be issued.
    """

    if type(target) is not AggregateClusterReviewTarget:
        raise BatchPhenotypingError("cluster review evidence authority requires exact target")
    evidence = reopen_cluster_review_evidence_authority(
        authority,
        maximum_byte_count=maximum_byte_count,
    )
    artifact = authority.artifact_ref
    if (
        artifact.artifact_ref_sha256 != target.evidence_artifact_ref_sha256
        or artifact.artifact_ref_sha256 == target.clustering_result_ref_sha256
        or artifact.sha256 != target.evidence_artifact_sha256
        or evidence.active_plan_sha256 != target.active_plan_sha256
        or evidence.source_state_head_sha256 != target.source_state_head_sha256
        or evidence.mode != target.mode
        or evidence.phenotyping_group_id != target.phenotyping_group_id
        or evidence.participant_ids != target.participant_ids
        or evidence.project_entry_ids != target.project_entry_ids
        or evidence.analysis_domain_bindings != target.analysis_domain_bindings
        or evidence.source_authority_sha256s != target.source_authority_sha256s
        or evidence.clustering_result_ref_sha256 != target.clustering_result_ref_sha256
        or evidence.clustering_result_sha256 != target.clustering_result_sha256
        or evidence.clustering_request_sha256 != target.clustering_request_sha256
        or evidence.clustering_generation_sha256 != target.clustering_generation_sha256
        or evidence.input_population_sha256 != target.input_population_sha256
        or evidence.assignment_inventory_sha256 != target.assignment_inventory_sha256
        or evidence.cluster_inventory_sha256 != target.cluster_inventory_sha256
        or tuple(item.cluster_id for item in evidence.clusters) != target.cluster_ids
        or evidence.evidence_sha256 != target.evidence_sha256
    ):
        raise BatchPhenotypingError("cluster review evidence content differs from target")
    return evidence


def _validate_response_against_target(
    response: Mapping[str, object],
    target: AggregateClusterReviewTarget,
) -> Mapping[str, str]:
    if (
        response["target_id"] != target.target_id
        or response["group_id"] != target.group_id
        or response["group_sha256"] != target.group_sha256
        or response["node_id"] != target.node_id
        or response["prior_attempt_id"] != target.attempt_id
        or response["prior_attempt_authorization_sha256"] != target.attempt_authorization_sha256
        or response["prior_terminal_record_sha256"]
        != target.needs_interaction_terminal_record_sha256
        or response["interaction_request_id"] != target.interaction_request_id
        or response["interaction_context_sha256"] != target.interaction_context_sha256
        or response["action"] != "accept"
        or response["response_validator_id"] != CLUSTER_REVIEW_RESPONSE_VALIDATOR_ID
        or response["response_validator_version"] != CLUSTER_REVIEW_RESPONSE_VALIDATOR_VERSION
    ):
        raise BatchPhenotypingError(
            "cluster-label review response contradicts its aggregate target"
        )
    body = response["response"]
    if (not isinstance(body, Mapping) or "cluster_labels" not in body
            or set(body) - {"cluster_labels", "excluded_cluster_ids"}):
        raise BatchPhenotypingError("cluster-label review response body is not closed")
    labels = _cluster_label_mapping(body["cluster_labels"], allowed_cluster_ids=target.cluster_ids)
    _excluded_cluster_ids(body.get("excluded_cluster_ids", ()),
        allowed_cluster_ids=target.cluster_ids, cluster_labels=labels)
    return labels


def _validate_authorized_cluster_label_review_response(
    authority: AuthorizedClusterLabelReviewResponse,
    *,
    target: AggregateClusterReviewTarget,
    expected_context: BatchNodeExecutionContext,
) -> AuthorizedClusterLabelReviewResponse:
    """Validate authorized cluster label review response for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        type(authority) is not AuthorizedClusterLabelReviewResponse
        or authority._issuer is not _AUTHORIZED_CLUSTER_LABEL_REVIEW_RESPONSE_ISSUER
    ):
        raise BatchPhenotypingError("authorized cluster-label review response issuer is invalid")
    if type(target) is not AggregateClusterReviewTarget:
        raise BatchPhenotypingError("authorized cluster-label review requires exact target")
    if type(expected_context) is not BatchNodeExecutionContext:
        raise BatchPhenotypingError(
            "authorized cluster-label review requires exact execution context"
        )
    if (
        authority.current_execution is not authority._current_execution_binding
        or authority.evidence_authority is not authority._evidence_authority_binding
        or authority.target is not authority._target_binding
        or authority.accepted_response is not authority._accepted_response_binding
        or authority.target is not target
    ):
        raise BatchPhenotypingError(
            "authorized cluster-label review material was copied or transplanted"
        )
    try:
        current = validate_current_execution_authority(
            authority.current_execution,
            expected_context=expected_context,
        )
    except (BatchArtifactError, AttributeError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "authorized cluster-label review execution context differs"
        ) from exc
    authorization = expected_context.authorization
    clustering_scope = current.clustering_scope
    if (
        current.node_id != target.node_id
        or current.node_kind != "cluster_label_review"
        or current.participant_ids != target.participant_ids
        or current.project_entry_ids != target.project_entry_ids
        or current.analysis_domain_bindings != target.analysis_domain_bindings
        or current.input_artifact_ref_sha256s != (target.evidence_artifact_ref_sha256,)
        or clustering_scope is None
        or clustering_scope["mode"] != target.mode.value
        or clustering_scope["phenotyping_group_id"] != target.phenotyping_group_id
        or (
            target.mode.value == "per_slide"
            and clustering_scope["validation_domain_ids"] != (current.validation_domain_id,)
        )
        or authority.evidence_authority.state_head_sha256 != current.state_head_sha256
        or authority.evidence_authority.active_plan_sha256 != current.active_plan_sha256
        or len(expected_context.inputs) != 1
        or authority.evidence_authority.artifact_ref != expected_context.inputs[0]
        or authority.evidence_authority.artifact_ref.configuration_bindings
        != current.configuration_bindings
        or authorization.retry_ordinal <= 0
        or authorization.interaction_response_sha256
        != authority.accepted_response["response_sha256"]
        or authorization.interaction_predecessor_terminal_record_sha256
        != target.needs_interaction_terminal_record_sha256
    ):
        raise BatchPhenotypingError(
            "authorized cluster-label review scope or retry lineage differs"
        )
    _validate_response_against_target(authority.accepted_response, target)
    expected_seal = digest(
        _authority_seal_material(
            authority=authority,
            current_execution=authority.current_execution,
            evidence_authority=authority.evidence_authority,
            target=authority.target,
            accepted_response=authority.accepted_response,
        )
    )
    if authority._seal_sha256 != expected_seal:
        raise BatchPhenotypingError("authorized cluster-label review response seal is invalid")
    return authority


def _issue_authorized_cluster_label_review_response(
    *,
    current_execution: BatchCurrentExecutionAuthority,
    target: AggregateClusterReviewTarget,
    evidence_authority: BatchCommittedArtifactAuthority,
    accepted_response: Mapping[str, object],
    expected_context: BatchNodeExecutionContext,
) -> AuthorizedClusterLabelReviewResponse:
    """Private constructor called only after the state owner reopens lineage."""

    if type(target) is not AggregateClusterReviewTarget:
        raise BatchPhenotypingError("cluster-label review issuance requires exact target")
    response = _response_from_mapping(accepted_response)
    _validate_response_against_target(response, target)
    validate_cluster_review_evidence_authority(evidence_authority, target=target)
    authority = object.__new__(AuthorizedClusterLabelReviewResponse)
    values: dict[str, object] = {
        "current_execution": current_execution,
        "evidence_authority": evidence_authority,
        "target": target,
        "accepted_response": response,
        "_seal_sha256": digest(
            _authority_seal_material(
                authority=authority,
                current_execution=current_execution,
                evidence_authority=evidence_authority,
                target=target,
                accepted_response=response,
            )
        ),
        "_issuer": _AUTHORIZED_CLUSTER_LABEL_REVIEW_RESPONSE_ISSUER,
        "_current_execution_binding": current_execution,
        "_evidence_authority_binding": evidence_authority,
        "_target_binding": target,
        "_accepted_response_binding": response,
    }
    for name in AuthorizedClusterLabelReviewResponse.__dataclass_fields__:
        object.__setattr__(authority, name, values[name])
    return _validate_authorized_cluster_label_review_response(
        authority,
        target=target,
        expected_context=expected_context,
    )


def accept_cluster_label_review(
    *,
    target: AggregateClusterReviewTarget,
    authority: AuthorizedClusterLabelReviewResponse,
    expected_context: BatchNodeExecutionContext,
) -> AcceptedClusterLabelReview:
    """Create compact accepted review material from state-issued authority."""

    validated = _validate_authorized_cluster_label_review_response(
        authority,
        target=target,
        expected_context=expected_context,
    )
    labels = _validate_response_against_target(validated.accepted_response, target)
    authorization = expected_context.authorization
    return _create_accepted_cluster_label_review(
        target=target,
        cluster_labels=labels,
        excluded_cluster_ids=tuple(validated.accepted_response["response"].get(
            "excluded_cluster_ids", ())),
        accepted_state_head_sha256=validated.current_execution.state_head_sha256,
        response_sha256=validated.response_sha256,
        retry_attempt_id=authorization.attempt_id,
        retry_authorization_sha256=authorization.authorization_sha256,
        retry_ordinal=authorization.retry_ordinal,
    )


__all__ = [
    "AuthorizedClusterLabelReviewResponse",
    "AuthorizedClusterLabelReviewResponseResolver",
    "accepted_cluster_label_review_identity_contract",
    "accepted_cluster_label_review_owning_identity",
    "accept_cluster_label_review",
    "cluster_review_evidence_identity_contract",
    "cluster_review_evidence_owning_identity",
    "reopen_cluster_review_evidence_authority",
    "validate_cluster_review_evidence_authority",
]
