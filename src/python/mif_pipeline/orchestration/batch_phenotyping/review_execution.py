"""Authenticated raw-profile aggregation for one committed clustering graph.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.cell_analysis.phenotyping_population import population_includes

from collections import Counter
from collections.abc import Mapping
import statistics

from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamError,
    MeasurementRowStreamExpectation,
    validate_measurement_row_stream,
)
from mif_pipeline.orchestration.batch_artifacts import (
    BatchCommittedArtifactAuthority,
)

from ._common import (
    BatchPhenotypingError,
    digest,
    feature_schema_sha256,
    integer,
)
from .authority import decode_committed_preparation, validate_measurement_authority
from .codec import decode_committed_batch_clustering_result
from .records import BatchClusterAssignment, UnsupervisedPreparationInput
from .review_records import (
    BatchClusterReviewEntryCount,
    BatchClusterReviewEvidence,
    BatchClusterReviewMarkerProfile,
    BatchClusterReviewSectionCount,
    BatchClusterReviewSummary,
)

# ==============================================================================
#                                Review execution
# ==============================================================================


def _authority_ref_sha256(value: BatchCommittedArtifactAuthority) -> str:
    return value.artifact_ref.artifact_ref_sha256


def _validate_row_commitments(*, authenticated, preparation, measurement) -> None:
    reference = measurement.measurement_rows
    checks = (
        (authenticated.cell_count, preparation.cell_count),
        (authenticated.cell_count, measurement.cell_count),
        (authenticated.cell_count, reference.row_count),
        (authenticated.cell_ids_sha256, preparation.cell_ids_sha256),
        (authenticated.cell_ids_sha256, measurement.cell_ids_sha256),
        (authenticated.cell_ids_sha256, reference.cell_ids_sha256),
        (
            authenticated.cell_to_domain_membership_sha256,
            preparation.cell_to_domain_membership_sha256,
        ),
        (
            authenticated.cell_to_domain_membership_sha256,
            measurement.cell_to_domain_membership_sha256,
        ),
        (
            authenticated.cell_to_domain_membership_sha256,
            reference.cell_to_domain_membership_sha256,
        ),
        (
            authenticated.domain_cell_commitments_sha256,
            preparation.domain_cell_commitments_sha256,
        ),
        (
            authenticated.domain_cell_commitments_sha256,
            measurement.domain_cell_commitments_sha256,
        ),
        (
            authenticated.domain_cell_commitments_sha256,
            reference.domain_cell_commitments_sha256,
        ),
        (authenticated.row_inventory_sha256, preparation.row_inventory_sha256),
        (authenticated.row_inventory_sha256, reference.row_inventory_sha256),
    )
    if any(actual != declared for actual, declared in checks):
        raise BatchPhenotypingError("cluster review rows differ from their measurement owner")
    if tuple(item.to_mapping() for item in authenticated.domain_cell_commitments) != tuple(
        dict(item) for item in measurement.domain_cell_commitments
    ):
        raise BatchPhenotypingError(
            "cluster review row domains differ from their measurement owner"
        )


def _validate_sources(
    *,
    clustering_authority: BatchCommittedArtifactAuthority,
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    maximum_clustering_result_byte_count: int,
):
    """Validate sources for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        type(clustering_authority) is not BatchCommittedArtifactAuthority
        or type(source_authorities) is not tuple
        or not source_authorities
        or not all(type(item) is BatchCommittedArtifactAuthority for item in source_authorities)
    ):
        raise BatchPhenotypingError("cluster review requires exact committed source authorities")
    result = decode_committed_batch_clustering_result(
        clustering_authority,
        source_authorities=source_authorities,
        maximum_byte_count=maximum_clustering_result_byte_count,
    )
    preparations = tuple(
        sorted(
            (
                item
                for item in source_authorities
                if item.producer_node_kind == "unsupervised_preparation"
            ),
            key=_authority_ref_sha256,
        )
    )
    measurements = tuple(
        sorted(
            (item for item in source_authorities if item.producer_node_kind == "measurements"),
            key=_authority_ref_sha256,
        )
    )
    if (
        not preparations
        or not measurements
        or len(preparations) + len(measurements) != len(source_authorities)
    ):
        raise BatchPhenotypingError(
            "cluster review sources must contain only preparation and measurement owners"
        )
    decoded = tuple(
        (
            decode_committed_preparation(
                authority,
                maximum_byte_count=maximum_clustering_result_byte_count,
            ),
            authority,
        )
        for authority in preparations
    )
    measurement_by_ref = {_authority_ref_sha256(item): item for item in measurements}
    required_measurement_refs = tuple(item.measurement_result_ref_sha256 for item, _ in decoded)
    if (
        len(measurement_by_ref) != len(measurements)
        or len(set(required_measurement_refs)) != len(required_measurement_refs)
        or set(measurement_by_ref) != set(required_measurement_refs)
    ):
        raise BatchPhenotypingError(
            "cluster review measurement owners do not exactly cover preparations"
        )
    resolved = tuple(
        (
            preparation,
            validate_measurement_authority(
                measurement_by_ref[preparation.measurement_result_ref_sha256],
                preparation,
            ),
        )
        for preparation, _ in decoded
    )
    first_features = resolved[0][0].feature_specs
    if (
        any(preparation.feature_specs != first_features for preparation, _ in resolved)
        or any(preparation.clustering_mode is not result.mode for preparation, _ in resolved)
        or tuple(sorted({preparation.sample_id for preparation, _ in resolved}))
        != result.participant_ids
        or tuple(sorted({preparation.project_entry_id for preparation, _ in resolved}))
        != result.project_entry_ids
        or result.evidence.feature_measurements
        != tuple(item.measurement for item in first_features)
        or result.evidence.canonical_biomarkers != tuple(item.biomarker for item in first_features)
    ):
        raise BatchPhenotypingError(
            "cluster review sources differ from the committed clustering graph"
        )
    return result, resolved, first_features


