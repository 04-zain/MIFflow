"""Closed owner inventory and runtime composition for image stages.

The `ImageRuntimeDependencies` record carries `state_store`, `project_stage`,
`project_provider`, `runner`, `project_operations` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from mif_pipeline.pipeline_core import (
    INTERACTION_ACTION_SCHEMA_VERSION,
    AnalysisConfig,
    validate_interaction_response,
)
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.interactions import validate_measurement_setup_context
from mif_pipeline.cell_analysis.measurement_setup import (
    configured_measurement_feature_requests,
    validate_measurement_feature_requests,
)
from mif_pipeline.pipeline_core.stage_result import (
    InteractionAction,
    InteractionReason,
    StageResult,
    StageStatus,
)
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectMutationLeaseProvider,
)
from mif_pipeline.qupath_runtime.catalogue_operations import (
    QuPathRunnerProjectOperations,
)
from mif_pipeline.qupath_runtime.runner import (
    QuPathImagePreflight,
    QuPathRunner,
)
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from .batch_analysis_preflight import (
    BatchAnalysisPreflightEvidence,
    analysis_preflight_identity_contract,
    build_analysis_frame_artifact_ref,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    AnalysisDomainBinding,
    BatchArtifactRef,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    RuntimeStageAdapter,
    StageAdapterRegistry,
    StageAdapterSpec,
    StageArtifactRequirement,
)
from .batch_dag import BatchPlan, ResourceKind
from .batch_analysis_domain_artifacts import (
    accepted_analysis_domain_identity_contract,
    build_analysis_domain_execution_result,
    decode_committed_accepted_analysis_domain,
    reopen_committed_accepted_analysis_domain_live_authority,
)
from .accepted_analysis_domain_records import SELECTED_MODE
from .batch_classifier_inference_artifacts import (
    project_secondary_roi_sources,
    _validate_classifier_inference_input_configuration_bindings,
    validate_classifier_inference_output,
    build_classifier_inference_result_execution_result,
    decode_committed_primary_classifier_inference_result,
    decode_committed_secondary_classifier_inference_result,
)
from .batch_classifier_inference_contracts import (
    primary_classifier_inference_result_identity_contract,
    secondary_classifier_inference_result_identity_contract,
)
from .batch_segmentation_artifacts import (
    build_segmentation_stage_codec as _build_segmentation_stage_codec,
    decode_committed_segmentation_result,
    segmentation_result_identity_contract,
)
from .batch_measurement_artifacts import (
    build_measurements_result_execution_result,
    measurements_result_identity_contract,
)
from .batch_registration import (
    BATCH_REGISTRATION_RESULT_REQUIRED_FIELDS,
    RegistrationExecutionRoute,
    batch_registration_result_identity_contract,
    decode_committed_registration_result,
    route_registration_execution,
)
from .batch_registration_runtime import execute_registration_committed
from .batch_tissue_artifacts import (
    build_tissue_acceptance_result_execution_result,
    build_tissue_candidate_result_execution_result,
    decode_committed_tissue_acceptance_result,
    reopen_committed_accepted_tissue_live_authority,
    tissue_acceptance_result_identity_contract,
    tissue_candidate_result_identity_contract,
)
from .batch_manifest import ResolvedBatchSample, ValidatedBatchManifest
from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs
from .batch_segmentation_setup import resolve_current_segmentation_setup
from .batch_measurement_setup import project_measurement_setup, resolve_current_measurement_setup
from .batch_cell_corrections import correction_result_identity_contract
from .batch_preprocessing_contracts import preprocessing_identity_contract
from .batch_project import (
    build_current_project_upsert_request,
    execute_managed_project_upsert,
    project_catalogue_entry_identity_contract,
    reopen_project_catalogue_entry_artifact,
)
from .batch_training.planning import training_plan_set_identity_contract
from .batch_training import (
    BatchTrainingError,
    ClassifierApplicationRequest,
    ClassifierArtifactKind,
    ClassifierAuthorityResolver,
    ClassifierDeploymentTargetEvidence,
    ClassifierKind,
    ClassifierPortabilityResult,
    ClassifierRecord,
    ClassifierRecordSetStore,
    TrainingPlanSetStore,
    NativeClassifierApplicationAuthority,
    PixelClassifierOwnerPayload,
    classifier_record_set_identity_contract,
    decode_current_committed_classifier_record_plan_set,
    decode_current_committed_classifier_record_set,
    validate_classifier_portability,
)
from .batch_state import (
    BatchRawNodeOutcome,
    BatchStateStore,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    ScheduledNodeResult,
)
from .input_validation import InputValidationReport, validate_analysis_input
from .batch_interactions import (
    BatchInteractionOwnerBinding,
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from .batch_service_registry import Task15RuntimeAdapterBinding
from .project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageCodecError,
    ProjectStageCodecValidationResult,
    ProjectStageRunIntent,
    ProjectStageTargetIntent,
    prepare_project_stage_run_request,
)
from .project_stage_plan import (
    ProjectStageAuthorizedEntry,
    ProjectStageCommittedExchangeReopener,
    ProjectStageLaunchMode,
    ProjectStageLifecycleError,
    ProjectStageRuntime,
    ProjectStageTargetExecutionEvidence,
    run_project_stage_headless,
)
from .workflow import (
    CurrentImageScientificInvocation,
    finalize_current_image_scientific_result,
    PrimaryClassifierDisposition,
    prepare_current_image_scientific_request,
)
from .batch_tissue_resolution_artifacts import (
    TISSUE_RESOLUTION_RESULT_IDENTITY_FIELDS,
    build_tissue_resolution_result_artifact_ref,
    tissue_resolution_result_identity_contract,
)
from .batch_tissue_setup_artifacts import (
    decode_committed_tissue_setup_result,
    tissue_setup_result_identity_contract,
)
from .batch_tissue_setup import build_tissue_setup_stage_codec
from .batch_tissue_channel_artifacts import (
    TISSUE_CHANNEL_RESULT_IDENTITY_FIELDS,
    build_tissue_channel_result_artifact_ref,
    decode_committed_tissue_channel_result,
    tissue_channel_result_identity_contract,
)
from mif_pipeline.pipeline_core.config import NativeLevelSelectionMode
from mif_pipeline.tissue_detection.resolution import (
    build_tissue_resolution_interaction_context,
    select_tissue_native_resolution,
)
from mif_pipeline.registration.executor import ProductionRegistrationExecutor

# ==============================================================================
#                              Batch stage adapters
# ==============================================================================


class ImageStageAdapterRegistryError(BatchArtifactError):
    """Task-12 adapters cannot be registered without reviewed live owners."""


def _require_supported_batch_image_modes(config: AnalysisConfig) -> None:
    """Require image modes implemented by the composed current batch owners."""

    if type(config) is not AnalysisConfig:
        raise ImageStageAdapterRegistryError(
            "batch image-mode validation requires an exact AnalysisConfig"
        )
    registration_mode = config.registration.mode
    if registration_mode not in {"none", "skip", "side_by_side", "warpy"}:
        raise ImageStageAdapterRegistryError(
            "Choose mIF only, paired images side by side, or paired-image alignment before processing."
        )
    if config.analysis_domain.selection_policy not in {"whole_tissue", "selected_rois", "prompt"}:
        raise ImageStageAdapterRegistryError(
            "batch analysis domains require the selected-ROI owner"
        )


class AnalysisInputValidator(Protocol):
    def __call__(self, config: AnalysisConfig) -> InputValidationReport: ...


class QuPathImageProbe(Protocol):
    def __call__(self, image_path: str | Path) -> QuPathImagePreflight: ...


def compose_single_output_direct_stage_outcome(
    context: BatchNodeExecutionContext,
    stage_result: StageResult,
    output: BatchArtifactRef | None,
) -> BatchRawNodeOutcome:
    """Compose one direct result without acquiring or committing state authority."""

    if not isinstance(context, BatchNodeExecutionContext):
        raise BatchArtifactError("direct stage outcome requires an execution context")
    if type(stage_result) is not StageResult:
        raise BatchArtifactError("direct stage outcome requires an exact StageResult")
    canonical = stage_result
    if canonical.stage != context.adapter_spec.node_kind:
        raise BatchArtifactError("direct stage outcome names a foreign stage")
    registry = StageAdapterRegistry((context.adapter_spec,))
    if canonical.status is StageStatus.COMPLETED:
        if output is None:
            raise BatchArtifactError("completed direct stage requires exactly one output")
        outputs = registry.validate_outputs(
            context.adapter_spec,
            (output,),
            authorization=context.authorization,
        )
    else:
        if output is not None:
            raise BatchArtifactError("non-completed direct stage cannot publish an output")
        outputs = ()
    execution_result = BatchNodeExecutionResult(
        authorization_sha256=context.authorization.authorization_sha256,
        outputs=outputs,
    )
    return BatchRawNodeOutcome(
        context=context,
        stage_result=canonical,
        execution_result=execution_result,
        execution_exchange_sha256=None,
    )


def execute_tissue_acceptance_transition(
    context: BatchNodeExecutionContext,
    *,
    candidate_authority: BatchCommittedArtifactAuthority,
    current_execution: BatchCurrentExecutionAuthority,
    tissue_setup_authority: BatchCommittedArtifactAuthority,
) -> BatchRawNodeOutcome:
    """Project one current accepted candidate without another QuPath run."""

    execution = build_tissue_acceptance_result_execution_result(
        context,
        candidate_authority,
        current_execution=current_execution,
        tissue_setup_authority=tissue_setup_authority,
    )
    if (
        execution.authorization_sha256 != context.authorization.authorization_sha256
        or len(execution.outputs) != 1
    ):
        raise BatchArtifactError("tissue acceptance owner returned a foreign execution result")
    output = execution.outputs[0]
    return compose_single_output_direct_stage_outcome(
        context,
        StageResult.completed(
            stage="tissue_acceptance",
            slide_id=output.scope.slide_id,
            generation_id=output.owning_identity.get("generation_id"),
        ),
        output,
    )


def execute_tissue_channel_stage(
    context: BatchNodeExecutionContext,
    *,
    resolved_sample: ResolvedBatchSample,
    state_store: BatchStateStore,
) -> BatchRawNodeOutcome:
    """Publish one configured channel selection without a QuPath operation."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(resolved_sample) is not ResolvedBatchSample
        or type(state_store) is not BatchStateStore
        or context.adapter_spec.node_kind != "tissue_channel"
        or context.adapter_spec.resource_kind != ResourceKind.SCIENTIFIC_COMPUTE.value
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
    ):
        raise BatchArtifactError("tissue-channel execution requires its exact direct owners")
    inputs = {item.artifact_kind: item for item in context.inputs}
    if (
        len(context.inputs) != 3
        or set(inputs) != {"analysis_frame", "tissue_resolution_result", "tissue_setup_result"}
        or resolved_sample.slide_id != inputs["analysis_frame"].scope.slide_id
        or resolved_sample.validation_domain_id
        != inputs["analysis_frame"].scope.validation_domain_id
        or hashlib.sha256(canonical_json_bytes(resolved_sample.config.to_mapping())).hexdigest()
        != resolved_sample.resolved_config_sha256
    ):
        raise BatchArtifactError("tissue-channel sample or input scope differs")
    if state_store.load_validated_interaction_response(context.authorization) is not None:
        raise BatchArtifactError(
            "tissue-channel cannot apply an interaction response to frozen config"
        )
    execution = state_store.authenticate_current_execution_context(context)
    frame_authority = state_store.authenticate_available_artifact(inputs["analysis_frame"])
    resolution_authority = state_store.authenticate_available_artifact(
        inputs["tissue_resolution_result"]
    )
    output = build_tissue_channel_result_artifact_ref(
        context,
        execution_authority=execution,
        analysis_frame_authority=frame_authority,
        tissue_resolution_authority=resolution_authority,
        tissue_setup_authority=state_store.authenticate_available_artifact(
            inputs["tissue_setup_result"]
        ),
        config=resolved_sample.config,
    )
    return compose_single_output_direct_stage_outcome(
        context,
        StageResult.completed(stage="tissue_channel", slide_id=resolved_sample.slide_id),
        output,
    )


def execute_no_secondary_completion_stage(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
) -> BatchRawNodeOutcome:
    """Authenticate exact S2 owners and issue completion without QuPath."""

    from .batch_phenotyping import (
        bind_committed_semantic_domain_branch,
        build_no_secondary_completion_execution_result,
        normalize_committed_phenotype_application,
    )

    if (
        type(context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
        or type(resolved_sample) is not ResolvedBatchSample
        or context.adapter_spec.node_kind != "no_secondary_completion"
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
        or context.adapter_spec.resource_kind != ResourceKind.SCIENTIFIC_COMPUTE.value
    ):
        raise BatchArtifactError("no-secondary completion requires exact direct stage owners")
    by_kind = {item.artifact_kind: item for item in context.inputs}
    if len(by_kind) != len(context.inputs):
        raise BatchArtifactError("no-secondary completion inputs are duplicated")
    current = state_store.authenticate_current_execution_context(context)
    authorities = {
        item.artifact_kind: state_store.authenticate_available_artifact(item)
        for item in context.inputs
    }
    required = {
        "analysis_frame",
        "accepted_analysis_domain",
        "measurements_result",
        "semantic_raw_inventory",
    }
    source_kinds = set(authorities) - required
    if source_kinds == {"marker_phenotype_result"}:
        phenotype_sources = (authorities["marker_phenotype_result"],)
    elif source_kinds == {"supervised_phenotype_result"}:
        phenotype_sources = (authorities["supervised_phenotype_result"],)
    elif source_kinds == {
        "cluster_label_import_result",
        "phenotype_view_attestation",
    }:
        phenotype_sources = (
            authorities["cluster_label_import_result"],
            authorities["phenotype_view_attestation"],
        )
    else:
        raise BatchArtifactError(
            "no-secondary completion requires one exact phenotype source branch"
        )
    phenotype = normalize_committed_phenotype_application(phenotype_sources)
    branch = bind_committed_semantic_domain_branch(
        phenotype_application=phenotype,
        raw_inventory_authority=authorities["semantic_raw_inventory"],
    )
    from .batch_secondary_definitions import secondary_definition_selections
    secondary_selection = secondary_definition_selections(
        state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256), state_store=state_store,
        participant_ids=(resolved_sample.sample_id,), node_id=context.node_id,
    ).get(resolved_sample.sample_id)
    execution = build_no_secondary_completion_execution_result(
        context=context,
        current_execution=current,
        semantic_branch=branch,
        analysis_frame_authority=authorities["analysis_frame"],
        accepted_domain_authority=authorities["accepted_analysis_domain"],
        measurements_authority=authorities["measurements_result"],
        resolved_sample=resolved_sample,
        secondary_selection=secondary_selection,
    )
    if (
        execution.authorization_sha256 != context.authorization.authorization_sha256
        or len(execution.outputs) != 1
    ):
        raise BatchArtifactError(
            "no-secondary completion owner returned a foreign execution result"
        )
    output = execution.outputs[0]
    return compose_single_output_direct_stage_outcome(
        context,
        StageResult.completed(
            stage="no_secondary_completion",
            slide_id=output.scope.slide_id,
            generation_id=output.owning_identity["completion_sha256"],
        ),
        output,
    )


def build_semantic_evidence_stage_codec(
    spec: StageAdapterSpec, *, owner_resolver: object
) -> ProjectStageCodec:
    from .batch_phenotyping.semantic_adapters import (
        build_semantic_evidence_stage_codec as build_owner_codec,
    )

    return build_owner_codec(spec, owner_resolver=owner_resolver)


def execute_semantic_evaluation_stage(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
) -> BatchRawNodeOutcome:
    from .batch_phenotyping.semantic_adapters import (
        execute_semantic_evaluation_stage as execute_owner,
    )

    return execute_owner(context, state_store=state_store, resolved_sample=resolved_sample)


def execute_final_count_cap_stage(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
) -> BatchRawNodeOutcome:
    from .batch_phenotyping.semantic_adapters import (
        execute_final_count_cap_stage as execute_owner,
    )

    return execute_owner(context, state_store=state_store, resolved_sample=resolved_sample)


def build_semantic_finalization_stage_codec(
    spec: StageAdapterSpec, *, owner_resolver: object
) -> ProjectStageCodec:
    from .batch_phenotyping.semantic_adapters import (
        build_semantic_finalization_stage_codec as build_owner_codec,
    )

    return build_owner_codec(spec, owner_resolver=owner_resolver)


def execute_tissue_acceptance_stage(
    context: BatchNodeExecutionContext, *, state_store: BatchStateStore
) -> BatchRawNodeOutcome:
    """Authenticate the one candidate prerequisite before direct acceptance."""
    if type(state_store) is not BatchStateStore:
        raise BatchArtifactError("tissue acceptance requires the composed state store")
    inputs = {item.artifact_kind: item for item in context.inputs}
    if len(context.inputs) != 3 or set(inputs) != {
        "analysis_frame",
        "tissue_candidate_result",
        "tissue_setup_result",
    }:
        raise BatchArtifactError("tissue acceptance requires exact frame and candidate inputs")
    return execute_tissue_acceptance_transition(
        context,
        candidate_authority=state_store.authenticate_available_artifact(
            inputs["tissue_candidate_result"]
        ),
        current_execution=state_store.authenticate_current_execution_context(context),
        tissue_setup_authority=state_store.authenticate_available_artifact(
            inputs["tissue_setup_result"]
        ),
    )


def _current_image_interaction_fields(
    state_store: BatchStateStore,
    context: BatchNodeExecutionContext,
) -> tuple[str | None, Mapping[str, object]]:
    """Project authenticated retry interaction material into one owner envelope."""

    # Both composed callers have already admitted this exact current execution.
    accepted = state_store.load_validated_interaction_response(context.authorization)
    if accepted is None:
        return None, MappingProxyType({})
    predecessor_sha256 = context.authorization.interaction_predecessor_terminal_record_sha256
    action = accepted.get("action")
    validated_resume = accepted.get("response")
    if (
        predecessor_sha256 is None
        or type(action) is not str
        or not isinstance(validated_resume, Mapping)
        or validated_resume.get("action") != action
        or not isinstance(validated_resume.get("response"), Mapping)
    ):
        raise BatchArtifactError("current-image retry response lacks validated action material")
    predecessor = state_store.load_attempt(predecessor_sha256)
    service_context = predecessor.stage_result.resume_context
    if not isinstance(service_context, Mapping):
        raise BatchArtifactError("current-image retry predecessor lacks compact service context")
    return action, MappingProxyType(
        {
            "service_context": service_context,
            "response": validated_resume["response"],
        }
    )


