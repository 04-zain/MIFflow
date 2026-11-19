"""State-authenticated adapter for supervised section deployment.

The `AuthorizedSupervisedSectionDeployment` record carries `deployment`,
`source_authorities`, `current_execution`, `response`, `prediction_byte_count` and its
remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from mif_pipeline.datasets.references import freeze_component_reference

from mif_pipeline.orchestration.batch_analysis_domain_artifacts import (
    accepted_analysis_domain_identity_contract,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    analysis_preflight_identity_contract,
)
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
    StageArtifactRequirement,
    artifact_ref_set_sha256,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    measurements_result_identity_contract,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    phenotyping_mode_identity_contract,
)
from mif_pipeline.orchestration.batch_preprocessing_contracts import nested_identity
from mif_pipeline.orchestration.batch_project import (
    project_catalogue_entry_identity_contract,
)
from mif_pipeline.orchestration.batch_stage_adapters import (
    build_project_current_image_stage_codec,
)
from mif_pipeline.orchestration.batch_state import BatchStateStore
from mif_pipeline.orchestration.batch_training import (
    ClassifierAuthorityResolver,
    ClassifierRecordSetStore,
    classifier_record_set_identity_contract,
)
from mif_pipeline.orchestration.phenotype_views import (
    PhenotypeViewStore,
)
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageTargetIntent,
)
from mif_pipeline.orchestration.project_stage_plan import (
    ProjectStageTargetExecutionEvidence,
)
from mif_pipeline.orchestration.workflow import CurrentImageScientificInvocation
from mif_pipeline.pipeline_core import AnalysisConfig, ROIAnalysisError, StageStatus
from mif_pipeline.qupath_runtime.catalogue import ProjectDesignation

from ._common import canonical_bytes, digest
from .supervised_execution import (
    PreparedSupervisedSectionView,
    SupervisedSectionDeployment,
    build_supervised_section_deployment,
    prepare_supervised_section_view,
    publish_supervised_prediction_review,
)
from .supervised_records import (
    AcceptedSupervisedPredictionReview,
    SupervisedNativePredictionStore,
    supervised_phenotype_result_identity_contract,
)

_ISSUER = object()


# ==============================================================================
#                              Supervised adapters
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedSupervisedSectionDeployment:
    deployment: SupervisedSectionDeployment
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    current_execution: BatchCurrentExecutionAuthority
    response: AcceptedSupervisedPredictionReview | None
    prediction_byte_count: int | None
    prepared_view: PreparedSupervisedSectionView | None
    prediction_store: SupervisedNativePredictionStore
    phenotype_view_store: PhenotypeViewStore
    resolved_config_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("supervised deployment authority is issuer-only")


def _requirement(
    contract,
    multiplicity: ArtifactMultiplicity,
) -> StageArtifactRequirement:
    return StageArtifactRequirement(
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=ArtifactScope(
            "template-domain",
            "template-sample",
            "template-slide",
            "template-entry",
            (),
        ),
        owning_identity=nested_identity(contract.required_fields),
        identity_contract=contract,
        compatibility_signature_sha256="0" * 64,
        producer_version="mif-pipeline-task13e-owner/v1",
        runtime_versions={
            name: "0.1.0" if name == "mif_pipeline" else "template"
            for name in contract.required_runtime_names
        },
        configuration_bindings=(ArtifactConfigurationBinding("template-sample", "0" * 64),),
        multiplicity=multiplicity,
    )


def build_supervised_phenotype_stage_spec() -> StageAdapterSpec:
    """Build the one section-scoped adapter specification."""

    inputs = (
        (accepted_analysis_domain_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (analysis_preflight_identity_contract(), ArtifactMultiplicity.SINGLE),
        (project_catalogue_entry_identity_contract(), ArtifactMultiplicity.SINGLE),
        (measurements_result_identity_contract(), ArtifactMultiplicity.SINGLE),
        (phenotyping_mode_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        (classifier_record_set_identity_contract(), ArtifactMultiplicity.SINGLE),
    )
    output = supervised_phenotype_result_identity_contract()
    return StageAdapterSpec(
        node_kind="supervised_phenotype_inference",
        node_version="1",
        required_inputs=tuple(_requirement(*item) for item in inputs),
        optional_inputs=(),
        declared_outputs=("supervised_phenotype_result",),
        output_identity_contracts=(output,),
        compatibility_signature_id="supervised-section-deployment/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id="supervised-section-deployment-equivalence/v1",
            byte_deterministic=False,
            canonical_identity_paths=(
                ("supervised_phenotype_result", "prediction_sha256"),
                ("supervised_phenotype_result", "phenotype_generation_id"),
            ),
            stable_inventory_paths=(
                ("supervised_phenotype_result", "cell_ids_sha256"),
                (
                    "supervised_phenotype_result",
                    "cell_to_domain_membership_sha256",
                ),
            ),
            scientific_value_paths=(
                ("supervised_phenotype_result", "confidence_threshold"),
                ("supervised_phenotype_result", "classifier_record_sha256"),
            ),
            qc_evidence_paths=(
                ("supervised_phenotype_result", "execution_exchange_sha256"),
                (
                    "supervised_phenotype_result",
                    "phenotype_view_inventory_sha256",
                ),
            ),
        ),
        resource_kind="qupath_image_write",
        dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
    )


def _response_and_predecessor(
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
) -> tuple[AcceptedSupervisedPredictionReview | None, int | None]:
    raw = state_store.load_validated_interaction_response(context.authorization)
    if raw is None:
        return None, None
    if raw.get("action") != "review_supervised_classifier" or not isinstance(
        raw.get("response"), Mapping
    ):
        raise BatchArtifactError("supervised retry response differs")
    predecessor_sha = context.authorization.interaction_predecessor_terminal_record_sha256
    if predecessor_sha is None:
        raise BatchArtifactError("supervised retry lacks its predecessor")
    predecessor = state_store.load_attempt(predecessor_sha)
    resume = predecessor.stage_result.resume_context
    if not isinstance(resume, Mapping):
        raise BatchArtifactError("supervised retry predecessor lacks review context")
    try:
        review = AcceptedSupervisedPredictionReview(
            raw["response"]["prediction_sha256"],
            raw["response"]["confidence_threshold"],
            raw["response"]["reviewer_id"],
        )
        byte_count = resume["byte_count"]
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchArtifactError("supervised retry response is invalid") from exc
    if (
        review.prediction_sha256 != resume.get("sha256")
        or type(byte_count) is not int
        or byte_count <= 0
    ):
        raise BatchArtifactError("supervised retry response is stale")
    return review, byte_count


def _saved_cutoff_predecessor(context, state_store, current, deployment):
    """Reuse only an exact predecessor prediction covered by a saved assignment."""
    from ..supervised_cutoff_selection import selected_cutoff
    plan = state_store._load_typed_plan(current.active_plan_sha256)
    measurement = deployment.measurement
    choice = selected_cutoff(plan.supervised_cutoff_selections,
        measurement.artifact_ref.scope.validation_domain_id,
        (measurement.sample_id, measurement.project_entry_id, measurement.slide_id),
        deployment.classifier_record.classifier_record_sha256)
    if choice is None:
        return None, None
    previous = state_store.settled_review_predecessor(context, execution=current,
        node_kind='supervised_phenotype_inference')
    if previous is None:
        return None, None
    terminal, authorization = previous
    if authorization.input_artifact_set_sha256 != context.authorization.input_artifact_set_sha256:
        return None, None
    if terminal.stage_result.status is StageStatus.COMPLETED:
        results = [ref for ref in terminal.output_artifacts
                   if ref.artifact_kind == 'supervised_phenotype_result']
        if len(results) != 1:
            raise BatchArtifactError('The retained assignment lacks its exact prediction owner.')
        identity = results[0].owning_identity
        sha, byte_count = identity['prediction_sha256'], identity['prediction_byte_count']
    else:
        resume = terminal.stage_result.resume_context
        if (terminal.stage_result.status is not StageStatus.NEEDS_INTERACTION
                or not isinstance(resume, Mapping) or resume.get('phase') != 'prediction_review'):
            raise BatchArtifactError('The retained prediction is not ready for assignment.')
        sha, byte_count = resume['sha256'], resume['byte_count']
    if type(byte_count) is not int or byte_count <= 0:
        raise BatchArtifactError('The retained prediction size is invalid.')
    return AcceptedSupervisedPredictionReview(sha, choice.cutoff, choice.reviewer_id), byte_count


def issue_authorized_supervised_section_deployment(
    *,
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
    classifier_record_set_store: ClassifierRecordSetStore,
    prediction_store: SupervisedNativePredictionStore,
    phenotype_view_store: PhenotypeViewStore,
    project_designation: ProjectDesignation,
    authority_resolver: ClassifierAuthorityResolver,
    authenticated_output_root: Path,
    resolved_config_sha256: str,
    source_run_id: str,
) -> AuthorizedSupervisedSectionDeployment:
    """Issue one attempt authority from current state and accepted owners."""

    current = state_store.authenticate_current_execution_context(context)
    sources = validate_committed_artifact_authority_set(
        tuple(state_store.authenticate_available_artifact(item) for item in context.inputs),
        expected_active_plan_sha256=current.active_plan_sha256,
    )
    if {item.state_head_sha256 for item in sources} != {current.state_head_sha256} or tuple(
        item.artifact_ref for item in sources
    ) != tuple(sorted(context.inputs, key=lambda item: item.artifact_ref_sha256)):
        raise BatchArtifactError("supervised deployment inputs are not current")
    try:
        deployment = build_supervised_section_deployment(
            authorities=sources,
            classifier_record_set_store=classifier_record_set_store,
            state_store=state_store,
            project_designation=project_designation,
            authority_resolver=authority_resolver,
            authenticated_output_root=authenticated_output_root,
        )
        response, byte_count = _response_and_predecessor(context, state_store)
        if response is None:
            response, byte_count = _saved_cutoff_predecessor(context, state_store, current, deployment)
        prepared = (
            None
            if response is None or byte_count is None
            else prepare_supervised_section_view(
                deployment,
                response,
                prediction_store=prediction_store,
                prediction_byte_count=byte_count,
                phenotype_view_store=phenotype_view_store,
                source_run_id=source_run_id,
            )
        )
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchArtifactError("supervised deployment derivation failed") from exc
    result = object.__new__(AuthorizedSupervisedSectionDeployment)
    for name, value in {
        "deployment": deployment,
        "source_authorities": sources,
        "current_execution": current,
        "response": response,
        "prediction_byte_count": byte_count,
        "prepared_view": prepared,
        "prediction_store": prediction_store,
        "phenotype_view_store": phenotype_view_store,
        "resolved_config_sha256": resolved_config_sha256,
        "_issuer": _ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _validate_authority(
    context: BatchNodeExecutionContext,
    authority: AuthorizedSupervisedSectionDeployment,
) -> AuthorizedSupervisedSectionDeployment:
    if (
        type(authority) is not AuthorizedSupervisedSectionDeployment
        or authority._issuer is not _ISSUER
    ):
        raise BatchArtifactError("supervised deployment authority is invalid")
    current = validate_current_execution_authority(
        authority.current_execution, expected_context=context
    )
    sources = validate_committed_artifact_authority_set(
        authority.source_authorities,
        expected_active_plan_sha256=current.active_plan_sha256,
    )
    if (
        {item.state_head_sha256 for item in sources} != {current.state_head_sha256}
        or tuple(item.artifact_ref for item in sources)
        != tuple(sorted(context.inputs, key=lambda item: item.artifact_ref_sha256))
        or authority.deployment.source_ref_sha256s
        != tuple(item.artifact_ref.artifact_ref_sha256 for item in sources)
    ):
        raise BatchArtifactError("supervised deployment authority is stale")
    return authority


@runtime_checkable
class SupervisedSectionDeploymentAuthorityResolver(Protocol):
    def __call__(
        self, context: BatchNodeExecutionContext
    ) -> AuthorizedSupervisedSectionDeployment: ...


def _invocation(
    authority: AuthorizedSupervisedSectionDeployment,
    *,
    config: AnalysisConfig,
    run_id: str,
) -> CurrentImageScientificInvocation:
    deployment = authority.deployment
    measurement = deployment.measurement
    record = deployment.classifier_record
    prepared = authority.prepared_view
    classifier = {
        "classifier_id": deployment.classifier.classifier_id,
        "classifier_sha256": deployment.classifier.classifier_sha256,
        "qupath_version": deployment.classifier.qupath_version,
        "class_ids": list(deployment.classifier.class_ids),
        "feature_schema": [item.to_mapping() for item in deployment.classifier.feature_schema],
        "validation_domain_id": deployment.classifier.validation_domain_id,
        "artifact_project_relative_path": (record.native_artifact_project_relative_path),
        "artifact_byte_count": record.native_artifact_byte_count,
    }
    prediction_identity = {
        "schema_version": "mif-batch-supervised-native-prediction/v1",
        "sample_id": measurement.sample_id,
        "project_entry_id": measurement.project_entry_id,
        "slide_id": measurement.slide_id,
        "section_id": measurement.section_id,
        "tissue_generation_id": measurement.tissue_generation_id,
        "classifier_record_sha256": record.classifier_record_sha256,
        "classifier_id": deployment.classifier.classifier_id,
        "class_ids": list(deployment.classifier.class_ids),
        "analysis_domain_bindings": [
            item.to_mapping() for item in measurement.analysis_domain_bindings
        ],
        "measurement_result_ref_sha256": (measurement.artifact_ref.artifact_ref_sha256),
        "cell_ids_sha256": deployment.selected_cell_ids_sha256,
        "cell_to_domain_membership_sha256": deployment.selected_membership_sha256,
    }
    return CurrentImageScientificInvocation(
        stage="supervised_deployment",
        config=config,
        run_id=run_id,
        project_entry_id=measurement.project_entry_id,
        section_id=measurement.section_id,
        scientific_state={
            "preflight": {
                "canonical_image_id": measurement.canonical_image_id,
                "canonical_image_sha256": measurement.canonical_image_sha256,
            },
            "tissue_acceptance": {
                "generation_id": measurement.tissue_generation_id,
            },
            "segmentation": {
                measurement.section_id: {
                    "segmentation_population_id": (measurement.segmentation_population_id)
                }
            },
            "supervised_deployment": {
                "deployment_mode": "batch_deployment",
                "classifier": classifier,
                "class_ids": list(deployment.classifier.class_ids),
                "feature_schema": [
                    item.to_mapping() for item in deployment.classifier.feature_schema
                ],
                "current_validation_domain_id": (deployment.classifier.validation_domain_id),
                "expected_cell_ids": [item.cell_id for item in deployment.cells],
                "phenotyping_cell_ids": sorted(deployment.included_cell_ids),
                "cell_id_to_analysis_domain_id": dict(deployment.cell_to_domain),
                "segmentation_population_id": (measurement.segmentation_population_id),
                "prediction_identity": prediction_identity,
                "materialize_display_classes": prepared is not None,
                "expected_prediction_sha256": (
                    None if prepared is None else prepared.stored_prediction.sha256
                ),
                "stored_prediction": (
                    None
                    if prepared is None
                    else {
                        "path": str(prepared.stored_prediction.locator),
                        "sha256": prepared.stored_prediction.sha256,
                        "byte_count": prepared.stored_prediction.byte_count,
                    }
                ),
                "phenotype_view_artifact": (
                    None if prepared is None else prepared.prepared.to_request_mapping()
                ),
                "phenotype_assignment_source": (
                    None if prepared is None else dict(prepared.prepared.assignment_source)
                ),
            },
        },
    )


def _raw_predictions(evidence: ProjectStageTargetExecutionEvidence):
    manifest = evidence.generation_manifest
    if not isinstance(manifest, Mapping):
        raise BatchArtifactError("supervised deployment requires a manifest")
    outputs = manifest.get("object_outputs")
    if not isinstance(outputs, Mapping) or not isinstance(
        outputs.get("predictions"), (list, tuple)
    ):
        raise BatchArtifactError("supervised manifest lacks predictions")
    return tuple(outputs["predictions"]), outputs


def build_supervised_deployment_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    authority: AuthorizedSupervisedSectionDeployment,
) -> BatchNodeExecutionResult:
    authority = _validate_authority(evidence.context, authority)
    if authority.response is None:
        rows, outputs = _raw_predictions(evidence)
        stored = publish_supervised_prediction_review(
            authority.deployment, rows, store=authority.prediction_store)
    else:
        if authority.prepared_view is None:
            raise BatchArtifactError("accepted supervised deployment lacks its prepared view")
        outputs = evidence.wire_stage_result.outputs
        stored = authority.prepared_view.stored_prediction
    common_fields = {
        "prediction_sha256",
        "prediction_rows_sha256",
        "cell_ids_sha256",
        "section_id",
        "materialized_display_classes",
    }
    if (
        outputs.get("prediction_sha256") != stored.sha256
        or outputs.get("prediction_rows_sha256") != stored.prediction.rows_sha256
        or outputs.get("cell_ids_sha256") != stored.prediction.cell_ids_sha256
        or outputs.get("section_id") != stored.prediction.section_id
    ):
        raise BatchArtifactError("supervised prediction output identity differs")
    wire = evidence.wire_stage_result
    if authority.response is None:
        context = wire.resume_context
        expected_context = {
            "schema_version": "supervised-phenotyping-interaction/v1",
            "phase": "prediction_review",
            "sha256": stored.sha256,
            "byte_count": stored.byte_count,
            "cell_count": len(stored.prediction.rows),
            "selected_display_view": "supervised_classifier",
        }
        expected_kind = {
            "qupath-object-classifier": "native_tree_vote_fraction",
            "qupath-object-svm-classifier": "native_svm_pairwise_vote_fraction",
            "qupath-object-logistic-regression-classifier": "native_logistic_ovr_normalized_score",
        }.get(authority.deployment.classifier_record.classifier_engine)
        if expected_kind is None:
            raise BatchArtifactError("supervised prediction classifier engine is unsupported")
        if not isinstance(context, Mapping) or type(context.get("class_count")) is not int:
            raise BatchArtifactError("supervised prediction score metadata differs")
        expected_context.update(
            score_kind=expected_kind,
            class_count=len(authority.deployment.classifier.class_ids),
        )
        if (
            set(outputs) != common_fields | {"predictions"}
            or outputs.get("materialized_display_classes") is not False
            or wire.status is not StageStatus.NEEDS_INTERACTION
            or not isinstance(context, Mapping)
            or dict(context) != expected_context
        ):
            raise BatchArtifactError("supervised first attempt review differs")
        return BatchNodeExecutionResult(evidence.context.authorization.authorization_sha256, ())
    prepared = authority.prepared_view
    if (
        prepared is None
        or set(outputs)
        != (
            common_fields
            | {
                "component_descriptor",
                "phenotype_view_artifact",
            }
        )
        or outputs.get("materialized_display_classes") is not True
        or wire.status is not StageStatus.COMPLETED
        or stored.sha256 != prepared.stored_prediction.sha256
        or outputs.get("phenotype_view_artifact") != prepared.prepared.to_request_mapping()
        or isinstance(outputs["phenotype_view_artifact"].get("cell_count"), bool)
        or evidence.observation.saved_state_disposition != "authenticated_changed"
        or evidence.observation.saved_state_after_sha256
        == evidence.authorized.saved_state_before_sha256
    ):
        raise BatchArtifactError("supervised retry result differs")
    identity_base = {
        "component_descriptor": dict(freeze_component_reference(outputs["component_descriptor"])),
        "sample_id": authority.deployment.measurement.sample_id,
        "project_entry_id": authority.deployment.measurement.project_entry_id,
        "slide_id": authority.deployment.measurement.slide_id,
        "section_id": authority.deployment.measurement.section_id,
        "tissue_generation_id": (authority.deployment.measurement.tissue_generation_id),
        "classifier_record_sha256": (
            authority.deployment.classifier_record.classifier_record_sha256
        ),
        "application_request_sha256": (
            authority.deployment.application_request.application_request_sha256
        ),
        "prediction_sha256": stored.sha256,
        "prediction_rows_sha256": stored.prediction.rows_sha256,
        "prediction_byte_count": stored.byte_count,
        "confidence_threshold": authority.response.confidence_threshold,
        "reviewer_id": authority.response.reviewer_id,
        "phenotype_generation_id": prepared.phenotype_generation_id,
        "phenotype_view_artifact_sha256": prepared.prepared.artifact_sha256,
        "phenotype_view_lineage_sha256": prepared.prepared.lineage_sha256,
        "cell_count": authority.deployment.measurement.cell_count,
        "cell_ids_sha256": authority.deployment.measurement.cell_ids_sha256,
        "cell_to_domain_membership_sha256": (
            authority.deployment.measurement.cell_to_domain_membership_sha256
        ),
        "source_ref_sha256s": list(authority.deployment.source_ref_sha256s),
        "source_ref_set_sha256": artifact_ref_set_sha256(tuple(evidence.context.inputs)),
        "saved_state_before_sha256": evidence.authorized.saved_state_before_sha256,
        "saved_state_after_sha256": evidence.observation.saved_state_after_sha256,
        "saved_state_disposition": evidence.observation.saved_state_disposition,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "node_id": evidence.authorized.node_id,
        "attempt_id": evidence.authorized.attempt_id,
        "attempt_authorization_sha256": (evidence.authorized.attempt_authorization_sha256),
        "request_codec_id": evidence.authorized.request_codec_id,
        "request_codec_version": evidence.authorized.request_codec_version,
    }
    contract = supervised_phenotype_result_identity_contract()

    def artifact(inventory_sha256: str) -> BatchArtifactRef:
        identity = {
            **identity_base,
            "phenotype_view_inventory_sha256": inventory_sha256,
        }
        measurement = authority.deployment.measurement
        return BatchArtifactRef(
            artifact_id="supervised-section-" + digest(identity)[:48],
            artifact_kind="supervised_phenotype_result",
            schema_name="mif-batch-supervised-native-prediction",
            schema_version=1,
            origin=ArtifactOrigin.INTERNAL_NODE,
            producer_node_id=evidence.authorized.node_id,
            producer_attempt_id=evidence.authorized.attempt_id,
            producer_attempt_authorization_sha256=(
                evidence.authorized.attempt_authorization_sha256
            ),
            external_declaration_sha256=None,
            external_validation_receipt_sha256=None,
            locator=stored.locator,
            sha256=stored.sha256,
            byte_count=stored.byte_count,
            scope=ArtifactScope(
                measurement.artifact_ref.scope.validation_domain_id,
                measurement.sample_id,
                measurement.slide_id,
                measurement.project_entry_id,
                measurement.analysis_domain_bindings,
            ),
            owning_identity=identity,
            identity_contract=contract,
            owning_identity_contract_sha256=contract.contract_sha256,
            producer_version="mif-pipeline-batch-supervised-deployment/v1",
            runtime_versions={
                "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
                "qupath": authority.deployment.classifier.qupath_version,
            },
            compatibility_signature_sha256=(evidence.context.compatibility_signature_sha256),
            configuration_bindings=measurement.artifact_ref.configuration_bindings,
        )

    artifact("0" * 64)
    view_store = authority.phenotype_view_store
    view_store.mark_component_applied(prepared.prepared, outputs['component_descriptor'])
    inventory = view_store.commit(prepared.prepared)
    output = artifact(inventory.inventory_sha256)
    return BatchNodeExecutionResult(evidence.authorized.attempt_authorization_sha256, (output,))


def build_supervised_phenotype_project_stage_codec(
    spec: StageAdapterSpec,
    *,
    authority_resolver: SupervisedSectionDeploymentAuthorityResolver,
) -> ProjectStageCodec:
    if (
        spec.node_kind != "supervised_phenotype_inference"
        or spec.output_identity_contracts != (supervised_phenotype_result_identity_contract(),)
        or not isinstance(authority_resolver, SupervisedSectionDeploymentAuthorityResolver)
    ):
        raise BatchArtifactError("supervised codec owner contract differs")

    def resolve(context):
        return _validate_authority(context, authority_resolver(context))

    def projector(context, intent: ProjectStageTargetIntent):
        authority = resolve(context)
        measurement = authority.deployment.measurement
        if (
            intent.sample_id != measurement.sample_id
            or intent.project_entry_id != measurement.project_entry_id
            or intent.slide_id != measurement.slide_id
            or intent.resolved_sample.resolved_config_sha256 != authority.resolved_config_sha256
        ):
            raise BatchArtifactError("supervised target differs")
        return _invocation(
            authority,
            config=intent.resolved_sample.config,
            run_id=intent.pipeline_run_id,
        )

    def validator(evidence):
        context = getattr(evidence, "context", None)
        if type(context) is not BatchNodeExecutionContext:
            raise BatchArtifactError("supervised result lacks execution context")
        return build_supervised_deployment_execution_result(evidence, resolve(context))

    return build_project_current_image_stage_codec(
        spec, invocation_projector=projector, result_validator=validator
    )


__all__ = [
    "AuthorizedSupervisedSectionDeployment",
    "SupervisedSectionDeploymentAuthorityResolver",
    "build_supervised_deployment_execution_result",
    "build_supervised_phenotype_project_stage_codec",
    "build_supervised_phenotype_stage_spec",
    "issue_authorized_supervised_section_deployment",
]
