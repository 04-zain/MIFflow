"""composition over the accepted owner inventory.

The `PhenotypingRuntimeDependencies` record carries `state_store`, `project_stage`,
`project_provider`, `runner`, `project_operations` and its remaining declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import traceback
import json
import os
from pathlib import Path
import platform
from threading import Lock
from types import MappingProxyType

from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactOrigin,
    ArtifactConfigurationBinding,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
)
from mif_pipeline.orchestration.batch_service_registry import (
    Task15RuntimeAdapterBinding,
)
from mif_pipeline.orchestration.batch_state import (
    BatchRawNodeOutcome,
    BatchStateStore,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    ScheduledNodeResult,
)
from mif_pipeline.orchestration.batch_dag import BatchPlan
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample, operational_sample_output_root
from mif_pipeline.orchestration.batch_training import (
    ClassifierRecordSetStore,
    NativeClassifierApplicationAuthority,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
)
from mif_pipeline.orchestration.batch_classifier_inference_artifacts import (
    bind_committed_classifier_roi_live_authority,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    build_phenotyping_mode_execution_result,
    resolve_phenotyping_mode_selection,
)
from mif_pipeline.orchestration.batch_stage_adapters import (
    execute_no_secondary_completion_stage,
)
from mif_pipeline.orchestration.batch_tissue_artifacts import (
    reopen_committed_accepted_tissue_live_authority,
)
from mif_pipeline.orchestration.batch_interactions import (
    BatchInteractionOwnerBinding,
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from mif_pipeline.orchestration.phenotype_views import PhenotypeViewStore
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecError,
    ProjectStageRunIntent,
    ProjectStageTargetIntent,
    prepare_project_stage_run_request,
)
from mif_pipeline.orchestration.project_stage_owner_capabilities import (
    ProjectStageOwnerCapabilityError,
    ProjectStageOwnerCapabilityPublisher,
    ProjectStageOwnerCapabilityResolver,
)
from mif_pipeline.orchestration.project_stage_plan import (
    ProjectStageLaunchMode,
    ProjectStageRuntime,
    run_project_stage_headless,
)
from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseProvider
from mif_pipeline.qupath_runtime.catalogue_operations import (
    QuPathRunnerProjectOperations,
)
from mif_pipeline.qupath_runtime.runner import QuPathRunner
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
from mif_pipeline.pipeline_core import (
    ClusteringMode,
    INTERACTION_ACTION_SCHEMA_VERSION,
    InteractionAction,
    InteractionReason,
    StageResult,
    validate_interaction_response,
)

from ._common import BatchPhenotypingError, canonical_bytes
from .review_authority import (
    accept_cluster_label_review,
    accepted_cluster_label_review_identity_contract,
    accepted_cluster_label_review_owning_identity,
    cluster_review_evidence_identity_contract,
    cluster_review_evidence_owning_identity,
    reopen_cluster_review_evidence_authority,
)
from .review_records import BatchClusterReviewEvidence
from .adapters import (
    PHENOTYPING_STAGE_NODE_KINDS,
    PhenotypingStageOwnerBinding,
    build_phenotyping_stage_owner_bindings,
    phenotyping_response_validator,
)
from .application_adapters import (
    build_phenotype_hierarchy_attestation_execution_result,
    issue_authorized_cluster_label_application,
    issue_authorized_phenotype_hierarchy_attestation,
)
from .authority import (
    clustering_result_identity_contract,
    clustering_result_owning_identity,
    decode_committed_preparation,
)
from .codec import persist_batch_clustering_result
from .execution import execute_batch_clustering
from .export_execution import execute_final_non_spatial_export_stage
from .handoff_execution import publish_phenotype_handoff_stage_owner_capability
from .marker_execution import (
    build_marker_gate_evidence,
    build_marker_gate_execution_result,
    build_marker_software_setup_request,
    open_authenticated_marker_measurement_population,
    resolve_marker_gate_domain_authority,
)
from .preparation import build_unsupervised_preparation_execution_result
from .marker_application_adapters import (
    issue_authorized_marker_rule_section_application,
)
from .live_inputs import reopen_committed_phenotyping_live_authorities
from .records import BatchClusteringRequest, UnsupervisedPreparationInput
from .supervised_adapters import issue_authorized_supervised_section_deployment
from .supervised_records import SupervisedNativePredictionStore
from .review_codec import dumps_batch_cluster_review_evidence
from .review_execution import build_batch_cluster_review_evidence
from .semantic_adapters import (
    execute_final_count_cap_stage,
    execute_semantic_evaluation_stage,
)
from .semantic_applicable import (
    decode_committed_final_count_cap_result,
    decode_committed_semantic_evaluation_result,
    decode_committed_semantic_evidence_result,
    publish_semantic_evidence_stage_owner_capability,
    publish_semantic_finalization_stage_owner_capability,
)
from .semantic_branch import bind_committed_semantic_domain_branch
from .semantic_raw_producer import (
    publish_semantic_raw_snapshot_invocation_authority,
)
from .semantic_records import normalize_committed_phenotype_application

# ==============================================================================
#                                  Composition
# ==============================================================================


@dataclass(frozen=True, slots=True)
class PhenotypingCodecDependencies:
    state_store: BatchStateStore
    classifier_record_set_store: ClassifierRecordSetStore
    phenotype_view_store: PhenotypeViewStore
    supervised_prediction_store: SupervisedNativePredictionStore
    classifier_application_authority: NativeClassifierApplicationAuthority
    project_stage_owner_capability_resolver: ProjectStageOwnerCapabilityResolver

    def __post_init__(self) -> None:
        authority = self.classifier_application_authority
        if (
            type(self.state_store) is not BatchStateStore
            or type(self.classifier_record_set_store) is not ClassifierRecordSetStore
            or type(self.phenotype_view_store) is not PhenotypeViewStore
            or type(self.supervised_prediction_store) is not SupervisedNativePredictionStore
            or type(authority) is not NativeClassifierApplicationAuthority
            or authority.state_store is not self.state_store
            or type(self.project_stage_owner_capability_resolver)
            is not ProjectStageOwnerCapabilityResolver
        ):
            raise BatchPhenotypingError(
                "phenotyping codec composition requires exact owner dependencies"
            )


@dataclass(frozen=True, slots=True)
class PhenotypingRuntimeDependencies:
    state_store: BatchStateStore
    project_stage: ProjectStageRuntime
    project_provider: ProjectMutationLeaseProvider
    runner: QuPathRunner
    project_operations: QuPathRunnerProjectOperations
    classifier_record_set_store: ClassifierRecordSetStore
    phenotype_view_store: PhenotypeViewStore
    supervised_prediction_store: SupervisedNativePredictionStore
    classifier_application_authority: NativeClassifierApplicationAuthority
    project_stage_owner_capability_publisher: ProjectStageOwnerCapabilityPublisher
    software_preferences_path: Path | None = None

    def __post_init__(self) -> None:
        authority = self.classifier_application_authority
        if (
            type(self.state_store) is not BatchStateStore
            or type(self.project_stage) is not ProjectStageRuntime
            or type(self.project_provider) is not ProjectMutationLeaseProvider
            or type(self.runner) is not QuPathRunner
            or type(self.project_operations) is not QuPathRunnerProjectOperations
            or type(self.classifier_record_set_store) is not ClassifierRecordSetStore
            or type(self.phenotype_view_store) is not PhenotypeViewStore
            or type(self.supervised_prediction_store) is not SupervisedNativePredictionStore
            or type(authority) is not NativeClassifierApplicationAuthority
            or authority.state_store is not self.state_store
            or authority.project_provider is not self.project_provider
            or type(self.project_stage_owner_capability_publisher)
            is not ProjectStageOwnerCapabilityPublisher
        ):
            raise BatchPhenotypingError(
                "phenotyping runtime composition requires exact owner dependencies"
            )


def _current_owner_inputs(
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
) -> tuple[
    BatchCurrentExecutionAuthority,
    object,
    ResolvedBatchSample,
    tuple[BatchCommittedArtifactAuthority, ...],
]:
    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError("phenotyping owner requires exact execution context")
    current = state_store.authenticate_current_execution_context(context)
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    samples = tuple(
        item
        for item in snapshot.manifest.enabled_samples
        if item.sample_id in current.participant_ids
    )
    if len(current.participant_ids) != 1 or len(samples) != 1:
        raise BatchArtifactError("section phenotyping owner requires one current resolved sample")
    authorities = tuple(
        state_store.authenticate_available_artifact(ref)
        for ref in sorted(context.inputs, key=lambda item: item.artifact_ref_sha256)
    )
    if any(
        item.state_head_sha256 != current.state_head_sha256
        or item.active_plan_sha256 != current.active_plan_sha256
        for item in authorities
    ):
        raise BatchArtifactError("phenotyping inputs are not current")
    return current, snapshot, samples[0], authorities


def _authorities_by_kind(
    authorities: Sequence[BatchCommittedArtifactAuthority],
) -> dict[str, tuple[BatchCommittedArtifactAuthority, ...]]:
    grouped: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in authorities:
        grouped.setdefault(authority.artifact_ref.artifact_kind, []).append(authority)
    return {kind: tuple(values) for kind, values in grouped.items()}


@dataclass(slots=True)
class _AttemptAuthorityResolver:
    build: Callable[..., object]
    _values: dict[str, tuple[BatchNodeExecutionContext, str | None, object]] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __call__(self, context: BatchNodeExecutionContext, *,
                 saved_state_before_sha256: str | None = None) -> object:
        key = context.authorization.authorization_sha256
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                if cached[0] != context or cached[1] != saved_state_before_sha256:
                    raise BatchArtifactError("phenotyping attempt authority context was replaced")
                return cached[2]
            value = (self.build(context) if saved_state_before_sha256 is None else
                     self.build(context, saved_state_before_sha256=saved_state_before_sha256))
            self._values[key] = (context, saved_state_before_sha256, value)
            return value


def _measurement_output_root(authorities: tuple[BatchCommittedArtifactAuthority, ...]) -> Path:
    """Reopen the publication directory recorded by the accepted measurement.

    Imported inputs may remain in the original workspace while newer results
    belong to the selected destination. The committed descriptor selects that
    exact input location independently of the saved scientific configuration.
    """
    from ..batch_measurement_artifacts import decode_committed_measurements_result

    measurements = tuple(item for item in authorities
                         if item.artifact_ref.artifact_kind == "measurements_result")
    if len(measurements) != 1:
        raise BatchArtifactError("phenotyping requires one accepted measurement publication")
    return decode_committed_measurements_result(measurements[0]).measurement_rows.path.parent.parent.parent


def _marker_authority_resolver(
    dependencies: PhenotypingCodecDependencies,
) -> _AttemptAuthorityResolver:
    def build(context: BatchNodeExecutionContext, *, saved_state_before_sha256: str) -> object:
        current, snapshot, sample, authorities = _current_owner_inputs(
            context, dependencies.state_store
        )
        from ..batch_measurement_artifacts import decode_committed_measurements_result
        from ..phenotyping_population_sources import current_population_sources, population_source_references
        measurement = decode_committed_measurements_result(next(
            item for item in authorities if item.artifact_ref.artifact_kind == "measurements_result"
        ))
        source_references = population_source_references(current_population_sources(
            snapshot=snapshot, measurement=measurement, config=sample.config
        ))
        return issue_authorized_marker_rule_section_application(
            context=context,
            current_execution=current,
            source_authorities=authorities,
            resolved_sample=sample,
            authenticated_output_root=_measurement_output_root(authorities),
            phenotype_view_store=dependencies.phenotype_view_store,
            source_run_id=context.authorization.attempt_id,
            expected_saved_state_before_sha256=saved_state_before_sha256,
            expected_population_source_ref_sha256s=source_references,
        )

    return _AttemptAuthorityResolver(build)


def _supervised_authority_resolver(
    dependencies: PhenotypingCodecDependencies,
) -> _AttemptAuthorityResolver:
    def build(context: BatchNodeExecutionContext) -> object:
        current, snapshot, sample, authorities = _current_owner_inputs(
            context, dependencies.state_store
        )
        by_kind = _authorities_by_kind(authorities)
        frames = by_kind.get("analysis_frame", ())
        projects = by_kind.get("project_catalogue_entry", ())
        domains = by_kind.get("accepted_analysis_domain", ())
        measurements = by_kind.get("measurements_result", ())
        if len(frames) != 1 or len(projects) != 1 or not domains or len(measurements) != 1:
            raise BatchArtifactError("supervised deployment lacks exact current source owners")
        classifier_resolver = dependencies.classifier_application_authority.bind_supervised_target(
            context=context,
            resolved_sample=sample,
            analysis_frame=frames[0].artifact_ref,
            project_catalogue_entry=projects[0].artifact_ref,
            accepted_analysis_domain_authorities=domains,
            measurements_result_authority=measurements[0],
        )
        head = snapshot.head
        designation = dependencies.classifier_application_authority.project_provider.resolve_current_designation(
            Path(head.project_path),
            expected_project_identity_sha256=head.project_identity_sha256,
            expected_authority_head_sha256=(head.project_authority_head_sha256),
        )
        return issue_authorized_supervised_section_deployment(
            context=context,
            state_store=dependencies.state_store,
            classifier_record_set_store=dependencies.classifier_record_set_store,
            prediction_store=dependencies.supervised_prediction_store,
            phenotype_view_store=dependencies.phenotype_view_store,
            project_designation=designation,
            authority_resolver=classifier_resolver,
            authenticated_output_root=_measurement_output_root(authorities),
            resolved_config_sha256=sample.resolved_config_sha256,
            source_run_id=context.authorization.attempt_id,
        )

    return _AttemptAuthorityResolver(build)


def _cluster_application_authority_resolver(
    dependencies: PhenotypingCodecDependencies,
) -> _AttemptAuthorityResolver:
    def build(context: BatchNodeExecutionContext, *, saved_state_before_sha256: str) -> object:
        current, _snapshot, sample, authorities = _current_owner_inputs(
            context, dependencies.state_store
        )
        return issue_authorized_cluster_label_application(
            context=context,
            current_execution=current,
            source_authorities=authorities,
            authenticated_output_root=_measurement_output_root(authorities),
            phenotype_view_store=dependencies.phenotype_view_store,
            source_run_id=context.authorization.attempt_id,
            expected_saved_state_before_sha256=saved_state_before_sha256,
        )

    return _AttemptAuthorityResolver(build)


def build_phenotyping_project_stage_codecs(
    *,
    dependencies: PhenotypingCodecDependencies,
) -> tuple[ProjectStageCodec, ...]:
    """Build the codecs declared by the current phenotyping stage bindings."""

    if type(dependencies) is not PhenotypingCodecDependencies:
        raise BatchPhenotypingError("phenotyping codec factory requires typed dependencies")
    marker = _marker_authority_resolver(dependencies)
    supervised = _supervised_authority_resolver(dependencies)
    cluster = _cluster_application_authority_resolver(dependencies)
    codecs: list[ProjectStageCodec] = []
    for binding in build_phenotyping_stage_owner_bindings():
        factory = binding.project_codec_factory
        if factory is None:
            continue
        if binding.spec.node_kind == "marker_rule_application":
            codec = factory(binding.spec, authority_resolver=marker)
        elif binding.spec.node_kind == "supervised_phenotype_inference":
            codec = factory(binding.spec, authority_resolver=supervised)
        elif binding.spec.node_kind == "clustering_label_import":
            codec = factory(binding.spec, authority_resolver=cluster)
        else:
            codec = factory(
                binding.spec,
                owner_resolver=(dependencies.project_stage_owner_capability_resolver),
            )
        if type(codec) is not ProjectStageCodec:
            raise BatchArtifactError("phenotyping codec owner returned a foreign codec")
        codecs.append(codec)
    return tuple(codecs)


def _resolved_sample_for_context(
    context: BatchNodeExecutionContext,
    sample_by_id: Mapping[str, ResolvedBatchSample],
    state_store: BatchStateStore,
    *,
    current: BatchCurrentExecutionAuthority | None = None,
    snapshot: object | None = None,
) -> ResolvedBatchSample:
    participants = context.authorization.dispatch_binding.participant_ids
    if len(participants) != 1 or participants[0] not in sample_by_id:
        raise BatchArtifactError("phenotyping stage requires one exact resolved participant")
    if current is None:
        current = state_store.authenticate_current_execution_context(context)
    if snapshot is None:
        snapshot = state_store.reopen_execution_snapshot(
            expected_head_sha256=current.state_head_sha256
        )
    try:
        enabled_samples = tuple(snapshot.manifest.enabled_samples)
    except (AttributeError, TypeError) as exc:
        raise BatchArtifactError(
            "phenotyping current manifest sample inventory is invalid"
        ) from exc
    current_by_id = {item.sample_id: item for item in enabled_samples}
    if len(current_by_id) != len(enabled_samples):
        raise BatchArtifactError("phenotyping current manifest sample inventory is duplicated")
    if set(sample_by_id) != set(current_by_id) or any(
        retained is not current_by_id[sample_id] and retained != current_by_id[sample_id]
        for sample_id, retained in sample_by_id.items()
    ):
        raise BatchArtifactError(
            "phenotyping retained sample inventory differs from current manifest"
        )
    retained = sample_by_id[participants[0]]
    manifest_sample = current_by_id.get(participants[0])
    if manifest_sample is None:
        raise BatchArtifactError("phenotyping retained sample differs from current manifest")
    return manifest_sample


@dataclass(frozen=True, slots=True)
class _RawPhenotypingRuntimeAdapter:
    spec: StageAdapterSpec
    owner_binding: PhenotypingStageOwnerBinding = field(repr=False, compare=False)
    sample_by_id: Mapping[str, ResolvedBatchSample] = field(repr=False, compare=False)
    dependencies: PhenotypingRuntimeDependencies = field(repr=False, compare=False)

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
            raise BatchArtifactError("raw phenotyping runtime received foreign execution authority")
        outcome = _execute_direct_owner(
            context=context,
            owner_binding=self.owner_binding,
            sample_by_id=self.sample_by_id,
            dependencies=self.dependencies,
            workspace_lease=workspace_lease,
        )
        if type(outcome) is not BatchRawNodeOutcome or outcome.context is not context:
            raise BatchArtifactError("raw phenotyping owner returned a foreign outcome")
        return outcome


@dataclass(frozen=True, slots=True)
class _ProjectPhenotypingRuntimeAdapter:
    spec: StageAdapterSpec
    plan: BatchPlan = field(repr=False, compare=False)
    sample_by_id: Mapping[str, ResolvedBatchSample] = field(repr=False, compare=False)
    dependencies: PhenotypingRuntimeDependencies = field(repr=False, compare=False)

    def execute_project_current_image(
        self,
        contexts: tuple[BatchNodeExecutionContext, ...],
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> tuple[ScheduledNodeResult, ...]:
        return _execute_project_owner_group(
            spec=self.spec,
            contexts=contexts,
            sample_by_id=self.sample_by_id,
            dependencies=self.dependencies,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
        )


def build_phenotyping_runtime_adapters(
    *,
    plan: BatchPlan,
    resolved_samples: tuple[ResolvedBatchSample, ...],
    dependencies: PhenotypingRuntimeDependencies,
    codec_dependencies: PhenotypingCodecDependencies,
) -> tuple[Task15RuntimeAdapterBinding, ...]:
    """Compose exactly the twenty accepted runtime bindings."""

    if (
        type(plan) is not BatchPlan
        or type(dependencies) is not PhenotypingRuntimeDependencies
        or type(codec_dependencies) is not PhenotypingCodecDependencies
    ):
        raise BatchPhenotypingError(
            "phenotyping runtime factory requires typed plan and dependencies"
        )
    try:
        (
            codec_dependencies.project_stage_owner_capability_resolver.require_paired_publisher(
                dependencies.project_stage_owner_capability_publisher
            )
        )
    except ProjectStageOwnerCapabilityError as exc:
        raise BatchPhenotypingError(
            "phenotyping capability publisher/resolver pair differs"
        ) from exc
    samples = tuple(resolved_samples)
    if type(resolved_samples) is not tuple or not all(
        type(item) is ResolvedBatchSample for item in samples
    ):
        raise BatchPhenotypingError("phenotyping runtime factory requires resolved samples")
    if len({item.sample_id for item in samples}) != len(samples):
        raise BatchPhenotypingError("phenotyping runtime samples must be unique")
    sample_by_id = MappingProxyType({item.sample_id: item for item in samples})
    result: list[Task15RuntimeAdapterBinding] = []
    for owner in build_phenotyping_stage_owner_bindings():
        spec = owner.spec
        if spec.node_kind == "cluster_label_review":
            binding = build_cluster_label_review_runtime_binding(
                spec=spec,
                state_store=dependencies.state_store,
            )
        elif owner.project_codec_factory is not None:
            binding = Task15RuntimeAdapterBinding(
                spec=spec,
                route="project_current_image",
                adapter=_ProjectPhenotypingRuntimeAdapter(
                    spec=spec,
                    plan=plan,
                    sample_by_id=sample_by_id,
                    dependencies=dependencies,
                ),
            )
        else:
            binding = Task15RuntimeAdapterBinding(
                spec=spec,
                route="raw_direct",
                adapter=_RawPhenotypingRuntimeAdapter(
                    spec=spec,
                    owner_binding=owner,
                    sample_by_id=sample_by_id,
                    dependencies=dependencies,
                ),
            )
        result.append(binding)
    if tuple(item.spec.node_kind for item in result) != PHENOTYPING_STAGE_NODE_KINDS:
        raise BatchArtifactError("phenotyping runtime inventory is incomplete")
    return tuple(result)


_PHENOTYPING_INTERACTION_OWNER_ROWS = (
    ("phenotyping_mode_resolution", "phenotyping_mode_required"),
    ("marker_gate_review", "marker_threshold_review_required"),
    ("marker_gate_review", "marker_composite_rules_required"),
    ("marker_combination_selection", "marker_composite_rules_required"),
    ("unsupervised_preparation", "clustering_configuration_required"),
    (
        "supervised_phenotype_inference",
        "supervised_classifier_review_required",
    ),
    ("semantic_evaluation", "roi_review_required"),
    ("semantic_evaluation", "semantic_region_decision_required"),
    ("final_count_cap", "secondary_structure_selection_required"),
)


def _phenotyping_interaction_validator(
    *,
    state_store: BatchStateStore,
    stage: str,
    reason: str,
    owner_validator: Callable[..., object] | None,
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
            raise TypeError("phenotyping interaction validation requires a typed target")
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
                "phenotyping_interaction_target_foreign",
            )
        try:
            head = state_store.load_head()
            authorization_request = state_store.reopen_interaction_base_authorization_request(
                target.interaction_request_id,
                expected_head_sha256=head.sha256,
            )
            if owner_validator is None:
                validated = validate_interaction_response(result, action, response)
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
            owner_result = owner_validator(
                target,
                action,
                response,
                authorization_request=authorization_request,
            )
            if type(owner_result) is not BatchTargetResponseValidation:
                raise BatchArtifactError("phenotyping owner returned foreign response validation")
            return owner_result
        except Exception:
            return BatchTargetResponseValidation(
                target.target_id,
                target.node_id,
                "rejected",
                None,
                None,
                None,
                None,
                "phenotyping_interaction_response_invalid",
            )

    return validate


def registered_phenotyping_interaction_owner_keys() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (stage, reason, INTERACTION_ACTION_SCHEMA_VERSION)
            for stage, reason in _PHENOTYPING_INTERACTION_OWNER_ROWS
        )
    )


def build_phenotyping_interaction_owner_bindings(
    *,
    dependencies: PhenotypingCodecDependencies,
) -> tuple[BatchInteractionOwnerBinding, ...]:
    """Return the registered ordinary phenotyping interaction owners."""

    if type(dependencies) is not PhenotypingCodecDependencies:
        raise BatchPhenotypingError("phenotyping interaction factory requires typed dependencies")
    return tuple(
        BatchInteractionOwnerBinding(
            stage=stage,
            interaction_reason=reason,
            action_schema_version=INTERACTION_ACTION_SCHEMA_VERSION,
            validate_target=_phenotyping_interaction_validator(
                state_store=dependencies.state_store,
                stage=stage,
                reason=reason,
                owner_validator=phenotyping_response_validator(stage),
            ),
        )
        for stage, reason in _PHENOTYPING_INTERACTION_OWNER_ROWS
    )


def _completed_outcome(
    context: BatchNodeExecutionContext,
    result: BatchNodeExecutionResult,
    *,
    slide_id: str,
) -> BatchRawNodeOutcome:
    return BatchRawNodeOutcome(
        context,
        StageResult.completed(
            stage=context.adapter_spec.node_kind,
            slide_id=slide_id,
        ),
        result,
        None,
    )


def _interaction_outcome(
    context: BatchNodeExecutionContext,
    result: StageResult,
) -> BatchRawNodeOutcome:
    return BatchRawNodeOutcome(
        context,
        result,
        BatchNodeExecutionResult(
            context.authorization.authorization_sha256,
            (),
        ),
        None,
    )


def _artifact_scope_for_aggregate(
    mode: ClusteringMode,
    authorities: Sequence[BatchCommittedArtifactAuthority],
    bindings: tuple[object, ...],
) -> ArtifactScope:
    preparations = tuple(
        item.artifact_ref
        for item in authorities
        if item.artifact_ref.artifact_kind == "unsupervised_preparation_result"
    )
    if mode is ClusteringMode.POOLED:
        domains = {item.scope.validation_domain_id for item in preparations}
        if len(domains) != 1 or None in domains:
            raise BatchArtifactError("pooled clustering requires one Processing group")
        return ArtifactScope(next(iter(domains)), None, None, None, bindings)
    if len(preparations) != 1:
        raise BatchArtifactError("per-slide clustering requires one preparation scope")
    source = preparations[0].scope
    return ArtifactScope(
        source.validation_domain_id,
        source.sample_id,
        source.slide_id,
        source.project_entry_id,
        bindings,
    )


def _configuration_union(
    authorities: Sequence[BatchCommittedArtifactAuthority],
) -> tuple[ArtifactConfigurationBinding, ...]:
    return tuple(
        sorted(
            {
                binding
                for authority in authorities
                for binding in authority.artifact_ref.configuration_bindings
            },
            key=lambda item: item.sample_id,
        )
    )


def _persist_owner_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise BatchArtifactError("phenotyping owner destination contains foreign bytes")
        return
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{hashlib.sha256(payload).hexdigest()[:12]}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _clustering_request_from_preparations(
    *,
    active_plan_sha256: str,
    participant_ids: tuple[str, ...],
    project_entry_ids: tuple[str, ...],
    scope: Mapping[str, object] | None,
    preparations: tuple[UnsupervisedPreparationInput, ...],
) -> BatchClusteringRequest:
    """Project settings only from decoded preparations; the caller owns authority."""
    if not preparations:
        raise BatchArtifactError("clustering lacks accepted preparation settings")
    first = preparations[0]
    if any(
        (item.clustering_mode, item.feature_specs, item.k, item.resolution, item.seed, item.method, item.method_settings)
        != (first.clustering_mode, first.feature_specs, first.k, first.resolution, first.seed, first.method, first.method_settings)
        for item in preparations
    ):
        raise BatchArtifactError(
            "aggregate clustering preparations disagree on scientific settings"
        )
    if scope is None or scope.get("mode") != first.clustering_mode.value:
        raise BatchArtifactError("clustering plan scope differs from accepted settings")
    return BatchClusteringRequest(
        active_plan_sha256=active_plan_sha256,
        mode=first.clustering_mode,
        participant_ids=participant_ids,
        project_entry_ids=project_entry_ids,
        phenotyping_group_id=None,
        feature_specs=first.feature_specs,
        k=first.k,
        resolution=first.resolution,
        seed=first.seed,
        maximum_cell_count=sum(item.cell_count for item in preparations),
        maximum_input_bytes=sum(item.measurement_rows_byte_count + item.measurement_matrix_bytes for item in preparations),
        method=first.method,
        method_settings=first.method_settings,
    )


def _execute_clustering_owner(
    *,
    context: BatchNodeExecutionContext,
    dependencies: PhenotypingRuntimeDependencies,
) -> BatchRawNodeOutcome:
    current = dependencies.state_store.authenticate_current_execution_context(context)
    snapshot = dependencies.state_store.reopen_execution_snapshot(
        expected_head_sha256=current.state_head_sha256
    )
    sample_by_id = {item.sample_id: item for item in snapshot.manifest.enabled_samples}
    try:
        samples = tuple(sample_by_id[item] for item in current.participant_ids)
    except KeyError as exc:
        raise BatchArtifactError("clustering participants differ from current manifest") from exc
    authorities = tuple(
        dependencies.state_store.authenticate_available_artifact(item)
        for item in sorted(context.inputs, key=lambda ref: ref.artifact_ref_sha256)
    )
    by_kind = _authorities_by_kind(authorities)
    preparations = by_kind.get("unsupervised_preparation_result", ())
    measurements = by_kind.get("measurements_result", ())
    if not samples or not preparations or not measurements:
        raise BatchArtifactError("clustering lacks its exact current sources")
    decoded_preparations = tuple(
        decode_committed_preparation(
            item,
            maximum_byte_count=item.artifact_ref.byte_count,
        )
        for item in preparations
    )
    request = _clustering_request_from_preparations(
        active_plan_sha256=current.active_plan_sha256,
        participant_ids=current.participant_ids,
        project_entry_ids=current.project_entry_ids,
        scope=current.clustering_scope,
        preparations=decoded_preparations,
    )
    from .refinement_execution import execute_refit_revision
    result=execute_refit_revision(store=dependencies.state_store,context=context,current=current,
        preparations=preparations,measurements=measurements,decoded_preparations=decoded_preparations)
    if result is None:
        result = execute_batch_clustering(
            request=request,
            context=context,
            execution_authority=current,
            preparation_authorities=preparations,
            measurement_authorities=measurements,
        )
    destination = (
        dependencies.state_store.layout.state
        / "phenotyping-results"
        / f"{result.result_sha256}.json"
    )
    persisted = persist_batch_clustering_result(result, destination)
    contract = clustering_result_identity_contract()
    bindings = tuple(
        sorted(
            {
                binding
                for item in preparations
                for binding in item.artifact_ref.scope.analysis_domain_bindings
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
    artifact = BatchArtifactRef(
        artifact_id="clustered-phenotype-result-" + result.result_sha256[:48],
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=persisted.locator,
        sha256=persisted.sha256,
        byte_count=persisted.byte_count,
        scope=_artifact_scope_for_aggregate(result.mode, preparations, bindings),
        owning_identity=clustering_result_owning_identity(result),
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-batch-clustering/v1",
        runtime_versions=dict(result.runtime_versions),
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=_configuration_union(authorities),
    )
    slide_id = result.entry_composition[0].slide_id
    return _completed_outcome(
        context,
        BatchNodeExecutionResult(
            context.authorization.authorization_sha256,
            (artifact,),
        ),
        slide_id=slide_id,
    )


def _execute_cluster_review_evidence_owner(
    *,
    context: BatchNodeExecutionContext,
    dependencies: PhenotypingRuntimeDependencies,
) -> BatchRawNodeOutcome:
    current = dependencies.state_store.authenticate_current_execution_context(context)
    authorities = tuple(
        dependencies.state_store.authenticate_available_artifact(item)
        for item in sorted(context.inputs, key=lambda ref: ref.artifact_ref_sha256)
    )
    by_kind = _authorities_by_kind(authorities)
    clustering = by_kind.get("clustered_phenotype_result", ())
    if len(clustering) != 1:
        raise BatchArtifactError("cluster review requires one clustering owner")
    source_authorities = tuple(item for item in authorities if item is not clustering[0])
    evidence = build_batch_cluster_review_evidence(
        clustering_authority=clustering[0],
        source_authorities=source_authorities,
        maximum_clustering_result_byte_count=(int(clustering[0].artifact_ref.byte_count or 0)),
        maximum_measurement_rows_byte_count=sum(
            int(item.artifact_ref.owning_identity.get("measurement_rows_byte_count", 0))
            + int(item.artifact_ref.owning_identity.get("measurement_matrix_bytes", 0))
            for item in source_authorities
            if item.artifact_ref.artifact_kind == "unsupervised_preparation_result"
        ),
    )
    payload = dumps_batch_cluster_review_evidence(evidence)
    destination = (
        dependencies.state_store.layout.state
        / "phenotyping-results"
        / f"{evidence.evidence_sha256}.cluster-review.json"
    )
    _persist_owner_bytes(destination, payload)
    contract = cluster_review_evidence_identity_contract()
    artifact = BatchArtifactRef(
        artifact_id="cluster-review-evidence-" + evidence.evidence_sha256[:48],
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=destination,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        scope=_artifact_scope_for_aggregate(
            evidence.mode,
            source_authorities,
            evidence.analysis_domain_bindings,
        ),
        owning_identity=cluster_review_evidence_owning_identity(evidence),
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-cluster-review-evidence/v1",
        runtime_versions={"python": platform.python_version()},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=_configuration_union(authorities),
    )
    slide_id = evidence.analysis_domain_bindings[0].slide_id
    return _completed_outcome(
        context,
        BatchNodeExecutionResult(
            context.authorization.authorization_sha256,
            (artifact,),
        ),
        slide_id=slide_id,
    )


def _execute_final_export_owner(
    *,
    context: BatchNodeExecutionContext,
    sample: ResolvedBatchSample,
    dependencies: PhenotypingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
) -> BatchRawNodeOutcome:
    tissue = tuple(
        dependencies.state_store.authenticate_available_artifact(item)
        for item in context.inputs
        if item.artifact_kind == "tissue_acceptance_result"
    )
    if len(tissue) != 1:
        raise BatchArtifactError("final export requires one accepted tissue owner")
    reopener = dependencies.project_stage.committed_exchange_reopener(
        workspace_lease=workspace_lease
    )
    accepted_tissue = reopen_committed_accepted_tissue_live_authority(
        tissue[0], exchange_reopener=reopener
    )
    return execute_final_non_spatial_export_stage(
        context,
        state_store=dependencies.state_store,
        resolved_sample=sample,
        accepted_tissue=accepted_tissue,
        phenotype_view_store=dependencies.phenotype_view_store,
        exchange_reopener=reopener,
        output_root=operational_sample_output_root(sample, dependencies.state_store.layout.root) / "analysis",
        pipeline_version=MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    )


def _execute_direct_owner(
    *,
    context: BatchNodeExecutionContext,
    owner_binding: PhenotypingStageOwnerBinding,
    sample_by_id: Mapping[str, ResolvedBatchSample],
    dependencies: PhenotypingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
) -> BatchRawNodeOutcome:
    kind = context.adapter_spec.node_kind
    if kind == "phenotyping_mode_resolution":
        sample = _resolved_sample_for_context(context, sample_by_id, dependencies.state_store)
        authority = resolve_phenotyping_mode_selection(
            context,
            resolved_sample=sample,
            state_store=dependencies.state_store,
        )
        if type(authority) is StageResult:
            return _interaction_outcome(context, authority)
        return _completed_outcome(
            context,
            build_phenotyping_mode_execution_result(authority),
            slide_id=sample.slide_id,
        )
    if kind == "marker_combination_selection":
        from .marker_combinations import execute_marker_combination_selection
        result = execute_marker_combination_selection(context, state_store=dependencies.state_store)
        if type(result) is StageResult:
            return _interaction_outcome(context, result)
        slide = context.inputs[0].owning_identity["measurement_authority_bindings"][0]["slide_id"]
        return _completed_outcome(context, result, slide_id=slide)
    if kind == "marker_gate_review":
        current = dependencies.state_store.authenticate_current_execution_context(context)
        snapshot = dependencies.state_store.reopen_execution_snapshot(
            expected_head_sha256=current.state_head_sha256
        )
        population = open_authenticated_marker_measurement_population(
            context,
            state_store=dependencies.state_store,
            manifest=snapshot.manifest,
        )
        if type(population) is StageResult:
            return _interaction_outcome(context, population)
        from .nimbus_execution import AutomaticNimbusScoreProvider
        from mif_pipeline.orchestration.setup.nimbus_requirements import NimbusPrerequisiteError

        nimbus_provider = AutomaticNimbusScoreProvider(
            runner=dependencies.runner,
            project_file=Path(snapshot.head.project_path),
            output_directory=dependencies.state_store.layout.root / "phenotyping-nimbus",
            preferences_path=dependencies.software_preferences_path,
        )
        try:
            evidence = build_marker_gate_evidence(
                population, state_store=dependencies.state_store,
                nimbus_score_provider=nimbus_provider,
            )
        except NimbusPrerequisiteError as exc:
            return _interaction_outcome(context, build_marker_software_setup_request(
                population, exc.requirement, state_store=dependencies.state_store
            ))
        authority = resolve_marker_gate_domain_authority(
            evidence, state_store=dependencies.state_store
        )
        if type(authority) is StageResult:
            return _interaction_outcome(context, authority)
        return _completed_outcome(
            context,
            build_marker_gate_execution_result(authority),
            slide_id=population.measurement_projections[0].slide_id,
        )
    if kind == "unsupervised_preparation":
        current, snapshot, sample, authorities = _current_owner_inputs(
            context, dependencies.state_store
        )
        del sample
        by_kind = _authorities_by_kind(authorities)
        measurements = by_kind.get("measurements_result", ())
        modes = by_kind.get("phenotyping_mode", ())
        if len(measurements) != 1 or not modes:
            raise BatchArtifactError("unsupervised preparation lacks exact committed sources")
        result = build_unsupervised_preparation_execution_result(
            context=context,
            execution_authority=current,
            manifest=snapshot.manifest,
            measurement_authority=measurements[0],
            mode_authorities=modes,
            state_store=dependencies.state_store,
        )
        if isinstance(result, StageResult):
            return _interaction_outcome(context, result)
        return _completed_outcome(
            context,
            result,
            slide_id=measurements[0].artifact_ref.scope.slide_id or "aggregate",
        )
    if kind in {
        "clustering_per_slide",
        "clustering_pooled",
    }:
        return _execute_clustering_owner(
            context=context,
            dependencies=dependencies,
        )
    if kind == "cluster_review_evidence":
        return _execute_cluster_review_evidence_owner(
            context=context,
            dependencies=dependencies,
        )
    if kind == "phenotype_hierarchy_attestation":
        current, _snapshot, sample, authorities = _current_owner_inputs(
            context, dependencies.state_store
        )
        authority = issue_authorized_phenotype_hierarchy_attestation(
            context=context,
            current_execution=current,
            source_authorities=authorities,
            phenotype_view_store=dependencies.phenotype_view_store,
            output_directory=operational_sample_output_root(sample, dependencies.state_store.layout.root) / "phenotyping",
        )
        return _completed_outcome(
            context,
            build_phenotype_hierarchy_attestation_execution_result(
                context=context, authority=authority
            ),
            slide_id=sample.slide_id,
        )
    if kind == "no_secondary_completion":
        return execute_no_secondary_completion_stage(
            context,
            state_store=dependencies.state_store,
            resolved_sample=_resolved_sample_for_context(
                context, sample_by_id, dependencies.state_store
            ),
        )
    if kind == "semantic_evaluation":
        return execute_semantic_evaluation_stage(
            context,
            state_store=dependencies.state_store,
            resolved_sample=_resolved_sample_for_context(
                context, sample_by_id, dependencies.state_store
            ),
        )
    if kind == "final_count_cap":
        return execute_final_count_cap_stage(
            context,
            state_store=dependencies.state_store,
            resolved_sample=_resolved_sample_for_context(
                context, sample_by_id, dependencies.state_store
            ),
        )
    if kind == "final_non_spatial_export":
        return _execute_final_export_owner(
            context=context,
            sample=_resolved_sample_for_context(context, sample_by_id, dependencies.state_store),
            dependencies=dependencies,
            workspace_lease=workspace_lease,
        )
    raise BatchArtifactError(f"phenotyping direct owner {kind!r} is not composed")


def _one_authority(
    by_kind: Mapping[str, tuple[BatchCommittedArtifactAuthority, ...]],
    kind: str,
) -> BatchCommittedArtifactAuthority:
    values = by_kind.get(kind, ())
    if len(values) != 1:
        raise BatchArtifactError(f"phenotyping owner requires one exact {kind} authority")
    return values[0]


def _live_tissue_domain_cell_inputs(
    *,
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
    dependencies: PhenotypingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
) -> tuple[object, object, tuple[object, ...]]:
    return reopen_committed_phenotyping_live_authorities(
        authorities,
        project_stage=dependencies.project_stage,
        workspace_lease=workspace_lease,
    )


_PHENOTYPE_SOURCE_KINDS = frozenset(
    {
        "marker_phenotype_result",
        "supervised_phenotype_result",
        "cluster_label_import_result",
    }
)


def _semantic_cell_selector_inventory(branch, measurements, snapshot, *, classifier_record_set_store):
    """Read declared phenotype choices and the saved measurement/channel bindings."""
    from ..secondary_cell_inventory import phenotype_selector_inventory
    from ..batch_measurement_artifacts import decode_committed_measurements_result

    declared = phenotype_selector_inventory(
        branch.phenotype_application.source_artifact_ref, snapshot,
        classifier_record_set_store=classifier_record_set_store)
    measured = decode_committed_measurements_result(measurements)
    selected_measurements = set(measured.selected_measurement_names)
    return {**declared,
            "measurement_ids": measured.selected_measurement_names,
            "channel_roles": tuple(channel["channel_id"] for channel in measured.canonical_channels),
            "measurement_channels": {item["measurement_name"]: item["channel_id"]
                                     for item in measured.measurement_inventory
                                     if item["measurement_name"] in selected_measurements}}


def _close_cell_edit_indices(cells):
    """Release sparse reads on their worker if capability publication fails."""
    for owner in cells:
        index = getattr(owner.cell_dataset, "_edit_index", None)
        if index is not None:
            index.close()


def _publish_project_owner_capability(
    *,
    saved_state_before_sha256: str,
    context: BatchNodeExecutionContext,
    sample: ResolvedBatchSample,
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
    snapshot: object,
    dependencies: PhenotypingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
) -> str | None:
    kind = context.adapter_spec.node_kind
    if kind not in {
        "semantic_raw_inventory_snapshot",
        "semantic_evidence",
        "semantic_finalization",
        "phenotype_handoff",
    }:
        return None
    current = dependencies.state_store.authenticate_current_execution_context(context)
    from ..batch_secondary_definitions import secondary_definition_selections
    secondary_selection = secondary_definition_selections(
        snapshot, state_store=dependencies.state_store, participant_ids=(sample.sample_id,), node_id=context.node_id,
    ).get(sample.sample_id)
    by_kind = _authorities_by_kind(authorities)
    publisher = dependencies.project_stage_owner_capability_publisher
    if kind == "semantic_raw_inventory_snapshot":
        tissue, domains, cells = _live_tissue_domain_cell_inputs(
            authorities=authorities,
            dependencies=dependencies,
            workspace_lease=workspace_lease,
        )
        try:
            classification_routes = (*by_kind.get("primary_inference_result", ()),
                                     *by_kind.get("training_plan_set", ()))
            if len(classification_routes) != 1:
                raise BatchArtifactError("Semantic export requires its accepted primary classification choice")
            rois = bind_committed_classifier_roi_live_authority(
                classification_routes[0],
                by_kind.get("secondary_inference_result", ()),
                analysis_domain_binding=current.analysis_domain_bindings[0],
                resolved_sample=sample,
                secondary_selection=secondary_selection,
                accepted_domain_authority=_one_authority(by_kind, "accepted_analysis_domain"),
                pipeline_run_id=cells[0].pipeline_run_id if len(cells) == 1 else None,
                exchange_reopener=dependencies.project_stage.committed_exchange_reopener(
                    workspace_lease=workspace_lease),
            )
            sources = tuple(
                item
                for item in authorities
                if item.artifact_ref.artifact_kind in _PHENOTYPE_SOURCE_KINDS
            )
            if len(cells) != 1 or len(sources) != 1:
                raise BatchArtifactError(
                    "semantic raw publication lacks exact cells or phenotype source"
                )
            publish_semantic_raw_snapshot_invocation_authority(
                publisher,
                context=context,
                current=current,
                resolved_sample=sample,
                accepted_tissue=tissue,
                accepted_domains=domains,
                classifier_rois=rois,
                segmentation_cells=cells[0],
                measurement_authority=_one_authority(by_kind, "measurements_result"),
                phenotype_source_authority=sources[0],
            )
            return str(cells[0].pipeline_run_id)
        except BaseException:
            _close_cell_edit_indices(cells)
            raise
    if kind == "semantic_evidence":
        sources = tuple(
            item
            for item in authorities
            if item.artifact_ref.artifact_kind in _PHENOTYPE_SOURCE_KINDS
        )
        if len(sources) != 1:
            raise BatchArtifactError("semantic evidence requires one phenotype source owner")
        raw = _one_authority(by_kind, "semantic_raw_inventory")
        attestations = by_kind.get("phenotype_view_attestation", ())
        if len(attestations) > 1:
            raise BatchArtifactError("semantic evidence has ambiguous hierarchy attestation")
        phenotype_sources = (*sources, *attestations) if sources[0].artifact_ref.artifact_kind == "cluster_label_import_result" else sources
        branch = bind_committed_semantic_domain_branch(
            phenotype_application=normalize_committed_phenotype_application(phenotype_sources),
            raw_inventory_authority=raw,
        )
        measurements = _one_authority(by_kind, "measurements_result")
        publish_semantic_evidence_stage_owner_capability(
            publisher,
            context=context,
            current=current,
            resolved_sample=sample,
            semantic_branch=branch,
            raw_inventory=raw,
            phenotype_view_attestation=(None if not attestations else attestations[0]),
            analysis_frame=_one_authority(by_kind, "analysis_frame"),
            accepted_domain=_one_authority(by_kind, "accepted_analysis_domain"),
            measurements=measurements,
            semantic_profiles=by_kind.get("semantic_profile", ()),
            cell_selector_inventory=_semantic_cell_selector_inventory(
                branch, measurements, snapshot,
                classifier_record_set_store=dependencies.classifier_record_set_store),
            secondary_selection=secondary_selection,
        )
        return str(measurements.artifact_ref.owning_identity["pipeline_run_id"])
    if kind == "semantic_finalization":
        all_authorities = {
            item.artifact_ref.artifact_ref_sha256: item
            for item in snapshot.committed_artifact_authorities
        }
        raw = _one_authority(by_kind, "semantic_raw_inventory")
        evaluation_authority = _one_authority(by_kind, "semantic_evaluation_result")
        count_authority = _one_authority(by_kind, "final_count_cap_result")
        evaluation_identity = evaluation_authority.artifact_ref.owning_identity
        profile_ref_ids = set(evaluation_identity.get("semantic_profile_ref_sha256s", ()))
        for ref in snapshot.accepted_artifacts:
            if ref.artifact_ref_sha256 in profile_ref_ids and ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL:
                all_authorities[ref.artifact_ref_sha256] = (
                    dependencies.state_store.authenticate_available_manifest_external_artifact(ref)
                )
        evidence_authority = all_authorities.get(
            str(evaluation_identity.get("semantic_evidence_ref_sha256"))
        )
        profile_authorities = tuple(
            all_authorities.get(str(digest))
            for digest in evaluation_identity.get("semantic_profile_ref_sha256s", ())
        )
        if evidence_authority is None or any(item is None for item in profile_authorities):
            raise BatchArtifactError("semantic finalization lacks retained evaluation predecessors")
        evidence_identity = evidence_authority.artifact_ref.owning_identity
        source = all_authorities.get(str(evidence_identity.get("phenotype_source_ref_sha256")))
        attestation_digest = evidence_identity.get("phenotype_view_attestation_ref_sha256")
        attestation = (
            None if attestation_digest is None else all_authorities.get(str(attestation_digest))
        )
        if source is None or (attestation_digest is not None and attestation is None):
            raise BatchArtifactError("semantic finalization lacks retained phenotype predecessors")
        branch = bind_committed_semantic_domain_branch(
            phenotype_application=normalize_committed_phenotype_application(
                (source, attestation) if source.artifact_ref.artifact_kind == "cluster_label_import_result" else (source,)),
            raw_inventory_authority=raw,
        )
        frame = _one_authority(by_kind, "analysis_frame")
        domain = _one_authority(by_kind, "accepted_analysis_domain")
        measurements = _one_authority(by_kind, "measurements_result")
        profiles = tuple(item for item in profile_authorities if item is not None)
        evidence = decode_committed_semantic_evidence_result(
            evidence_authority,
            raw_inventory=raw,
            semantic_branch=branch,
            phenotype_view_attestation=attestation,
            analysis_frame=frame,
            accepted_domain=domain,
            measurements=measurements,
            semantic_profiles=profiles,
            resolved_sample=sample,
            secondary_selection=secondary_selection,
        )
        evaluation = decode_committed_semantic_evaluation_result(
            evaluation_authority,
            semantic_evidence=evidence,
            raw_inventory=raw,
            semantic_profiles=profiles,
            resolved_sample=sample,
            secondary_selection=secondary_selection,
        )
        count = decode_committed_final_count_cap_result(
            count_authority,
            semantic_evaluation=evaluation,
            raw_inventory=raw,
            accepted_domain=domain,
            resolved_sample=sample,
            secondary_selection=secondary_selection,
        )
        publish_semantic_finalization_stage_owner_capability(
            publisher,
            context=context,
            current=current,
            resolved_sample=sample,
            semantic_evaluation=evaluation,
            final_count_cap=count,
            raw_inventory=raw,
            analysis_frame=frame,
            accepted_domain=domain,
            measurements=measurements,
        )
        return str(evaluation.pipeline_run_id)
    tissue, domains, cells = _live_tissue_domain_cell_inputs(
        authorities=authorities,
        dependencies=dependencies,
        workspace_lease=workspace_lease,
    )
    try:
        publish_phenotype_handoff_stage_owner_capability(
            publisher,
            context=context,
            current=current,
            resolved_sample=sample,
            accepted_tissue=tissue,
            accepted_domains=domains,
            segmentation_cells=cells,
            committed_inputs=authorities,
            output_root=operational_sample_output_root(sample, dependencies.state_store.layout.root) / "analysis" / "exports",
            workspace_root=dependencies.state_store.layout.root,
            saved_state_before_sha256=saved_state_before_sha256,
            secondary_selection=secondary_selection,
        )
        return str(cells[0].pipeline_run_id)
    except BaseException:
        _close_cell_edit_indices(cells)
        raise


def _accept_completed_marker_retry(
    *, contexts, spec, dependencies, workspace_lease, terminal_committer,
):
    """Accept an unchanged native marker result through a fresh public Retry.

    We keep the original failed terminal, native exchange and prepared component.
    Only result validation and publication run again; assignment calculation and
    QuPath execution have already completed.
    """
    from ..batch_failed_retry import _check_live_target
    from .marker_application_adapters import build_marker_rule_application_execution_result

    state = dependencies.state_store
    with state.current_project_control(operation="retry", lease=workspace_lease) as control:
        control.project_processes.assert_all_drained(
            batch_id=control.head.batch_id,
            project_identity_sha256=control.head.project_identity_sha256)
        reopener = dependencies.project_stage.committed_exchange_reopener(
            workspace_lease=workspace_lease)
        retained = tuple(reopener.reopen_failed_target_for_owner(
            node_id=context.node_id,
            terminal_record_sha256=context.authorization.failed_retry.terminal_record_sha256,
            expected_binding=(spec.spec_sha256,
                QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value,
                "phenotyping", "marker_rule_application", "1"),
        ) for context in contexts)
        if any(item.completed_evidence is None for item in retained):
            return None
        snapshot = dependencies.project_provider.inspect_live_project(control.project_lease)
        outcomes = []
        for context, previous in zip(contexts, retained, strict=True):
            _check_live_target(state_store=state, head=control.head,
                reopened=previous, snapshot=snapshot, workspace_lease=workspace_lease)
            current, _snapshot, sample, authorities = _current_owner_inputs(context, state)
            authority = issue_authorized_marker_rule_section_application(
                context=context, current_execution=current, source_authorities=authorities,
                resolved_sample=sample, authenticated_output_root=_measurement_output_root(authorities),
                phenotype_view_store=dependencies.phenotype_view_store,
                source_run_id=previous.authorized.attempt_id, completed_failure=previous)
            evidence = previous.completed_evidence
            result = build_marker_rule_application_execution_result(
                evidence, authority, acceptance_context=context)
            outcomes.append(BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.completed(stage=spec.node_kind,
                    slide_id=previous.authorized.slide_id,
                    generation_id=evidence.wire_stage_result.generation_id),
                execution_result=result,
                execution_exchange_sha256=evidence.execution_exchange_sha256,
            ))
        return terminal_committer.commit_raw(tuple(outcomes))


def _execute_project_owner_group(
    *,
    spec: StageAdapterSpec,
    contexts: tuple[BatchNodeExecutionContext, ...],
    sample_by_id: Mapping[str, ResolvedBatchSample],
    dependencies: PhenotypingRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    terminal_committer: BatchTerminalCommitter,
) -> tuple[ScheduledNodeResult, ...]:
    values = tuple(contexts)
    if (
        not values
        or any(
            type(item) is not BatchNodeExecutionContext or item.adapter_spec is not spec
            for item in values
        )
        or type(workspace_lease) is not BatchWorkspaceLease
    ):
        raise BatchArtifactError("project phenotyping runtime requires one exact context group")
    if spec.node_kind == "marker_rule_application" and all(
            context.authorization.failed_retry is not None for context in values):
        recovered = _accept_completed_marker_retry(
            contexts=values, spec=spec, dependencies=dependencies,
            workspace_lease=workspace_lease, terminal_committer=terminal_committer)
        if recovered is not None:
            return recovered
    state_store = dependencies.state_store
    head = state_store.load_head()
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
    catalogue = state_store.project_catalogue_store(workspace_lease).load_current()
    if catalogue is None:
        raise BatchArtifactError("project phenotyping runtime lacks a current project catalogue")
    current_saved_state_by_entry_id = state_store.current_project_entry_saved_state_sha256_by_id(
        expected_head_sha256=head.sha256,
        lease=workspace_lease,
    )
    node_by_id = {item.node_id: item for item in snapshot.plan.nodes}
    entry_by_id = {item.project_entry_id: item for item in catalogue.entries}
    targets: list[ProjectStageTargetIntent] = []
    published: list[BatchNodeExecutionContext] = []
    try:
        for context in values:
            current = state_store.authenticate_current_execution_context(context)
            sample = _resolved_sample_for_context(
                context,
                sample_by_id,
                state_store,
                current=current,
                snapshot=snapshot,
            )
            entries = context.authorization.dispatch_binding.project_entry_ids
            node = node_by_id.get(context.node_id)
            if (
                node is None
                or node.kind != spec.node_kind
                or node.adapter_spec_sha256 != spec.spec_sha256
                or len(entries) != 1
                or node.project_entry_ids != entries
                or entries[0] not in entry_by_id
            ):
                raise BatchArtifactError("project phenotyping context differs from current plan")
            entry = entry_by_id[entries[0]]
            if (
                entry.sample_id != sample.sample_id
                or entry.slide_id != sample.slide_id
                or entry.image_role != "primary_mif"
            ):
                raise BatchArtifactError("project phenotyping entry differs from resolved sample")
            authorities = tuple(
                (state_store.authenticate_available_manifest_external_artifact(item)
                 if item.origin is ArtifactOrigin.MANIFEST_EXTERNAL
                 else state_store.authenticate_available_artifact(item))
                for item in sorted(context.inputs, key=lambda ref: ref.artifact_ref_sha256)
            )
            try:
                owner_pipeline_run_id = _publish_project_owner_capability(
                    saved_state_before_sha256=current_saved_state_by_entry_id[entry.project_entry_id],
                    context=context,
                    sample=sample,
                    authorities=authorities,
                    snapshot=snapshot,
                    dependencies=dependencies,
                    workspace_lease=workspace_lease,
                )
            except BatchPhenotypingError as exc:
                # Owner validation precedes native preparation. A retained
                # native run still requires its existing recovery path.
                provider = state_store._project_mutation_provider
                if (spec.node_kind != "semantic_evidence" or provider is None
                        or provider._unresolved_active_records_for_project(
                            project_uri=catalogue.project_uri,
                            project_identity_sha256=head.project_identity_sha256)):
                    raise
                for published_context in published:
                    dependencies.project_stage_owner_capability_publisher.abort(published_context)
                published.clear()
                return terminal_committer.commit_raw(tuple(
                    BatchRawNodeOutcome(
                        context=item,
                        stage_result=StageResult.failed(
                            stage=spec.node_kind,
                            slide_id=entry_by_id[item.authorization.dispatch_binding.project_entry_ids[0]].slide_id,
                            error_code="semantic_evidence_preparation_failed",
                            error_message=("Region review could not be prepared. Completed results are saved. "
                                           "Report this error with its details."),
                            error_context={"request_error": "".join(traceback.format_exception(exc, limit=0)).strip()},
                        ),
                        execution_result=BatchNodeExecutionResult(
                            item.authorization.authorization_sha256, ()),
                        execution_exchange_sha256=None,
                    )
                    for item in values
                ))
            if owner_pipeline_run_id is not None:
                published.append(context)
            targets.append(
                ProjectStageTargetIntent(
                    project_entry_id=entry.project_entry_id,
                    sample_id=sample.sample_id,
                    slide_id=sample.slide_id,
                    pipeline_run_id=(
                        head.batch_id if owner_pipeline_run_id is None else owner_pipeline_run_id
                    ),
                    image_role="primary_mif",
                    analysis_domain_bindings=current.analysis_domain_bindings,
                    prerequisite_artifact_ref_sha256s=tuple(
                        sorted(item.artifact_ref_sha256 for item in context.inputs)
                    ),
                    saved_state_before_sha256=(
                        current_saved_state_by_entry_id[entry.project_entry_id]
                    ),
                    resolved_sample=sample,
                )
            )
        target_values = tuple(sorted(targets, key=lambda item: item.project_entry_id))
        target_ids = {item.project_entry_id for item in target_values}
        input_sets = tuple(
            {item.artifact_ref_sha256 for item in context.inputs} for context in values
        )
        shared = tuple(sorted(set.intersection(*input_sets)))
        authority_layout = state_store.authority_layout_sha256
        if authority_layout is None:
            raise BatchArtifactError("project phenotyping runtime lacks retained authority layout")
        asset = {
            "marker_rule_application": (
                QuPathScriptAsset.APPLY_KNOWN_MARKER_CELL_CLASSIFIERS.value
            ),
            "supervised_phenotype_inference": (QuPathScriptAsset.TRAIN_SUPERVISED_PHENOTYPES.value),
            "clustering_label_import": (QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value),
            "semantic_raw_inventory_snapshot": (
                QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value
            ),
            "semantic_evidence": (QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value),
            "semantic_finalization": (QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value),
            "phenotype_handoff": (QuPathScriptAsset.EXPORT_CELL_AND_ROI_OBJECTS.value),
        }[spec.node_kind]
        intent = ProjectStageRunIntent(
            authority_layout_sha256=authority_layout,
            batch_id=head.batch_id,
            run_plan_id=(
                "phenotyping-"
                + spec.node_kind.replace("_", "-")
                + "-"
                + hashlib.sha256(
                    json.dumps(
                        [item.authorization.authorization_sha256 for item in values],
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
            stage_id=spec.node_kind,
            stage_version=spec.node_version,
            project_uri=catalogue.project_uri,
            project_identity_sha256=head.project_identity_sha256,
            expected_project_authority_head_sha256=(head.project_authority_head_sha256),
            project_catalogue_sha256=catalogue.catalogue_sha256,
            launch_mode=ProjectStageLaunchMode.HEADLESS_PROJECT_SCRIPT,
            target_script_asset_id=asset,
            guard_script_asset_id=QuPathScriptAsset.BATCH_STAGE_GUARD.value,
            targets=target_values,
            excluded_project_entry_ids=tuple(sorted(set(entry_by_id) - target_ids)),
            shared_artifact_ref_sha256s=shared,
            launch_record_wait_millis=30_000,
        )
        try:
            prepared = prepare_project_stage_run_request(
                intent,
                values,
                dependencies.project_stage,
                workspace_lease=workspace_lease,
            )
        except ProjectStageCodecError as exc:
            # Request projection precedes publication and child creation. An
            # existing unresolved native run must still use native recovery.
            provider = state_store._project_mutation_provider
            if (spec.node_kind != "clustering_label_import" or provider is None
                    or provider._unresolved_active_records_for_project(
                        project_uri=catalogue.project_uri,
                        project_identity_sha256=head.project_identity_sha256)):
                raise
            return terminal_committer.commit_raw(tuple(
                BatchRawNodeOutcome(
                    context=context,
                    stage_result=StageResult.failed(
                        stage=spec.node_kind,
                        slide_id=target.slide_id,
                        error_code="cluster_label_request_failed",
                        error_message=("Cluster labels could not be prepared for saving. "
                            "Select Retry to use the saved choices. To change them, open Change options "
                            "for cluster review and select Change cluster labels."),
                        error_context={"request_error": "".join(traceback.format_exception(exc, limit=0)).strip()},
                    ),
                    execution_result=BatchNodeExecutionResult(
                        context.authorization.authorization_sha256, ()),
                    execution_exchange_sha256=None,
                )
                for context in values
                for target in target_values
                if target.project_entry_id in context.authorization.dispatch_binding.project_entry_ids
            ))
        return run_project_stage_headless(
            prepared,
            dependencies.project_stage,
            workspace_lease,
            terminal_committer,
        )
    except BaseException:
        for context in published:
            try:
                dependencies.project_stage_owner_capability_publisher.abort(context)
            except Exception:
                pass
        raise


def _validate_current_evidence(
    *,
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
) -> BatchClusterReviewEvidence:
    current = state_store.authenticate_current_execution_context(context)
    if len(context.inputs) != 1:
        raise BatchArtifactError("cluster-label review requires one aggregate evidence input")
    evidence_ref = context.inputs[0]
    try:
        evidence = reopen_cluster_review_evidence_authority(
            state_store.authenticate_available_artifact(evidence_ref)
        )
    except BatchPhenotypingError as exc:
        raise BatchArtifactError(
            "cluster-label review evidence is not current and authentic"
        ) from exc
    clustering_scope = current.clustering_scope
    if (
        current.node_kind != "cluster_label_review"
        or current.participant_ids != evidence.participant_ids
        or current.project_entry_ids != evidence.project_entry_ids
        or current.analysis_domain_bindings != evidence.analysis_domain_bindings
        or current.configuration_bindings != evidence_ref.configuration_bindings
        or current.input_artifact_ref_sha256s != (evidence_ref.artifact_ref_sha256,)
        or clustering_scope is None
        or clustering_scope["mode"] != evidence.mode.value
        or clustering_scope["phenotyping_group_id"] != evidence.phenotyping_group_id
        or current.validation_domain_id != evidence_ref.scope.validation_domain_id
    ):
        raise BatchArtifactError(
            "cluster-label review execution scope differs from aggregate evidence"
        )
    return evidence


@dataclass(frozen=True, slots=True)
class ClusterLabelReviewRawAdapter:
    """Close first-attempt publication and retry acceptance in one owner adapter."""

    spec: StageAdapterSpec
    state_store: BatchStateStore

    def __post_init__(self) -> None:
        contract = accepted_cluster_label_review_identity_contract()
        if type(self.spec) is not StageAdapterSpec:
            raise BatchArtifactError("cluster-label review adapter requires an exact stage spec")
        if type(self.state_store) is not BatchStateStore:
            raise BatchArtifactError("cluster-label review adapter requires the exact state owner")
        if (
            self.spec.node_kind != "cluster_label_review"
            or self.spec.dispatch_mode is not DispatchMode.SINGLE
            or self.spec.resource_kind != "scientific_compute"
            or self.spec.declared_outputs != ("accepted_cluster_label_review",)
            or self.spec.output_identity_contracts != (contract,)
            or len(self.spec.required_inputs) != 1
            or self.spec.required_inputs[0].artifact_kind != "cluster_review_evidence"
            or self.spec.optional_inputs
        ):
            raise BatchArtifactError(
                "cluster-label review adapter spec differs from owner contract"
            )

    def execute_raw(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
    ) -> BatchRawNodeOutcome:
        del workspace_lease
        if type(context) is not BatchNodeExecutionContext:
            raise BatchArtifactError("cluster-label review requires an exact execution context")
        if context.adapter_spec != self.spec:
            raise BatchArtifactError("cluster-label review context uses a foreign stage spec")
        authorization = context.authorization
        response_sha256 = authorization.interaction_response_sha256
        predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
        if response_sha256 is None:
            if predecessor_sha256 is not None or authorization.retry_ordinal != 0:
                raise BatchArtifactError("cluster-label review initial attempt has retry lineage")
            evidence = _validate_current_evidence(
                context=context,
                state_store=self.state_store,
            )
            slide_id = evidence.analysis_domain_bindings[0].slide_id
            stage_result = StageResult.needs_interaction(
                stage="cluster_label_review",
                slide_id=slide_id,
                reason=InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED,
                message="Review cluster labels",
                allowed_actions=(InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value,),
                resume_context={
                    "schema_version": ("mif-batch-cluster-label-review-interaction/v1"),
                    "evidence_sha256": evidence.evidence_sha256,
                },
                checkpoint_id=evidence.evidence_sha256,
            )
            result = BatchNodeExecutionResult(
                authorization.authorization_sha256,
                (),
            )
            return BatchRawNodeOutcome(context, stage_result, result, None)

        if predecessor_sha256 is None or authorization.retry_ordinal <= 0:
            raise BatchArtifactError("cluster-label review retry lacks exact predecessor lineage")
        target, response = self.state_store.reopen_authorized_cluster_label_review(context)
        accepted = accept_cluster_label_review(
            target=target,
            authority=response,
            expected_context=context,
        )
        payload = canonical_bytes(accepted.to_mapping())
        destination = (
            self.state_store.layout.state
            / "phenotyping-results"
            / f"{accepted.result_sha256}.accepted-cluster-review.json"
        )
        _persist_owner_bytes(destination, payload)
        contract = accepted_cluster_label_review_identity_contract()
        evidence_ref = context.inputs[0]
        artifact = BatchArtifactRef(
            artifact_id="accepted-cluster-label-review-" + accepted.result_sha256[:48],
            artifact_kind=contract.artifact_kind,
            schema_name=contract.schema_name,
            schema_version=contract.schema_version,
            origin=ArtifactOrigin.INTERNAL_NODE,
            producer_node_id=context.node_id,
            producer_attempt_id=authorization.attempt_id,
            producer_attempt_authorization_sha256=(authorization.authorization_sha256),
            external_declaration_sha256=None,
            external_validation_receipt_sha256=None,
            locator=destination,
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
            scope=evidence_ref.scope,
            owning_identity=accepted_cluster_label_review_owning_identity(accepted),
            identity_contract=contract,
            owning_identity_contract_sha256=contract.contract_sha256,
            producer_version="mif-pipeline-cluster-label-review/v1",
            runtime_versions={"python": platform.python_version()},
            compatibility_signature_sha256=(context.compatibility_signature_sha256),
            configuration_bindings=evidence_ref.configuration_bindings,
        )
        result = BatchNodeExecutionResult(
            authorization.authorization_sha256,
            (artifact,),
        )
        return BatchRawNodeOutcome(
            context,
            StageResult.completed(
                stage="cluster_label_review",
                slide_id=target.analysis_domain_bindings[0].slide_id,
            ),
            result,
            None,
        )


def build_cluster_label_review_runtime_binding(
    *,
    spec: StageAdapterSpec,
    state_store: BatchStateStore,
) -> Task15RuntimeAdapterBinding:
    """Bind the accepted aggregate-review owner to the raw direct route."""

    adapter = ClusterLabelReviewRawAdapter(spec=spec, state_store=state_store)
    return Task15RuntimeAdapterBinding(
        spec=spec,
        route="raw_direct",
        adapter=adapter,
    )


__all__ = [
    "registered_phenotyping_interaction_owner_keys",
    "ClusterLabelReviewRawAdapter",
    "PhenotypingCodecDependencies",
    "PhenotypingRuntimeDependencies",
    "build_cluster_label_review_runtime_binding",
    "build_phenotyping_interaction_owner_bindings",
    "build_phenotyping_project_stage_codecs",
    "build_phenotyping_runtime_adapters",
]