def build_tissue_candidate_stage_codec(
    spec: StageAdapterSpec, *, state_store: BatchStateStore
) -> ProjectStageCodec:
    """Compose the sole tissue asset from committed continuation state."""
    if type(state_store) is not BatchStateStore:
        raise BatchArtifactError("tissue candidate codec requires the composed state store")

    def projector(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener,
    ) -> object:
        inputs = {item.artifact_kind: item for item in context.inputs}
        if len(context.inputs) != 3 or set(inputs) != {
            "analysis_frame",
            "tissue_channel_result",
            "tissue_setup_result",
        }:
            raise BatchArtifactError(
                "tissue candidate requires exact committed continuation inputs"
            )
        execution = state_store.authenticate_current_execution_context(context)
        setup = decode_committed_tissue_setup_result(
            state_store.authenticate_available_artifact(inputs["tissue_setup_result"])
        )
        frame = validate_analysis_frame_artifact_ref(inputs["analysis_frame"])
        resolved_channels = resolve_analysis_channels(
            intent.resolved_sample.config,
            inputs["analysis_frame"],
            sample_id=intent.sample_id,
            validation_domain_id=inputs["analysis_frame"].scope.validation_domain_id,
        )
        channel = decode_committed_tissue_channel_result(
            state_store.authenticate_available_artifact(inputs["tissue_channel_result"])
        )
        if (
            setup.analysis_frame_ref_sha256 != inputs["analysis_frame"].artifact_ref_sha256
            or setup.canonical_image_id != frame.spatial_frame.canonical_image_id
            or setup.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
            or setup.resolved_config_sha256 != intent.resolved_sample.resolved_config_sha256
            or setup.project_entry_id != intent.project_entry_id
            or setup.sample_id != intent.sample_id
            or setup.slide_id != intent.slide_id
            or channel.analysis_frame_ref_sha256 != inputs["analysis_frame"].artifact_ref_sha256
            or channel.project_entry_id != intent.project_entry_id
            or channel.sample_id != intent.sample_id
            or channel.slide_id != intent.slide_id
            or channel.resolved_config_sha256 != intent.resolved_sample.resolved_config_sha256
            or channel.tissue_setup_result_ref_sha256 != setup.artifact_ref.artifact_ref_sha256
            or channel.tissue_setup_result_sha256 != setup.tissue_setup_result_sha256
        ):
            raise BatchArtifactError("tissue candidate committed continuation identity differs")
        from .batch_settled_review_revision import revision_root
        root = revision_root(state_store._load_plan_snapshot(execution.active_plan_sha256), context.node_id)
        review_required = root is not None and root["kind"] in {"tissue_setup", "tissue_resolution", "tissue_candidate"}
        new_native_read = False
        action = None
        action_context: Mapping[str, object] = MappingProxyType({})
        tissue_candidate_state: Mapping[str, object] = MappingProxyType({})
        if context.authorization.interaction_response_sha256 is not None:
            retry = committed_exchange_reopener.reopen_retry_predecessor_for_owner(
                context,
                expected_binding=(
                    spec.spec_sha256,
                    PROJECT_CURRENT_IMAGE_ASSET_BY_NODE[spec.node_kind],
                    PROJECT_CURRENT_IMAGE_WIRE_STAGE_BY_NODE[spec.node_kind],
                    PROJECT_CURRENT_IMAGE_TERMINAL_STAGE_BY_NODE[spec.node_kind],
                    PROJECT_CURRENT_IMAGE_CODEC_VERSION_BY_NODE[spec.node_kind],
                ),
            )
            manifest = retry.predecessor_evidence.generation_manifest
            if not isinstance(manifest, Mapping):
                raise BatchArtifactError("tissue retry predecessor lacks its generation manifest")
            action = retry.accepted_action
            action_context = retry.action_context
            current_manifest = dict(manifest)
            current_manifest["invocation_id"] = context.authorization.attempt_id
            current_manifest["run_id"] = intent.pipeline_run_id
            tissue_candidate_state = MappingProxyType({"generation_manifest": current_manifest})
        elif review_required:
            previous = committed_exchange_reopener.reopen_settled_review_predecessor_for_owner(
                context, execution=execution, expected_binding=(
                    spec.spec_sha256, PROJECT_CURRENT_IMAGE_ASSET_BY_NODE[spec.node_kind],
                    PROJECT_CURRENT_IMAGE_WIRE_STAGE_BY_NODE[spec.node_kind],
                    PROJECT_CURRENT_IMAGE_TERMINAL_STAGE_BY_NODE[spec.node_kind],
                    PROJECT_CURRENT_IMAGE_CODEC_VERSION_BY_NODE[spec.node_kind]))
            if previous is not None:
                manifest = previous.evidence.generation_manifest
                if not isinstance(manifest, Mapping):
                    raise BatchArtifactError("settled tissue review lacks its generation manifest")
                accepted = tuple(generation for generation in manifest["generations"]
                    if generation["status"] == "accepted")
                if len(accepted) != 1:
                    raise BatchArtifactError("settled tissue review requires one retained accepted generation")
                prior = accepted[0]
                new_native_read = (root["kind"] == "tissue_setup"
                    or prior["tissue_resolution_selection"] != channel.selection.to_mapping())
                action = "repeat"
                action_context = MappingProxyType({"service_context": {
                    "native_level_read_attestation": None if new_native_read else prior["native_level_read_attestation"]},
                    "response": {}})
                current_manifest = dict(manifest)
                current_manifest["invocation_id"] = context.authorization.attempt_id
                current_manifest["run_id"] = intent.pipeline_run_id
                tissue_candidate_state = MappingProxyType({"generation_manifest": current_manifest})
        return CurrentImageScientificInvocation(
            stage="tissue_candidate",
            config=intent.resolved_sample.config,
            run_id=intent.pipeline_run_id,
            project_entry_id=intent.project_entry_id,
            scientific_state={
                "tissue_setup": {
                    "tissue_detection": setup.tissue_detection.to_mapping(),
                    "tissue_setup_result_ref_sha256": setup.artifact_ref.artifact_ref_sha256,
                    "tissue_setup_result_sha256": setup.tissue_setup_result_sha256,
                },
                "preflight": {
                    "channels": [item.to_mapping() for item in resolved_channels],
                    "canonical_image_id": (frame.spatial_frame.canonical_image_id),
                    "canonical_image_sha256": (frame.spatial_frame.canonical_image_sha256),
                },
                "tissue_resolution": {
                    "tissue_resolution_selection": (channel.selection.to_mapping()),
                    "native_pyramid_inventory": [
                        item.to_mapping() for item in channel.native_pyramid_inventory
                    ],
                },
                "tissue_channel": {
                    "tissue_resolution_selection": (channel.selection.to_mapping()),
                    "native_level_read_attestation": None,
                },
                "tissue_candidate": tissue_candidate_state,
                "tissue_review": {"review_required": review_required, "new_native_read": new_native_read},
            },
            action=action,
            action_context=action_context,
        )

    def validate_result(evidence):
        inputs = {item.artifact_kind: item for item in evidence.context.inputs}
        execution = state_store.authenticate_current_execution_context(evidence.context)
        from .batch_settled_review_revision import revision_root
        root = revision_root(state_store._load_plan_snapshot(execution.active_plan_sha256), evidence.context.node_id)
        return build_tissue_candidate_result_execution_result(
            evidence, review_revision=(
                root["settled_review_revision"]
                if root is not None and root["kind"] in {
                    "tissue_setup", "tissue_resolution", "tissue_candidate"
                }
                else None
            ),
            tissue_setup_authority=state_store.authenticate_available_artifact(
                inputs["tissue_setup_result"]
            ),
        )

    # The binding remains the accepted detector asset/wire/terminal/version.
    return build_project_current_image_stage_codec(
        spec,
        invocation_projector=projector,
        result_validator=validate_result,
        exchange_reopening=True,
    )


def _accepted_tissue_state(
    *, generation_id: str, sections: tuple[Mapping[str, object], ...]
) -> dict[str, object]:
    return {
        "generation_id": generation_id,
        "sections": [
            {
                "section_id": item["section_id"],
                "roi_id": item["roi_id"],
                "path_object_id": item["path_object_id"],
            }
            for item in sections
        ],
    }


def analysis_domain_review(invocation, *, project_path):
    """Return the current area-choice or annotation review for this image.

    We reuse the domain workflow's selection rules. The batch boundary supplies
    its actual project location for the saved annotation path shown by the form.
    A reviewed snapshot action or a whole-tissue selection proceeds to its owner.
    """
    from .workflow import StageInvocation, _analysis_domain_interaction

    local = StageInvocation(
        stage=invocation.stage, config=invocation.config, run_id=invocation.run_id,
        state={**invocation.scientific_state, "project": {
            "project_entry_id": invocation.project_entry_id,
            "project_file": str(project_path),
        }}, checkpoint_store=None, action=invocation.action,
        action_context=invocation.action_context,
    )
    return _analysis_domain_interaction(local)


def current_analysis_domain_invocation(context, intent, *, state_store, committed_exchange_reopener):
    """Resolve accepted tissue, registration and the current area-selection action.

    Both the initial review and subsequent native snapshot use this same input
    association, preserving the selected sample, image and tissue generation.
    """
    spec = context.adapter_spec
    by_kind = {item.artifact_kind: item for item in context.inputs}
    if len(by_kind) != len(context.inputs) or set(by_kind) != {
        "analysis_frame",
        "registration_result",
        "tissue_acceptance_result",
    }:
        raise BatchArtifactError("analysis-domain selection requires exact committed inputs")
    _require_supported_batch_image_modes(intent.resolved_sample.config)
    current = state_store.authenticate_current_execution_context(context)
    authorities = {
        name: state_store.authenticate_available_artifact(ref) for name, ref in by_kind.items()
    }
    frame = validate_analysis_frame_artifact_ref(by_kind["analysis_frame"])
    tissue = decode_committed_tissue_acceptance_result(authorities["tissue_acceptance_result"])
    registration = decode_committed_registration_result(authorities["registration_result"])
    input_digests = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    if (
        any(
            authority.state_head_sha256 != current.state_head_sha256
            or authority.active_plan_sha256 != current.active_plan_sha256
            for authority in authorities.values()
        )
        or current.node_kind != spec.node_kind
        or current.participant_ids != (intent.sample_id,)
        or current.project_entry_ids != (intent.project_entry_id,)
        or current.analysis_domain_bindings
        or current.input_artifact_ref_sha256s != input_digests
        or intent.prerequisite_artifact_ref_sha256s != input_digests
        or tissue.sample_id != intent.sample_id
        or tissue.slide_id != intent.slide_id
        or tissue.project_entry_id != intent.project_entry_id
        or tissue.analysis_frame_ref_sha256 != by_kind["analysis_frame"].artifact_ref_sha256
        or registration.sample_id != intent.sample_id
        or registration.slide_id != intent.slide_id
        or registration.primary_project_entry_id != intent.project_entry_id
        or registration.tissue_generation_id != tissue.generation_id
        or registration.primary_image_sha256 != frame.spatial_frame.canonical_image_sha256
    ):
        raise BatchArtifactError("analysis-domain committed continuation identity differs")
    action, action_context = _current_image_interaction_fields(state_store, context)
    action_context = dict(action_context)
    from .batch_settled_review_revision import revision_root
    root = revision_root(state_store._load_plan_snapshot(current.active_plan_sha256), context.node_id)
    review_required = root is not None and root["kind"] == "analysis_domain_selection"
    if review_required and action is None:
        retained = committed_exchange_reopener.reopen_settled_review_predecessor_for_owner(
            context, execution=current, expected_binding=(
                spec.spec_sha256, PROJECT_CURRENT_IMAGE_ASSET_BY_NODE[spec.node_kind],
                PROJECT_CURRENT_IMAGE_WIRE_STAGE_BY_NODE[spec.node_kind],
                PROJECT_CURRENT_IMAGE_TERMINAL_STAGE_BY_NODE[spec.node_kind],
                PROJECT_CURRENT_IMAGE_CODEC_VERSION_BY_NODE[spec.node_kind]))
        if retained is not None:
            accepted = retained.evidence.generation_manifest["object_outputs"]
            if accepted["selection_status"] != "accepted":
                raise BatchArtifactError("retained region snapshot is not accepted")
            action_context["service_context"] = {
                "lineage_predecessor_candidate": accepted,
                "accepted_retirement_predecessor": accepted,
            }
    if action in {"review_analysis_rois", "accept_analysis_rois"}:
        # The annotation exchange publishes selected regions after the draft closes.
        # Native admission checks the saved selection and the closed project entry.
        action_context["saved_closed_acknowledged"] = True
    elif action == "use_whole_tissue":
        # The choice changes only the admitted selection. Runtime admission
        # proves the saved entry is closed; this action does not edit a draft.
        action_context["saved_closed_acknowledged"] = True
    return CurrentImageScientificInvocation(
        stage="analysis_domain_selection",
        action=action, action_context=action_context,
        config=intent.resolved_sample.config,
        run_id=intent.pipeline_run_id,
        project_entry_id=intent.project_entry_id,
        scientific_state={
            "preflight": {
                "canonical_image_id": tissue.canonical_image_id,
                "canonical_image_sha256": tissue.canonical_image_sha256,
            },
            "tissue_acceptance": _accepted_tissue_state(
                generation_id=tissue.generation_id,
                sections=tissue.section_inventory,
            ),
            "registration": {"mode": registration.mode},
            "analysis_domain_review_required": review_required,
        },
    )


def build_analysis_domain_selection_stage_codec(spec, *, state_store):
    """Prepare native snapshots after the researcher completes the area choice."""
    return build_project_current_image_stage_codec(
        spec,
        invocation_projector=lambda context, intent, *, committed_exchange_reopener: current_analysis_domain_invocation(
            context, intent, state_store=state_store, committed_exchange_reopener=committed_exchange_reopener,
        ),
        result_validator=lambda evidence: build_analysis_domain_execution_result(evidence, state_store=state_store),
        exchange_reopening=True,
    )


