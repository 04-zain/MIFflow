"""Central committed-authority validation for batch protein-marker clustering.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import json

from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
)
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
)
from mif_pipeline.cell_analysis.measurement_plan import CellFeatureError
from ._common import (
    PREPARATION_PRODUCER_VERSION,
    BatchPhenotypingError,
    canonical_bytes,
    read_bounded_file,
)
from .records import BatchClusteringExecutionResult, UnsupervisedPreparationInput

# ==============================================================================
#                                   Authority
# ==============================================================================


def unsupervised_preparation_owning_identity(
    value: UnsupervisedPreparationInput,
) -> dict[str, object]:
    if type(value) is not UnsupervisedPreparationInput:
        raise BatchPhenotypingError("preparation must be an exact owner input")
    return {
        "sample_id": value.sample_id,
        "subject_id": value.subject_id,
        "project_entry_id": value.project_entry_id,
        "slide_id": value.slide_id,
        "section_id": value.section_id,
        "image_role": value.image_role,
        "phenotyping_group_id": value.phenotyping_group_id,
        "resolved_config_sha256": value.resolved_config_sha256,
        "panel_sha256": value.panel_sha256,
        "validation_domain_ids": list(value.validation_domain_ids),
        "analysis_domain_ids": list(value.analysis_domain_ids),
        "segmentation_generation_id": value.segmentation_generation_id,
        "measurement_generation_id": value.measurement_generation_id,
        "measurement_plan_sha256": value.measurement_plan_sha256,
        "measurement_result_ref_sha256": value.measurement_result_ref_sha256,
        "measurement_rows_reference_sha256": value.measurement_rows_reference_sha256,
        "measurement_rows_sha256": value.measurement_rows_sha256,
        "measurement_rows_byte_count": value.measurement_rows_byte_count,
        "measurement_matrix_bytes": value.measurement_matrix_bytes,
        "row_inventory_sha256": value.row_inventory_sha256,
        "measurement_feature_schema_sha256": value.measurement_feature_schema_sha256,
        "cell_ids_sha256": value.cell_ids_sha256,
        "cell_to_domain_membership_sha256": value.cell_to_domain_membership_sha256,
        "domain_cell_commitments_sha256": value.domain_cell_commitments_sha256,
        "measurement_saved_state_after_sha256": (value.measurement_saved_state_after_sha256),
        "measurement_execution_exchange_sha256": (value.measurement_execution_exchange_sha256),
        "population_selection": {"kind": value.population_selection["kind"], "class_ids": list(value.population_selection["class_ids"])},
        "clustering_mode": value.clustering_mode.value,
        "k": value.k,
        "resolution": value.resolution,
        "seed": value.seed,
        "feature_schema_sha256": value.feature_schema_sha256,
        "cell_inventory_sha256": value.cell_inventory_sha256,
        "cell_count": value.cell_count,
    }


def unsupervised_preparation_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="batch-unsupervised-input/v2",
        artifact_kind="unsupervised_preparation_result",
        schema_name="mif-batch-unsupervised-input",
        schema_version=2,
        required_fields=(
            "analysis_domain_ids",
            "cell_count",
            "cell_inventory_sha256",
            "population_selection.kind",
            "population_selection.class_ids",
            "clustering_mode",
            "feature_schema_sha256",
            "image_role",
            "k",
            "measurement_execution_exchange_sha256",
            "measurement_generation_id",
            "measurement_plan_sha256",
            "measurement_result_ref_sha256",
            "measurement_rows_byte_count",
            "measurement_matrix_bytes",
            "measurement_rows_reference_sha256",
            "measurement_rows_sha256",
            "measurement_saved_state_after_sha256",
            "measurement_feature_schema_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "domain_cell_commitments_sha256",
            "row_inventory_sha256",
            "panel_sha256",
            "phenotyping_group_id",
            "project_entry_id",
            "resolution",
            "resolved_config_sha256",
            "sample_id",
            "section_id",
            "seed",
            "segmentation_generation_id",
            "slide_id",
            "subject_id",
            "validation_domain_ids",
        ),
        optional_fields=(),
        required_runtime_names=("python",),
    )


def decode_committed_preparation(
    authority: BatchCommittedArtifactAuthority,
    *,
    maximum_byte_count: int,
) -> UnsupervisedPreparationInput:
    """Decode bytes only after the caller has centrally authenticated the authority."""

    ref = authority.artifact_ref
    if (
        authority.producer_node_kind != "unsupervised_preparation"
        or ref.artifact_kind != "unsupervised_preparation_result"
        or (ref.schema_name, ref.schema_version) != ("mif-batch-unsupervised-input", 2)
        or ref.identity_contract != unsupervised_preparation_identity_contract()
        or ref.owning_identity_contract_sha256
        != unsupervised_preparation_identity_contract().contract_sha256
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.producer_version != PREPARATION_PRODUCER_VERSION
        or ref.locator is None
        or ref.sha256 is None
        or ref.byte_count is None
        or "python" not in ref.runtime_versions
        or authority.execution_exchange_sha256 is not None
    ):
        raise BatchPhenotypingError("unsupervised preparation authority is invalid")
    payload = read_bounded_file(
        ref.locator,
        expected_byte_count=ref.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if len(payload) != ref.byte_count or hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise BatchPhenotypingError("unsupervised preparation bytes changed")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("unsupervised preparation is not JSON") from exc
    if canonical_bytes(decoded) != payload:
        raise BatchPhenotypingError("unsupervised preparation JSON is not canonical")
    preparation = UnsupervisedPreparationInput.from_mapping(decoded)
    if canonical_bytes(ref.owning_identity) != canonical_bytes(
        unsupervised_preparation_owning_identity(preparation)
    ):
        raise BatchPhenotypingError("unsupervised preparation identity differs from its bytes")
    if (
        ref.scope.sample_id != preparation.sample_id
        or ref.scope.slide_id != preparation.slide_id
        or ref.scope.project_entry_id != preparation.project_entry_id
        or (ref.scope.validation_domain_id,) != preparation.validation_domain_ids
        or tuple(item.analysis_domain_id for item in ref.scope.analysis_domain_bindings)
        != preparation.analysis_domain_ids
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0].sample_id != preparation.sample_id
        or ref.configuration_bindings[0].resolved_config_sha256
        != preparation.resolved_config_sha256
    ):
        raise BatchPhenotypingError("unsupervised preparation artifact scope differs")
    return preparation


def validate_measurement_authority(
    authority: BatchCommittedArtifactAuthority,
    preparation: UnsupervisedPreparationInput,
) -> ValidatedMeasurementsResultProjection:
    """Validate the narrow owner projection; never mint authority here."""

    try:
        projection = decode_committed_measurements_result(authority)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("measurement authority is invalid") from exc
    ref = projection.artifact_ref
    if ref.artifact_ref_sha256 != preparation.measurement_result_ref_sha256:
        raise BatchPhenotypingError("measurement authority reference differs")
    expected = (
        (projection.project_entry_id, preparation.project_entry_id),
        (projection.section_id, preparation.section_id),
        (ref.owning_identity.get("image_role"), "primary_mif"),
        (projection.segmentation_generation_id, preparation.segmentation_generation_id),
        (projection.measurement_generation_id, preparation.measurement_generation_id),
        (projection.measurement_plan_sha256, preparation.measurement_plan_sha256),
        (projection.feature_schema_sha256, preparation.measurement_feature_schema_sha256),
        (
            projection.measurement_rows.reference_sha256,
            preparation.measurement_rows_reference_sha256,
        ),
        (projection.measurement_rows.sha256, preparation.measurement_rows_sha256),
        (
            projection.measurement_rows.byte_count,
            preparation.measurement_rows_byte_count,
        ),
        (
            projection.measurement_rows.row_inventory_sha256,
            preparation.row_inventory_sha256,
        ),
        (projection.cell_count, preparation.cell_count),
        (projection.cell_count * len(preparation.feature_specs) * 8, preparation.measurement_matrix_bytes),
        (projection.cell_ids_sha256, preparation.cell_ids_sha256),
        (
            projection.cell_to_domain_membership_sha256,
            preparation.cell_to_domain_membership_sha256,
        ),
        (
            projection.domain_cell_commitments_sha256,
            preparation.domain_cell_commitments_sha256,
        ),
        (
            projection.saved_state_after_sha256,
            preparation.measurement_saved_state_after_sha256,
        ),
        (
            projection.execution_exchange_sha256,
            preparation.measurement_execution_exchange_sha256,
        ),
    )
    if any(actual != declared for actual, declared in expected):
        raise BatchPhenotypingError("measurement authority differs from unsupervised preparation")
    if (
        ref.scope.sample_id != preparation.sample_id
        or ref.scope.slide_id != preparation.slide_id
        or ref.scope.project_entry_id != preparation.project_entry_id
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0].sample_id != preparation.sample_id
        or ref.configuration_bindings[0].resolved_config_sha256
        != preparation.resolved_config_sha256
    ):
        raise BatchPhenotypingError("measurement authority scope differs")
    if (
        tuple(item.analysis_domain_id for item in projection.analysis_domain_bindings)
        != preparation.analysis_domain_ids
    ):
        raise BatchPhenotypingError(
            "preparation does not retain the complete measurement domain set"
        )
    configured = tuple(
        {
            "feature_id": item.feature_id,
            "source": item.measurement,
            "kind": item.kind,
            "channel_id": item.channel_id,
            "biomarker": item.biomarker,
            "compartment": item.compartment,
            "units": item.units,
            "producer": item.producer,
            "producer_version": item.producer_version,
            "opt_in": item.opt_in,
            "transform": item.transform,
            "weight": item.weight,
        }
        for item in preparation.feature_specs
    )
    try:
        bound = projection.measurement_plan.bind_clustering_features(configured)
    except (CellFeatureError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "clustering features do not bind to the owned measurement plan"
        ) from exc
    if bound != preparation.feature_specs:
        raise BatchPhenotypingError("clustering features differ from the owned measurement plan")
    commitments = {
        item["analysis_domain_id"]: item
        for item in projection.domain_cell_commitments
        if item["analysis_domain_id"] in preparation.analysis_domain_ids
    }
    if tuple(sorted(commitments)) != preparation.analysis_domain_ids:
        raise BatchPhenotypingError(
            "preparation domains do not exactly bind measurement commitments"
        )
    return projection


def clustering_result_owning_identity(
    value: BatchClusteringExecutionResult,
) -> dict[str, object]:
    """Return the compact identity projection used by the batch artifact edge."""

    if type(value) is not BatchClusteringExecutionResult:
        raise BatchPhenotypingError("clustering result must be an exact owner record")
    return {
        "mode": value.mode.value,
        "phenotyping_group_id": value.phenotyping_group_id,
        "participant_ids": list(value.participant_ids),
        "project_entry_ids": list(value.project_entry_ids),
        "request_sha256": value.request_sha256,
        "source_authority_sha256s": list(value.source_authority_sha256s),
        "source_state_head_sha256": value.source_state_head_sha256,
        "active_plan_sha256": value.active_plan_sha256,
        "input_population_sha256": value.input_population_sha256,
        "output_assignment_sha256": value.output_assignment_sha256,
        "graph_algorithm": value.evidence.graph_algorithm,
        "feature_transform_sha256": value.evidence.feature_transform_sha256,
        "node_identity_sha256": value.evidence.node_identity_sha256,
        "graph_sha256": value.evidence.graph_sha256,
        "clustering_generation_sha256": value.clustering_generation_sha256,
        "result_sha256": value.result_sha256,
        "label_review_status": value.label_review_status,
    }


def clustering_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="batch-clustering-result/v1",
        artifact_kind="clustered_phenotype_result",
        schema_name="mif-batch-clustering-result",
        schema_version=1,
        required_fields=(
            "active_plan_sha256",
            "clustering_generation_sha256",
            "feature_transform_sha256",
            "graph_algorithm",
            "graph_sha256",
            "input_population_sha256",
            "label_review_status",
            "mode",
            "node_identity_sha256",
            "output_assignment_sha256",
            "participant_ids",
            "phenotyping_group_id",
            "project_entry_ids",
            "request_sha256",
            "result_sha256",
            "source_authority_sha256s",
            "source_state_head_sha256",
        ),
        optional_fields=(),
        required_runtime_names=("igraph", "leidenalg", "numpy", "python", "scipy"),
    )


__all__ = [
    "clustering_result_identity_contract",
    "clustering_result_owning_identity",
    "decode_committed_preparation",
    "unsupervised_preparation_identity_contract",
    "unsupervised_preparation_owning_identity",
    "validate_measurement_authority",
]
