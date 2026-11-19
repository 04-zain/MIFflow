"""Closed records for one accepted marker-rule application per section.

The `MarkerRuleSectionApplication` record carries `sample_id`, `project_entry_id`,
`slide_id`, `canonical_image_id`, `canonical_image_sha256` and its remaining declared
fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mif_pipeline.cell_analysis import AcceptedThreshold
from mif_pipeline.orchestration.batch_artifacts import ArtifactIdentityContract, BatchArtifactRef
from mif_pipeline.orchestration.phenotype_views import PhenotypeViewStore, PreparedPhenotypeView

# ==============================================================================
#                           Marker application records
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ValidatedMarkerGateProjection:
    artifact_ref: BatchArtifactRef
    validation_domain_id: str
    canonical_marker_ids: tuple[str, ...]
    marker_settings: Mapping[str, object]
    marker_settings_sha256: str
    setup_lineage: Mapping[str, object] | None
    measurement_authority_bindings: tuple[Mapping[str, object], ...]
    accepted_thresholds: tuple[AcceptedThreshold, ...]
    evidence_sha256: str
    domain_gate_sha256: str


@dataclass(frozen=True, slots=True)
class MarkerRuleSectionApplication:
    sample_id: str
    project_entry_id: str
    slide_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    segmentation_population_id: str
    measurement_generation_id: str
    validation_domain_id: str
    phenotype_generation_id: str
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    source_ref_sha256s: tuple[str, ...]
    expected_saved_state_before_sha256: str
    prepared: PreparedPhenotypeView
    view_store: PhenotypeViewStore


def marker_section_application_identity_contract() -> ArtifactIdentityContract:
    fields = {
        "component_descriptor.path",
        "component_descriptor.sha256",
        "sample_id",
        "project_entry_id",
        "slide_id",
        "canonical_image_id",
        "canonical_image_sha256",
        "section_id",
        "tissue_generation_id",
        "segmentation_population_id",
        "measurement_generation_id",
        "validation_domain_id",
        "phenotype_generation_id",
        "cell_ids_sha256",
        "cell_to_domain_membership_sha256",
        "phenotype_view_artifact_sha256",
        "phenotype_view_lineage_sha256",
        "phenotype_view_inventory_sha256",
        "source_ref_sha256s",
        "source_ref_set_sha256",
        "saved_state_before_sha256",
        "saved_state_after_sha256",
        "saved_state_disposition",
        "execution_exchange_sha256",
        "node_id",
        "attempt_id",
        "attempt_authorization_sha256",
        "request_codec_id",
        "request_codec_version",
    }
    return ArtifactIdentityContract(
        contract_id="mif-batch-marker-gate-section-application-identity/v1",
        artifact_kind="marker_phenotype_result",
        schema_name="mif-batch-marker-gate-section-application",
        schema_version=1,
        required_fields=tuple(sorted(fields)),
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


__all__ = [
    "MarkerRuleSectionApplication",
    "ValidatedMarkerGateProjection",
    "marker_section_application_identity_contract",
]
