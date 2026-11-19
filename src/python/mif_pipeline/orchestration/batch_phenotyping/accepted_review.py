"""Immutable accepted decision record for aggregate cluster-label review.

The `AcceptedClusterLabelReview` record carries `action`, `cluster_labels`, `mode`,
`phenotyping_group_id`, `participant_ids` and its remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from mif_pipeline.orchestration.batch_artifacts import AnalysisDomainBinding
from mif_pipeline.pipeline_core.config import ClusteringMode
from mif_pipeline.cell_analysis.phenotyping_population import NOT_PHENOTYPED

from ._common import (
    BatchPhenotypingError,
    canonical_ids,
    closed_mapping,
    digest,
    exact_tuple,
    integer,
    sequence,
    sha256,
    text,
)
from .review_target import AggregateClusterReviewTarget

ACCEPTED_CLUSTER_LABEL_REVIEW_SCHEMA = "mif-batch-accepted-cluster-label-review/v1"


# ==============================================================================
#                                Accepted review
# ==============================================================================


def _binding_key(
    value: AnalysisDomainBinding,
) -> tuple[str, str, str, int, str]:
    return (
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.project_entry_id,
    )


def _cluster_label_mapping(
    value: object,
    *,
    allowed_cluster_ids: tuple[str, ...] | None = None,
) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchPhenotypingError("cluster_labels must be a string-keyed mapping")
    labels = {
        text(key, "cluster_labels cluster_id"): text(item, "cluster_labels display_label")
        for key, item in value.items()
    }
    if any(label.strip() == NOT_PHENOTYPED for label in labels.values()):
        raise BatchPhenotypingError("Use Include in phenotyping to exclude a cluster instead of naming it Not phenotyped")
    if allowed_cluster_ids is not None and not set(labels) <= set(allowed_cluster_ids):
        raise BatchPhenotypingError("cluster_labels names an unknown cluster ID")
    return MappingProxyType(dict(sorted(labels.items())))


def _excluded_cluster_ids(value: object, *, allowed_cluster_ids: tuple[str, ...],
                          cluster_labels: Mapping[str, str]) -> tuple[str, ...]:
    """Keep excluded clusters explicit without changing the fitted membership."""
    excluded = canonical_ids(tuple(sequence(value, "excluded_cluster_ids")),
                             "excluded_cluster_ids", allow_empty=True)
    if not set(excluded) <= set(allowed_cluster_ids):
        raise BatchPhenotypingError("excluded_cluster_ids names an unknown cluster ID")
    if set(excluded).intersection(cluster_labels):
        raise BatchPhenotypingError("An excluded cluster cannot also have a phenotype name")
    return excluded


_ACCEPTED_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "cluster_labels",
        "mode",
        "phenotyping_group_id",
        "participant_ids",
        "project_entry_ids",
        "analysis_domain_bindings",
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
    }
)


@dataclass(frozen=True, slots=True)
class AcceptedClusterLabelReview:
    """Compact accepted labels over an unchanged neutral cluster inventory."""

    action: str
    cluster_labels: Mapping[str, str]
    mode: ClusteringMode
    phenotyping_group_id: str | None
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    active_plan_sha256: str
    source_state_head_sha256: str
    request_state_head_sha256: str
    accepted_state_head_sha256: str
    source_authority_sha256s: tuple[str, ...]
    clustering_result_ref_sha256: str
    clustering_result_sha256: str
    clustering_request_sha256: str
    clustering_generation_sha256: str
    input_population_sha256: str
    assignment_inventory_sha256: str
    cluster_inventory_sha256: str
    cluster_ids: tuple[str, ...]
    evidence_artifact_ref_sha256: str
    evidence_artifact_sha256: str
    evidence_sha256: str
    review_target_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    response_sha256: str
    prior_attempt_id: str
    prior_attempt_authorization_sha256: str
    prior_terminal_record_sha256: str
    retry_attempt_id: str
    retry_authorization_sha256: str
    retry_ordinal: int
    result_sha256: str
    excluded_cluster_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the AcceptedClusterLabelReview fields when the record is constructed.

        The checks cover ``action``, ``cluster_ids``, ``cluster_labels``,
        ``participant_ids``, ``project_entry_ids``, ``source_authority_sha256s``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if self.action != "accept":
            raise BatchPhenotypingError("accepted cluster-label review action must be accept")
        cluster_ids = canonical_ids(self.cluster_ids, "cluster_ids")
        labels = _cluster_label_mapping(self.cluster_labels, allowed_cluster_ids=cluster_ids)
        excluded = _excluded_cluster_ids(self.excluded_cluster_ids,
            allowed_cluster_ids=cluster_ids, cluster_labels=labels)
        try:
            mode = ClusteringMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("accepted cluster-label review mode is invalid") from exc
        group_id = (
            None
            if self.phenotyping_group_id is None
            else text(self.phenotyping_group_id, "phenotyping_group_id")
        )
        if group_id is not None:
            raise BatchPhenotypingError("phenotyping_group_id is a reserved null field")
        participants = canonical_ids(self.participant_ids, "participant_ids")
        entries = canonical_ids(self.project_entry_ids, "project_entry_ids")
        sources = canonical_ids(self.source_authority_sha256s, "source_authority_sha256s")
        for value in sources:
            sha256(value, "source_authority_sha256s")
        bindings = exact_tuple(self.analysis_domain_bindings, "analysis_domain_bindings")
        if not bindings or not all(type(item) is AnalysisDomainBinding for item in bindings):
            raise BatchPhenotypingError("accepted cluster-label review bindings are invalid")
        typed_bindings: tuple[AnalysisDomainBinding, ...] = bindings  # type: ignore[assignment]
        binding_keys = tuple(_binding_key(item) for item in typed_bindings)
        if (
            binding_keys != tuple(sorted(binding_keys))
            or len(set(binding_keys)) != len(binding_keys)
            or tuple(sorted({item.sample_id for item in typed_bindings})) != participants
            or tuple(sorted({item.project_entry_id for item in typed_bindings})) != entries
            or mode is ClusteringMode.PER_SLIDE
            and (len(participants) != 1 or len(entries) != 1)
        ):
            raise BatchPhenotypingError("accepted cluster-label review graph scope differs")
        for name in (
            "active_plan_sha256",
            "source_state_head_sha256",
            "request_state_head_sha256",
            "accepted_state_head_sha256",
            "clustering_result_ref_sha256",
            "clustering_result_sha256",
            "clustering_request_sha256",
            "clustering_generation_sha256",
            "input_population_sha256",
            "assignment_inventory_sha256",
            "cluster_inventory_sha256",
            "evidence_artifact_ref_sha256",
            "evidence_artifact_sha256",
            "evidence_sha256",
            "review_target_sha256",
            "interaction_context_sha256",
            "response_sha256",
            "prior_attempt_authorization_sha256",
            "prior_terminal_record_sha256",
            "retry_authorization_sha256",
            "result_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        for name in (
            "interaction_request_id",
            "prior_attempt_id",
            "retry_attempt_id",
        ):
            object.__setattr__(self, name, text(getattr(self, name), name))
        retry_ordinal = integer(self.retry_ordinal, "retry_ordinal", positive=True)
        object.__setattr__(self, "cluster_labels", labels)
        object.__setattr__(self, "excluded_cluster_ids", excluded)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "phenotyping_group_id", group_id)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        object.__setattr__(self, "analysis_domain_bindings", typed_bindings)
        object.__setattr__(self, "source_authority_sha256s", sources)
        object.__setattr__(self, "cluster_ids", cluster_ids)
        object.__setattr__(self, "retry_ordinal", retry_ordinal)
        if self.result_sha256 != digest(self.material_mapping()):
            raise BatchPhenotypingError("accepted cluster-label review digest differs")

    def material_mapping(self) -> dict[str, object]:
        return {
            "schema_version": ACCEPTED_CLUSTER_LABEL_REVIEW_SCHEMA,
            "action": self.action,
            "cluster_labels": dict(self.cluster_labels),
            **({"excluded_cluster_ids": list(self.excluded_cluster_ids)}
               if self.excluded_cluster_ids else {}),
            "mode": self.mode.value,
            "phenotyping_group_id": self.phenotyping_group_id,
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "active_plan_sha256": self.active_plan_sha256,
            "source_state_head_sha256": self.source_state_head_sha256,
            "request_state_head_sha256": self.request_state_head_sha256,
            "accepted_state_head_sha256": self.accepted_state_head_sha256,
            "source_authority_sha256s": list(self.source_authority_sha256s),
            "clustering_result_ref_sha256": self.clustering_result_ref_sha256,
            "clustering_result_sha256": self.clustering_result_sha256,
            "clustering_request_sha256": self.clustering_request_sha256,
            "clustering_generation_sha256": self.clustering_generation_sha256,
            "input_population_sha256": self.input_population_sha256,
            "assignment_inventory_sha256": self.assignment_inventory_sha256,
            "cluster_inventory_sha256": self.cluster_inventory_sha256,
            "cluster_ids": list(self.cluster_ids),
            "evidence_artifact_ref_sha256": self.evidence_artifact_ref_sha256,
            "evidence_artifact_sha256": self.evidence_artifact_sha256,
            "evidence_sha256": self.evidence_sha256,
            "review_target_sha256": self.review_target_sha256,
            "interaction_request_id": self.interaction_request_id,
            "interaction_context_sha256": self.interaction_context_sha256,
            "response_sha256": self.response_sha256,
            "prior_attempt_id": self.prior_attempt_id,
            "prior_attempt_authorization_sha256": (self.prior_attempt_authorization_sha256),
            "prior_terminal_record_sha256": self.prior_terminal_record_sha256,
            "retry_attempt_id": self.retry_attempt_id,
            "retry_authorization_sha256": self.retry_authorization_sha256,
            "retry_ordinal": self.retry_ordinal,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self.material_mapping(), "result_sha256": self.result_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "AcceptedClusterLabelReview":
        """Decode the serialized fields into a validated AcceptedClusterLabelReview record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        fields = _ACCEPTED_FIELDS | ({"excluded_cluster_ids"}
            if isinstance(value, Mapping) and "excluded_cluster_ids" in value else set())
        value = closed_mapping(value, fields, "accepted cluster-label review")
        if value["schema_version"] != ACCEPTED_CLUSTER_LABEL_REVIEW_SCHEMA:
            raise BatchPhenotypingError("accepted cluster-label review schema is unsupported")
        try:
            return cls(
                action=value["action"],
                cluster_labels=value["cluster_labels"],
                excluded_cluster_ids=tuple(sequence(value.get("excluded_cluster_ids", ()),
                                                   "excluded_cluster_ids")),
                mode=ClusteringMode(value["mode"]),
                phenotyping_group_id=value["phenotyping_group_id"],
                participant_ids=tuple(sequence(value["participant_ids"], "participant_ids")),
                project_entry_ids=tuple(sequence(value["project_entry_ids"], "project_entry_ids")),
                analysis_domain_bindings=tuple(
                    AnalysisDomainBinding.from_mapping(item)
                    for item in sequence(
                        value["analysis_domain_bindings"],
                        "analysis_domain_bindings",
                    )
                ),
                active_plan_sha256=value["active_plan_sha256"],
                source_state_head_sha256=value["source_state_head_sha256"],
                request_state_head_sha256=value["request_state_head_sha256"],
                accepted_state_head_sha256=value["accepted_state_head_sha256"],
                source_authority_sha256s=tuple(
                    sequence(
                        value["source_authority_sha256s"],
                        "source_authority_sha256s",
                    )
                ),
                clustering_result_ref_sha256=value["clustering_result_ref_sha256"],
                clustering_result_sha256=value["clustering_result_sha256"],
                clustering_request_sha256=value["clustering_request_sha256"],
                clustering_generation_sha256=value["clustering_generation_sha256"],
                input_population_sha256=value["input_population_sha256"],
                assignment_inventory_sha256=value["assignment_inventory_sha256"],
                cluster_inventory_sha256=value["cluster_inventory_sha256"],
                cluster_ids=tuple(sequence(value["cluster_ids"], "cluster_ids")),
                evidence_artifact_ref_sha256=value["evidence_artifact_ref_sha256"],
                evidence_artifact_sha256=value["evidence_artifact_sha256"],
                evidence_sha256=value["evidence_sha256"],
                review_target_sha256=value["review_target_sha256"],
                interaction_request_id=value["interaction_request_id"],
                interaction_context_sha256=value["interaction_context_sha256"],
                response_sha256=value["response_sha256"],
                prior_attempt_id=value["prior_attempt_id"],
                prior_attempt_authorization_sha256=value["prior_attempt_authorization_sha256"],
                prior_terminal_record_sha256=value["prior_terminal_record_sha256"],
                retry_attempt_id=value["retry_attempt_id"],
                retry_authorization_sha256=value["retry_authorization_sha256"],
                retry_ordinal=value["retry_ordinal"],
                result_sha256=value["result_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchPhenotypingError("accepted cluster-label review content is invalid") from exc


def _create_accepted_cluster_label_review(
    *,
    target: AggregateClusterReviewTarget,
    cluster_labels: Mapping[str, str],
    accepted_state_head_sha256: str,
    response_sha256: str,
    retry_attempt_id: str,
    retry_authorization_sha256: str,
    retry_ordinal: int,
    excluded_cluster_ids: tuple[str, ...] = (),
) -> AcceptedClusterLabelReview:
    if type(target) is not AggregateClusterReviewTarget:
        raise BatchPhenotypingError("accepted cluster-label review requires exact target")
    labels = _cluster_label_mapping(cluster_labels, allowed_cluster_ids=target.cluster_ids)
    excluded = _excluded_cluster_ids(excluded_cluster_ids,
        allowed_cluster_ids=target.cluster_ids, cluster_labels=labels)
    values: dict[str, object] = {
        "action": "accept",
        "cluster_labels": labels,
        **({"excluded_cluster_ids": excluded} if excluded else {}),
        "mode": target.mode,
        "phenotyping_group_id": target.phenotyping_group_id,
        "participant_ids": target.participant_ids,
        "project_entry_ids": target.project_entry_ids,
        "analysis_domain_bindings": target.analysis_domain_bindings,
        "active_plan_sha256": target.active_plan_sha256,
        "source_state_head_sha256": target.source_state_head_sha256,
        "request_state_head_sha256": target.request_state_head_sha256,
        "accepted_state_head_sha256": accepted_state_head_sha256,
        "source_authority_sha256s": target.source_authority_sha256s,
        "clustering_result_ref_sha256": target.clustering_result_ref_sha256,
        "clustering_result_sha256": target.clustering_result_sha256,
        "clustering_request_sha256": target.clustering_request_sha256,
        "clustering_generation_sha256": target.clustering_generation_sha256,
        "input_population_sha256": target.input_population_sha256,
        "assignment_inventory_sha256": target.assignment_inventory_sha256,
        "cluster_inventory_sha256": target.cluster_inventory_sha256,
        "cluster_ids": target.cluster_ids,
        "evidence_artifact_ref_sha256": target.evidence_artifact_ref_sha256,
        "evidence_artifact_sha256": target.evidence_artifact_sha256,
        "evidence_sha256": target.evidence_sha256,
        "review_target_sha256": target.target_sha256,
        "interaction_request_id": target.interaction_request_id,
        "interaction_context_sha256": target.interaction_context_sha256,
        "response_sha256": response_sha256,
        "prior_attempt_id": target.attempt_id,
        "prior_attempt_authorization_sha256": (target.attempt_authorization_sha256),
        "prior_terminal_record_sha256": (target.needs_interaction_terminal_record_sha256),
        "retry_attempt_id": retry_attempt_id,
        "retry_authorization_sha256": retry_authorization_sha256,
        "retry_ordinal": retry_ordinal,
    }
    material = {
        "schema_version": ACCEPTED_CLUSTER_LABEL_REVIEW_SCHEMA,
        **{
            name: (
                item.value
                if isinstance(item, ClusteringMode)
                else (
                    dict(item)
                    if name == "cluster_labels"
                    else (
                        [binding.to_mapping() for binding in item]
                        if name == "analysis_domain_bindings"
                        else (
                            list(item)
                            if name
                            in {
                                "participant_ids",
                                "project_entry_ids",
                                "source_authority_sha256s",
                                "cluster_ids",
                                "excluded_cluster_ids",
                            }
                            else item
                        )
                    )
                )
            )
            for name, item in values.items()
        },
    }
    return AcceptedClusterLabelReview(
        **values, result_sha256=digest(material)
    )  # type: ignore[arg-type]


__all__ = [
    "ACCEPTED_CLUSTER_LABEL_REVIEW_SCHEMA",
    "AcceptedClusterLabelReview",
]
