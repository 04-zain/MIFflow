"""Scientific owner for exact-section reviewed cluster-label application.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import cast

from mif_pipeline.cell_analysis import apply_cluster_labels
from mif_pipeline.cell_analysis.phenotyping_population import population_includes, NOT_PHENOTYPED
from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamExpectation,
    validate_measurement_row_stream,
)
from mif_pipeline.orchestration.accepted_analysis_domain_records import (
    ValidatedAcceptedAnalysisDomainProjection,
)
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    ValidatedPhenotypingModeProjection,
)
from mif_pipeline.orchestration.batch_segmentation_artifacts import (
    ValidatedSegmentationResultProjection,
)
from mif_pipeline.orchestration.phenotype_views import (
    PhenotypeViewStore,
    PreparedPhenotypeView,
    stable_cell_ids_sha256,
)
from mif_pipeline.pipeline_core import AnalysisDomainKind
from mif_pipeline.pipeline_core.config import PhenotypingMode
from mif_pipeline.pipeline_core.models import CellRecord

from ._common import (
    BatchPhenotypingError,
    canonical_bytes,
    digest,
    plain_json,
    read_bounded_file,
    sha256,
    text,
)
from .accepted_review import AcceptedClusterLabelReview
from .application_records import (
    ClusterLabelSectionApplication,
    ClusterLabelSectionPopulation,
    PhenotypeHierarchyAttestation,
    ValidatedClusterLabelImportProjection,
    ValidatedSemanticRawInventoryProjection,
)
from .records import (
    BatchClusteringExecutionResult,
    BatchClusteringLabelProjection,
)

# ==============================================================================
#                             Application execution
# ==============================================================================


def reopen_cluster_label_section_population(
    *,
    segmentation: ValidatedSegmentationResultProjection,
    measurements: ValidatedMeasurementsResultProjection,
    accepted_domains: tuple[ValidatedAcceptedAnalysisDomainProjection, ...],
    phenotyping_modes: tuple[ValidatedPhenotypingModeProjection, ...],
    authenticated_output_root: "Path",
) -> ClusterLabelSectionPopulation:
    """Reopen exact cell/domain membership from committed stage-owner views."""

    from pathlib import Path

    if type(segmentation) is not ValidatedSegmentationResultProjection:
        raise BatchPhenotypingError("section population requires segmentation owner")
    if type(measurements) is not ValidatedMeasurementsResultProjection:
        raise BatchPhenotypingError("section population requires measurement owner")
    if (
        type(accepted_domains) is not tuple
        or not accepted_domains
        or not all(
            type(item) is ValidatedAcceptedAnalysisDomainProjection for item in accepted_domains
        )
    ):
        raise BatchPhenotypingError("section population requires accepted domains")
    if (
        type(phenotyping_modes) is not tuple
        or not phenotyping_modes
        or not all(type(item) is ValidatedPhenotypingModeProjection for item in phenotyping_modes)
    ):
        raise BatchPhenotypingError("section population requires mode authorities")
    if not isinstance(authenticated_output_root, Path):
        raise BatchPhenotypingError("section population requires output root")
    bindings = measurements.analysis_domain_bindings
    if (
        measurements.sample_id != segmentation.sample_id
        or measurements.project_entry_id != segmentation.project_entry_id
        or measurements.slide_id != segmentation.slide_id
        or measurements.section_id != segmentation.section_id
        or measurements.canonical_image_id != segmentation.canonical_image_id
        or measurements.canonical_image_sha256 != segmentation.canonical_image_sha256
        or measurements.tissue_generation_id != segmentation.tissue_generation_id
        or measurements.tissue_path_object_id != segmentation.tissue_path_object_id
        or measurements.tissue_roi_id != segmentation.tissue_roi_id
        or measurements.segmentation_generation_id != segmentation.generation_ids[0]
        or bindings != segmentation.analysis_domain_bindings
        or measurements.segmentation_population_id != segmentation.segmentation_population_id
        or measurements.segmentation_population_sha256 != segmentation.section_population_sha256
    ):
        raise BatchPhenotypingError("measurement and segmentation section authorities differ")
    accepted_refs = tuple(
        sorted(item.artifact_ref.artifact_ref_sha256 for item in accepted_domains)
    )
    if accepted_refs != segmentation.accepted_analysis_domain_ref_sha256s:
        raise BatchPhenotypingError("accepted domain authority set differs from segmentation")
    binding_by_id = {item.analysis_domain_id: item for item in bindings}
    domain_kinds: dict[str, AnalysisDomainKind] = {}
    for projection in accepted_domains:
        if (
            projection.sample_id != segmentation.sample_id
            or projection.project_entry_id != segmentation.project_entry_id
            or projection.slide_id != segmentation.slide_id
            or projection.canonical_image_id != segmentation.canonical_image_id
            or projection.canonical_image_sha256 != segmentation.canonical_image_sha256
            or projection.tissue_generation_id != segmentation.tissue_generation_id
        ):
            raise BatchPhenotypingError("accepted domain belongs to another image")
        for owner in projection.domain_authorities:
            binding = binding_by_id.get(owner.analysis_domain_id)
            if binding is None:
                continue
            if (
                owner.section_id != segmentation.section_id
                or owner.tissue_generation_id != segmentation.tissue_generation_id
                or owner.tissue_path_object_id != segmentation.tissue_path_object_id
                or owner.tissue_roi_id != segmentation.tissue_roi_id
                or owner.analysis_domain_revision != binding.analysis_domain_revision
                or owner.analysis_domain_revision_sha256 != binding.analysis_domain_revision_sha256
                or owner.analysis_domain_id in domain_kinds
            ):
                raise BatchPhenotypingError("accepted domain revision or section differs")
            domain_kinds[owner.analysis_domain_id] = owner.analysis_domain_kind
    if set(domain_kinds) != set(binding_by_id):
        raise BatchPhenotypingError("accepted domains do not exactly cover section")
    mode_by_binding = {item.analysis_domain_binding: item for item in phenotyping_modes}
    if set(mode_by_binding) != set(bindings) or any(
        item.selected_mode is not PhenotypingMode.UNSUPERVISED_CLUSTERING
        or item.measurements_result_ref_sha256 != measurements.artifact_ref.artifact_ref_sha256
        or item.sample_id != segmentation.sample_id
        or item.project_entry_id != segmentation.project_entry_id
        or item.slide_id != segmentation.slide_id
        for item in phenotyping_modes
    ):
        raise BatchPhenotypingError(
            "phenotyping-mode authorities do not exactly select unsupervised section"
        )

    reference = measurements.measurement_rows
    descriptor = MarkerIntensityRowsDescriptor(
        path=reference.path,
        sha256=reference.sha256,
        byte_count=reference.byte_count,
        row_count=reference.row_count,
        content_schema_version=reference.content_schema_version,
    )
    marker_features = tuple(
        item
        for item in measurements.measurement_plan.features
        if item.kind == FeatureKind.EXPRESSION.value
    )
    cells: dict[str, str] = {}
    selections = tuple(plain_json(item.population_selection) for item in phenotyping_modes)
    if any(item != selections[0] for item in selections[1:]):
        raise BatchPhenotypingError("Phenotyping cells differ between analysis regions in this image")
    selection = selections[0]
    selected_cells: list[str] = []

    def retain_identity(cell_id: str, analysis_domain_id: str, values: tuple[float, ...], membership) -> None:
        del values
        if cell_id in cells:
            raise BatchPhenotypingError("measurement rows duplicate a stable cell ID")
        cells[cell_id] = analysis_domain_id
        if population_includes(selection, membership):
            selected_cells.append(cell_id)

    stream = validate_measurement_row_stream(
        descriptor=descriptor,
        authenticated_output_root=authenticated_output_root,
        expected=MeasurementRowStreamExpectation(
            project_entry_id=segmentation.project_entry_id,
            slide_id=segmentation.slide_id,
            section_id=segmentation.section_id,
            segmentation_generation_id=measurements.measurement_generation_id,
            measurement_generation_id=measurements.measurement_generation_id,
            segmentation_population_id=segmentation.segmentation_population_id,
            cell_dataset_sha256=segmentation.cell_evidence_sha256,
            analysis_domain_bindings=tuple(
                {
                    "analysis_domain_id": item.analysis_domain_id,
                    "analysis_domain_revision_sha256": (item.analysis_domain_revision_sha256),
                }
                for item in bindings
            ),
            feature_ids=tuple(item.feature_id for item in marker_features),
            measurement_names=tuple(item.measurement for item in marker_features),
        ),
        provisional_population_callback=retain_identity,
        population_selection=selection,
        expected_base_population=(segmentation.cell_count, segmentation.cell_ids_sha256,
                                  segmentation.domain_cell_mapping_sha256),
    )
    if (
        stream.cell_count != measurements.cell_count
        or stream.cell_ids_sha256 != measurements.cell_ids_sha256
        or stream.cell_to_domain_membership_sha256 != measurements.cell_to_domain_membership_sha256
        or stream.domain_cell_commitments_sha256 != measurements.domain_cell_commitments_sha256
        or stream.row_inventory_sha256 != reference.row_inventory_sha256
    ):
        raise BatchPhenotypingError("reopened measurement population differs")
    return ClusterLabelSectionPopulation(
        sample_id=segmentation.sample_id,
        project_entry_id=segmentation.project_entry_id,
        slide_id=segmentation.slide_id,
        canonical_image_id=segmentation.canonical_image_id,
        canonical_image_sha256=segmentation.canonical_image_sha256,
        section_id=segmentation.section_id,
        tissue_generation_id=segmentation.tissue_generation_id,
        tissue_path_object_id=segmentation.tissue_path_object_id,
        tissue_roi_id=segmentation.tissue_roi_id,
        segmentation_generation_id=measurements.segmentation_generation_id,
        segmentation_population_id=segmentation.segmentation_population_id,
        measurement_generation_id=measurements.measurement_generation_id,
        domain_kinds=domain_kinds,
        cell_domains=cells,
        selected_cell_ids=tuple(sorted(selected_cells)),
        cell_ids_sha256=measurements.cell_ids_sha256,
        cell_to_domain_membership_sha256=(measurements.cell_to_domain_membership_sha256),
        segmentation_result_ref_sha256=(segmentation.artifact_ref.artifact_ref_sha256),
        measurements_result_ref_sha256=(measurements.artifact_ref.artifact_ref_sha256),
        accepted_domain_ref_sha256s=accepted_refs,
        phenotyping_mode_ref_sha256s=tuple(
            sorted(item.artifact_ref.artifact_ref_sha256 for item in phenotyping_modes)
        ),
        saved_state_after_sha256=measurements.saved_state_after_sha256,
    )


@dataclass(frozen=True, slots=True)
class _LocalClusterCell:
    """Only the fields the public cluster-label owner reads and replaces.

    Geometry is deliberately absent: application is an identity/label join,
    and inventing coordinates solely to satisfy a broad storage model would
    create false scientific evidence.
    """

    cell_id: str
    slide_id: str
    section_id: str
    analysis_domain_id: str
    analysis_domain_kind: AnalysisDomainKind
    tissue_roi_id: str
    tissue_generation_id: str
    primary_phenotype: str
    phenotype_components: tuple[str, ...]
    phenotype_source: str
    phenotype_confidence: float | None
    cluster_id: str
    cluster_label: str | None
    phenotyping_generation_id: str


def _artifact_assignments(rows: tuple[Mapping[str, object], ...]) -> list[dict[str, object]]:
    return [
        {
            "cell_id": row["cell_id"],
            "analysis_domain_id": row["analysis_domain_id"],
            "primary_phenotype": row["primary_phenotype"],
            "phenotype_components": list(row["phenotype_components"]),
            "phenotype_source": row["phenotype_source"],
            "marker_states": {},
            "phenotype_confidence": row["phenotype_confidence"],
            "cluster_id": row["cluster_id"],
            "cluster_label": row["cluster_label"],
        }
        for row in rows
    ]


def build_section_cluster_label_application(
    *,
    accepted_review: AcceptedClusterLabelReview,
    accepted_review_artifact_ref_sha256: str,
    projection: BatchClusteringLabelProjection,
    population: ClusterLabelSectionPopulation,
    source_run_id: str,
    expected_saved_state_before_sha256: str,
) -> ClusterLabelSectionApplication:
    """Join one composite projection to one authenticated native population.

    This is the only boundary where pooled composite cell identities become
    QuPath-local cell IDs.  The native importer remains responsible for the
    subsequent current-entry hierarchy lookup and atomic save.
    """

    if type(accepted_review) is not AcceptedClusterLabelReview:
        raise BatchPhenotypingError(
            "cluster-label application requires an accepted review owner record"
        )
    if type(projection) is not BatchClusteringLabelProjection:
        raise BatchPhenotypingError(
            "cluster-label application requires an exact neutral projection"
        )
    if type(population) is not ClusterLabelSectionPopulation:
        raise BatchPhenotypingError(
            "cluster-label application requires an authenticated section population"
        )
    run_id = text(source_run_id, "source_run_id")
    accepted_review_ref = sha256(
        accepted_review_artifact_ref_sha256,
        "accepted_review_artifact_ref_sha256",
    )
    if (
        projection.sample_id != population.sample_id
        or projection.project_entry_id != population.project_entry_id
        or projection.clustering_generation_sha256 != accepted_review.clustering_generation_sha256
        or population.sample_id not in accepted_review.participant_ids
        or population.project_entry_id not in accepted_review.project_entry_ids
    ):
        raise BatchPhenotypingError(
            "neutral projection differs from accepted review or target entry"
        )
    accepted_binding_ids = {
        item.analysis_domain_id
        for item in accepted_review.analysis_domain_bindings
        if item.sample_id == population.sample_id
        and item.project_entry_id == population.project_entry_id
        and item.slide_id == population.slide_id
    }
    if not set(population.domain_kinds).issubset(accepted_binding_ids):
        raise BatchPhenotypingError(
            "section population differs from accepted analysis-domain scope"
        )

    section_assignments = tuple(
        item for item in projection.assignments if item.section_id == population.section_id
    )
    composite_ids = tuple(
        (item.sample_id, item.project_entry_id, item.cell_id) for item in section_assignments
    )
    expected_ids = population.selected_cell_ids
    actual_ids = tuple(item.cell_id for item in section_assignments)
    if (
        actual_ids != expected_ids
        or len(set(composite_ids)) != len(composite_ids)
        or any(
            item.sample_id != population.sample_id
            or item.project_entry_id != population.project_entry_id
            or item.slide_id != population.slide_id
            or item.analysis_domain_id != population.cell_domains.get(item.cell_id)
            for item in section_assignments
        )
    ):
        raise BatchPhenotypingError(
            "section projection does not exactly cover the authenticated population"
        )
    cells = tuple(
        _LocalClusterCell(
            cell_id=item.cell_id,
            slide_id=item.slide_id,
            section_id=item.section_id,
            analysis_domain_id=item.analysis_domain_id,
            analysis_domain_kind=population.domain_kinds[item.analysis_domain_id],
            tissue_roi_id=population.tissue_roi_id,
            tissue_generation_id=population.tissue_generation_id,
            primary_phenotype=item.cluster_id,
            phenotype_components=(),
            phenotype_source="cluster_id",
            phenotype_confidence=None,
            cluster_id=item.cluster_id,
            cluster_label=None,
            phenotyping_generation_id="unapplied",
        )
        for item in section_assignments
    )
    # ``apply_cluster_labels`` intentionally operates only on the shared label
    # fields above.  Casting avoids creating fictitious CellRecord geometry.
    local_cluster_ids = {item.cluster_id for item in section_assignments}
    excluded_cluster_ids = frozenset(accepted_review.excluded_cluster_ids)
    view = apply_cluster_labels(
        cast(tuple[CellRecord, ...], cells),
        {
            cluster_id: label
            for cluster_id, label in accepted_review.cluster_labels.items()
            if cluster_id in local_cluster_ids
        },
        source_identity=digest({
            "accepted_review_artifact_ref_sha256": accepted_review_ref,
            "source_run_id": run_id,
            "measurements_result_ref_sha256": population.measurements_result_ref_sha256,
            "phenotyping_mode_ref_sha256s": list(population.phenotyping_mode_ref_sha256s),
        }),
        excluded_cluster_ids=tuple(cluster_id for cluster_id in accepted_review.excluded_cluster_ids
                                   if cluster_id in local_cluster_ids),
    )
    rows_by_id = {row["cell_id"]: dict(row) for row in view.import_rows}
    for cell_id, domain_id in population.cell_domains.items():
        if cell_id in rows_by_id:
            continue
        rows_by_id[cell_id] = {
            "cell_id": cell_id, "slide_id": population.slide_id,
            "section_id": population.section_id, "analysis_domain_id": domain_id,
            "analysis_domain_kind": population.domain_kinds[domain_id].value,
            "tissue_roi_id": population.tissue_roi_id,
            "tissue_generation_id": population.tissue_generation_id,
            "phenotyping_generation_id": view.generation_id,
            "primary_phenotype": NOT_PHENOTYPED, "phenotype_components": (),
            "phenotype_source": "population_excluded", "phenotype_confidence": None,
            "cluster_id": None, "cluster_label": None,
        }
    rows = tuple(rows_by_id[cell_id] for cell_id in population.cell_domains)
    excluded_count = sum(row["primary_phenotype"] == NOT_PHENOTYPED for row in rows)
    cell_ids = tuple(population.cell_domains)
    cell_ids_sha256 = stable_cell_ids_sha256(cell_ids)
    if cell_ids_sha256 != population.cell_ids_sha256:
        raise BatchPhenotypingError(
            "native stable-cell digest differs from the clustering projection"
        )
    composite_projection_sha256 = digest(
        [
            {
                "sample_id": item.sample_id,
                "project_entry_id": item.project_entry_id,
                "section_id": item.section_id,
                "cell_id": item.cell_id,
                "analysis_domain_id": item.analysis_domain_id,
                "cluster_id": item.cluster_id,
            }
            for item in section_assignments
        ]
    )
    artifact = {
        "schema_version": "phenotype-view-artifact/v1",
        "mode": "unsupervised_clustering",
        "slide_id": population.slide_id,
        "canonical_image_sha256": population.canonical_image_sha256,
        "tissue_generation_id": population.tissue_generation_id,
        "phenotype_generation_id": view.generation_id,
        "section_id": population.section_id,
        "source_run_id": run_id,
        "cell_ids": list(cell_ids),
        "cell_ids_sha256": cell_ids_sha256,
        "assignments": _artifact_assignments(rows),
        "evidence": {
            "schema_version": "mif-batch-cluster-label-application-evidence/v1",
            "accepted_review_sha256": accepted_review.result_sha256,
            "excluded_cluster_ids": list(accepted_review.excluded_cluster_ids),
            "selected_count": len(population.selected_cell_ids),
            "outside_population_count": len(rows) - len(population.selected_cell_ids),
            "phenotyped_count": len(rows) - excluded_count,
            "excluded_count": excluded_count,
            "accepted_review_artifact_ref_sha256": accepted_review_ref,
            "clustering_result_ref_sha256": (accepted_review.clustering_result_ref_sha256),
            "clustering_result_sha256": accepted_review.clustering_result_sha256,
            "clustering_generation_sha256": (accepted_review.clustering_generation_sha256),
            "entry_projection_sha256": projection.assignment_inventory_sha256,
            "composite_projection_sha256": composite_projection_sha256,
            "segmentation_result_ref_sha256": (population.segmentation_result_ref_sha256),
            "measurements_result_ref_sha256": (population.measurements_result_ref_sha256),
            "accepted_domain_ref_sha256s": list(population.accepted_domain_ref_sha256s),
            "phenotyping_mode_ref_sha256s": list(population.phenotyping_mode_ref_sha256s),
            "segmentation_population_id": population.segmentation_population_id,
            "cell_to_domain_membership_sha256": (population.cell_to_domain_membership_sha256),
        },
        "lineage": {
            "schema_version": "mif-batch-phenotype-view-lineage/v1",
            "source_run_id": run_id,
            "accepted_review_sha256": accepted_review.result_sha256,
            "composite_projection_sha256": composite_projection_sha256,
            "source_ref_sha256s": sorted(
                {
                    accepted_review.clustering_result_ref_sha256,
                    accepted_review_ref,
                    population.segmentation_result_ref_sha256,
                    population.measurements_result_ref_sha256,
                    *population.accepted_domain_ref_sha256s,
                    *population.phenotyping_mode_ref_sha256s,
                }
            ),
        },
    }
    source_refs = tuple(artifact["lineage"]["source_ref_sha256s"])
    return ClusterLabelSectionApplication(
        sample_id=population.sample_id,
        project_entry_id=population.project_entry_id,
        slide_id=population.slide_id,
        canonical_image_id=population.canonical_image_id,
        canonical_image_sha256=population.canonical_image_sha256,
        section_id=population.section_id,
        tissue_generation_id=population.tissue_generation_id,
        tissue_path_object_id=population.tissue_path_object_id,
        tissue_roi_id=population.tissue_roi_id,
        segmentation_generation_id=population.segmentation_generation_id,
        segmentation_population_id=population.segmentation_population_id,
        measurement_generation_id=population.measurement_generation_id,
        clustering_generation_sha256=accepted_review.clustering_generation_sha256,
        accepted_review_sha256=accepted_review.result_sha256,
        composite_projection_sha256=composite_projection_sha256,
        cell_ids_sha256=cell_ids_sha256,
        cell_to_domain_membership_sha256=(population.cell_to_domain_membership_sha256),
        phenotype_generation_id=view.generation_id,
        import_rows=rows,
        artifact=artifact,
        source_ref_sha256s=source_refs,
        expected_saved_state_before_sha256=sha256(
            expected_saved_state_before_sha256, "expected_saved_state_before_sha256"),
    )


def derive_section_cluster_label_application(
    *,
    clustering_result: BatchClusteringExecutionResult,
    clustering_result_artifact_ref_sha256: str,
    accepted_review: AcceptedClusterLabelReview,
    accepted_review_artifact_ref_sha256: str,
    population: ClusterLabelSectionPopulation,
    source_run_id: str,
    expected_saved_state_before_sha256: str,
) -> ClusterLabelSectionApplication:
    """Derive one section plan from exact graph/review/population owners."""

    if type(clustering_result) is not BatchClusteringExecutionResult:
        raise BatchPhenotypingError("application requires an exact clustering result")
    graph_ref = sha256(
        clustering_result_artifact_ref_sha256,
        "clustering_result_artifact_ref_sha256",
    )
    if type(accepted_review) is not AcceptedClusterLabelReview:
        raise BatchPhenotypingError("application requires an exact accepted review")
    compositions = tuple(
        item
        for item in clustering_result.entry_composition
        if item.sample_id == population.sample_id
        and item.project_entry_id == population.project_entry_id
    )
    projections = tuple(
        item
        for item in clustering_result.entry_projections
        if item.sample_id == population.sample_id
        and item.project_entry_id == population.project_entry_id
    )
    if len(compositions) != 1 or len(projections) != 1:
        raise BatchPhenotypingError("clustering result does not contain one exact target entry")
    composition = compositions[0]
    projection = projections[0]
    section_assignments = tuple(
        item for item in projection.assignments if item.section_id == population.section_id
    )
    section_cell_ids = tuple(item.cell_id for item in section_assignments)
    if (
        graph_ref != accepted_review.clustering_result_ref_sha256
        or clustering_result.result_sha256 != accepted_review.clustering_result_sha256
        or clustering_result.request_sha256 != accepted_review.clustering_request_sha256
        or clustering_result.clustering_generation_sha256
        != accepted_review.clustering_generation_sha256
        or clustering_result.input_population_sha256 != accepted_review.input_population_sha256
        or clustering_result.output_assignment_sha256 != accepted_review.assignment_inventory_sha256
        or projection.source_cell_inventory_sha256 != composition.cell_inventory_sha256
        or projection.assignment_inventory_sha256
        != digest([item.to_mapping() for item in projection.assignments])
        or population.measurements_result_ref_sha256 not in composition.measurement_ref_sha256s
        or population.measurement_generation_id not in composition.measurement_generation_ids
        or population.segmentation_generation_id not in composition.segmentation_generation_ids
        or population.section_id not in composition.section_ids
        or set(population.domain_kinds) - set(composition.analysis_domain_ids)
        or section_cell_ids != population.selected_cell_ids
    ):
        raise BatchPhenotypingError(
            "clustering/review lineage differs from current measurement population"
        )
    return build_section_cluster_label_application(
        accepted_review=accepted_review,
        accepted_review_artifact_ref_sha256=(accepted_review_artifact_ref_sha256),
        projection=projection,
        population=population,
        source_run_id=source_run_id,
        expected_saved_state_before_sha256=expected_saved_state_before_sha256,
    )


def prepare_section_cluster_label_application(
    plan: ClusterLabelSectionApplication,
    *,
    store: PhenotypeViewStore,
) -> PreparedPhenotypeView:
    """Prepare immutable view bytes without advancing the selected-view head."""

    if type(plan) is not ClusterLabelSectionApplication:
        raise BatchPhenotypingError("label import preparation requires exact plan")
    if type(store) is not PhenotypeViewStore:
        raise BatchPhenotypingError("label import preparation requires view store")
    artifact = plain_json(plan.artifact)
    assert isinstance(artifact, Mapping)
    return store.prepare(artifact)


def _attestation(
    values: Mapping[str, object], *, inventory_sha256: str
) -> PhenotypeHierarchyAttestation:
    complete = {**values, "phenotype_view_inventory_sha256": inventory_sha256}
    return PhenotypeHierarchyAttestation(
        **complete,
        attestation_sha256=digest(
            {
                "schema_version": "mif-batch-phenotype-view-attestation/v1",
                **complete,
            }
        ),
    )  # type: ignore[arg-type]


def attest_committed_section_cluster_label_application(
    *,
    cluster_import: ValidatedClusterLabelImportProjection,
    raw_inventory: ValidatedSemanticRawInventoryProjection,
    store: PhenotypeViewStore,
    maximum_phenotype_artifact_byte_count: int = 512 * 1024 * 1024,
) -> PhenotypeHierarchyAttestation:
    """Reopen immutable view bytes and attest only committed import/raw owners."""

    if type(cluster_import) is not ValidatedClusterLabelImportProjection:
        raise BatchPhenotypingError("attestation requires committed import owner")
    if type(raw_inventory) is not ValidatedSemanticRawInventoryProjection:
        raise BatchPhenotypingError("attestation requires committed raw owner")
    if type(store) is not PhenotypeViewStore:
        raise BatchPhenotypingError("attestation requires phenotype view store")
    identity = cluster_import.identity
    snapshot = raw_inventory.snapshot
    import_ref_sha256 = cluster_import.artifact_ref.artifact_ref_sha256
    exact_fields = (
        "sample_id",
        "project_entry_id",
        "slide_id",
        "section_id",
        "canonical_image_sha256",
        "tissue_generation_id",
        "segmentation_population_id",
        "cell_ids_sha256",
        "cell_to_domain_membership_sha256",
    )
    if (
        cluster_import.state_head_sha256 != raw_inventory.state_head_sha256
        or cluster_import.active_plan_sha256 != raw_inventory.active_plan_sha256
        or raw_inventory.phenotype_source_artifact_ref_sha256 != import_ref_sha256
        or any(getattr(snapshot, field) != identity[field] for field in exact_fields)
        or snapshot.raw_export_ref_sha256 != raw_inventory.artifact_ref.artifact_ref_sha256
        or snapshot.saved_state_after_sha256 != identity["saved_state_after_sha256"]
    ):
        raise BatchPhenotypingError("committed import and raw hierarchy differ")
    locator_material = {
        "mode": "unsupervised_clustering",
        "slide_id": identity["slide_id"],
        "canonical_image_sha256": identity["canonical_image_sha256"],
        "tissue_generation_id": identity["tissue_generation_id"],
        "phenotype_generation_id": identity["phenotype_generation_id"],
        "section_id": identity["section_id"],
    }
    artifact_path = store.artifact_path(
        locator_material, artifact_sha256=identity["phenotype_view_artifact_sha256"]
    )
    try:
        byte_count = artifact_path.stat().st_size
    except OSError as exc:
        raise BatchPhenotypingError("prepared phenotype artifact is unavailable") from exc
    payload = read_bounded_file(
        artifact_path,
        expected_byte_count=byte_count,
        maximum_byte_count=maximum_phenotype_artifact_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != identity["phenotype_view_artifact_sha256"]:
        raise BatchPhenotypingError("prepared phenotype artifact bytes changed")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("prepared phenotype artifact is not JSON") from exc
    prepared = store.reopen_prepared(decoded)
    if (
        prepared.artifact_sha256 != identity["phenotype_view_artifact_sha256"]
        or prepared.lineage_sha256 != identity["phenotype_view_lineage_sha256"]
        or prepared.payload.get("phenotype_generation_id") != identity["phenotype_generation_id"]
        or prepared.payload.get("cell_ids_sha256") != identity["cell_ids_sha256"]
    ):
        raise BatchPhenotypingError("prepared phenotype view differs from import")

    hierarchy_evidence = store.mark_component_applied(prepared, identity["component_descriptor"])
    values = {
        "sample_id": identity["sample_id"],
        "project_entry_id": identity["project_entry_id"],
        "slide_id": identity["slide_id"],
        "section_id": identity["section_id"],
        "canonical_image_sha256": identity["canonical_image_sha256"],
        "tissue_generation_id": identity["tissue_generation_id"],
        "segmentation_population_id": identity["segmentation_population_id"],
        "phenotype_generation_id": identity["phenotype_generation_id"],
        "cluster_label_import_result_ref_sha256": import_ref_sha256,
        "raw_export_ref_sha256": snapshot.raw_export_ref_sha256,
        "raw_export_sha256": snapshot.raw_export_sha256,
        "cell_ids_sha256": identity["cell_ids_sha256"],
        "cell_to_domain_membership_sha256": (identity["cell_to_domain_membership_sha256"]),
        "phenotype_view_artifact_sha256": prepared.artifact_sha256,
        "phenotype_view_lineage_sha256": prepared.lineage_sha256,
        "saved_state_after_sha256": snapshot.saved_state_after_sha256,
        "hierarchy_evidence_sha256": digest(dict(hierarchy_evidence)),
    }
    _attestation(values, inventory_sha256="0" * 64)
    inventory = store.commit(prepared)
    return _attestation(values, inventory_sha256=inventory.inventory_sha256)


__all__ = [
    "attest_committed_section_cluster_label_application",
    "build_section_cluster_label_application",
    "derive_section_cluster_label_application",
    "prepare_section_cluster_label_application",
    "reopen_cluster_label_section_population",
]
