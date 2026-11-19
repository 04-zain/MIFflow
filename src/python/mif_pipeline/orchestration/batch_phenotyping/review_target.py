"""Immutable aggregate interaction records for pooled cluster review.

The target deliberately has no singular sample, slide, or validation-domain identity. One
target describes one complete clustering graph and references the bounded review-evidence
artifact instead of copying its profiles.

The `AggregateClusterReviewTarget` record carries `target_id`, `node_id`, `attempt_id`,
`attempt_authorization_sha256`, `needs_interaction_terminal_record_sha256` and its remaining
declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from mif_pipeline.orchestration.batch_artifacts import AnalysisDomainBinding
from mif_pipeline.pipeline_core import validate_safe_path_segment
from mif_pipeline.pipeline_core.config import ClusteringMode

from ._common import (
    BatchPhenotypingError,
    canonical_ids,
    closed_mapping,
    digest,
    exact_tuple,
    sequence,
    sha256,
    text,
)
from .review_codec import dumps_batch_cluster_review_evidence
from .review_records import BatchClusterReviewEvidence

AGGREGATE_CLUSTER_REVIEW_TARGET_SCHEMA = "mif-batch-aggregate-cluster-review-target/v1"
# ==============================================================================
#                                 Review target
# ==============================================================================


def _interaction_request_id(
    *,
    node_id: str,
    attempt_id: str,
    attempt_authorization_sha256: str,
    interaction_context_sha256: str,
) -> str:
    """Use the generic durable interaction-request identity without state import."""

    try:
        node = validate_safe_path_segment(
            node_id, "interaction request node_id", error_type=BatchPhenotypingError
        )
        attempt = validate_safe_path_segment(
            attempt_id,
            "interaction request attempt_id",
            error_type=BatchPhenotypingError,
        )
    except TypeError as exc:
        raise BatchPhenotypingError("aggregate cluster-review request identity is invalid") from exc
    return "batch-interaction-" + digest(
        {
            "node_id": node,
            "attempt_id": attempt,
            "attempt_authorization_sha256": sha256(
                attempt_authorization_sha256,
                "interaction request attempt_authorization_sha256",
            ),
            "interaction_context_sha256": sha256(
                interaction_context_sha256,
                "interaction request interaction_context_sha256",
            ),
        }
    )


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


_TARGET_FIELDS = frozenset(
    {
        "schema_version",
        "target_id",
        "node_id",
        "attempt_id",
        "attempt_authorization_sha256",
        "needs_interaction_terminal_record_sha256",
        "interaction_request_id",
        "interaction_context_sha256",
        "active_plan_sha256",
        "source_state_head_sha256",
        "request_state_head_sha256",
        "mode",
        "phenotyping_group_id",
        "participant_ids",
        "project_entry_ids",
        "analysis_domain_bindings",
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
        "target_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class AggregateClusterReviewTarget:
    """One exact pending review for one complete clustering graph."""

    target_id: str
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    needs_interaction_terminal_record_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    active_plan_sha256: str
    source_state_head_sha256: str
    request_state_head_sha256: str
    mode: ClusteringMode
    phenotyping_group_id: str | None
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
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
    target_sha256: str

    def __post_init__(self) -> None:
        """Validate the AggregateClusterReviewTarget fields when the record is constructed.

        The checks cover ``participant_ids``, ``project_entry_ids``,
        ``source_authority_sha256s``, ``cluster_ids``, ``analysis_domain_bindings``,
        ``interaction_request_id``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        for name in ("target_id", "node_id", "attempt_id"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in (
            "attempt_authorization_sha256",
            "needs_interaction_terminal_record_sha256",
            "interaction_context_sha256",
            "active_plan_sha256",
            "source_state_head_sha256",
            "request_state_head_sha256",
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
            "target_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        try:
            mode = ClusteringMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("aggregate cluster-review mode is invalid") from exc
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
        cluster_ids = canonical_ids(self.cluster_ids, "cluster_ids")
        bindings = exact_tuple(self.analysis_domain_bindings, "analysis_domain_bindings")
        if not bindings or not all(type(item) is AnalysisDomainBinding for item in bindings):
            raise BatchPhenotypingError("aggregate cluster-review domain bindings are invalid")
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
            raise BatchPhenotypingError("aggregate cluster-review graph scope differs")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "phenotyping_group_id", group_id)
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(self, "project_entry_ids", entries)
        object.__setattr__(self, "analysis_domain_bindings", typed_bindings)
        object.__setattr__(self, "source_authority_sha256s", sources)
        object.__setattr__(self, "cluster_ids", cluster_ids)
        expected_request_id = _interaction_request_id(
            node_id=self.node_id,
            attempt_id=self.attempt_id,
            attempt_authorization_sha256=self.attempt_authorization_sha256,
            interaction_context_sha256=self.interaction_context_sha256,
        )
        if self.interaction_request_id != expected_request_id:
            raise BatchPhenotypingError(
                "aggregate cluster-review request identity is not canonical"
            )
        expected_target_id = "batch-cluster-review-target-" + digest(self._identity_mapping())
        if self.target_id != expected_target_id:
            raise BatchPhenotypingError("aggregate cluster-review target ID is not canonical")
        if self.target_sha256 != digest({**self._identity_mapping(), "target_id": self.target_id}):
            raise BatchPhenotypingError("aggregate cluster-review target digest differs")

    def _identity_mapping(self) -> dict[str, object]:
        return {
            "schema_version": AGGREGATE_CLUSTER_REVIEW_TARGET_SCHEMA,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "attempt_authorization_sha256": self.attempt_authorization_sha256,
            "needs_interaction_terminal_record_sha256": (
                self.needs_interaction_terminal_record_sha256
            ),
            "interaction_request_id": self.interaction_request_id,
            "interaction_context_sha256": self.interaction_context_sha256,
            "active_plan_sha256": self.active_plan_sha256,
            "source_state_head_sha256": self.source_state_head_sha256,
            "request_state_head_sha256": self.request_state_head_sha256,
            "mode": self.mode.value,
            "phenotyping_group_id": self.phenotyping_group_id,
            "participant_ids": list(self.participant_ids),
            "project_entry_ids": list(self.project_entry_ids),
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
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
        }

    @property
    def group_id(self) -> str:
        return "batch-cluster-review-group-" + digest(
            {
                "active_plan_sha256": self.active_plan_sha256,
                "mode": self.mode.value,
                "phenotyping_group_id": self.phenotyping_group_id,
                "participant_ids": list(self.participant_ids),
                "project_entry_ids": list(self.project_entry_ids),
                "clustering_generation_sha256": (self.clustering_generation_sha256),
                "evidence_sha256": self.evidence_sha256,
            }
        )

    @property
    def group_sha256(self) -> str:
        return digest(
            {
                "group_id": self.group_id,
                "target_sha256": self.target_sha256,
            }
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._identity_mapping(),
            "target_id": self.target_id,
            "target_sha256": self.target_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        node_id: str,
        attempt_id: str,
        attempt_authorization_sha256: str,
        needs_interaction_terminal_record_sha256: str,
        interaction_context_sha256: str,
        request_state_head_sha256: str,
        evidence_artifact_ref_sha256: str,
        evidence: BatchClusterReviewEvidence,
    ) -> "AggregateClusterReviewTarget":
        if type(evidence) is not BatchClusterReviewEvidence:
            raise BatchPhenotypingError("aggregate cluster review requires exact evidence")
        request_id = _interaction_request_id(
            node_id=node_id,
            attempt_id=attempt_id,
            attempt_authorization_sha256=attempt_authorization_sha256,
            interaction_context_sha256=interaction_context_sha256,
        )
        values: dict[str, object] = {
            "node_id": node_id,
            "attempt_id": attempt_id,
            "attempt_authorization_sha256": attempt_authorization_sha256,
            "needs_interaction_terminal_record_sha256": (needs_interaction_terminal_record_sha256),
            "interaction_request_id": request_id,
            "interaction_context_sha256": interaction_context_sha256,
            "active_plan_sha256": evidence.active_plan_sha256,
            "source_state_head_sha256": evidence.source_state_head_sha256,
            "request_state_head_sha256": request_state_head_sha256,
            "mode": evidence.mode,
            "phenotyping_group_id": evidence.phenotyping_group_id,
            "participant_ids": evidence.participant_ids,
            "project_entry_ids": evidence.project_entry_ids,
            "analysis_domain_bindings": evidence.analysis_domain_bindings,
            "source_authority_sha256s": evidence.source_authority_sha256s,
            "clustering_result_ref_sha256": (evidence.clustering_result_ref_sha256),
            "clustering_result_sha256": evidence.clustering_result_sha256,
            "clustering_request_sha256": evidence.clustering_request_sha256,
            "clustering_generation_sha256": (evidence.clustering_generation_sha256),
            "input_population_sha256": evidence.input_population_sha256,
            "assignment_inventory_sha256": (evidence.assignment_inventory_sha256),
            "cluster_inventory_sha256": evidence.cluster_inventory_sha256,
            "cluster_ids": tuple(item.cluster_id for item in evidence.clusters),
            "evidence_artifact_ref_sha256": evidence_artifact_ref_sha256,
            "evidence_artifact_sha256": hashlib.sha256(
                dumps_batch_cluster_review_evidence(evidence)
            ).hexdigest(),
            "evidence_sha256": evidence.evidence_sha256,
        }
        identity = {
            "schema_version": AGGREGATE_CLUSTER_REVIEW_TARGET_SCHEMA,
            **{
                name: (item.value if isinstance(item, ClusteringMode) else item)
                for name, item in values.items()
            },
        }
        identity["participant_ids"] = list(evidence.participant_ids)
        identity["project_entry_ids"] = list(evidence.project_entry_ids)
        identity["analysis_domain_bindings"] = [
            item.to_mapping() for item in evidence.analysis_domain_bindings
        ]
        identity["source_authority_sha256s"] = list(evidence.source_authority_sha256s)
        identity["cluster_ids"] = [item.cluster_id for item in evidence.clusters]
        target_id = "batch-cluster-review-target-" + digest(identity)
        return cls(
            **values,
            target_id=target_id,
            target_sha256=digest({**identity, "target_id": target_id}),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "AggregateClusterReviewTarget":
        """Decode the serialized fields into a validated AggregateClusterReviewTarget record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        value = closed_mapping(value, _TARGET_FIELDS, "aggregate cluster-review target")
        if value["schema_version"] != AGGREGATE_CLUSTER_REVIEW_TARGET_SCHEMA:
            raise BatchPhenotypingError("aggregate cluster-review target schema is unsupported")
        try:
            return cls(
                target_id=value["target_id"],
                node_id=value["node_id"],
                attempt_id=value["attempt_id"],
                attempt_authorization_sha256=value["attempt_authorization_sha256"],
                needs_interaction_terminal_record_sha256=value[
                    "needs_interaction_terminal_record_sha256"
                ],
                interaction_request_id=value["interaction_request_id"],
                interaction_context_sha256=value["interaction_context_sha256"],
                active_plan_sha256=value["active_plan_sha256"],
                source_state_head_sha256=value["source_state_head_sha256"],
                request_state_head_sha256=value["request_state_head_sha256"],
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
                target_sha256=value["target_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchPhenotypingError(
                "aggregate cluster-review target content is invalid"
            ) from exc


__all__ = [
    "AGGREGATE_CLUSTER_REVIEW_TARGET_SCHEMA",
    "AggregateClusterReviewTarget",
]