def _build_classifier_inference_stage_codec(
    spec: StageAdapterSpec,
    *,
    state_store: BatchStateStore,
    classifier_record_set_store: ClassifierRecordSetStore,
    classifier_authority_resolver: ClassifierAuthorityResolver | None,
    classifier_application_authority: NativeClassifierApplicationAuthority | None = None,
    training_plan_set_store: TrainingPlanSetStore | None = None,
    kind: str,
    secondary_structure_id: str | None = None,
) -> ProjectStageCodec:
    """Compose one classifier request from freshly authenticated record authority."""

    if type(state_store) is not BatchStateStore:
        raise BatchArtifactError("classifier inference requires the composed state store")
    if type(classifier_record_set_store) is not ClassifierRecordSetStore:
        raise BatchArtifactError("classifier inference requires the record-set owner store")
    if (classifier_authority_resolver is None) == (classifier_application_authority is None):
        raise BatchArtifactError("classifier inference requires exactly one authority owner")
    if (
        classifier_application_authority is not None
        and type(classifier_application_authority) is not NativeClassifierApplicationAuthority
    ):
        raise BatchArtifactError("classifier inference application authority differs")
    if (kind, spec.node_kind) not in {
        ("primary", "primary_classifier_inference"),
        ("secondary", "secondary_classifier_inference"),
    }:
        raise BatchArtifactError("classifier inference kind and specification differ")

    expected_record_kind = (
        ClassifierKind.PRIMARY_COMPARTMENT
        if kind == "primary"
        else ClassifierKind.SECONDARY_STRUCTURE
    )

    def authenticated_invocation(
        context: BatchNodeExecutionContext,
        *,
        target: ProjectStageTargetIntent | ProjectStageAuthorizedEntry,
        config: AnalysisConfig,
        resolved_config_sha256: str,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener,
    ) -> CurrentImageScientificInvocation:
        if kind == "primary" and type(training_plan_set_store) is not TrainingPlanSetStore:
            raise BatchArtifactError("primary inference requires its accepted-plan store")
        required = {
            "analysis_frame",
            "project_catalogue_entry",
            "accepted_analysis_domain",
            "classifier_record_set",
        }
        if kind == "secondary":
            routes = {ref.artifact_kind for ref in context.inputs} & {
                "primary_inference_result", "training_plan_set"}
            if len(routes) != 1:
                raise BatchArtifactError("secondary inference requires one accepted primary classification route")
            required.update(routes)
        by_kind: dict[str, BatchArtifactRef] = {}
        prior_secondary_refs: list[BatchArtifactRef] = []
        for ref in context.inputs:
            if kind == "secondary" and ref.artifact_kind == "secondary_inference_result":
                prior_secondary_refs.append(ref)
                continue
            if ref.artifact_kind in by_kind:
                raise BatchArtifactError("classifier inference singleton input kind is duplicated")
            by_kind[ref.artifact_kind] = ref
        if set(by_kind) != required:
            raise BatchArtifactError("classifier inference input set is not exact")

        current = state_store.authenticate_current_execution_context(context)
        snapshot = state_store.reopen_execution_snapshot(
            expected_head_sha256=current.state_head_sha256
        )
        def authenticate_input(ref):
            if ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL:
                return state_store.authenticate_available_manifest_external_artifact(ref)
            return state_store.authenticate_available_artifact(ref)

        authorities = {name: authenticate_input(ref) for name, ref in by_kind.items()}
        prior_secondary_authorities = tuple(
            state_store.authenticate_available_artifact(ref) for ref in prior_secondary_refs
        )
        frame = validate_analysis_frame_artifact_ref(by_kind["analysis_frame"])
        _catalogue, entry = reopen_project_catalogue_entry_artifact(
            by_kind["project_catalogue_entry"]
        )
        domain = decode_committed_accepted_analysis_domain(authorities["accepted_analysis_domain"])
        accepted_plan = None
        primary_settings = None
        imported_material = None
        settings_source_ref_sha256 = None
        if by_kind["classifier_record_set"].origin is ArtifactOrigin.MANIFEST_EXTERNAL:
            from .batch_training.accepted_model_reuse import (
                decode_imported_classifier_record_set, validate_imported_primary_settings,
            )
            imported_material = decode_imported_classifier_record_set(
                authorities["classifier_record_set"], state_store=state_store,
            )
            record_set = imported_material.record_set
            if kind == "primary":
                primary_settings = validate_imported_primary_settings(config, imported_material)
                settings_source_ref_sha256 = by_kind["classifier_record_set"].artifact_ref_sha256
        elif kind == "primary":
            try:
                record_set, accepted_plan = decode_current_committed_classifier_record_plan_set(
                    authorities["classifier_record_set"],
                    record_store=classifier_record_set_store,
                    plan_store=training_plan_set_store,
                    state_store=state_store,
                )
            except BatchTrainingError as exc:
                raise BatchArtifactError("primary inference accepted plan is invalid") from exc
            if len(accepted_plan.plan_set.plans) != 1:
                raise BatchArtifactError("primary inference requires one accepted plan")
            primary_settings = accepted_plan.plan_set.plans[0].request.primary_settings
            settings_source_ref_sha256 = accepted_plan.artifact_ref.artifact_ref_sha256
            if primary_settings is None:
                raise BatchArtifactError("primary inference accepted settings are missing")
        else:
            record_set = decode_current_committed_classifier_record_set(
                authorities["classifier_record_set"],
                store=classifier_record_set_store,
                state_store=state_store,
            )
        if record_set.classifier_kind is not expected_record_kind:
            raise BatchArtifactError("classifier record-set kind differs from inference")

        structure = None
        resolved_sample: ResolvedBatchSample | None = None
        structure_id = secondary_structure_id
        current_plan_node = None
        if classifier_application_authority is not None:
            nodes = tuple(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
            samples = tuple(
                item
                for item in snapshot.manifest.samples
                if item.enabled and item.sample_id == target.sample_id
            )
            if len(nodes) != 1 or len(samples) != 1:
                raise BatchArtifactError("classifier inference plan node or sample differs")
            current_plan_node = nodes[0]
            resolved_sample = samples[0]
            if (
                snapshot.head.sha256 != current.state_head_sha256
                or snapshot.head.active_plan_sha256 != current.active_plan_sha256
                or current_plan_node.kind != spec.node_kind
                or current_plan_node.participant_ids != (target.sample_id,)
                or current_plan_node.project_entry_ids != (target.project_entry_id,)
                or resolved_sample.slide_id != target.slide_id
                or resolved_sample.config != config
                or resolved_sample.resolved_config_sha256 != resolved_config_sha256
            ):
                raise BatchArtifactError("classifier inference current plan target differs")
            if kind == "primary":
                if (
                    current_plan_node.structure_id is not None
                    or current_plan_node.classifier_id is not None
                    or current_plan_node.eligible_parent_ids
                ):
                    raise BatchArtifactError(
                        "primary inference plan carries secondary target state"
                    )
                structure_id = None
            else:
                structure_id = current_plan_node.structure_id

        if kind == "primary":
            if len(record_set.output_inventory) != 1:
                raise BatchArtifactError("primary record set must have one output")
            output_id, selected_digest = record_set.output_inventory[0]
            assert primary_settings is not None
            configured_classifier_id = primary_settings.primary_classifier.classifier_id
            eligible_parent_ids: tuple[str, ...] = ()
        else:
            from .batch_secondary_definitions import resolve_secondary_structures, secondary_definition_selections
            if resolved_sample is None:
                resolved_sample = next(item for item in snapshot.manifest.enabled_samples
                                       if item.sample_id == target.sample_id)
            secondary_selection = secondary_definition_selections(
                snapshot, state_store=state_store, participant_ids=(resolved_sample.sample_id,), node_id=context.node_id,
            ).get(resolved_sample.sample_id)
            secondary_structures = resolve_secondary_structures(resolved_sample, secondary_selection)
            structure = next(
                (item for item in secondary_structures if item.structure_id == structure_id),
                None,
            )
            if structure is None:
                raise BatchArtifactError("secondary inference structure is not configured")
            output_id = structure.classifier.classifier_id
            inventory = dict(record_set.output_inventory)
            if output_id not in inventory:
                raise BatchArtifactError("secondary record set lacks the selected output")
            selected_digest = inventory[output_id]
            configured_classifier_id = structure.classifier.classifier_id
            eligible_parent_ids = tuple(structure.eligible_parent_ids)
            if current_plan_node is not None and (
                current_plan_node.classifier_id != configured_classifier_id
                or current_plan_node.eligible_parent_ids != eligible_parent_ids
            ):
                raise BatchArtifactError(
                    "secondary inference plan target differs from configuration"
                )
        matches = tuple(
            record
            for record in record_set.classifier_records
            if record.classifier_record_sha256 == selected_digest
        )
        if len(matches) != 1:
            raise BatchArtifactError("classifier record selection is not bijective")
        record = matches[0]
        if (
            type(record) is not ClassifierRecord
            or record.classifier_id != configured_classifier_id
            or record.validation_domain_id != domain.artifact_ref.scope.validation_domain_id
            or record.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE
            or type(record.owner_payload) is not PixelClassifierOwnerPayload
        ):
            raise BatchArtifactError("selected classifier record differs from target")

        bindings = tuple(target.analysis_domain_bindings)
        expected_configuration = (
            ArtifactConfigurationBinding(target.sample_id, resolved_config_sha256),
        )
        _validate_classifier_inference_input_configuration_bindings(
            {
                **{name: tuple(ref.configuration_bindings) for name, ref in by_kind.items()},
                **({"secondary_inference_result": tuple(binding for ref in prior_secondary_refs
                    for binding in ref.configuration_bindings)} if prior_secondary_refs else {}),
            },
            expected_configuration[0],
        )
        input_digests = tuple(sorted(ref.artifact_ref_sha256 for ref in context.inputs))
        # The current pending head is the authorization's successor; state-store
        # authentication above already proves its unbroken admission ancestry.
        if (
            len(bindings) != 1
            or any(
                authority.state_head_sha256 != current.state_head_sha256
                or authority.active_plan_sha256 != current.active_plan_sha256
                for authority in (*authorities.values(), *prior_secondary_authorities)
            )
            or len(domain.domain_authorities) != 1
            or domain.artifact_ref != by_kind["accepted_analysis_domain"]
            or domain.state_head_sha256 != current.state_head_sha256
            or domain.active_plan_sha256 != current.active_plan_sha256
            or domain.sample_id != target.sample_id
            or domain.slide_id != target.slide_id
            or domain.project_entry_id != target.project_entry_id
            or domain.resolved_config_sha256 != resolved_config_sha256
            or tuple(domain.artifact_ref.scope.analysis_domain_bindings) != bindings
            or record_set.validation_domain_id != domain.artifact_ref.scope.validation_domain_id
            or entry.project_entry_id != target.project_entry_id
            or entry.sample_id != target.sample_id
            or entry.slide_id != target.slide_id
            or entry.validation_domain_id != domain.artifact_ref.scope.validation_domain_id
            or frame.spatial_frame.canonical_image_sha256 != entry.image_sha256
            or context.project_identity_sha256
            != by_kind["project_catalogue_entry"].owning_identity.get("project_identity_sha256")
            or current.node_kind != spec.node_kind
            or current.node_scope != "sample"
            or current.validation_domain_id != domain.artifact_ref.scope.validation_domain_id
            or current.participant_ids != (target.sample_id,)
            or current.project_entry_ids != (target.project_entry_id,)
            or current.analysis_domain_bindings != bindings
            or current.configuration_bindings != expected_configuration
            or current.input_artifact_ref_sha256s != input_digests
            or target.prerequisite_artifact_ref_sha256s != input_digests
        ):
            raise BatchArtifactError("classifier inference current authority differs")

        primary = None
        secondary_sources = None
        if kind == "secondary":
            primary = (decode_committed_primary_classifier_inference_result(
                authorities["primary_inference_result"]
            ) if "primary_inference_result" in authorities else None)
            authority = domain.domain_authorities[0]
            if primary is not None and (
                primary.state_head_sha256 != current.state_head_sha256
                or primary.active_plan_sha256 != current.active_plan_sha256
                or primary.artifact_ref.scope != domain.artifact_ref.scope
                or primary.pipeline_run_id != target.pipeline_run_id
                or primary.canonical_image_id != domain.canonical_image_id
                or primary.canonical_image_sha256 != domain.canonical_image_sha256
                or primary.tissue_generation_id != domain.tissue_generation_id
                or primary.section_id != authority.section_id
                or primary.analysis_domain_bindings != bindings
                or primary.analysis_domain_path_object_id != authority.source_path_object_id
            ):
                raise BatchArtifactError("secondary primary predecessor differs")
            secondary_sources = project_secondary_roi_sources(
                configured_structure=structure, primary=primary,
                prior_secondaries=tuple(decode_committed_secondary_classifier_inference_result(item)
                                        for item in prior_secondary_authorities),
                configured_structures=secondary_structures,
                accepted_domain_ref=domain.artifact_ref,
                primary_skip_ref=by_kind.get("training_plan_set"),
                pipeline_run_id=target.pipeline_run_id,
                target_project_identity_sha256=context.project_identity_sha256,
            )

        project_identity_sha256 = by_kind["project_catalogue_entry"].owning_identity.get(
            "project_identity_sha256"
        )
        if type(project_identity_sha256) is not str:
            raise BatchArtifactError("project catalogue lacks project identity")
        authority_resolver = classifier_authority_resolver
        if classifier_application_authority is not None:
            assert resolved_sample is not None
            try:
                authority_resolver = classifier_application_authority.bind_pixel_target(
                    context=context,
                    resolved_sample=resolved_sample,
                    classifier_role=kind,
                    structure_id=structure_id,
                    analysis_frame=by_kind["analysis_frame"],
                    project_catalogue_entry=by_kind["project_catalogue_entry"],
                    accepted_analysis_domain_authorities=(authorities["accepted_analysis_domain"],),
                    accepted_record_set=record_set,
                    accepted_plan_set=accepted_plan,
                    accepted_record_authority=authorities["classifier_record_set"],
                )
            except (BatchTrainingError, TypeError, ValueError) as exc:
                raise BatchArtifactError("classifier application authority is invalid") from exc
        assert authority_resolver is not None
        try:
            project = authority_resolver.resolve_project(project_identity_sha256)
            observation = authority_resolver.observe_deployment_target(
                project_entry_id=target.project_entry_id,
                analysis_frame=by_kind["analysis_frame"],
                project_catalogue_entry=by_kind["project_catalogue_entry"],
                accepted_analysis_domain_authorities=(authorities["accepted_analysis_domain"],),
            )
            target_evidence = ClassifierDeploymentTargetEvidence.create(
                project_designation=project,
                analysis_frame=by_kind["analysis_frame"],
                project_catalogue_entry=by_kind["project_catalogue_entry"],
                accepted_analysis_domain_authorities=(authorities["accepted_analysis_domain"],),
                selected_observation=observation,
            )
            application = ClassifierApplicationRequest.create(
                record=record,
                target_project=project,
                target_evidence=(target_evidence,),
            )
            portability = validate_classifier_portability(
                record,
                application,
                authority_resolver=authority_resolver,
            )
        except (
            BatchTrainingError,
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise BatchArtifactError("classifier application authority is invalid") from exc
        _require_compatible_classifier_portability(portability)

        # Material is projected only after the typed portability decision succeeds.
        assert record.native_artifact_project_relative_path is not None
        assert record.native_artifact_sha256 is not None
        assert record.native_artifact_byte_count is not None
        owner = record.owner_payload
        assert isinstance(owner, PixelClassifierOwnerPayload)
        application_mapping = {
            "classifier_record_set_ref_sha256": by_kind[
                "classifier_record_set"
            ].artifact_ref_sha256,
            "classifier_record_set_sha256": record_set.classifier_record_set_sha256,
            "output_id": output_id,
            "classifier_record_sha256": record.classifier_record_sha256,
            "classifier_id": record.classifier_id,
            "classifier_version": record.classifier_version,
            "classifier_kind": record.classifier_kind.value,
            "application_request_sha256": application.application_request_sha256,
            "target_project_identity_sha256": application.target_project_identity_sha256,
            "deployment_binding_set_sha256": application.deployment_binding_set_sha256,
            "target_evidence_set_sha256": application.target_evidence_set_sha256,
            "project_relative_path": record.native_artifact_project_relative_path,
            "source_model_path": (None if imported_material is None
                                  else str(imported_material.model_path(record))),
            "native_classifier_sha256": record.native_artifact_sha256,
            "native_classifier_byte_count": record.native_artifact_byte_count,
            "pixel_classifier_definition": owner.classifier_definition.to_mapping(),
            "pixel_classifier_definition_sha256": owner.classifier_definition_sha256,
        }

        # The compact domain authority omits revision history. Reopen its existing
        # owners and retain the canonical revision, including its real predecessor.
        active = domain.domain_authorities[0]
        tissue_authorities = tuple(
            item
            for item in snapshot.committed_artifact_authorities
            if item.artifact_ref.artifact_ref_sha256
            in domain.source_input_artifact_ref_sha256s
            and item.artifact_ref.artifact_kind == "tissue_acceptance_result"
        )
        if len(tissue_authorities) != 1:
            raise BatchArtifactError("classifier inference tissue predecessor is unavailable")
        tissue = reopen_committed_accepted_tissue_live_authority(
            tissue_authorities[0], exchange_reopener=committed_exchange_reopener
        )
        live_domains = reopen_committed_accepted_analysis_domain_live_authority(
            authorities["accepted_analysis_domain"],
            accepted_tissue=tissue,
            exchange_reopener=committed_exchange_reopener,
        )
        manifests = tuple(
            item
            for item in live_domains.manifests
            if item.content_sha256 == active.analysis_domain_manifest_sha256
        )
        if live_domains.committed != domain or len(manifests) != 1:
            raise BatchArtifactError("classifier inference current domain manifest differs")
        revisions = tuple(
            item
            for item in manifests[0].revisions
            if item.analysis_domain_id == active.analysis_domain_id
            and item.revision == active.analysis_domain_revision
            and item.content_sha256 == active.analysis_domain_revision_sha256
        )
        if len(revisions) != 1:
            raise BatchArtifactError("classifier inference current domain revision differs")
        revision = revisions[0].to_mapping()

        # Close the TOCTOU window around record/material projection.
        refreshed = state_store.authenticate_current_execution_context(context)
        for name, ref in by_kind.items():
            if authenticate_input(ref) != authorities[name]:
                raise BatchArtifactError("classifier inference input authority changed")
        for ref, authority in zip(prior_secondary_refs, prior_secondary_authorities, strict=True):
            if state_store.authenticate_available_artifact(ref) != authority:
                raise BatchArtifactError("secondary parent authority changed")
        if refreshed != current:
            raise BatchArtifactError("classifier inference execution authority changed")

        scientific_state = {
            "preflight": {
                "canonical_image_id": domain.canonical_image_id,
                "canonical_image_sha256": domain.canonical_image_sha256,
            },
            "tissue_acceptance": {
                "generation_id": domain.tissue_generation_id,
                "sections": [
                    {
                        "section_id": active.section_id,
                        "roi_id": active.tissue_roi_id,
                        "path_object_id": active.tissue_path_object_id,
                    }
                ],
            },
            "analysis_domain_selection": {
                "selection_status": "accepted",
                "selected_annotations_locked": True,
                "domain_set_sha256": domain.domain_set.domain_set_sha256,
                "analysis_domain_manifests": [{"revisions": [revision]}],
            },
            "classifier_application": application_mapping,
            "classifier_review_status": record.review_decision,
        }
        if kind == "primary":
            assert primary_settings is not None and settings_source_ref_sha256 is not None
            scientific_state["primary_setup"] = {
                "settings": primary_settings.to_mapping(),
                "settings_source_ref_sha256": settings_source_ref_sha256,
            }
        if kind == "secondary":
            assert secondary_sources is not None
            if secondary_selection is not None:
                scientific_state["secondary_structure_settings"] = {
                    "structures": [item.to_mapping() for item in secondary_structures],
                    "selection_ref_sha256": secondary_selection.artifact_ref.artifact_ref_sha256,
                }
            scientific_state["secondary_parent_sources"] = [item.to_mapping() for item in secondary_sources.parent_sources]
            scientific_state["secondary_predecessor_result_ref_sha256"] = secondary_sources.predecessor_result_ref_sha256
            scientific_state["primary_domain_status"] = "active" if primary is None else primary.domain_status
        action, action_context = _current_image_interaction_fields(state_store, context)
        return CurrentImageScientificInvocation(
            stage=("primary_classifier" if kind == "primary" else "secondary_classifier"),
            config=config,
            run_id=target.pipeline_run_id,
            project_entry_id=target.project_entry_id,
            scientific_state=scientific_state,
            section_id=active.section_id,
            analysis_domain_id=active.analysis_domain_id,
            analysis_domain_revision=active.analysis_domain_revision,
            analysis_domain_revision_sha256=active.analysis_domain_revision_sha256,
            analysis_domain_path_object_id=active.source_path_object_id,
            structure_id=(None if structure is None else structure.structure_id),
            eligible_parent_ids=eligible_parent_ids,
            action=action,
            action_context=action_context,
        )

    def projector(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener,
    ) -> object:
        return authenticated_invocation(
            context,
            target=intent,
            config=intent.resolved_sample.config,
            resolved_config_sha256=intent.resolved_sample.resolved_config_sha256,
            committed_exchange_reopener=committed_exchange_reopener,
        )

    def validate_result(
        evidence: ProjectStageTargetExecutionEvidence,
    ) -> BatchNodeExecutionResult | ProjectStageCodecValidationResult:
        validated = validate_classifier_inference_output(evidence, kind=kind)
        if type(validated) is BatchNodeExecutionResult:
            return validated
        finalized = finalize_current_image_scientific_result(
            validated.invocation, request=validated.request,
            wire_stage_result=evidence.wire_stage_result,
            generation_manifest=evidence.generation_manifest,
            primary_count_decision=validated.primary_count_decision,
        )
        if isinstance(finalized, PrimaryClassifierDisposition):
            return build_classifier_inference_result_execution_result(validated, primary_disposition=finalized)
        if isinstance(finalized, Mapping) and kind == "secondary":
            return build_classifier_inference_result_execution_result(validated)
        if type(finalized) is StageResult and finalized.status is StageStatus.NEEDS_INTERACTION:
            decision = validated.primary_count_decision
            if decision is None:
                raise BatchArtifactError("classifier review lacks a primary count decision")
            owner_result = build_classifier_inference_result_execution_result(
                validated, primary_disposition=PrimaryClassifierDisposition(
                    decision, "excluded_below_minimum" if decision.domain_exclusion is not None else "active"),
            )
            return ProjectStageCodecValidationResult(owner_result, replace(finalized, stage=spec.node_kind))
        raise BatchArtifactError("completed classifier result did not finalize scientifically")

    return build_project_current_image_stage_codec(
        spec,
        invocation_projector=projector,
        result_validator=validate_result,
        exchange_reopening=True,
    )


def _require_compatible_classifier_portability(
    portability: object,
) -> ClassifierPortabilityResult:
    if (
        type(portability) is not ClassifierPortabilityResult
        or portability.compatible is not True
        or portability.incompatible_entry_ids
        or portability.reasons
    ):
        raise BatchArtifactError("classifier record is not portable to this target")
    return portability


def build_primary_classifier_inference_stage_codec(
    spec: StageAdapterSpec,
    *,
    state_store: BatchStateStore,
    classifier_record_set_store: ClassifierRecordSetStore,
    classifier_authority_resolver: ClassifierAuthorityResolver | None = None,
    classifier_application_authority: NativeClassifierApplicationAuthority | None = None,
    training_plan_set_store: TrainingPlanSetStore | None = None,
) -> ProjectStageCodec:
    return _build_classifier_inference_stage_codec(
        spec,
        state_store=state_store,
        classifier_record_set_store=classifier_record_set_store,
        classifier_authority_resolver=classifier_authority_resolver,
        classifier_application_authority=classifier_application_authority,
        training_plan_set_store=training_plan_set_store,
        kind="primary",
    )


def build_secondary_classifier_inference_stage_codec(
    spec: StageAdapterSpec,
    *,
    state_store: BatchStateStore,
    classifier_record_set_store: ClassifierRecordSetStore,
    classifier_authority_resolver: ClassifierAuthorityResolver | None = None,
    structure_id: str | None = None,
    classifier_application_authority: NativeClassifierApplicationAuthority | None = None,
) -> ProjectStageCodec:
    return _build_classifier_inference_stage_codec(
        spec,
        state_store=state_store,
        classifier_record_set_store=classifier_record_set_store,
        classifier_authority_resolver=classifier_authority_resolver,
        classifier_application_authority=classifier_application_authority,
        kind="secondary",
        secondary_structure_id=structure_id,
    )


def build_measurements_stage_codec(
    spec: StageAdapterSpec, *, state_store: BatchStateStore
) -> ProjectStageCodec:
    """Compose native measurements from the committed population and frame."""

    if type(state_store) is not BatchStateStore:
        raise BatchArtifactError("measurements codec requires the composed state store")

    def authenticated_invocation(
        context: BatchNodeExecutionContext,
        *,
        target: ProjectStageTargetIntent | ProjectStageAuthorizedEntry,
        config: AnalysisConfig,
        resolved_config_sha256: str,
    ) -> tuple[
        CurrentImageScientificInvocation,
        BatchCommittedArtifactAuthority,
        BatchCommittedArtifactAuthority,
        BatchCurrentExecutionAuthority,
    ]:
        sample_id = target.sample_id
        project_entry_id = target.project_entry_id
        inputs = {item.artifact_kind: item for item in context.inputs}
        if (len(context.inputs) != len(inputs) or not {"segmentation_result", "analysis_frame"} <= set(inputs)
                <= {"segmentation_result", "analysis_frame", "segmentation_correction_result"}):
            raise BatchArtifactError(
                "measurements require exactly one committed segmentation and frame input"
            )
        current = state_store.authenticate_current_execution_context(context)
        segmentation_authority = state_store.authenticate_available_artifact(
            inputs["segmentation_result"]
        )
        frame_authority = state_store.authenticate_available_artifact(inputs["analysis_frame"])
        segmentation = decode_committed_segmentation_result(segmentation_authority)
        correction = None
        if "segmentation_correction_result" in inputs:
            from .batch_cell_corrections import decode_committed_cell_correction
            selection = decode_committed_cell_correction(
                state_store.authenticate_available_artifact(inputs["segmentation_correction_result"]), inputs["segmentation_result"])
            correction = dict(artifact_ref_sha256=inputs["segmentation_correction_result"].artifact_ref_sha256,
                              cell_edits=dict(selection["cell_edits"]), geometry_revision=selection["geometry_revision"])
        frame = validate_analysis_frame_artifact_ref(inputs["analysis_frame"])
        channels = resolve_analysis_channels(
            config,
            inputs["analysis_frame"],
            sample_id=sample_id,
            validation_domain_id=current.validation_domain_id,
        )
        from .batch_measurement_artifacts import validate_current_measurement_population
        validate_current_measurement_population(context=context, current=current,
            segmentation=segmentation, target=target)
        input_digests = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        expected_configuration = (ArtifactConfigurationBinding(sample_id, resolved_config_sha256),)
        # Admission advances the pending head before request projection. The
        # state owner above authenticates the current successor and ancestry.
        if (
            segmentation.artifact_ref != inputs["segmentation_result"]
            or frame_authority.artifact_ref != inputs["analysis_frame"]
            or frame_authority.producer_node_kind != "analysis_preflight"
            or frame_authority.state_head_sha256 != current.state_head_sha256
            or frame_authority.active_plan_sha256 != current.active_plan_sha256
            or frame.spatial_frame.canonical_image_id != segmentation.canonical_image_id
            or frame.spatial_frame.canonical_image_sha256 != segmentation.canonical_image_sha256
            or segmentation.artifact_ref.configuration_bindings != expected_configuration
        ):
            raise BatchArtifactError(
                "measurements current execution or segmentation identity differs"
            )
        snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
        node = next(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
        revision = node.measurement_settings_revision
        from .saved_segmentation_cells import segmentation_cells
        boundary_source = segmentation_cells(segmentation_authority, state_store).description["scope"].get("cell_boundary_source")
        features = project_measurement_setup(
            config=config,
            cell_boundary_source=boundary_source,
            channels=channels,
            section_id=segmentation.section_id,
            node_id=context.node_id,
            input_artifact_ref_sha256s=input_digests,
            validated_response=(
                None
                if revision is not None or configured_measurement_feature_requests(config)
                else state_store.load_validated_interaction_response(context.authorization)
            ),
            settings_revision=revision,
        )
        if isinstance(features, StageResult):
            raise BatchArtifactError(
                "measurements require accepted extraction settings before native preparation"
            )
        from .phenotyping_population_sources import current_population_sources
        population_sources = current_population_sources(snapshot=snapshot, measurement=segmentation, config=config)
        from .batch_measurement_setup import reusable_measurement_selection
        reuse = reusable_measurement_selection(
            snapshot=snapshot, node=node, context=context, segmentation=segmentation,
            channels=channels, features=features, config=config, state_store=state_store,
        )
        invocation = CurrentImageScientificInvocation(
            stage="measurements",
            config=config,
            run_id=target.pipeline_run_id,
            project_entry_id=project_entry_id,
            scientific_state={
                "phenotyping_population_sources": population_sources,
                **({"measurement_reuse": reuse} if reuse is not None else {}),
                **({"cell_correction": correction} if correction is not None else {}),
                "measurement_setup": {
                    "canonical_channels": [item.to_mapping() for item in channels],
                    "feature_requests": [dict(item) for item in features],
                    "segmentation_model_version": segmentation.segmentation_model_version,
                    "settings_revision": None if revision is None else revision.to_mapping(),
                },
                "preflight": {
                    "canonical_image_id": segmentation.canonical_image_id,
                    "canonical_image_sha256": segmentation.canonical_image_sha256,
                },
                "tissue_acceptance": {
                    "generation_id": segmentation.tissue_generation_id,
                    "sections": [
                        {
                            "section_id": segmentation.section_id,
                            "roi_id": segmentation.tissue_roi_id,
                            "path_object_id": segmentation.tissue_path_object_id,
                        }
                    ],
                },
                "registration": {"mode": config.registration.mode},
                "segmentation": {
                    segmentation.section_id: {
                        "segmentation_population_id": (segmentation.segmentation_population_id),
                        "cell_count": segmentation.cell_count,
                        "cell_evidence_sha256": segmentation.cell_evidence_sha256,
                        "cell_ids_sha256": segmentation.cell_ids_sha256,
                        "domain_cell_mapping_sha256": (segmentation.domain_cell_mapping_sha256),
                        "segmentation_population": {
                            "cell_count": segmentation.cell_count,
                            "cell_ids_sha256": segmentation.cell_ids_sha256,
                            "domain_cell_mapping_sha256": (segmentation.domain_cell_mapping_sha256),
                            "active_domain_revisions": [
                                {
                                    "analysis_domain_id": item.analysis_domain_id,
                                    "content_sha256": (item.analysis_domain_revision_sha256),
                                }
                                for item in segmentation.analysis_domain_bindings
                            ],
                        },
                    }
                },
            },
            section_id=segmentation.section_id,
        )
        return invocation, segmentation_authority, frame_authority, current

    def projector(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
    ) -> object:
        invocation, _authority, _frame_authority, _current = authenticated_invocation(
            context,
            target=intent,
            config=intent.resolved_sample.config,
            resolved_config_sha256=(intent.resolved_sample.resolved_config_sha256),
        )
        return invocation

    def validate_measurements_result(
        evidence: ProjectStageTargetExecutionEvidence,
    ) -> BatchNodeExecutionResult:
        validation_inputs = evidence.reopen_validation_inputs()
        try:
            config = AnalysisConfig.from_mapping(validation_inputs.resolved_config)
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError("measurements resolved configuration is invalid") from exc
        authorized = evidence.authorized
        invocation, segmentation_authority, frame_authority, current_execution = (
            authenticated_invocation(
                evidence.context,
                target=authorized,
                config=config,
                resolved_config_sha256=authorized.resolved_config_sha256,
            )
        )
        expected_request = prepare_current_image_scientific_request(invocation)
        expected_request["sample_id"] = authorized.sample_id
        expected_request_payload = canonical_json_bytes(expected_request)
        if (
            hashlib.sha256(expected_request_payload).hexdigest()
            != validation_inputs.scientific_request_body_sha256
            or len(expected_request_payload) != validation_inputs.scientific_request_body_byte_count
        ):
            raise BatchArtifactError(
                "measurements scientific projection differs from committed state"
            )
        # The artifact owner reopens and validates the complete row stream against
        # these committed inputs. A standalone enriched result is not consumed here.
        return build_measurements_result_execution_result(
            evidence,
            segmentation_authority,
            analysis_frame_authority=frame_authority,
            current_execution_authority=current_execution,
            correction_authority=next((state_store.authenticate_available_artifact(item)
                for item in evidence.context.inputs if item.artifact_kind == "segmentation_correction_result"), None),
        )

    return build_project_current_image_stage_codec(
        spec,
        invocation_projector=projector,
        result_validator=validate_measurements_result,
    )


def execute_tissue_resolution_stage(
    context: BatchNodeExecutionContext,
    *,
    resolved_sample: ResolvedBatchSample,
    state_store: BatchStateStore,
) -> BatchRawNodeOutcome:
    """Select and freeze a native level from the committed detector setup."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(resolved_sample) is not ResolvedBatchSample
        or type(state_store) is not BatchStateStore
    ):
        raise BatchArtifactError("tissue-resolution execution requires exact composed owners")
    inputs = {item.artifact_kind: item for item in context.inputs}
    if (
        len(context.inputs) != 3
        or set(inputs) != {"analysis_frame", "project_catalogue_entry", "tissue_setup_result"}
        or context.authorization.dispatch_binding.participant_ids != (resolved_sample.sample_id,)
        or type(resolved_sample.config) is not AnalysisConfig
        or hashlib.sha256(canonical_json_bytes(resolved_sample.config.to_mapping())).hexdigest()
        != resolved_sample.resolved_config_sha256
        or inputs["analysis_frame"].configuration_bindings
        != (
            ArtifactConfigurationBinding(
                resolved_sample.sample_id,
                resolved_sample.resolved_config_sha256,
            ),
        )
        or resolved_sample.slide_id != inputs["analysis_frame"].scope.slide_id
        or resolved_sample.validation_domain_id
        != inputs["analysis_frame"].scope.validation_domain_id
    ):
        raise BatchArtifactError("tissue-resolution sample or input scope differs")
    try:
        execution = state_store.authenticate_current_execution_context(context)
        frame_authority = state_store.authenticate_available_artifact(inputs["analysis_frame"])
        project_authority = state_store.authenticate_available_artifact(
            inputs["project_catalogue_entry"]
        )
        setup_authority = state_store.authenticate_available_artifact(inputs["tissue_setup_result"])
        setup = decode_committed_tissue_setup_result(setup_authority)
        retained_head = state_store.load_head()
        inventory = tuple(level.to_mapping() for level in setup.native_pyramid_inventory)
        tissue_config = setup.tissue_detection
        if setup.resolved_config_sha256 != resolved_sample.resolved_config_sha256:
            raise BatchArtifactError("tissue-resolution settings differ from admission")
        response = state_store.load_validated_interaction_response(context.authorization)
        node = state_store._load_plan_snapshot(execution.active_plan_sha256).node_by_id[context.node_id]
        if "settled_review_revision" in node:
            # This node explicitly reopens the level decision. Detector/image
            # configuration stays immutable; only its selection method is reviewed.
            from dataclasses import replace
            tissue_config = replace(tissue_config,
                native_level_selection_mode=NativeLevelSelectionMode.PROMPT,
                native_resolution_index=None)
        if tissue_config.native_level_selection_mode is NativeLevelSelectionMode.PROMPT:
            interaction_context = build_tissue_resolution_interaction_context(
                tissue_config, inventory
            )
            compatible_indices = tuple(setup.native_observation["compatible_native_resolution_indices"])
            interaction_context["compatible_native_resolution_indices"] = list(compatible_indices)
            interaction_context_sha256 = hashlib.sha256(
                canonical_json_bytes(interaction_context)
            ).hexdigest()
            reviewed_selection = None
            if response is not None:
                if (
                    response.get("action") != InteractionAction.SELECT_TISSUE_NATIVE_LEVEL.value
                    or not isinstance(response.get("response"), Mapping)
                    or not isinstance(response["response"].get("response"), Mapping)
                ):
                    raise BatchArtifactError("tissue-resolution response has an invalid action or selection")
                # The store authenticated this immutable response and its predecessor.
                # Compare its scientific choice with the current detector, rather than
                # requiring identical presentation metadata on an older saved prompt.
                reviewed_selection = response["response"]["response"]
            incompatible_saved_choice = (
                reviewed_selection is not None
                and reviewed_selection.get("native_resolution_index") not in compatible_indices
            )
            if response is None or incompatible_saved_choice:
                return compose_single_output_direct_stage_outcome(
                    context,
                    StageResult.needs_interaction(
                        stage="tissue_resolution",
                        slide_id=resolved_sample.slide_id,
                        reason=InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED,
                        message=(
                            "The saved resolution does not match this detector. Choose a compatible "
                            "resolution below, or return to detector setup to choose a different detector."
                            if incompatible_saved_choice else
                            "Choose the image resolution to use for tissue detection."
                        ),
                        allowed_actions=(InteractionAction.SELECT_TISSUE_NATIVE_LEVEL.value,),
                        resume_context=interaction_context,
                        checkpoint_id=("tissue-resolution-" + interaction_context_sha256[:48]),
                    ),
                    None,
                )
            # We pass the accepted index and level-zero choice to the numerical
            # selector. The enclosing record retains the reviewed image inventory.
            selection = select_tissue_native_resolution(
                tissue_config,
                inventory,
                response=reviewed_selection,
            )
        else:
            if response is not None:
                raise BatchArtifactError(
                    "non-interactive tissue-resolution attempt carries a response"
                )
            selection = select_tissue_native_resolution(tissue_config, inventory)
        final_head = state_store.load_head()
        if final_head != retained_head:
            raise BatchArtifactError(
                "tissue-resolution state changed during native-level selection"
            )
        output = build_tissue_resolution_result_artifact_ref(
            context,
            execution_authority=execution,
            analysis_frame_authority=frame_authority,
            project_catalogue_entry_authority=project_authority,
            tissue_setup_authority=setup_authority,
            selection=selection,
            native_pyramid_inventory=inventory,
        )
    except BatchArtifactError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise BatchArtifactError("tissue-resolution acquisition or selection failed") from exc
    return compose_single_output_direct_stage_outcome(
        context,
        StageResult.completed(
            stage="tissue_resolution",
            slide_id=resolved_sample.slide_id,
        ),
        output,
    )


@dataclass(frozen=True, slots=True)
class AnalysisPreflightStageAdapter:
    """Bind analysis preflight to one immutable resolved-sample inventory."""

    spec: StageAdapterSpec
    resolved_samples: tuple[ResolvedBatchSample, ...]
    input_validator: AnalysisInputValidator
    image_probe: QuPathImageProbe
    _sample_by_id: MappingProxyType = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate the AnalysisPreflightStageAdapter fields when the record is constructed.

        The checks cover ``spec``, ``resolved_samples``, ``input_validator``,
        ``image_probe``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        if type(self.spec) is not StageAdapterSpec or self.spec.node_kind != "analysis_preflight":
            raise BatchArtifactError(
                "analysis preflight adapter requires its exact stage specification"
            )
        if type(self.resolved_samples) is not tuple or not all(
            type(item) is ResolvedBatchSample for item in self.resolved_samples
        ):
            raise BatchArtifactError("analysis preflight adapter requires typed resolved samples")
        if not callable(self.input_validator) or not callable(self.image_probe):
            raise BatchArtifactError(
                "analysis preflight adapter requires validator and image probe callables"
            )
        sample_ids = tuple(item.sample_id for item in self.resolved_samples)
        if len(set(sample_ids)) != len(sample_ids):
            raise BatchArtifactError("analysis preflight adapter has duplicate sample IDs")
        if any(type(item.config) is not AnalysisConfig for item in self.resolved_samples):
            raise BatchArtifactError(
                "analysis preflight adapter samples require exact AnalysisConfig values"
            )
        if any(
            hashlib.sha256(canonical_json_bytes(item.config.to_mapping())).hexdigest()
            != item.resolved_config_sha256
            for item in self.resolved_samples
        ):
            raise BatchArtifactError("analysis preflight sample configuration digest differs")
        object.__setattr__(
            self,
            "_sample_by_id",
            MappingProxyType({item.sample_id: item for item in self.resolved_samples}),
        )

    def execute_raw(
        self,
        context: BatchNodeExecutionContext,
    ) -> BatchRawNodeOutcome:
        if not isinstance(context, BatchNodeExecutionContext):
            raise BatchArtifactError("analysis preflight execution requires an exact context")
        if context.adapter_spec != self.spec:
            raise BatchArtifactError("analysis preflight adapter received a foreign specification")
        participant_ids = context.authorization.dispatch_binding.participant_ids
        if len(participant_ids) != 1:
            raise BatchArtifactError("analysis preflight requires exactly one dispatch participant")
        sample = self._sample_by_id.get(participant_ids[0])
        if sample is None:
            raise BatchArtifactError(
                "analysis preflight dispatch participant is not a resolved sample"
            )
        if len(context.inputs) != 1 or context.inputs[0].artifact_kind != "processed_image":
            raise BatchArtifactError(
                "analysis preflight requires exactly one processed OME-TIFF input"
            )
        processed = context.inputs[0]
        binding = processed.configuration_bindings
        if (
            processed.scope.validation_domain_id != sample.validation_domain_id
            or processed.scope.sample_id != sample.sample_id
            or processed.scope.slide_id != sample.slide_id
            or processed.scope.project_entry_id is not None
            or processed.scope.analysis_domain_bindings
            or len(binding) != 1
            or binding[0].sample_id != sample.sample_id
            or binding[0].resolved_config_sha256 != sample.resolved_config_sha256
            or processed.locator is None
        ):
            raise BatchArtifactError(
                "analysis preflight input differs from its resolved sample authority"
            )
        selected_config = replace(sample.config, input_image=processed.locator,
                                  provenance=processed.locator.parent / "provenance.json")
        report = self.input_validator(selected_config)
        if not isinstance(report, InputValidationReport):
            raise BatchArtifactError("analysis preflight validator returned an untyped report")
        if (
            report.image_path != selected_config.input_image
            or report.provenance_path != selected_config.provenance
        ):
            raise BatchArtifactError(
                "analysis preflight report paths differ from resolved configuration"
            )
        probe = self.image_probe(selected_config.input_image)
        if not isinstance(probe, QuPathImagePreflight):
            raise BatchArtifactError("analysis preflight image probe returned an untyped result")
        evidence = BatchAnalysisPreflightEvidence.create(report, probe)
        output = build_analysis_frame_artifact_ref(context, processed, evidence)
        if validate_analysis_frame_artifact_ref(output) != evidence:
            raise BatchArtifactError(
                "analysis preflight owner reopen differs from composed evidence"
            )
        return compose_single_output_direct_stage_outcome(
            context,
            StageResult.completed(
                stage=context.adapter_spec.node_kind,
                slide_id=sample.slide_id,
            ),
            output,
        )


def _domain_binding_owner_sort_key(
    value: AnalysisDomainBinding,
) -> tuple[str, str, str, int, str]:
    """Match the canonical ``ArtifactScope`` analysis-domain binding order."""

    return (
        value.sample_id,
        value.slide_id,
        value.analysis_domain_id,
        value.analysis_domain_revision,
        value.project_entry_id,
    )


@dataclass(frozen=True, slots=True)
class SectionSegmentationFanIn:
    """Require one accepted primary choice for each segmented analysis region."""

    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]

    def __post_init__(self) -> None:
        bindings = tuple(self.analysis_domain_bindings)
        if not bindings or not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise ValueError("section fan-in requires one or more domain bindings")
        if tuple(sorted(bindings, key=_domain_binding_owner_sort_key)) != bindings:
            raise ValueError("section fan-in domain bindings must be in canonical sorted order")
        binding_keys = tuple(_domain_binding_owner_sort_key(item) for item in bindings)
        if len(set(binding_keys)) != len(binding_keys):
            raise ValueError("section fan-in domain bindings must be unique")
        object.__setattr__(self, "analysis_domain_bindings", bindings)

    @property
    def required_primary_bindings(self) -> tuple[AnalysisDomainBinding, ...]:
        return self.analysis_domain_bindings

    def validate_coverage(
        self,
        *,
        primary_bindings: tuple[AnalysisDomainBinding, ...],
        primary_skipped_bindings: tuple[AnalysisDomainBinding, ...] = (),
    ) -> None:
        actual_primary = tuple(primary_bindings) + tuple(primary_skipped_bindings)
        if not all(isinstance(item, AnalysisDomainBinding) for item in actual_primary):
            raise ValueError("primary coverage must contain domain bindings")
        if len(set(actual_primary)) != len(actual_primary):
            raise ValueError("duplicate primary result coverage")
        expected_primary = set(self.required_primary_bindings)
        primary_set = set(actual_primary)
        extra_primary = primary_set - expected_primary
        missing_primary = expected_primary - primary_set
        if extra_primary:
            raise ValueError("extra primary result coverage")
        if missing_primary:
            raise ValueError("missing primary result coverage")



IMAGE_STAGE_NODE_KINDS = (
    "analysis_preflight",
    "project_upsert",
    "paired_image_preparation",
    "tissue_setup",
    "tissue_resolution",
    "tissue_channel",
    "tissue_candidate",
    "tissue_acceptance",
    "registration",
    "analysis_domain_selection",
    "primary_classifier_inference",
    "secondary_classifier_inference",
    "segmentation",
    "measurements",
)

# This mapping is intentionally local.  ``batch_dag`` retains the whole
# application mapping, including training parents and stages.
IMAGE_STAGE_STAGE_ADAPTATION = MappingProxyType(
    {
        "preflight": ("analysis_preflight",),
        "project": ("project_upsert", "paired_image_preparation"),
        "tissue_setup": ("tissue_setup",),
        "tissue_resolution": ("tissue_resolution",),
        "tissue_channel": ("tissue_channel",),
        "tissue_candidate": ("tissue_candidate",),
        "tissue_acceptance": ("tissue_acceptance",),
        "registration": ("registration",),
        "analysis_domain_selection": ("analysis_domain_selection",),
        "primary_classifier": ("primary_classifier_inference",),
        # Zero configured secondary structures produce no node.  That
        # conditional is graph materialization, not an empty adapter/result.
        "secondary_classifier": ("secondary_classifier_inference",),
        "segmentation": ("segmentation",),
        "measurements": ("measurements",),
    }
)

OUTPUT_KIND_BY_NODE = MappingProxyType(
    dict(
        zip(
            IMAGE_STAGE_NODE_KINDS,
            (
                "analysis_frame",
                "project_catalogue_entry",
                "project_catalogue_entry",
                "tissue_setup_result",
                "tissue_resolution_result",
                "tissue_channel_result",
                "tissue_candidate_result",
                "tissue_acceptance_result",
                "registration_result",
                "accepted_analysis_domain",
                "primary_inference_result",
                "secondary_inference_result",
                "segmentation_result",
                "measurements_result",
            ),
            strict=True,
        )
    )
)

INPUT_KINDS_BY_NODE = MappingProxyType(
    {
        "analysis_preflight": ("processed_image",),
        "project_upsert": ("analysis_frame", "processed_image"),
        "paired_image_preparation": ("analysis_frame", "processed_image", "project_catalogue_entry"),
        "tissue_setup": ("analysis_frame", "project_catalogue_entry"),
        "tissue_resolution": ("analysis_frame", "project_catalogue_entry", "tissue_setup_result"),
        "tissue_channel": ("analysis_frame", "tissue_resolution_result", "tissue_setup_result"),
        "tissue_candidate": ("analysis_frame", "tissue_channel_result", "tissue_setup_result"),
        "tissue_acceptance": ("analysis_frame", "tissue_candidate_result", "tissue_setup_result"),
        "registration": ("analysis_frame", "tissue_acceptance_result"),
        "analysis_domain_selection": (
            "analysis_frame",
            "registration_result",
            "tissue_acceptance_result",
        ),
        "primary_classifier_inference": (
            "accepted_analysis_domain",
            "analysis_frame",
            "project_catalogue_entry",
            "classifier_record_set",
        ),
        "secondary_classifier_inference": (
            "accepted_analysis_domain",
            "analysis_frame",
            "project_catalogue_entry",
            "classifier_record_set",
        ),
        # Segmentation uses accepted tissue/primary regions. Secondary
        # structures are detected only after the phenotype result exists.
        "segmentation": (
            "accepted_analysis_domain",
            "analysis_frame",
        ),
        "measurements": ("segmentation_result", "analysis_frame"),
    }
)

INPUT_MULTIPLICITY_BY_NODE = MappingProxyType(
    {
        "analysis_preflight": MappingProxyType({"processed_image": "single"}),
        "project_upsert": MappingProxyType(
            {
                "analysis_frame": "single",
                "processed_image": "single",
            }
        ),
        "paired_image_preparation": MappingProxyType({
            "analysis_frame": "single", "processed_image": "single",
            "project_catalogue_entry": "single",
        }),
        "tissue_setup": MappingProxyType(
            {
                "analysis_frame": "single",
                "project_catalogue_entry": "single",
            }
        ),
        "tissue_resolution": MappingProxyType(
            {
                "analysis_frame": "single",
                "project_catalogue_entry": "single",
                "tissue_setup_result": "single",
            }
        ),
        "tissue_channel": MappingProxyType(
            {
                "analysis_frame": "single",
                "tissue_resolution_result": "single",
                "tissue_setup_result": "single",
            }
        ),
        "tissue_candidate": MappingProxyType(
            {
                "analysis_frame": "single",
                "tissue_channel_result": "single",
                "tissue_setup_result": "single",
            }
        ),
        "tissue_acceptance": MappingProxyType(
            {
                "analysis_frame": "single",
                "tissue_candidate_result": "single",
                "tissue_setup_result": "single",
            }
        ),
        "registration": MappingProxyType(
            {
                "analysis_frame": "single",
                "tissue_acceptance_result": "single",
            }
        ),
        "analysis_domain_selection": MappingProxyType(
            {
                "analysis_frame": "single",
                "registration_result": "single",
                "tissue_acceptance_result": "single",
            }
        ),
        "primary_classifier_inference": MappingProxyType(
            {
                "accepted_analysis_domain": "single",
                "analysis_frame": "single",
                "project_catalogue_entry": "single",
                "classifier_record_set": "single",
            }
        ),
        "secondary_classifier_inference": MappingProxyType(
            {
                "accepted_analysis_domain": "single",
                "analysis_frame": "single",
                "project_catalogue_entry": "single",
                "classifier_record_set": "single",
            }
        ),
        "segmentation": MappingProxyType(
            {
                "accepted_analysis_domain": "single",
                "analysis_frame": "single",
            }
        ),
        "measurements": MappingProxyType(
            {"segmentation_result": "single", "analysis_frame": "single"}
        ),
        "marker_rule_application": MappingProxyType(
            {
                "marker_gate": "single",
                "marker_combination": "single",
                "measurements_result": "single",
                "phenotyping_mode": "exact_section_domain_binding_cover",
            }
        ),
    }
)

OPTIONAL_INPUT_KINDS_BY_NODE = MappingProxyType(
    {
        "analysis_preflight": (),
        "project_upsert": (),
        "paired_image_preparation": (),
        "tissue_setup": (),
        "tissue_resolution": (),
        "tissue_channel": (),
        "tissue_candidate": (),
        "tissue_acceptance": (),
        "registration": ("project_catalogue_entry",),
        "analysis_domain_selection": (),
        "primary_classifier_inference": (),
        "secondary_classifier_inference": ("primary_inference_result", "training_plan_set", "secondary_inference_result"),
        "segmentation": ("primary_inference_result", "training_plan_set"),
        "measurements": ("segmentation_correction_result",),
        "marker_rule_application": (),
    }
)

OPTIONAL_INPUT_MULTIPLICITY_BY_NODE = MappingProxyType(
    {
        node_kind: MappingProxyType(
            {
                artifact_kind: (
                    ArtifactMultiplicity.EXACT_SET.value
                    if (node_kind == "segmentation" or
                        (node_kind == "secondary_classifier_inference" and artifact_kind == "secondary_inference_result"))
                    else ArtifactMultiplicity.SINGLE.value
                )
                for artifact_kind in kinds
            }
        )
        for node_kind, kinds in OPTIONAL_INPUT_KINDS_BY_NODE.items()
    }
)

DISPATCH_BY_NODE = MappingProxyType(
    {
        "analysis_preflight": DispatchMode.SINGLE,
        "project_upsert": DispatchMode.SINGLE,
        "paired_image_preparation": DispatchMode.SINGLE,
        "tissue_setup": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "tissue_resolution": DispatchMode.SINGLE,
        "tissue_channel": DispatchMode.SINGLE,
        "tissue_candidate": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "tissue_acceptance": DispatchMode.SINGLE,
        "registration": DispatchMode.SINGLE,
        "analysis_domain_selection": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "primary_classifier_inference": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "secondary_classifier_inference": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "segmentation": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "measurements": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        "marker_rule_application": DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
    }
)

RESOURCE_BY_NODE = MappingProxyType(
    {
        "analysis_preflight": ResourceKind.QUPATH_IMAGE_READ.value,
        "project_upsert": ResourceKind.PROJECT_CATALOGUE_MUTATION.value,
        "paired_image_preparation": ResourceKind.PROJECT_CATALOGUE_MUTATION.value,
        "tissue_setup": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "tissue_resolution": ResourceKind.QUPATH_IMAGE_READ.value,
        "tissue_channel": ResourceKind.SCIENTIFIC_COMPUTE.value,
        "tissue_candidate": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "tissue_acceptance": ResourceKind.SCIENTIFIC_COMPUTE.value,
        "registration": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "analysis_domain_selection": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "primary_classifier_inference": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "secondary_classifier_inference": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "segmentation": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "measurements": ResourceKind.QUPATH_IMAGE_WRITE.value,
        "marker_rule_application": ResourceKind.QUPATH_IMAGE_WRITE.value,
    }
)

# The closed scheduler enum still has no native-resolution or specialized
# workspace-then-project lease resource. Those rows remain explicitly
# unresolved until their scheduler resource contracts are widened.
RESOURCE_CONTRACT_STATUS_BY_NODE = MappingProxyType(
    {
        "analysis_preflight": "existing_owner",
        "project_upsert": "existing_owner",
        "paired_image_preparation": "existing_owner",
        "tissue_setup": "existing_owner",
        "tissue_resolution": "existing_owner",
        "tissue_channel": "existing_owner",
        "tissue_candidate": "existing_owner",
        "tissue_acceptance": "existing_owner",
        "registration": "existing_owner",
        "analysis_domain_selection": "existing_owner",
        "primary_classifier_inference": "existing_owner",
        "secondary_classifier_inference": "existing_owner",
        "segmentation": "existing_owner",
        "measurements": "existing_owner",
        "marker_rule_application": "existing_owner",
        "supervised_phenotype_inference": "existing_owner",
        "clustering_label_import": "existing_owner",
    }
)

PROJECT_CURRENT_IMAGE_ASSET_BY_NODE = MappingProxyType(
    {
        "tissue_setup": QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value,
        "tissue_candidate": QuPathScriptAsset.DETECT_TISSUE_SECTIONS.value,
        "analysis_domain_selection": QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value,
        "primary_classifier_inference": (
            QuPathScriptAsset.APPLY_PRIMARY_COMPARTMENT_CLASSIFIER.value
        ),
        "secondary_classifier_inference": (
            QuPathScriptAsset.APPLY_SECONDARY_STRUCTURE_CLASSIFIERS.value
        ),
        "saved_result_retirement": QuPathScriptAsset.RETIRE_SAVED_RESULTS.value,
        "segmentation_correction": QuPathScriptAsset.CORRECT_KNOWN_CELL.value,
        "segmentation": QuPathScriptAsset.RUN_INSTANSEG.value,
        "measurements": QuPathScriptAsset.PREPARE_PHENOTYPING_MEASUREMENTS.value,
        "clustering_label_import": QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value,
        "marker_rule_application": (QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value),
        "supervised_phenotype_inference": (QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES.value),
        "semantic_raw_inventory_snapshot": (QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value),
        "phenotype_handoff": (QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value),
        "semantic_evidence": (QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value),
        "semantic_finalization": (QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value),
    }
)

# These are the frozen registry-key facts used to construct one fresh
# owner codec.  Unclosed stage-owner capabilities remain gated below.
PROJECT_CURRENT_IMAGE_WIRE_STAGE_BY_NODE = MappingProxyType(
    {
        "tissue_setup": "tissue_setup",
        "tissue_candidate": "tissue_detection",
        "analysis_domain_selection": "analysis_domain_selection",
        "primary_classifier_inference": "primary_classifier",
        "secondary_classifier_inference": "secondary_classifier",
        "saved_result_retirement": "saved_result_retirement",
        "segmentation_correction": "segmentation_correction",
        "segmentation": "segmentation",
        "measurements": "measurements",
        "clustering_label_import": "cell_labels_import",
        "marker_rule_application": "phenotyping",
        "supervised_phenotype_inference": "supervised_phenotyping",
        "semantic_raw_inventory_snapshot": "exports",
        "phenotype_handoff": "exports",
        "semantic_evidence": "semantic_validation",
        "semantic_finalization": "semantic_validation",
    }
)
PROJECT_CURRENT_IMAGE_TERMINAL_STAGE_BY_NODE = MappingProxyType(
    {node_kind: node_kind for node_kind in PROJECT_CURRENT_IMAGE_ASSET_BY_NODE}
)
PROJECT_CURRENT_IMAGE_CODEC_VERSION_BY_NODE = MappingProxyType(
    {
        node_kind: (
            "2"
            if node_kind in {"segmentation", "primary_classifier_inference", "measurements"}
            else "1"
        )
        for node_kind in PROJECT_CURRENT_IMAGE_ASSET_BY_NODE
    }
)

PROJECT_CURRENT_IMAGE_WORKFLOW_STAGE_BY_NODE = MappingProxyType(
    {
        "tissue_candidate": "tissue_candidate",
        "analysis_domain_selection": "analysis_domain_selection",
        "primary_classifier_inference": "primary_classifier",
        "secondary_classifier_inference": "secondary_classifier",
        "saved_result_retirement": "saved_result_retirement",
        "segmentation_correction": "segmentation_correction",
        "segmentation": "segmentation",
        "measurements": "measurements",
        "clustering_label_import": "cell_labels_import",
        "marker_rule_application": "phenotyping",
        "supervised_phenotype_inference": "supervised_deployment",
        "semantic_evidence": "semantic_validation",
        "semantic_finalization": "semantic_validation",
    }
)

_PROJECT_STAGE_PUBLIC_EVIDENCE_FIELDS = (
    "context",
    "authorized",
    "observation",
    "wire_stage_result",
    "generation_manifest",
    "execution_exchange_sha256",
    "validated_exchange_sha256",
)


def _validate_current_image_scientific_invocation(
    invocation: object,
    *,
    context: BatchNodeExecutionContext,
    intent: ProjectStageTargetIntent,
    spec: StageAdapterSpec,
) -> CurrentImageScientificInvocation:
    if type(invocation) is not CurrentImageScientificInvocation:
        raise ProjectStageCodecError(
            "current-image owner must project one exact scientific invocation"
        )
    expected_stage = PROJECT_CURRENT_IMAGE_WORKFLOW_STAGE_BY_NODE[spec.node_kind]
    input_refs = tuple(context.inputs)
    input_digests = tuple(sorted(item.artifact_ref_sha256 for item in input_refs))
    if (
        invocation.stage != expected_stage
        or invocation.project_entry_id != intent.project_entry_id
        or invocation.config.slide_id != intent.slide_id
        or invocation.run_id != intent.pipeline_run_id
        or invocation.config != intent.resolved_sample.config
        or input_digests != intent.prerequisite_artifact_ref_sha256s
    ):
        raise ProjectStageCodecError(
            "current-image scientific invocation identity or config differs"
        )
    for artifact in input_refs:
        scope = artifact.scope
        if (
            scope.sample_id not in {None, intent.sample_id}
            or scope.slide_id not in {None, intent.slide_id}
            or scope.project_entry_id not in {None, intent.project_entry_id}
        ):
            raise ProjectStageCodecError(
                "current-image scientific input scope differs from target identity"
            )
    return invocation


def build_project_current_image_stage_codec(
    spec: StageAdapterSpec,
    *,
    invocation_projector: Callable[..., object],
    result_validator: Callable[[ProjectStageTargetExecutionEvidence], object],
    exchange_reopening: bool = False,
) -> ProjectStageCodec:
    """Build one fresh codec around the path-free scientific seam.

    Tissue candidate, analysis-domain selection, segmentation, and measurements have
    complete public output-owner contracts in the present inventory. Candidate interaction
    material is validated but not published; tissue acceptance is a separate direct
    transition and has no codec or QuPath asset. Other stages remain explicit hard stops."""

    if type(spec) is not StageAdapterSpec:
        raise ProjectStageCodecError("current-image codec requires one exact adapter specification")
    if spec.node_kind not in PROJECT_CURRENT_IMAGE_ASSET_BY_NODE:
        raise ProjectStageCodecError("adapter specification is not a current-image stage")
    if (
        spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        or spec.resource_kind != ResourceKind.QUPATH_IMAGE_WRITE.value
    ):
        raise ProjectStageCodecError(
            "current-image adapter specification has foreign dispatch authority"
        )
    if spec.node_kind not in {
        "tissue_candidate",
        "analysis_domain_selection",
        "primary_classifier_inference",
        "secondary_classifier_inference",
        "segmentation",
        "measurements",
        "clustering_label_import",
        "marker_rule_application",
        "supervised_phenotype_inference",
    }:
        raise ImageStageAdapterRegistryError(
            f"Task-12 {spec.node_kind} live owner/ref or classifier compatibility "
            "contract is not closed"
        )
    if (
        not callable(invocation_projector)
        or not callable(result_validator)
        or type(exchange_reopening) is not bool
    ):
        raise ProjectStageCodecError("current-image codec requires callable scientific owners")
    binding = ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=PROJECT_CURRENT_IMAGE_ASSET_BY_NODE[spec.node_kind],
        wire_stage_id=PROJECT_CURRENT_IMAGE_WIRE_STAGE_BY_NODE[spec.node_kind],
        terminal_stage_id=PROJECT_CURRENT_IMAGE_TERMINAL_STAGE_BY_NODE[spec.node_kind],
        request_codec_version=PROJECT_CURRENT_IMAGE_CODEC_VERSION_BY_NODE[spec.node_kind],
    )

    def project_request(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
        *,
        committed_exchange_reopener: ProjectStageCommittedExchangeReopener | None = None,
    ) -> Mapping[str, object]:
        if exchange_reopening:
            if type(committed_exchange_reopener) is not ProjectStageCommittedExchangeReopener:
                raise ProjectStageCodecError(
                    "current-image stage owner requires one runtime reopener"
                )
            projected = invocation_projector(
                context,
                intent,
                committed_exchange_reopener=committed_exchange_reopener,
            )
        else:
            if committed_exchange_reopener is not None:
                raise ProjectStageCodecError(
                    "ordinary current-image owner cannot receive a reopener"
                )
            projected = invocation_projector(context, intent)
        invocation = _validate_current_image_scientific_invocation(
            projected,
            context=context,
            intent=intent,
            spec=spec,
        )
        request = prepare_current_image_scientific_request(invocation)
        request["sample_id"] = intent.sample_id
        return request

    def validate_result(
        evidence: object,
    ) -> BatchNodeExecutionResult | ProjectStageCodecValidationResult:
        if type(evidence) is not ProjectStageTargetExecutionEvidence:
            raise ProjectStageCodecError(
                "current-image result requires exact seven-field execution evidence"
            )
        public_fields = tuple(
            name for name in evidence.__dataclass_fields__ if not name.startswith("_")
        )
        if public_fields != _PROJECT_STAGE_PUBLIC_EVIDENCE_FIELDS:
            raise ProjectStageCodecError("current-image execution evidence field contract differs")
        authorized = evidence.authorized
        evidence_context = evidence.context
        if (
            evidence_context.adapter_spec != spec
            or authorized.adapter_spec_sha256 != spec.spec_sha256
            or authorized.terminal_stage_id != spec.node_kind
            or authorized.request_codec_id != binding.target_script_asset_id
            or authorized.request_codec_version != binding.request_codec_version
            or authorized.wire_stage_id != binding.wire_stage_id
            or evidence.wire_stage_result.stage != binding.wire_stage_id
        ):
            raise ProjectStageCodecError("current-image execution evidence identity differs")
        result = result_validator(evidence)
        execution_result = (
            result.execution_result if type(result) is ProjectStageCodecValidationResult else result
        )
        if type(execution_result) is not BatchNodeExecutionResult:
            raise ProjectStageCodecError(
                "current-image result owner must return one BatchNodeExecutionResult"
            )
        if (
            execution_result.authorization_sha256
            != evidence_context.authorization.authorization_sha256
        ):
            raise ProjectStageCodecError("current-image result has foreign attempt authorization")
        # The project runtime validates output scope once, after admitting the
        # owner's terminal status. A completed native snapshot can still need
        # review and therefore has no accepted artifacts to publish.
        return result

    factory = (
        ProjectStageCodec.for_exchange_reopening_stage_owner
        if exchange_reopening
        else ProjectStageCodec.for_stage_owner
    )
    return factory(
        binding=binding,
        spec=spec,
        request_projector=project_request,
        result_validator=validate_result,
    )


# Exact public observation fields for an authenticated manifest.  The
# nested stage-owner record is deliberately not named here until its codec
# contract is approved.
PROJECT_STAGE_GENERATION_MANIFEST_EVIDENCE_FIELDS = (
    "generation_manifest_payload_sha256",
    "generation_manifest_byte_count",
    "saved_state_after_sha256",
)

# Exact public owner schema inventory used by the immutable image-stage specs.
OWNER_SCHEMA_BY_ARTIFACT = MappingProxyType(
    {
        "processed_image": ("mif-batch-reference-unmixing-completion", 2, "existing_owner"),
        "analysis_frame": ("mif-batch-analysis-preflight", 2, "existing_owner"),
        "project_catalogue_entry": (
            "mif-batch-project-catalogue",
            1,
            "existing_owner",
        ),
        "tissue_resolution_result": (
            "coarse-tissue-resolution-selection",
            1,
            "existing_owner",
        ),
        "tissue_setup_result": ("tissue-detector-setup", 1, "existing_owner"),
        "tissue_channel_result": (
            "tissue-detection-channel-selection",
            1,
            "existing_owner",
        ),
        "tissue_candidate_result": ("roi-generation-manifest", 1, "existing_owner"),
        "tissue_acceptance_result": ("roi-generation-manifest", 1, "existing_owner"),
        "registration_result": (
            "mif-batch-registration-result",
            1,
            "existing_owner",
        ),
        "accepted_analysis_domain": ("mif-batch-accepted-analysis-domain", 1, "existing_owner"),
        "primary_inference_result": ("roi-generation-manifest", 1, "existing_owner"),
        "secondary_inference_result": ("roi-generation-manifest", 1, "existing_owner"),
        "segmentation_result": ("roi-generation-manifest", 1, "existing_owner"),
        "segmentation_correction_result": ("roi-generation-manifest", 1, "existing_owner"),
        "measurements_result": ("roi-generation-manifest", 1, "existing_owner"),
        "classifier_record_set": ("mif-classifier-record-set", 1, "existing_owner"),
        "training_plan_set": ("mif-training-plan-set", 2, "existing_owner"),
    }
)

OWNER_IDENTITY_FIELDS_BY_ARTIFACT = MappingProxyType(
    {
        "processed_image": preprocessing_identity_contract("processed_image").required_fields,
        "analysis_frame": analysis_preflight_identity_contract().required_fields,
        "project_catalogue_entry": (project_catalogue_entry_identity_contract().required_fields),
        "tissue_resolution_result": (TISSUE_RESOLUTION_RESULT_IDENTITY_FIELDS),
        "tissue_setup_result": tissue_setup_result_identity_contract().required_fields,
        "tissue_channel_result": TISSUE_CHANNEL_RESULT_IDENTITY_FIELDS,
        "tissue_candidate_result": (tissue_candidate_result_identity_contract().required_fields),
        "tissue_acceptance_result": (tissue_acceptance_result_identity_contract().required_fields),
        "registration_result": BATCH_REGISTRATION_RESULT_REQUIRED_FIELDS,
        "accepted_analysis_domain": (accepted_analysis_domain_identity_contract().required_fields),
        "primary_inference_result": (
            primary_classifier_inference_result_identity_contract().required_fields
        ),
        "secondary_inference_result": (
            secondary_classifier_inference_result_identity_contract().required_fields
        ),
        "segmentation_result": segmentation_result_identity_contract().required_fields,
        "segmentation_correction_result": correction_result_identity_contract().required_fields,
        "measurements_result": measurements_result_identity_contract().required_fields,
        "classifier_record_set": (classifier_record_set_identity_contract().required_fields),
        "training_plan_set": training_plan_set_identity_contract().required_fields,
    }
)

# This is an inventory of the precise owner projection required by ,
# not a replacement public owner contract.  The currently available public
# owner contracts are delegated below.  Project and scientific owners must publish their own
# immutable ArtifactIdentityContract before this registry may construct a
# StageAdapterSpec; duplicating one here would silently make orchestration the
# source of truth for their data semantics.
OWNER_CONTRACT_STATUS_BY_ARTIFACT = MappingProxyType(
    {
        artifact_kind: (
            "existing_public_owner"
            if artifact_kind
            in {
                "processed_image",
                "analysis_frame",
                "project_catalogue_entry",
                "tissue_resolution_result",
                "tissue_setup_result",
                "tissue_channel_result",
                "tissue_candidate_result",
                "tissue_acceptance_result",
                "registration_result",
                "accepted_analysis_domain",
                "primary_inference_result",
                "secondary_inference_result",
                "segmentation_result",
                "segmentation_correction_result",
                "measurements_result",
                "classifier_record_set",
                "training_plan_set",
            }
            else "owner_contract_prerequisite"
        )
        for artifact_kind in OWNER_IDENTITY_FIELDS_BY_ARTIFACT
    }
)

# The wrapper envelope is frozen by , while owner-specific nested
# content names/counts are not yet frozen by a reviewed codec.
NESTED_OWNER_CODEC_STATUS_BY_ARTIFACT = MappingProxyType(
    {
        "tissue_setup_result": "existing_owner",
        "tissue_candidate_result": "existing_owner",
        "tissue_acceptance_result": "existing_owner",
        "accepted_analysis_domain": "existing_owner",
        "primary_inference_result": "existing_owner",
        "secondary_inference_result": "existing_owner",
        "segmentation_result": "existing_owner",
        "measurements_result": "existing_owner",
    }
)

SEGMENTATION_FAN_IN_STATUS = "existing_owner"

_ZERO_SHA256 = "0" * 64
_TEMPLATE_SCOPE = ArtifactScope(None, None, None, None, ())
_TEMPLATE_BINDING = ArtifactConfigurationBinding("template", _ZERO_SHA256)


def _template_value(field: str) -> object:
    if field.endswith("_sha256"):
        return _ZERO_SHA256
    if field == "analysis_domain_revision":
        return 1
    if field == "native_level_read_attestation":
        return False
    return "template"


def _identity_template(fields: tuple[str, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in fields:
        cursor = result
        parts = field.split(".")
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            assert isinstance(child, dict)
            cursor = child
        cursor[parts[-1]] = _template_value(field)
    return result


def owner_identity_contract(artifact_kind: str) -> ArtifactIdentityContract:
    if artifact_kind == "processed_image":
        return preprocessing_identity_contract(artifact_kind)
    if artifact_kind == "analysis_frame":
        return analysis_preflight_identity_contract()
    if artifact_kind == "project_catalogue_entry":
        return project_catalogue_entry_identity_contract()
    if artifact_kind == "tissue_setup_result":
        return tissue_setup_result_identity_contract()
    if artifact_kind == "tissue_resolution_result":
        return tissue_resolution_result_identity_contract()
    if artifact_kind == "tissue_channel_result":
        return tissue_channel_result_identity_contract()
    if artifact_kind == "tissue_candidate_result":
        return tissue_candidate_result_identity_contract()
    if artifact_kind == "tissue_acceptance_result":
        return tissue_acceptance_result_identity_contract()
    if artifact_kind == "registration_result":
        return batch_registration_result_identity_contract()
    if artifact_kind == "accepted_analysis_domain":
        return accepted_analysis_domain_identity_contract()
    if artifact_kind == "primary_inference_result":
        return primary_classifier_inference_result_identity_contract()
    if artifact_kind == "secondary_inference_result":
        return secondary_classifier_inference_result_identity_contract()
    if artifact_kind == "segmentation_result":
        return segmentation_result_identity_contract()
    if artifact_kind == "segmentation_correction_result":
        return correction_result_identity_contract()
    if artifact_kind == "measurements_result":
        return measurements_result_identity_contract()
    if artifact_kind == "classifier_record_set":
        return classifier_record_set_identity_contract()
    if artifact_kind == "training_plan_set":
        return training_plan_set_identity_contract()
    if OWNER_CONTRACT_STATUS_BY_ARTIFACT[artifact_kind] != "existing_public_owner":
        raise ImageStageAdapterRegistryError(
            f"Task-12 owner contract prerequisite for {artifact_kind}: "
            "the owning subsystem has not exposed a public identity contract"
        )
    schema_name, schema_version, _status = OWNER_SCHEMA_BY_ARTIFACT[artifact_kind]
    fields = OWNER_IDENTITY_FIELDS_BY_ARTIFACT[artifact_kind]
    return ArtifactIdentityContract(
        contract_id=f"mif-batch-{artifact_kind.replace('_', '-')}-identity/v1",
        artifact_kind=artifact_kind,
        schema_name=schema_name,
        schema_version=schema_version,
        required_fields=fields,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _identity_contract(artifact_kind: str) -> ArtifactIdentityContract:
    return owner_identity_contract(artifact_kind)


def _requirement(
    artifact_kind: str, multiplicity: ArtifactMultiplicity
) -> StageArtifactRequirement:
    contract = _identity_contract(artifact_kind)
    return StageArtifactRequirement(
        artifact_kind=artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=_TEMPLATE_SCOPE,
        owning_identity=_identity_template(contract.required_fields),
        identity_contract=contract,
        compatibility_signature_sha256=_ZERO_SHA256,
        producer_version="mif-pipeline-task12-owner/v1",
        runtime_versions={
            name: "0.1.0" if name == "mif_pipeline" else "template"
            for name in contract.required_runtime_names
        },
        configuration_bindings=(_TEMPLATE_BINDING,),
        multiplicity=multiplicity,
    )


def _pending_schema_artifacts() -> tuple[str, ...]:
    used = {
        *OUTPUT_KIND_BY_NODE.values(),
        *(kind for values in INPUT_KINDS_BY_NODE.values() for kind in values),
        *(kind for values in OPTIONAL_INPUT_KINDS_BY_NODE.values() for kind in values),
    }
    return tuple(
        sorted(
            artifact_kind
            for artifact_kind in used
            if OWNER_SCHEMA_BY_ARTIFACT[artifact_kind][2] != "existing_owner"
        )
    )


def _pending_owner_contract_artifacts() -> tuple[str, ...]:
    used = {
        *OUTPUT_KIND_BY_NODE.values(),
        *(kind for values in INPUT_KINDS_BY_NODE.values() for kind in values),
        *(kind for values in OPTIONAL_INPUT_KINDS_BY_NODE.values() for kind in values),
    }
    return tuple(
        sorted(
            artifact_kind
            for artifact_kind in used
            if OWNER_CONTRACT_STATUS_BY_ARTIFACT[artifact_kind] != "existing_public_owner"
        )
    )


def _pending_resource_nodes() -> tuple[str, ...]:
    return tuple(
        node_kind
        for node_kind in IMAGE_STAGE_NODE_KINDS
        if RESOURCE_CONTRACT_STATUS_BY_NODE[node_kind] != "existing_owner"
    )


def _pending_nested_owner_codecs() -> tuple[str, ...]:
    return tuple(
        sorted(
            artifact_kind
            for artifact_kind, status in NESTED_OWNER_CODEC_STATUS_BY_ARTIFACT.items()
            if status != "existing_owner"
        )
    )


def _pending_section_fan_in() -> tuple[str, ...]:
    return () if SEGMENTATION_FAN_IN_STATUS == "existing_owner" else ("segmentation",)


def build_registration_stage_spec() -> StageAdapterSpec:
    """Construct the independently runnable direct registration stage spec."""

    output_contract = batch_registration_result_identity_contract()
    return StageAdapterSpec(
        node_kind="registration",
        node_version="1",
        required_inputs=tuple(
            _requirement(kind, ArtifactMultiplicity.SINGLE)
            for kind in INPUT_KINDS_BY_NODE["registration"]
        ),
        optional_inputs=(_requirement("project_catalogue_entry", ArtifactMultiplicity.SINGLE),),
        declared_outputs=(output_contract.artifact_kind,),
        output_identity_contracts=(output_contract,),
        compatibility_signature_id="mif-batch-registration-compatibility/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id="mif-batch-registration-equivalence/v1",
            byte_deterministic=False,
            canonical_identity_paths=(
                ("registration_result", "sample_id"),
                ("registration_result", "slide_id"),
                ("registration_result", "validation_domain_id"),
                ("registration_result", "primary_project_entry_id"),
                ("registration_result", "primary_image_sha256"),
                ("registration_result", "project_identity_sha256"),
            ),
            stable_inventory_paths=(
                (
                    "registration_result",
                    "primary_project_entry_projection_sha256",
                ),
                (
                    "registration_result",
                    "primary_saved_state_attestation_sha256",
                ),
                ("registration_result", "tissue_generation_id"),
                (
                    "registration_result",
                    "tissue_generation_content_sha256",
                ),
                ("registration_result", "tissue_section_inventory_sha256"),
                ("registration_result", "tissue_section_ids_sha256"),
                ("registration_result", "tissue_roi_ids_sha256"),
                ("registration_result", "tissue_path_object_ids_sha256"),
            ),
            scientific_value_paths=(
                ("registration_result", "resolved_config_sha256"),
                ("registration_result", "registration_decision_sha256"),
            ),
            qc_evidence_paths=(("registration_result", "registration_decision_sha256"),),
        ),
        resource_kind=ResourceKind.QUPATH_IMAGE_WRITE.value,
        dispatch_mode=DispatchMode.SINGLE,
    )


def build_image_stage_specs() -> tuple[StageAdapterSpec, ...]:
    """Construct the closed, plan-independent image-stage specs."""

    pending_schemas = _pending_schema_artifacts()
    pending_owner_contracts = _pending_owner_contract_artifacts()
    pending_resources = _pending_resource_nodes()
    pending_nested_codecs = _pending_nested_owner_codecs()
    pending_section_fan_in = _pending_section_fan_in()
    if (
        pending_schemas
        or pending_owner_contracts
        or pending_resources
        or pending_nested_codecs
        or pending_section_fan_in
    ):
        raise ImageStageAdapterRegistryError(
            "Task-12 owner schema decisions, owner contract prerequisites, resource "
            "decisions, section fan-in, and nested owner codec decisions are required "
            "before StageAdapterSpec construction: "
            f"schemas={', '.join(pending_schemas)}; "
            f"owner_contracts={', '.join(pending_owner_contracts)}; "
            f"resources={', '.join(pending_resources)}; "
            f"nested_codecs={', '.join(pending_nested_codecs)}; "
            f"section_fan_in={', '.join(pending_section_fan_in)}"
        )
    specs: list[StageAdapterSpec] = []
    for node_kind in IMAGE_STAGE_NODE_KINDS:
        output_kind = OUTPUT_KIND_BY_NODE[node_kind]
        output_contract = _identity_contract(output_kind)
        paths = tuple((output_kind, field) for field in output_contract.required_fields[:4])
        if node_kind in {
            "primary_classifier_inference",
            "secondary_classifier_inference",
        }:
            canonical_identity_paths = tuple(
                (output_kind, field)
                for field in (
                    "analysis_domain_binding_set_sha256",
                    "application_request_sha256",
                    "classifier_record_set_ref_sha256",
                    "selected_output_id",
                )
            )
            stable_inventory_paths = ((output_kind, "roi_inventory.sha256"),)
            scientific_value_paths = tuple(
                (output_kind, field)
                for field in (
                    (
                        "primary_count_decision.decision_sha256",
                        "domain_status",
                    )
                    if node_kind == "primary_classifier_inference"
                    else (
                        "eligible_parent_ids_sha256",
                        "predecessor_result_ref_sha256",
                        "primary_domain_status",
                    )
                )
            )
            qc_evidence_paths = tuple(
                (output_kind, field)
                for field in (
                    "generation_manifest_payload_sha256",
                    "validated_exchange_sha256",
                )
            )
        else:
            canonical_identity_paths = (paths[0],)
            stable_inventory_paths = (paths[1],)
            scientific_value_paths = (paths[2],)
            qc_evidence_paths = (paths[3],)
        specs.append(
            StageAdapterSpec(
                node_kind=node_kind,
                node_version="1",
                required_inputs=tuple(
                    _requirement(
                        kind,
                        ArtifactMultiplicity(INPUT_MULTIPLICITY_BY_NODE[node_kind][kind]),
                    )
                    for kind in INPUT_KINDS_BY_NODE[node_kind]
                ),
                optional_inputs=tuple(
                    _requirement(
                        kind,
                        ArtifactMultiplicity(OPTIONAL_INPUT_MULTIPLICITY_BY_NODE[node_kind][kind]),
                    )
                    for kind in OPTIONAL_INPUT_KINDS_BY_NODE[node_kind]
                ),
                declared_outputs=(output_kind,),
                output_identity_contracts=(output_contract,),
                compatibility_signature_id=(
                    f"mif-batch-{node_kind.replace('_', '-')}-compatibility/v1"
                ),
                equivalence_contract=ArtifactEquivalenceContract(
                    contract_id=f"mif-batch-{node_kind.replace('_', '-')}-equivalence/v1",
                    byte_deterministic=False,
                    canonical_identity_paths=canonical_identity_paths,
                    stable_inventory_paths=stable_inventory_paths,
                    scientific_value_paths=scientific_value_paths,
                    qc_evidence_paths=qc_evidence_paths,
                ),
                resource_kind=RESOURCE_BY_NODE[node_kind],  # type: ignore[arg-type]
                dispatch_mode=DISPATCH_BY_NODE[node_kind],
            )
        )
    from .batch_saved_result_retirement import build_saved_result_retirement_spec
    from .batch_cell_corrections import build_cell_correction_spec
    return (*specs, build_saved_result_retirement_spec(), build_cell_correction_spec())


@dataclass(frozen=True, slots=True)
class ImageCodecDependencies:
    state_store: BatchStateStore
    project_provider: ProjectMutationLeaseProvider
    classifier_record_set_store: ClassifierRecordSetStore
    classifier_application_authority: NativeClassifierApplicationAuthority
    project_operations: QuPathRunnerProjectOperations
    training_plan_set_store: TrainingPlanSetStore | None = None
    prerequisite_inputs: (
        Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs] | None
    ) = None

    def __post_init__(self) -> None:
        if (
            type(self.state_store) is not BatchStateStore
            or type(self.project_provider) is not ProjectMutationLeaseProvider
            or type(self.classifier_record_set_store) is not ClassifierRecordSetStore
            or type(self.classifier_application_authority)
            is not NativeClassifierApplicationAuthority
            or type(self.project_operations) is not QuPathRunnerProjectOperations
            or (
                self.training_plan_set_store is not None
                and type(self.training_plan_set_store) is not TrainingPlanSetStore
            )
            or (self.prerequisite_inputs is not None and not callable(self.prerequisite_inputs))
            or self.classifier_application_authority.state_store is not self.state_store
            or self.classifier_application_authority.project_provider is not self.project_provider
        ):
            raise ImageStageAdapterRegistryError(
                "image codec composition requires its exact runtime owners"
            )


@dataclass(frozen=True, slots=True)
class ImageRuntimeDependencies:
    state_store: BatchStateStore
    project_stage: ProjectStageRuntime
    project_provider: ProjectMutationLeaseProvider
    runner: QuPathRunner
    project_operations: QuPathRunnerProjectOperations
    registration_executor: ProductionRegistrationExecutor
    classifier_record_set_store: ClassifierRecordSetStore
    classifier_application_authority: NativeClassifierApplicationAuthority

    def __post_init__(self) -> None:
        """Validate the ImageRuntimeDependencies fields when the record is constructed.

        The checks cover ``state_store``, ``project_provider``, ``project_stage``,
        ``runner``, ``project_operations``, ``registration_executor``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if (
            type(self.state_store) is not BatchStateStore
            or type(self.project_stage) is not ProjectStageRuntime
            or type(self.project_provider) is not ProjectMutationLeaseProvider
            or type(self.runner) is not QuPathRunner
            or type(self.project_operations) is not QuPathRunnerProjectOperations
            or type(self.registration_executor) is not ProductionRegistrationExecutor
            or type(self.classifier_record_set_store) is not ClassifierRecordSetStore
            or type(self.classifier_application_authority)
            is not NativeClassifierApplicationAuthority
            or self.classifier_application_authority.state_store is not self.state_store
            or self.classifier_application_authority.project_provider is not self.project_provider
        ):
            raise ImageStageAdapterRegistryError(
                "image runtime composition requires its exact runtime owners"
            )
        expected_project_specs = tuple(
            spec
            for spec in build_image_stage_specs()
            if spec.dispatch_mode is DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        )
        try:
            self.project_stage.require_composition_coherence(
                state_store=self.state_store,
                project_provider=self.project_provider,
                runner=self.runner,
                project_operations=self.project_operations,
                expected_adapter_specs=expected_project_specs,
            )
        except ProjectStageLifecycleError as exc:
            raise ImageStageAdapterRegistryError(
                "image project-stage runtime owners differ"
            ) from exc


def build_image_project_stage_codecs(
    *, dependencies: ImageCodecDependencies
) -> tuple[ProjectStageCodec, ...]:
    """Compose the current-image scientific stage owners and detector setup."""

    if type(dependencies) is not ImageCodecDependencies:
        raise ImageStageAdapterRegistryError("image codec composition requires typed dependencies")
    by_kind = {spec.node_kind: spec for spec in build_image_stage_specs()}
    state_store = dependencies.state_store
    record_store = dependencies.classifier_record_set_store
    application_authority = dependencies.classifier_application_authority
    from .batch_saved_result_retirement import build_saved_result_retirement_codec
    from .batch_cell_corrections import build_cell_correction_codec
    return (
        build_saved_result_retirement_codec(by_kind["saved_result_retirement"], state_store=state_store),
        build_cell_correction_codec(by_kind["segmentation_correction"], state_store=state_store),
        build_tissue_setup_stage_codec(
            by_kind["tissue_setup"],
            state_store=state_store,
            project_operations=dependencies.project_operations,
        ),
        build_tissue_candidate_stage_codec(by_kind["tissue_candidate"], state_store=state_store),
        build_analysis_domain_selection_stage_codec(
            by_kind["analysis_domain_selection"], state_store=state_store
        ),
        build_primary_classifier_inference_stage_codec(
            by_kind["primary_classifier_inference"],
            state_store=state_store,
            classifier_record_set_store=record_store,
            classifier_application_authority=application_authority,
            training_plan_set_store=dependencies.training_plan_set_store,
        ),
        build_secondary_classifier_inference_stage_codec(
            by_kind["secondary_classifier_inference"],
            state_store=state_store,
            classifier_record_set_store=record_store,
            classifier_application_authority=application_authority,
        ),
        _build_segmentation_stage_codec(
            by_kind["segmentation"],
            state_store=state_store,
            prerequisite_inputs=dependencies.prerequisite_inputs,
        ),
        build_measurements_stage_codec(by_kind["measurements"], state_store=state_store),
    )


def _resolved_sample_for_context(
    context: BatchNodeExecutionContext,
    sample_by_id: Mapping[str, ResolvedBatchSample],
) -> ResolvedBatchSample:
    participants = context.authorization.dispatch_binding.participant_ids
    if len(participants) != 1 or participants[0] not in sample_by_id:
        raise BatchArtifactError("image stage requires one exact resolved manifest participant")
    return sample_by_id[participants[0]]


@dataclass(frozen=True, slots=True)
class _RawImageRuntimeAdapter:
    spec: StageAdapterSpec
    execute_owner: Callable[
        [BatchNodeExecutionContext, BatchWorkspaceLease], BatchRawNodeOutcome
    ] = field(repr=False, compare=False)

    def execute_raw(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
    ) -> BatchRawNodeOutcome:
        if (
            type(context) is not BatchNodeExecutionContext
            or context.adapter_spec is not self.spec
            or type(workspace_lease) is not BatchWorkspaceLease
        ):
            raise BatchArtifactError("raw image runtime received foreign execution authority")
        outcome = self.execute_owner(context, workspace_lease)
        if type(outcome) is not BatchRawNodeOutcome or outcome.context is not context:
            raise BatchArtifactError("raw image owner returned a foreign outcome")
        return outcome


@dataclass(frozen=True, slots=True)
class _ProjectUpsertRuntimeAdapter:
    spec: StageAdapterSpec
    dependencies: ImageRuntimeDependencies = field(repr=False, compare=False)

    def execute_committed(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> ScheduledNodeResult:
        if context.adapter_spec is not self.spec:
            raise BatchArtifactError("project-upsert runtime received a foreign specification")
        request = build_current_project_upsert_request(
            context,
            state_store=self.dependencies.state_store,
            project_provider=self.dependencies.project_provider,
            workspace_lease=workspace_lease,
        )
        result = execute_managed_project_upsert(
            context,
            request,
            runner=self.dependencies.runner,
            project_operations=self.dependencies.project_operations,
            state_store=self.dependencies.state_store,
            project_provider=self.dependencies.project_provider,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )
        if type(result) is not ScheduledNodeResult:
            raise BatchArtifactError("managed project-upsert owner returned a foreign result")
        return result


@dataclass(frozen=True, slots=True)
class _RegistrationRuntimeAdapter:
    spec: StageAdapterSpec
    plan: BatchPlan
    dependencies: ImageRuntimeDependencies = field(repr=False, compare=False)

    def execute_committed(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> ScheduledNodeResult:
        if context.adapter_spec is not self.spec:
            raise BatchArtifactError("registration runtime received a foreign specification")
        return execute_registration_committed(
            context,
            dependencies=self.dependencies,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )


@dataclass(frozen=True, slots=True)
class _ProjectCurrentImageRuntimeAdapter:
    spec: StageAdapterSpec
    plan: BatchPlan
    resolved_samples: tuple[ResolvedBatchSample, ...]
    dependencies: ImageRuntimeDependencies = field(repr=False, compare=False)

    def _recover_completed_region_result(
        self, contexts, *, workspace_lease, terminal_committer,
    ) -> tuple[ScheduledNodeResult, ...] | None:
        """Restore unchanged saved regions through their fresh Retry attempt.

        The native snapshot and failed terminal retain their original identities.
        We revalidate that evidence and the live image, then restore a candidate
        review or publish regions already explicitly accepted and locked. Only
        the accepted artifact's producer changes to the current authorization;
        its scientific identity and original native exchange stay unchanged.
        """
        from .batch_failed_retry import _check_live_target

        state = self.dependencies.state_store
        with state.current_project_control(operation="retry", lease=workspace_lease) as control:
            control.project_processes.assert_all_drained(
                batch_id=control.head.batch_id,
                project_identity_sha256=control.head.project_identity_sha256,
            )
            reopener = self.dependencies.project_stage.committed_exchange_reopener(
                workspace_lease=workspace_lease)
            retained = tuple(reopener.reopen_failed_target_for_owner(
                node_id=context.node_id,
                terminal_record_sha256=context.authorization.failed_retry.terminal_record_sha256,
                expected_binding=(self.spec.spec_sha256,
                    PROJECT_CURRENT_IMAGE_ASSET_BY_NODE[self.spec.node_kind],
                    self.spec.node_kind, self.spec.node_kind, "1"),
            ) for context in contexts)
            if any(item.completed_evidence is None for item in retained):
                return None
            snapshot = self.dependencies.project_provider.inspect_live_project(control.project_lease)
            outcomes = []
            for context, previous in zip(contexts, retained, strict=True):
                _check_live_target(state_store=state, head=control.head,
                    reopened=previous, snapshot=snapshot, workspace_lease=workspace_lease)
                current = state.authenticate_current_execution_context(context)
                if (
                    context.authorization.failed_retry.terminal_record_sha256 != previous.terminal_record_sha256
                    or previous.state_head_sha256 != current.state_head_sha256
                    or context.node_id != previous.context.node_id
                    or context.inputs != previous.context.inputs
                    or context.authorization.retry_ordinal != previous.context.authorization.retry_ordinal + 1
                ):
                    raise BatchArtifactError("Region recovery does not continue its failed native snapshot")
                evidence = previous.completed_evidence
                result = build_analysis_domain_execution_result(evidence, state_store=state)
                if type(result) is ProjectStageCodecValidationResult:
                    if (result.execution_result.outputs
                            or result.terminal_stage_result.status is not StageStatus.NEEDS_INTERACTION
                            or result.terminal_stage_result.interaction_reason
                            is not InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED):
                        raise BatchArtifactError("Region recovery requires its saved candidate review")
                    stage_result = result.terminal_stage_result
                    execution_result = BatchNodeExecutionResult(
                        context.authorization.authorization_sha256, ())
                elif type(result) is BatchNodeExecutionResult:
                    if (result.authorization_sha256 != previous.context.authorization.authorization_sha256
                            or len(result.outputs) != 1
                            or result.outputs[0].artifact_kind != "accepted_analysis_domain"
                            or result.outputs[0].owning_identity["selection_mode"] != SELECTED_MODE):
                        raise BatchArtifactError("Region recovery requires its explicitly accepted selection")
                    execution_result = BatchNodeExecutionResult(
                        context.authorization.authorization_sha256,
                        tuple(replace(output,
                            producer_attempt_id=context.authorization.attempt_id,
                            producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
                        ) for output in result.outputs))
                    stage_result = StageResult.completed(stage=self.spec.node_kind,
                        slide_id=previous.authorized.slide_id,
                        generation_id=evidence.wire_stage_result.generation_id)
                else:
                    raise BatchArtifactError("Region recovery returned an unsupported saved result")
                outcomes.append(BatchRawNodeOutcome(
                    context=context,
                    stage_result=stage_result,
                    execution_result=execution_result,
                    execution_exchange_sha256=evidence.execution_exchange_sha256,
                ))
            return terminal_committer.commit_raw(tuple(outcomes))

    def execute_project_current_image(
        self,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> tuple[ScheduledNodeResult, ...]:
        values = tuple(contexts)
        if not values or any(
            type(item) is not BatchNodeExecutionContext or item.adapter_spec is not self.spec
            for item in values
        ):
            raise BatchArtifactError("current-image runtime requires one exact stage context group")
        if self.spec.node_kind == "analysis_domain_selection" and all(
            context.authorization.failed_retry is not None for context in values
        ):
            recovered = self._recover_completed_region_result(
                values, workspace_lease=workspace_lease, terminal_committer=terminal_committer)
            if recovered is not None:
                return recovered
        state_store = self.dependencies.state_store
        pending_results: tuple[ScheduledNodeResult, ...] = ()
        deferred_edit_recovery = False
        if self.spec.node_kind in {"saved_result_retirement", "segmentation_correction"} and any(
            context.authorization.failed_retry is not None for context in values
        ):
            from .batch_saved_result_retirement import accept_completed_edit_retries

            head = state_store.load_head()
            deferred_edit_recovery = bool(
                self.dependencies.project_provider._unresolved_active_records_for_project(
                    project_uri=Path(head.project_path).resolve().as_uri(),
                    project_identity_sha256=head.project_identity_sha256))
            if not deferred_edit_recovery:
                pending_results, values = accept_completed_edit_retries(
                    values, spec=self.spec, dependencies=self.dependencies,
                    workspace_lease=workspace_lease, terminal_committer=terminal_committer)
                if not values:
                    return pending_results
        cellpose_worker = False
        if self.spec.node_kind in {"segmentation", "measurements"}:
            resolve_setup = (
                resolve_current_segmentation_setup
                if self.spec.node_kind == "segmentation"
                else resolve_current_measurement_setup
            )
            pending = []
            for context in values:
                selection = resolve_setup(context, state_store=state_store)
                if self.spec.node_kind == "segmentation" and getattr(selection, "method", None) == "cellpose":
                    cellpose_worker = True
                if isinstance(selection, StageResult):
                    pending.append(
                        BatchRawNodeOutcome(
                            context=context,
                            stage_result=selection,
                            execution_result=BatchNodeExecutionResult(
                                authorization_sha256=context.authorization.authorization_sha256,
                                outputs=(),
                            ),
                            execution_exchange_sha256=None,
                        )
                    )
            if pending:
                pending_results = terminal_committer.commit_raw(tuple(pending))
                pending_ids = {item.context.node_id for item in pending}
                values = tuple(item for item in values if item.node_id not in pending_ids)
                if not values:
                    return pending_results
        head = state_store.load_head()
        snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
        catalogue = state_store.project_catalogue_store(workspace_lease).load_current()
        if catalogue is None:
            raise BatchArtifactError("current-image runtime lacks a project catalogue")
        current_saved_state_by_entry_id = (
            state_store.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=head.sha256,
                lease=workspace_lease,
            )
        )
        sample_by_id = {item.sample_id: item for item in self.resolved_samples}
        if tuple(sorted(sample_by_id.values(), key=lambda item: item.sample_id)) != tuple(
            sorted(snapshot.manifest.enabled_samples, key=lambda item: item.sample_id)
        ):
            raise BatchArtifactError(
                "current-image runtime samples differ from the current manifest"
            )
        node_by_id = {item.node_id: item for item in snapshot.plan.nodes}
        entry_by_id = {item.project_entry_id: item for item in catalogue.entries}
        targets: list[ProjectStageTargetIntent] = []
        for context in values:
            node = node_by_id.get(context.node_id)
            sample = _resolved_sample_for_context(context, sample_by_id)
            entries = context.authorization.dispatch_binding.project_entry_ids
            if (
                node is None
                or node.kind != self.spec.node_kind
                or node.adapter_spec_sha256 != self.spec.spec_sha256
                or node.participant_ids != (sample.sample_id,)
                or len(entries) != 1
                or node.project_entry_ids != entries
                or entries[0] not in entry_by_id
            ):
                raise BatchArtifactError("current-image context differs from its exact plan node")
            entry = entry_by_id[entries[0]]
            if (
                entry.sample_id != sample.sample_id
                or entry.slide_id != sample.slide_id
                or entry.image_role != "primary_mif"
            ):
                raise BatchArtifactError(
                    "current-image project entry differs from its resolved sample"
                )
            targets.append(
                ProjectStageTargetIntent(
                    project_entry_id=entry.project_entry_id,
                    sample_id=sample.sample_id,
                    slide_id=sample.slide_id,
                    # Scientific run identity survives scheduler attempts and
                    # successive image stages within this batch.
                    pipeline_run_id=head.batch_id,
                    image_role="primary_mif",
                    analysis_domain_bindings=tuple(node.analysis_domain_bindings),
                    prerequisite_artifact_ref_sha256s=tuple(
                        sorted(item.artifact_ref_sha256 for item in context.inputs)
                    ),
                    saved_state_before_sha256=(
                        current_saved_state_by_entry_id[entry.project_entry_id]
                    ),
                    resolved_sample=sample,
                )
            )
        if self.spec.node_kind == "analysis_domain_selection":
            from .batch_analysis_domain_artifacts import build_direct_whole_tissue_analysis_domain_execution_result
            resolved = []
            for context, target in zip(values, targets, strict=True):
                invocation = current_analysis_domain_invocation(context, target, state_store=state_store,
                    committed_exchange_reopener=self.dependencies.project_stage.committed_exchange_reopener(workspace_lease=workspace_lease))
                review = analysis_domain_review(invocation, project_path=head.project_path)
                if review is not None:
                    resolved.append(compose_single_output_direct_stage_outcome(context, review, None))
                elif invocation.action == "use_whole_tissue" or (
                    invocation.action is None
                    and invocation.config.analysis_domain.selection_policy == "whole_tissue"
                    and invocation.scientific_state.get("analysis_domain_review_required") is not True
                ):
                    service = invocation.action_context.get("service_context", {})
                    if service.get("accepted_retirement_predecessor") is not None:
                        continue
                    output = build_direct_whole_tissue_analysis_domain_execution_result(
                        context,
                        current_execution_authority=state_store.authenticate_current_execution_context(context),
                        state_store=state_store, workspace_lease=workspace_lease,
                    ).outputs[0]
                    resolved.append(compose_single_output_direct_stage_outcome(
                        context,
                        StageResult.completed(stage="analysis_domain_selection", slide_id=target.slide_id,
                            generation_id=output.owning_identity["tissue_generation_id"]),
                        output,
                    ))
            if resolved:
                pending_results += terminal_committer.commit_raw(tuple(resolved))
                resolved_ids = {item.context.node_id for item in resolved}
                remaining = [(context, target) for context, target in zip(values, targets, strict=True)
                             if context.node_id not in resolved_ids]
                if not remaining:
                    return pending_results
                values = tuple(context for context, _ in remaining)
                targets = [target for _, target in remaining]

        targets_tuple = tuple(sorted(targets, key=lambda item: item.project_entry_id))
        target_entry_ids = {item.project_entry_id for item in targets_tuple}
        catalogue_entry_ids = {item.project_entry_id for item in catalogue.entries}
        input_sets = tuple(
            {item.artifact_ref_sha256 for item in context.inputs} for context in values
        )
        shared = tuple(sorted(set.intersection(*input_sets)))
        authority_layout = state_store.authority_layout_sha256
        if authority_layout is None:
            raise BatchArtifactError("current-image runtime lacks authority-layout identity")
        intent = ProjectStageRunIntent(
            authority_layout_sha256=authority_layout,
            batch_id=head.batch_id,
            run_plan_id=(
                "image-"
                + self.spec.node_kind.replace("_", "-")
                + "-"
                + hashlib.sha256(
                    canonical_json_bytes(
                        [item.authorization.authorization_sha256 for item in values]
                    )
                ).hexdigest()[:16]
            ),
            stage_id=self.spec.node_kind,
            stage_version=self.spec.node_version,
            project_uri=catalogue.project_uri,
            project_identity_sha256=head.project_identity_sha256,
            expected_project_authority_head_sha256=(head.project_authority_head_sha256),
            project_catalogue_sha256=catalogue.catalogue_sha256,
            launch_mode=ProjectStageLaunchMode.HEADLESS_PROJECT_SCRIPT,
            target_script_asset_id=PROJECT_CURRENT_IMAGE_ASSET_BY_NODE[self.spec.node_kind],
            guard_script_asset_id=QuPathScriptAsset.BATCH_STAGE_GUARD.value,
            targets=targets_tuple,
            excluded_project_entry_ids=tuple(sorted(catalogue_entry_ids - target_entry_ids)),
            shared_artifact_ref_sha256s=shared,
            launch_record_wait_millis=30_000,
        )
        prepared = prepare_project_stage_run_request(
            intent,
            values,
            self.dependencies.project_stage,
            workspace_lease=workspace_lease,
        )
        if deferred_edit_recovery:
            self.dependencies.project_stage.settle_unlaunched_preparation(
                prepared, workspace_lease, terminal_committer)
            recovered, remaining = accept_completed_edit_retries(
                values, spec=self.spec, dependencies=self.dependencies,
                workspace_lease=workspace_lease, terminal_committer=terminal_committer)
            if recovered:
                # Committing a recovered member advances the batch head. Build
                # only the remaining group's request against that new head.
                return pending_results + recovered + (
                    self.execute_project_current_image(remaining,
                        workspace_lease=workspace_lease, terminal_committer=terminal_committer)
                    if remaining else ())
        if cellpose_worker:
            from mif_pipeline.pipeline_core.execution_resources import current_operation_budget, operation_budget
            budget = current_operation_budget()
            if budget is None or budget.memory_bytes < 4 * 1024**3:
                raise BatchArtifactError("Cellpose requires a 4 GiB total operation allowance")
            # The one live Python model shares this reservation with QuPath.
            # Keep its model/arrays separate from Java's heap, direct memory and caches.
            with operation_budget(budget.memory_bytes, budget.cpu_threads,
                    native_memory_bytes=budget.memory_bytes - 3 * 1024**3):
                return pending_results + run_project_stage_headless(
                    prepared, self.dependencies.project_stage, workspace_lease, terminal_committer)
        return pending_results + run_project_stage_headless(
            prepared,
            self.dependencies.project_stage,
            workspace_lease,
            terminal_committer,
        )


def build_image_stage_adapters(
    *,
    plan: BatchPlan,
    resolved_samples: tuple[ResolvedBatchSample, ...],
    dependencies: ImageRuntimeDependencies,
) -> tuple[Task15RuntimeAdapterBinding, ...]:
    """Compose the accepted image-stage runtime bindings."""

    if type(plan) is not BatchPlan or type(dependencies) is not ImageRuntimeDependencies:
        raise ImageStageAdapterRegistryError(
            "image runtime composition requires a typed plan and dependencies"
        )
    samples = tuple(resolved_samples)
    if (
        type(resolved_samples) is not tuple
        or not samples
        or not all(type(item) is ResolvedBatchSample for item in samples)
        or len({item.sample_id for item in samples}) != len(samples)
    ):
        raise ImageStageAdapterRegistryError(
            "image runtime composition requires unique resolved samples"
        )
    for sample in samples:
        _require_supported_batch_image_modes(sample.config)
    sample_by_id = MappingProxyType({item.sample_id: item for item in samples})
    specs = build_image_stage_specs()
    bindings: list[Task15RuntimeAdapterBinding] = []
    for spec in specs:
        node_kind = spec.node_kind
        if node_kind == "analysis_preflight":
            owner = AnalysisPreflightStageAdapter(
                spec=spec,
                resolved_samples=samples,
                input_validator=validate_analysis_input,
                image_probe=dependencies.runner.preflight_image,
            )
            adapter = _RawImageRuntimeAdapter(
                spec,
                lambda context, _lease, owner=owner: owner.execute_raw(context),
            )
            route = "raw_direct"
        elif node_kind in {"project_upsert", "paired_image_preparation"}:
            adapter = _ProjectUpsertRuntimeAdapter(spec, dependencies)
            route = "owner_committed"
        elif node_kind == "tissue_resolution":
            adapter = _RawImageRuntimeAdapter(
                spec,
                lambda context, _lease: execute_tissue_resolution_stage(
                    context,
                    resolved_sample=_resolved_sample_for_context(context, sample_by_id),
                    state_store=dependencies.state_store,
                ),
            )
            route = "raw_direct"
        elif node_kind == "tissue_channel":
            adapter = _RawImageRuntimeAdapter(
                spec,
                lambda context, _lease: execute_tissue_channel_stage(
                    context,
                    resolved_sample=_resolved_sample_for_context(context, sample_by_id),
                    state_store=dependencies.state_store,
                ),
            )
            route = "raw_direct"
        elif node_kind == "tissue_acceptance":
            adapter = _RawImageRuntimeAdapter(
                spec,
                lambda context, _lease: execute_tissue_acceptance_stage(
                    context, state_store=dependencies.state_store
                ),
            )
            route = "raw_direct"
        elif node_kind == "registration":
            adapter = _RegistrationRuntimeAdapter(spec, plan, dependencies)
            route = "owner_committed"
        else:
            adapter = _ProjectCurrentImageRuntimeAdapter(spec, plan, samples, dependencies)
            route = "project_current_image"
        bindings.append(Task15RuntimeAdapterBinding(spec, route, adapter))  # type: ignore[arg-type]
    return tuple(bindings)


_IMAGE_INTERACTION_OWNER_ROWS = (
    ("tissue_setup", "tissue_detector_setup_required"),
    ("tissue_resolution", "tissue_native_level_required"),
    ("tissue_candidate", "tissue_section_review_required"),
    ("registration", "warpy_registration_required"),
    ("registration", "registration_review_required"),
    ("registration", "transfer_review_required"),
    ("analysis_domain_selection", "analysis_domain_choice_required"),
    ("analysis_domain_selection", "analysis_domain_rois_required"),
    ("analysis_domain_selection", "analysis_domain_review_required"),
    ("primary_classifier_inference", "primary_training_annotations_required"),
    ("primary_classifier_inference", "primary_classifier_review_required"),
    ("primary_classifier_inference", "primary_structure_count_review_required"),
    ("secondary_classifier_inference", "secondary_training_annotations_required"),
    ("segmentation", "segmentation_setup_required"),
    ("measurements", "measurement_setup_required"),
)


def _image_interaction_validator(
    *,
    state_store: BatchStateStore,
    stage: str,
    reason: str,
) -> Callable[
    [BatchInteractionTarget, str, Mapping[str, object]],
    BatchTargetResponseValidation,
]:
    def validate(
        target: BatchInteractionTarget,
        action: str,
        response: Mapping[str, object],
    ) -> BatchTargetResponseValidation:
        if not isinstance(target, BatchInteractionTarget):
            raise TypeError("image interaction validation requires a typed target")
        result = target.stage_result
        if (
            result.stage != stage
            or result.interaction_reason is None
            or result.interaction_reason.value != reason
        ):
            return BatchTargetResponseValidation(
                target.target_id,
                target.node_id,
                "rejected",
                None,
                None,
                None,
                None,
                "image_interaction_target_foreign",
            )
        try:
            head = state_store.load_head()
            authorization_request = state_store.reopen_interaction_base_authorization_request(
                target.interaction_request_id,
                expected_head_sha256=head.sha256,
            )
            validated = validate_interaction_response(result, action, response)
            if reason == "measurement_setup_required":
                validate_measurement_feature_requests(
                    response["feature_requests"],
                    channels=validate_measurement_setup_context(result),
                )
        except Exception:
            return BatchTargetResponseValidation(
                target.target_id,
                target.node_id,
                "rejected",
                None,
                None,
                None,
                None,
                "image_interaction_response_invalid",
            )
        return BatchTargetResponseValidation(
            target.target_id,
            target.node_id,
            "accepted",
            validated,
            authorization_request,
            "roi-interaction-validator",
            INTERACTION_ACTION_SCHEMA_VERSION,
            None,
        )

    return validate


def registered_image_interaction_owner_keys() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (stage, reason, INTERACTION_ACTION_SCHEMA_VERSION)
            for stage, reason in _IMAGE_INTERACTION_OWNER_ROWS
        )
    )


def build_image_interaction_owner_bindings(
    *, dependencies: ImageCodecDependencies
) -> tuple[BatchInteractionOwnerBinding, ...]:
    """Return only the closed ordinary interaction owner inventory."""

    if type(dependencies) is not ImageCodecDependencies:
        raise ImageStageAdapterRegistryError(
            "image interaction composition requires typed dependencies"
        )
    return tuple(
        BatchInteractionOwnerBinding(
            stage=stage,
            interaction_reason=reason,
            action_schema_version=INTERACTION_ACTION_SCHEMA_VERSION,
            validate_target=_image_interaction_validator(
                state_store=dependencies.state_store,
                stage=stage,
                reason=reason,
            ),
        )
        for stage, reason in _IMAGE_INTERACTION_OWNER_ROWS
    )


__all__ = [
    "registered_image_interaction_owner_keys",
    "DISPATCH_BY_NODE",
    "IMAGE_STAGE_NODE_KINDS",
    "IMAGE_STAGE_STAGE_ADAPTATION",
    "INPUT_KINDS_BY_NODE",
    "INPUT_MULTIPLICITY_BY_NODE",
    "ImageStageAdapterRegistryError",
    "ImageCodecDependencies",
    "ImageRuntimeDependencies",
    "OPTIONAL_INPUT_KINDS_BY_NODE",
    "OPTIONAL_INPUT_MULTIPLICITY_BY_NODE",
    "OUTPUT_KIND_BY_NODE",
    "OWNER_IDENTITY_FIELDS_BY_ARTIFACT",
    "OWNER_CONTRACT_STATUS_BY_ARTIFACT",
    "NESTED_OWNER_CODEC_STATUS_BY_ARTIFACT",
    "PROJECT_STAGE_GENERATION_MANIFEST_EVIDENCE_FIELDS",
    "OWNER_SCHEMA_BY_ARTIFACT",
    "PROJECT_CURRENT_IMAGE_ASSET_BY_NODE",
    "PROJECT_CURRENT_IMAGE_CODEC_VERSION_BY_NODE",
    "PROJECT_CURRENT_IMAGE_TERMINAL_STAGE_BY_NODE",
    "PROJECT_CURRENT_IMAGE_WIRE_STAGE_BY_NODE",
    "PROJECT_CURRENT_IMAGE_WORKFLOW_STAGE_BY_NODE",
    "RESOURCE_BY_NODE",
    "RESOURCE_CONTRACT_STATUS_BY_NODE",
    "RegistrationExecutionRoute",
    "SectionSegmentationFanIn",
    "SEGMENTATION_FAN_IN_STATUS",
    "build_image_stage_adapters",
    "build_image_interaction_owner_bindings",
    "build_image_project_stage_codecs",
    "build_image_stage_specs",
    "build_analysis_domain_selection_stage_codec",
    "build_registration_stage_spec",
    "build_measurements_stage_codec",
    "build_semantic_evidence_stage_codec",
    "build_semantic_finalization_stage_codec",
    "build_primary_classifier_inference_stage_codec",
    "build_secondary_classifier_inference_stage_codec",
    "build_tissue_candidate_stage_codec",
    "build_project_current_image_stage_codec",
    "execute_tissue_acceptance_transition",
    "execute_no_secondary_completion_stage",
    "execute_semantic_evaluation_stage",
    "execute_final_count_cap_stage",
    "execute_tissue_acceptance_stage",
    "execute_tissue_channel_stage",
    "execute_tissue_resolution_stage",
    "owner_identity_contract",
    "route_registration_execution",
]
