"""Bounded owner-side reopening for phenotyping capabilities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.orchestration.batch_analysis_domain_artifacts import (
    reopen_committed_accepted_analysis_domain_live_authority,
)
from mif_pipeline.orchestration.batch_artifacts import (
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
)
from mif_pipeline.orchestration.batch_segmentation_artifacts import (
    decode_committed_segmentation_result,
    reopen_committed_segmentation_cell_live_authority,
)
from mif_pipeline.orchestration.batch_state import BatchWorkspaceLease
from mif_pipeline.orchestration.batch_tissue_artifacts import (
    reopen_committed_accepted_tissue_live_authority,
)
from mif_pipeline.orchestration.project_stage_plan import ProjectStageRuntime

# ==============================================================================
#                                  Live inputs
# ==============================================================================


def reopen_committed_phenotyping_live_authorities(
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
    *,
    project_stage: ProjectStageRuntime,
    workspace_lease: BatchWorkspaceLease,
) -> tuple[object, object, tuple[object, ...]]:
    """Rehydrate the exact accepted tissue/domain/cell owner chain."""

    if (
        type(authorities) is not tuple
        or not authorities
        or not all(type(item) is BatchCommittedArtifactAuthority for item in authorities)
        or type(project_stage) is not ProjectStageRuntime
        or type(workspace_lease) is not BatchWorkspaceLease
    ):
        raise BatchArtifactError("phenotyping live-input rehydration authorities are invalid")
    by_kind: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in authorities:
        by_kind.setdefault(authority.artifact_ref.artifact_kind, []).append(authority)
    tissue_authorities = tuple(by_kind.get("tissue_acceptance_result", ()))
    domain_authorities = tuple(by_kind.get("accepted_analysis_domain", ()))
    segmentation_authorities = tuple(by_kind.get("segmentation_result", ()))
    if len(tissue_authorities) != 1 or len(domain_authorities) != 1 or not segmentation_authorities:
        raise BatchArtifactError("phenotyping live-input owner chain is incomplete")
    reopener = project_stage.committed_exchange_reopener(workspace_lease=workspace_lease)
    tissue = reopen_committed_accepted_tissue_live_authority(
        tissue_authorities[0], exchange_reopener=reopener
    )
    domains = reopen_committed_accepted_analysis_domain_live_authority(
        domain_authorities[0],
        accepted_tissue=tissue,
        exchange_reopener=reopener,
    )
    from mif_pipeline.orchestration.batch_measurement_artifacts import decode_committed_measurements_result
    measurements = {}
    for item in by_kind.get("measurements_result", ()):
        selected = decode_committed_measurements_result(item)
        if selected.section_id in measurements:
            raise BatchArtifactError("Current cell corrections have multiple measurement results for one section")
        measurements[selected.section_id] = selected
    cells = tuple(
        reopen_committed_segmentation_cell_live_authority(
            item,
            accepted_domains=domains,
            exchange_reopener=reopener,
            measurement_result=measurements.get(decode_committed_segmentation_result(item).section_id),
        )
        for item in segmentation_authorities
    )
    return tissue, domains, cells


__all__ = ["reopen_committed_phenotyping_live_authorities"]
