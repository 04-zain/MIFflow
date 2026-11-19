"""Scientific owner composition for section-scoped supervised deployment.

The `PreparedSupervisedSectionView` record carries `deployment`, `stored_prediction`,
`review`, `result`, `phenotype_generation_id` and its remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from mif_pipeline.cell_analysis import (
    SupervisedDeploymentCellIdentity,
    SupervisedDeploymentClassifierIdentity,
    SupervisedDeploymentPredictionResult,
    reconcile_deployment_predictions,
)
from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.phenotyping_population import population_includes
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamExpectation,
    validate_measurement_row_stream,
)
from mif_pipeline.orchestration.batch_artifacts import (
    BatchCommittedArtifactAuthority,
    validate_committed_artifact_authority_set,
)
from mif_pipeline.orchestration.batch_interactions import (
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    decode_committed_phenotyping_mode,
)
from mif_pipeline.orchestration.batch_state import BatchAttemptAuthorizationRequest
from mif_pipeline.orchestration.batch_training import (
    ClassifierApplicationRequest,
    ClassifierArtifactKind,
    ClassifierAuthorityResolver,
    ClassifierDeploymentTargetEvidence,
    ClassifierKind,
    ClassifierRecord,
    ClassifierRecordSetStore,
    SupervisedClassifierOwnerPayload,
    decode_current_committed_classifier_record_set,
    validate_classifier_portability,
)
from mif_pipeline.orchestration.batch_state import BatchStateStore
from mif_pipeline.orchestration.phenotype_views import (
    PhenotypeViewStore,
    PreparedPhenotypeView,
)
from mif_pipeline.pipeline_core import PhenotypingMode
from mif_pipeline.qupath_runtime.catalogue import ProjectDesignation

from ._common import BatchPhenotypingError, digest
from .supervised_records import (
    AcceptedSupervisedPredictionReview,
    StoredSupervisedNativePrediction,
    SupervisedNativePrediction,
    SupervisedNativePredictionStore,
)

_RESPONSE_VALIDATOR_ID = "mif-batch-supervised-prediction-review-validator"
_RESPONSE_VALIDATOR_VERSION = "1"


# ==============================================================================
#                              Supervised execution
# ==============================================================================


@dataclass(frozen=True, slots=True)
class SupervisedSectionDeployment:
    measurement: ValidatedMeasurementsResultProjection
    classifier_record: ClassifierRecord
    classifier: SupervisedDeploymentClassifierIdentity
    application_request: ClassifierApplicationRequest
    cells: tuple[SupervisedDeploymentCellIdentity, ...]
    cell_to_domain: Mapping[str, str]
    source_ref_sha256s: tuple[str, ...]
    included_cell_ids: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_to_domain", MappingProxyType(dict(self.cell_to_domain)))
        object.__setattr__(self, "included_cell_ids", frozenset(self.included_cell_ids))
        if not self.included_cell_ids <= set(self.cell_to_domain):
            raise BatchPhenotypingError("Selected supervised cells are outside the measured population")

    @property
    def selected_cell_ids_sha256(self) -> str:
        return digest(sorted(self.included_cell_ids))

    @property
    def selected_membership_sha256(self) -> str:
        return digest({key: self.cell_to_domain[key] for key in sorted(self.included_cell_ids)})


@dataclass(frozen=True, slots=True)
class PreparedSupervisedSectionView:
    deployment: SupervisedSectionDeployment
    stored_prediction: StoredSupervisedNativePrediction
    review: AcceptedSupervisedPredictionReview
    result: SupervisedDeploymentPredictionResult
    phenotype_generation_id: str
    prepared: PreparedPhenotypeView
    view_store: PhenotypeViewStore
    artifact: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact", MappingProxyType(dict(self.artifact)))


def _measurement_cells(
    measurement: ValidatedMeasurementsResultProjection,
    *,
    authenticated_output_root: Path,
    population_selection: Mapping[str, object],
) -> tuple[tuple[SupervisedDeploymentCellIdentity, ...], Mapping[str, str], frozenset[str]]:
    expression = tuple(
        item
        for item in measurement.measurement_plan.features
        if item.kind == FeatureKind.EXPRESSION.value
    )
    cells: list[SupervisedDeploymentCellIdentity] = []
    membership: dict[str, str] = {}
    included = set()

    def retain(cell_id: str, domain_id: str, _values: tuple[float, ...], population) -> None:
        if population_includes(population_selection, population):
            included.add(cell_id)
        cells.append(
            SupervisedDeploymentCellIdentity(cell_id, measurement.slide_id, measurement.section_id)
        )
        membership[cell_id] = domain_id

    stream = validate_measurement_row_stream(
        descriptor=MarkerIntensityRowsDescriptor(
            path=measurement.measurement_rows.path,
            sha256=measurement.measurement_rows.sha256,
            byte_count=measurement.measurement_rows.byte_count,
            row_count=measurement.measurement_rows.row_count,
            content_schema_version=measurement.measurement_rows.content_schema_version,
        ),
        authenticated_output_root=authenticated_output_root,
        expected=MeasurementRowStreamExpectation(
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
            feature_ids=tuple(item.feature_id for item in expression),
            measurement_names=tuple(item.measurement for item in expression),
        ),
        provisional_population_callback=retain,
        population_selection=population_selection,
    )
    expected_commitments = tuple(
        (
            item["analysis_domain_id"],
            item["analysis_domain_revision_sha256"],
            item["cell_count"],
            item["cell_ids_sha256"],
        )
        for item in measurement.domain_cell_commitments
    )
    observed_commitments = tuple(
        (
            item.analysis_domain_id,
            item.analysis_domain_revision_sha256,
            item.cell_count,
            item.cell_ids_sha256,
        )
        for item in stream.domain_cell_commitments
    )
    if (
        stream.cell_count != measurement.cell_count
        or stream.cell_ids_sha256 != measurement.cell_ids_sha256
        or stream.cell_to_domain_membership_sha256 != measurement.cell_to_domain_membership_sha256
        or stream.domain_cell_commitments_sha256 != measurement.domain_cell_commitments_sha256
        or stream.row_inventory_sha256 != measurement.measurement_rows.row_inventory_sha256
        or observed_commitments != expected_commitments
        or len(membership) != len(cells)
    ):
        raise BatchPhenotypingError("supervised deployment measurement population differs")
    return tuple(cells), MappingProxyType(membership), frozenset(included)


def build_supervised_section_deployment(
    *,
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
    classifier_record_set_store: ClassifierRecordSetStore,
    state_store: BatchStateStore,
    project_designation: ProjectDesignation,
    authority_resolver: ClassifierAuthorityResolver,
    authenticated_output_root: Path,
) -> SupervisedSectionDeployment:
    """Reopen /12 owners and issue one portable section application."""

    current = validate_committed_artifact_authority_set(authorities)
    if not current or len({item.state_head_sha256 for item in current}) != 1:
        raise BatchPhenotypingError("supervised deployment inputs require one current state head")
    by_kind: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in current:
        by_kind.setdefault(authority.artifact_ref.artifact_kind, []).append(authority)
    expected = {
        "analysis_frame",
        "project_catalogue_entry",
        "accepted_analysis_domain",
        "measurements_result",
        "phenotyping_mode",
        "classifier_record_set",
    }
    if set(by_kind) != expected or any(
        len(by_kind[kind]) != 1
        for kind in (
            "analysis_frame",
            "project_catalogue_entry",
            "measurements_result",
            "classifier_record_set",
        )
    ):
        raise BatchPhenotypingError("supervised deployment input authority set differs")
    record_set = decode_current_committed_classifier_record_set(
        by_kind["classifier_record_set"][0],
        store=classifier_record_set_store,
        state_store=state_store,
    )
    records = tuple(
        item
        for item in record_set.classifier_records
        if item.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
    )
    if len(records) != 1 or not isinstance(
        records[0].owner_payload, SupervisedClassifierOwnerPayload
    ):
        raise BatchPhenotypingError("supervised deployment requires one accepted supervised record")
    record = records[0]
    owner = record.owner_payload
    if record.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE:
        raise BatchPhenotypingError("supervised deployment requires an accepted native classifier")
    measurement = decode_committed_measurements_result(by_kind["measurements_result"][0])
    modes = tuple(decode_committed_phenotyping_mode(item) for item in by_kind["phenotyping_mode"])
    if (
        tuple(
            sorted(
                (item.analysis_domain_binding for item in modes),
                key=lambda item: (
                    item.sample_id,
                    item.slide_id,
                    item.analysis_domain_id,
                    item.analysis_domain_revision,
                    item.project_entry_id,
                ),
            )
        )
        != measurement.analysis_domain_bindings
        or any(
            item.selected_mode is not PhenotypingMode.SUPERVISED_CLASSIFIER
            or item.measurements_result_ref_sha256 != measurement.artifact_ref.artifact_ref_sha256
            for item in modes
        )
        or owner.validation_domain_id != measurement.artifact_ref.scope.validation_domain_id
    ):
        raise BatchPhenotypingError("supervised classifier feature, mode, or domain scope differs")
    domains = tuple(by_kind["accepted_analysis_domain"])
    domain_bindings = tuple(
        sorted(
            {
                binding
                for authority in domains
                for binding in authority.artifact_ref.scope.analysis_domain_bindings
            },
            key=lambda item: (
                item.sample_id,
                item.slide_id,
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.project_entry_id,
            ),
        )
    )
    if domain_bindings != measurement.analysis_domain_bindings:
        raise BatchPhenotypingError(
            "supervised accepted-domain cover differs from the section population"
        )
    observation = authority_resolver.observe_deployment_target(
        project_entry_id=measurement.project_entry_id,
        analysis_frame=by_kind["analysis_frame"][0].artifact_ref,
        project_catalogue_entry=by_kind["project_catalogue_entry"][0].artifact_ref,
        accepted_analysis_domain_authorities=domains,
    )
    target = ClassifierDeploymentTargetEvidence.create(
        project_designation=project_designation,
        analysis_frame=by_kind["analysis_frame"][0].artifact_ref,
        project_catalogue_entry=by_kind["project_catalogue_entry"][0].artifact_ref,
        accepted_analysis_domain_authorities=domains,
        selected_observation=observation,
    )
    request = ClassifierApplicationRequest.create(
        record=record,
        target_project=project_designation,
        target_evidence=(target,),
    )
    portability = validate_classifier_portability(
        record, request, authority_resolver=authority_resolver
    )
    if not portability.compatible:
        raise BatchPhenotypingError("supervised classifier is not portable")
    population_selection = modes[0].population_selection
    if any(item.population_selection != population_selection for item in modes):
        raise BatchPhenotypingError("Choose the same phenotyping population for every analysis region in this image")
    cells, membership, included = _measurement_cells(
        measurement, authenticated_output_root=authenticated_output_root,
        population_selection=population_selection,
    )
    # Resolve project/native material again, then reauthenticate state after
    # the portability decision to close the decision-to-projection window.
    if validate_classifier_portability(
        record, request, authority_resolver=authority_resolver
    ) != portability or any(
        state_store.authenticate_available_artifact(item.artifact_ref) != item for item in current
    ):
        raise BatchPhenotypingError("supervised deployment authority changed after portability")
    classifier = SupervisedDeploymentClassifierIdentity(
        classifier_id=owner.classifier_id,
        classifier_sha256=owner.classifier_sha256,
        qupath_version=owner.qupath_version,
        class_ids=owner.class_ids,
        feature_schema=owner.feature_schema,
        validation_domain_id=owner.validation_domain_id,
    )
    return SupervisedSectionDeployment(
        measurement=measurement,
        classifier_record=record,
        classifier=classifier,
        application_request=request,
        cells=cells,
        cell_to_domain=membership,
        included_cell_ids=included,
        source_ref_sha256s=tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in current)),
    )


def publish_supervised_prediction_review(
    deployment: SupervisedSectionDeployment,
    rows: Sequence[Mapping[str, object]],
    *,
    store: SupervisedNativePredictionStore,
) -> StoredSupervisedNativePrediction:
    prediction = SupervisedNativePrediction.create(
        sample_id=deployment.measurement.sample_id,
        project_entry_id=deployment.measurement.project_entry_id,
        slide_id=deployment.measurement.slide_id,
        section_id=deployment.measurement.section_id,
        tissue_generation_id=deployment.measurement.tissue_generation_id,
        classifier_record_sha256=(deployment.classifier_record.classifier_record_sha256),
        classifier_id=deployment.classifier.classifier_id,
        class_ids=deployment.classifier.class_ids,
        analysis_domain_bindings=deployment.measurement.analysis_domain_bindings,
        measurement_result_ref_sha256=(deployment.measurement.artifact_ref.artifact_ref_sha256),
        cell_ids_sha256=deployment.selected_cell_ids_sha256,
        cell_to_domain_membership_sha256=deployment.selected_membership_sha256,
        rows=rows,
    )
    if {item["cell_id"] for item in prediction.rows} != set(deployment.included_cell_ids):
        raise BatchPhenotypingError("supervised prediction does not cover exact section cells")
    return store.publish(prediction)


def prepare_supervised_section_view(
    deployment: SupervisedSectionDeployment,
    review: AcceptedSupervisedPredictionReview,
    *,
    prediction_store: SupervisedNativePredictionStore,
    prediction_byte_count: int,
    phenotype_view_store: PhenotypeViewStore,
    source_run_id: str,
) -> PreparedSupervisedSectionView:
    stored = prediction_store.reopen(review.prediction_sha256, prediction_byte_count)
    prediction = stored.prediction
    if (
        prediction.classifier_record_sha256 != deployment.classifier_record.classifier_record_sha256
        or prediction.measurement_result_ref_sha256
        != deployment.measurement.artifact_ref.artifact_ref_sha256
        or prediction.cell_ids_sha256 != deployment.selected_cell_ids_sha256
        or prediction.cell_to_domain_membership_sha256
        != deployment.selected_membership_sha256
    ):
        raise BatchPhenotypingError("reviewed prediction differs from current deployment owners")
    result = reconcile_deployment_predictions(
        deployment.cells,
        prediction.rows,
        classifier=deployment.classifier,
        confidence_threshold=review.confidence_threshold,
        included_cell_ids=deployment.included_cell_ids,
    )
    generation_id = digest(
        {
            "classifier_record_sha256": (deployment.classifier_record.classifier_record_sha256),
            "cell_ids_sha256": deployment.measurement.cell_ids_sha256,
            "prediction_sha256": prediction.prediction_sha256,
            "confidence_threshold": review.confidence_threshold,
            "accepted_response": review.to_mapping(),
        }
    )
    assignments = tuple(
        {
            "cell_id": row["cell_id"],
            "analysis_domain_id": deployment.cell_to_domain[row["cell_id"]],
            "primary_phenotype": row["primary_phenotype"],
            "phenotype_source": row["phenotype_source"],
            "phenotype_components": [],
            "marker_states": {},
            "phenotype_confidence": row["confidence"],
            "cluster_id": None,
            "cluster_label": None,
        }
        for row in result.import_rows
    )
    artifact = {
        "schema_version": "phenotype-view-artifact/v1",
        "mode": "supervised_classifier",
        "slide_id": deployment.measurement.slide_id,
        "canonical_image_sha256": deployment.measurement.canonical_image_sha256,
        "tissue_generation_id": deployment.measurement.tissue_generation_id,
        "phenotype_generation_id": generation_id,
        "section_id": deployment.measurement.section_id,
        "source_run_id": source_run_id,
        "cell_ids": [item.cell_id for item in deployment.cells],
        "cell_ids_sha256": deployment.measurement.cell_ids_sha256,
        "assignments": list(assignments),
        "evidence": {
            "schema_version": "mif-batch-supervised-application-evidence/v1",
            "classifier_record_sha256": (deployment.classifier_record.classifier_record_sha256),
            "application_request_sha256": (
                deployment.application_request.application_request_sha256
            ),
            "prediction_sha256": prediction.prediction_sha256,
            "prediction_rows_sha256": prediction.rows_sha256,
            "confidence_threshold": review.confidence_threshold,
            "reviewer_id": review.reviewer_id,
            "source_ref_sha256s": list(deployment.source_ref_sha256s),
        },
        "lineage": {
            "schema_version": "mif-batch-phenotype-view-lineage/v1",
            "source_run_id": source_run_id,
            "source_ref_sha256s": list(deployment.source_ref_sha256s),
        },
    }
    prepared = phenotype_view_store.prepare(artifact)
    return PreparedSupervisedSectionView(
        deployment=deployment,
        stored_prediction=stored,
        review=review,
        result=result,
        phenotype_generation_id=generation_id,
        prepared=prepared,
        view_store=phenotype_view_store,
        artifact=artifact,
    )


def validate_supervised_prediction_target_response(
    target: BatchInteractionTarget,
    action: str,
    response: Mapping[str, object],
    *,
    authorization_request: BatchAttemptAuthorizationRequest,
) -> BatchTargetResponseValidation:
    """Validate one exact section review without mutating state."""

    try:
        if action != "review_supervised_classifier":
            raise BatchPhenotypingError("supervised review action is invalid")
        context = target.stage_result.resume_context
        if not isinstance(context, Mapping) or set(response) != {
            "prediction_sha256",
            "confidence_threshold",
            "reviewer_id",
        }:
            raise BatchPhenotypingError("supervised response is malformed")
        review = AcceptedSupervisedPredictionReview(
            response["prediction_sha256"],
            response["confidence_threshold"],
            response["reviewer_id"],
        )
        if review.prediction_sha256 != context.get("sha256"):
            raise BatchPhenotypingError("supervised response prediction is stale")
        score_kind = context.get("score_kind", "native_tree_vote_fraction")
        class_count = context.get("class_count")
        if score_kind not in {
            "native_tree_vote_fraction", "native_svm_pairwise_vote_fraction",
            "native_logistic_ovr_normalized_score",
        } or (class_count is not None and (type(class_count) is not int or class_count < 2)):
            raise BatchPhenotypingError("supervised prediction score metadata is invalid")
        if (
            score_kind == "native_svm_pairwise_vote_fraction"
            and class_count == 2 and review.confidence_threshold != 0.0
        ):
            raise BatchPhenotypingError("binary SVM vote agreement cannot define score rejection")
    except (BatchPhenotypingError, TypeError, ValueError, KeyError, AttributeError):
        return BatchTargetResponseValidation(
            target_id=target.target_id,
            node_id=target.node_id,
            status="rejected",
            response=None,
            authorization_request=None,
            response_validator_id=None,
            response_validator_version=None,
            rejection_code="supervised_prediction_review_invalid",
        )
    return BatchTargetResponseValidation(
        target_id=target.target_id,
        node_id=target.node_id,
        status="accepted",
        response=review.to_mapping(),
        authorization_request=authorization_request,
        response_validator_id=_RESPONSE_VALIDATOR_ID,
        response_validator_version=_RESPONSE_VALIDATOR_VERSION,
        rejection_code=None,
    )


__all__ = [
    "PreparedSupervisedSectionView",
    "SupervisedSectionDeployment",
    "build_supervised_section_deployment",
    "prepare_supervised_section_view",
    "publish_supervised_prediction_review",
    "validate_supervised_prediction_target_response",
]
