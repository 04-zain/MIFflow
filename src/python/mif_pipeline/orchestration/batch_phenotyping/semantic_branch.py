"""Issuer-only join of accepted phenotype application and raw semantic inventory.

The `ValidatedSemanticDomainBranch` record carries `phenotype_application`, `raw_inventory`,
`branch_kind`, `analysis_domain_bindings`, `configured_secondary_ids` and its remaining
declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mif_pipeline.orchestration.batch_artifacts import (
    AnalysisDomainBinding,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    validate_artifact_ref,
)

from ._common import BatchPhenotypingError, digest
from .application_codec import decode_committed_semantic_raw_inventory
from .application_records import ValidatedSemanticRawInventoryProjection
from .semantic_records import (
    ValidatedPhenotypeApplicationProjection,
    validate_phenotype_application_projection,
)

_BRANCH_ISSUER = object()


# ==============================================================================
#                                Semantic branch
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSemanticDomainBranch:
    """Process-local result of binding one raw snapshot to its phenotype source."""

    phenotype_application: ValidatedPhenotypeApplicationProjection
    raw_inventory: ValidatedSemanticRawInventoryProjection
    branch_kind: Literal["applicable", "no_secondary"]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    configured_secondary_ids: tuple[str, ...]
    candidate_secondary_roi_ids: tuple[str, ...]
    state_head_sha256: str
    active_plan_sha256: str
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic domain branch is issuer-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("semantic domain branch is nonserializable")


def _branch_material(value: ValidatedSemanticDomainBranch) -> dict[str, object]:
    raw = value.raw_inventory
    artifact_ref = validate_artifact_ref(raw.artifact_ref)
    snapshot = raw.snapshot
    return {
        "branch_object_identity": id(value),
        "phenotype_application_object_identity": id(value.phenotype_application),
        "raw_inventory_object_identity": id(raw),
        "raw_inventory": {
            "artifact_ref": artifact_ref.to_mapping(),
            "state_head_sha256": raw.state_head_sha256,
            "active_plan_sha256": raw.active_plan_sha256,
            "terminal_record_sha256": raw.terminal_record_sha256,
            "phenotype_source_artifact_ref_sha256": (raw.phenotype_source_artifact_ref_sha256),
            "configured_secondary_ids": list(raw.configured_secondary_ids),
            "candidate_secondary_roi_ids": list(raw.candidate_secondary_roi_ids),
            "snapshot": {
                "sample_id": snapshot.sample_id,
                "project_entry_id": snapshot.project_entry_id,
                "slide_id": snapshot.slide_id,
                "section_id": snapshot.section_id,
                "canonical_image_sha256": snapshot.canonical_image_sha256,
                "tissue_generation_id": snapshot.tissue_generation_id,
                "segmentation_population_id": snapshot.segmentation_population_id,
                "raw_export_ref_sha256": snapshot.raw_export_ref_sha256,
                "raw_export_sha256": snapshot.raw_export_sha256,
                "saved_state_after_sha256": snapshot.saved_state_after_sha256,
                "cell_count": snapshot.cell_count,
                "analysis_domain_ids": list(snapshot.analysis_domain_ids),
                "cell_ids_sha256": snapshot.cell_ids_sha256,
                "cell_to_domain_membership_sha256": (snapshot.cell_to_domain_membership_sha256),
            },
        },
        "branch_kind": value.branch_kind,
        "analysis_domain_bindings": [
            binding.to_mapping() for binding in value.analysis_domain_bindings
        ],
        "configured_secondary_ids": list(value.configured_secondary_ids),
        "candidate_secondary_roi_ids": list(value.candidate_secondary_roi_ids),
        "state_head_sha256": value.state_head_sha256,
        "active_plan_sha256": value.active_plan_sha256,
    }


def _issue_branch(**values: object) -> ValidatedSemanticDomainBranch:
    result = object.__new__(ValidatedSemanticDomainBranch)
    for name in ValidatedSemanticDomainBranch.__dataclass_fields__:
        object.__setattr__(result, name, values.get(name))
    object.__setattr__(result, "_issuer", _BRANCH_ISSUER)
    object.__setattr__(result, "_seal_sha256", digest(_branch_material(result)))
    return result


def validate_semantic_domain_branch(
    value: ValidatedSemanticDomainBranch,
) -> ValidatedSemanticDomainBranch:
    """Reject reconstructed, substituted, or mutated branch projections."""

    if type(value) is not ValidatedSemanticDomainBranch or value._issuer is not _BRANCH_ISSUER:
        raise BatchPhenotypingError("semantic domain branch is invalid")
    validate_phenotype_application_projection(value.phenotype_application)
    if type(value.raw_inventory) is not ValidatedSemanticRawInventoryProjection:
        raise BatchPhenotypingError("semantic raw inventory projection is invalid")
    try:
        material = _branch_material(value)
    except (BatchArtifactError, TypeError, ValueError, AttributeError) as exc:
        raise BatchPhenotypingError("semantic raw inventory projection is invalid") from exc
    if value._seal_sha256 != digest(material):
        raise BatchPhenotypingError("semantic domain branch is invalid")
    return value


def bind_committed_semantic_domain_branch(
    *,
    phenotype_application: ValidatedPhenotypeApplicationProjection,
    raw_inventory_authority: BatchCommittedArtifactAuthority,
) -> ValidatedSemanticDomainBranch:
    """Bind one decoded committed raw inventory to its accepted source exactly."""

    phenotype = validate_phenotype_application_projection(phenotype_application)
    try:
        raw = decode_committed_semantic_raw_inventory(raw_inventory_authority)
    except (BatchPhenotypingError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("semantic raw inventory is not current") from exc

    raw_scope = raw.artifact_ref.scope
    if (
        phenotype.source_artifact_kind == "supervised_phenotype_result"
        and phenotype.source_artifact_ref.scope.validation_domain_id
        != raw_scope.validation_domain_id
    ):
        raise BatchPhenotypingError(
            "semantic raw inventory validation domain differs from phenotype source"
        )
    if (
        raw.state_head_sha256 != phenotype.state_head_sha256
        or raw.active_plan_sha256 != phenotype.active_plan_sha256
        or raw.phenotype_source_artifact_ref_sha256
        != phenotype.source_artifact_ref.artifact_ref_sha256
        or raw_scope.sample_id != phenotype.sample_id
        or raw_scope.project_entry_id != phenotype.project_entry_id
        or raw_scope.slide_id != phenotype.slide_id
        or len(raw_scope.analysis_domain_bindings) != 1
        or raw_scope.analysis_domain_bindings[0] not in phenotype.analysis_domain_bindings
        or raw.artifact_ref.configuration_bindings
        != phenotype.source_artifact_ref.configuration_bindings
        or raw.snapshot.sample_id != phenotype.sample_id
        or raw.snapshot.project_entry_id != phenotype.project_entry_id
        or raw.snapshot.slide_id != phenotype.slide_id
        or raw.snapshot.section_id != phenotype.section_id
        or raw.snapshot.tissue_generation_id != phenotype.tissue_generation_id
        or raw.snapshot.analysis_domain_ids != (raw_scope.analysis_domain_bindings[0].analysis_domain_id,)
        or raw.snapshot.saved_state_after_sha256 != phenotype.saved_state_after_sha256
    ):
        raise BatchPhenotypingError("semantic raw inventory differs from phenotype source")

    branch_kind: Literal["applicable", "no_secondary"] = (
        "no_secondary"
        if not raw.configured_secondary_ids and not raw.candidate_secondary_roi_ids
        else "applicable"
    )
    return _issue_branch(
        phenotype_application=phenotype,
        raw_inventory=raw,
        branch_kind=branch_kind,
        analysis_domain_bindings=raw_scope.analysis_domain_bindings,
        configured_secondary_ids=raw.configured_secondary_ids,
        candidate_secondary_roi_ids=raw.candidate_secondary_roi_ids,
        state_head_sha256=raw.state_head_sha256,
        active_plan_sha256=raw.active_plan_sha256,
    )


__all__ = [
    "ValidatedSemanticDomainBranch",
    "bind_committed_semantic_domain_branch",
    "validate_semantic_domain_branch",
]