def build_batch_cluster_review_evidence(
    *,
    clustering_authority: BatchCommittedArtifactAuthority,
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    maximum_clustering_result_byte_count: int,
    maximum_measurement_rows_byte_count: int,
) -> BatchClusterReviewEvidence:
    """Build one profile-first summary from exact committed graph inputs.

    Row values are provisional until every pinned stream validates.  They are
    retained only long enough to calculate exact means and medians under the
    caller's aggregate byte ceiling, and never appear in the returned record.
    """

    clustering_limit = integer(
        maximum_clustering_result_byte_count,
        "maximum_clustering_result_byte_count",
        positive=True,
    )
    row_limit = integer(
        maximum_measurement_rows_byte_count,
        "maximum_measurement_rows_byte_count",
        positive=True,
    )
    result, resolved, selected_features = _validate_sources(
        clustering_authority=clustering_authority,
        source_authorities=source_authorities,
        maximum_clustering_result_byte_count=clustering_limit,
    )
    total_row_bytes = sum(measurement.measurement_rows.byte_count + measurement.cell_count * len(selected_features) * 8 for _, measurement in resolved)
    if total_row_bytes > row_limit:
        raise BatchPhenotypingError(
            "cluster review measurement rows exceed the aggregate byte bound"
        )

    assignments: Mapping[tuple[str, str, str], BatchClusterAssignment] = {
        (item.sample_id, item.project_entry_id, item.cell_id): item for item in result.assignments
    }
    if len(assignments) != len(result.assignments):
        raise BatchPhenotypingError("cluster review assignment identity is duplicated")
    cluster_ids = tuple(sorted({item.cluster_id for item in result.assignments}))
    values_by_cluster: dict[str, dict[str, list[float]]] = {
        cluster_id: {item.feature_id: [] for item in selected_features}
        for cluster_id in cluster_ids
    }
    entry_counts: dict[str, Counter[tuple[str, str]]] = {
        cluster_id: Counter() for cluster_id in cluster_ids
    }
    section_counts: dict[str, Counter[tuple[str, str, str, str]]] = {
        cluster_id: Counter() for cluster_id in cluster_ids
    }
    seen: set[tuple[str, str, str]] = set()
    section_scope: set[tuple[str, str, str, str]] = set()

    try:
        for preparation, measurement in resolved:
            reference = measurement.measurement_rows
            marker_features = tuple(
                item
                for item in measurement.measurement_plan.features
                if item.kind == FeatureKind.EXPRESSION.value
            )
            marker_index_by_id = {
                item.feature_id: index for index, item in enumerate(marker_features)
            }
            if (
                len(marker_index_by_id) != len(marker_features)
                or any(item.feature_id not in marker_index_by_id for item in selected_features)
                or feature_schema_sha256(marker_features) != reference.feature_schema_sha256
            ):
                raise BatchPhenotypingError(
                    "cluster review marker schema differs from its measurement owner"
                )
            selected_indexes = {
                item.feature_id: marker_index_by_id[item.feature_id] for item in selected_features
            }
            path = reference.path
            if path.parent.name != "measurement-rows" or path.parent.parent.name != "batch":
                raise BatchPhenotypingError(
                    "cluster review rows are outside their canonical owner location"
                )
            descriptor = MarkerIntensityRowsDescriptor(
                path=path,
                sha256=reference.sha256,
                byte_count=reference.byte_count,
                row_count=reference.row_count,
                content_schema_version=reference.content_schema_version,
            )
            expected = MeasurementRowStreamExpectation(
                project_entry_id=measurement.project_entry_id,
                slide_id=measurement.slide_id,
                section_id=measurement.section_id,
                segmentation_generation_id=measurement.measurement_generation_id,
                measurement_generation_id=measurement.measurement_generation_id,
                segmentation_population_id=measurement.segmentation_population_id,
                analysis_domain_bindings=tuple(
                    {
                        "analysis_domain_id": item.analysis_domain_id,
                        "analysis_domain_revision_sha256": (item.analysis_domain_revision_sha256),
                    }
                    for item in measurement.analysis_domain_bindings
                ),
                feature_ids=tuple(item.feature_id for item in marker_features),
                measurement_names=tuple(item.measurement for item in marker_features),
            )
            section_key = (
                preparation.sample_id,
                preparation.project_entry_id,
                preparation.slide_id,
                preparation.section_id,
            )
            section_scope.add(section_key)

            def receive(
                cell_id: str,
                analysis_domain_id: str,
                values: tuple[float, ...],
                membership: Mapping[str, object],
                *,
                _preparation: UnsupervisedPreparationInput = preparation,
                _section_key: tuple[str, str, str, str] = section_key,
                _selected_indexes: Mapping[str, int] = selected_indexes,
            ) -> None:
                if not population_includes(_preparation.population_selection, membership):
                    return
                identity = (
                    _preparation.sample_id,
                    _preparation.project_entry_id,
                    cell_id,
                )
                assignment = assignments.get(identity)
                if (
                    assignment is None
                    or identity in seen
                    or assignment.analysis_domain_id != analysis_domain_id
                    or assignment.slide_id != _preparation.slide_id
                    or assignment.section_id != _preparation.section_id
                ):
                    raise BatchPhenotypingError(
                        "cluster review row identity differs from clustering assignments"
                    )
                seen.add(identity)
                cluster_id = assignment.cluster_id
                for feature in selected_features:
                    values_by_cluster[cluster_id][feature.feature_id].append(
                        values[_selected_indexes[feature.feature_id]]
                    )
                entry_counts[cluster_id][
                    (_preparation.sample_id, _preparation.project_entry_id)
                ] += 1
                section_counts[cluster_id][_section_key] += 1

            authenticated = validate_measurement_row_stream(
                descriptor=descriptor,
                authenticated_output_root=path.parent.parent.parent,
                expected=expected,
                provisional_population_callback=receive,
                population_selection=preparation.population_selection,
            )
            _validate_row_commitments(
                authenticated=authenticated,
                preparation=preparation,
                measurement=measurement,
            )
    except BatchPhenotypingError:
        values_by_cluster.clear()
        entry_counts.clear()
        section_counts.clear()
        seen.clear()
        raise
    except (MeasurementRowStreamError, TypeError, ValueError) as exc:
        values_by_cluster.clear()
        entry_counts.clear()
        section_counts.clear()
        seen.clear()
        raise BatchPhenotypingError("cluster review measurement rows are invalid") from exc
    except Exception as exc:
        values_by_cluster.clear()
        entry_counts.clear()
        section_counts.clear()
        seen.clear()
        raise BatchPhenotypingError("cluster review row delivery failed") from exc

    if seen != set(assignments):
        raise BatchPhenotypingError(
            "cluster review rows do not exactly cover clustering assignments"
        )
    entry_scope = tuple(
        sorted({(item.sample_id, item.project_entry_id) for item in result.entry_composition})
    )
    canonical_sections = tuple(sorted(section_scope))
    ordered_features = tuple(sorted(selected_features, key=lambda item: item.feature_id))
    summaries = tuple(
        BatchClusterReviewSummary(
            cluster_id=cluster_id,
            cell_count=len(next(iter(values_by_cluster[cluster_id].values()))),
            marker_profiles=tuple(
                BatchClusterReviewMarkerProfile(
                    feature_id=feature.feature_id,
                    measurement=feature.measurement,
                    biomarker=feature.biomarker,
                    compartment=feature.compartment,
                    units=feature.units,
                    cell_count=len(values_by_cluster[cluster_id][feature.feature_id]),
                    raw_mean=statistics.fmean(values_by_cluster[cluster_id][feature.feature_id]),
                    raw_median=statistics.median(values_by_cluster[cluster_id][feature.feature_id]),
                )
                for feature in ordered_features
            ),
            entry_counts=tuple(
                BatchClusterReviewEntryCount(
                    sample_id=sample_id,
                    project_entry_id=entry_id,
                    cell_count=entry_counts[cluster_id][(sample_id, entry_id)],
                )
                for sample_id, entry_id in entry_scope
            ),
            section_counts=tuple(
                BatchClusterReviewSectionCount(
                    sample_id=sample_id,
                    project_entry_id=entry_id,
                    slide_id=slide_id,
                    section_id=section_id,
                    cell_count=section_counts[cluster_id][
                        (sample_id, entry_id, slide_id, section_id)
                    ],
                )
                for sample_id, entry_id, slide_id, section_id in canonical_sections
            ),
        )
        for cluster_id in cluster_ids
    )
    return BatchClusterReviewEvidence(
        mode=result.mode,
        phenotyping_group_id=result.phenotyping_group_id,
        participant_ids=result.participant_ids,
        project_entry_ids=result.project_entry_ids,
        analysis_domain_bindings=(clustering_authority.artifact_ref.scope.analysis_domain_bindings),
        active_plan_sha256=result.active_plan_sha256,
        source_state_head_sha256=result.source_state_head_sha256,
        source_authority_sha256s=result.source_authority_sha256s,
        clustering_result_ref_sha256=(clustering_authority.artifact_ref.artifact_ref_sha256),
        clustering_result_sha256=result.result_sha256,
        clustering_request_sha256=result.request_sha256,
        clustering_generation_sha256=result.clustering_generation_sha256,
        input_population_sha256=result.input_population_sha256,
        assignment_inventory_sha256=result.output_assignment_sha256,
        feature_schema_sha256=feature_schema_sha256(selected_features),
        cluster_inventory_sha256=digest([item.to_mapping() for item in summaries]),
        cell_count=len(result.assignments),
        cluster_count=len(summaries),
        clusters=summaries,
        refit_summary=(dict(parent_cluster_ids=result.refinement.selected_cluster_ids,
            child_cluster_ids=tuple(sorted(result.refinement.child_labels.values())),
            cell_count=result.refinement.evidence.node_count,
            method='flowsom' if result.refinement.evidence.graph_algorithm=='flowsom/v1' else 'phenograph',
            parent_result_sha256=result.refinement.parent_result_sha256) if result.refinement is not None else None),
    )


__all__ = ["build_batch_cluster_review_evidence"]
