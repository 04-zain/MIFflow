"""composition of the existing aggregate-training owners.

The `TrainingRuntimeDependencies` record carries `state_store`, `project_stage`,
`project_provider`, `runner`, `engine_registry` and its remaining declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from types import MappingProxyType
from typing import Callable, Mapping

from mif_pipeline.pipeline_core import SecondaryStructureSpec
from mif_pipeline.pipeline_core import (
    INTERACTION_ACTION_SCHEMA_VERSION,
    InteractionAction,
    InteractionReason,
)
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseProvider
from mif_pipeline.qupath_runtime.runner import QuPathRunner
from mif_pipeline.qupath_runtime.scripts import (
    QuPathScriptAsset,
    registered_script_bundle_sha256,
)

from ..accepted_analysis_domain_records import (
    ValidatedAcceptedAnalysisDomainProjection,
    decode_committed_accepted_analysis_domain,
)
from ..batch_artifacts import (
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    StageAdapterSpec,
)
from ..batch_classifier_inference_artifacts import (
    ValidatedPrimaryClassifierInferenceResultProjection,
    decode_committed_primary_classifier_inference_result,
)
from ..batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
)
from ..batch_phenotyping_mode_artifacts import (
    ValidatedPhenotypingModeProjection,
    decode_committed_phenotyping_mode,
)
from ..batch_state import (
    BatchAttemptAuthorizationRequest,
    BatchRawNodeOutcome,
    BatchStateStore,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    ScheduledNodeResult,
)
from ..batch_manifest import ResolvedBatchSample
from ..batch_service_registry import Task15RuntimeAdapterBinding
from ..batch_interactions import (
    BatchInteractionOwnerBinding,
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from ..batch_dag import BatchPlan
from ..batch_project import reopen_current_project_entry_channel_observation
from ..project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageTargetIntent,
    ProjectStageRunIntent,
    prepare_project_stage_run_request,
)
from ..project_stage_plan import (
    ProjectStageCommittedExchangeReopener,
    ProjectStageRuntime,
    ProjectStageTargetExecutionEvidence,
    ProjectStageLaunchMode,
    run_project_stage_headless,
)
from ..batch_analysis_domain_artifacts import (
    reopen_committed_accepted_analysis_domain_live_authority,
)
from ..batch_tissue_artifacts import (
    reopen_committed_accepted_tissue_live_authority,
)
from ._shared import BatchTrainingError, ClassifierKind, EvaluationIntent
from .records import ClassifierRecordSet
from .engines import (
    TrainingEngineProviderRegistry,
    validate_authorized_training_interaction_response,
)
from .evaluation import evaluate_held_out_native_classifier
from .evidence import (
    MissingPixelTrainingClassesError,
    assemble_pixel_training_evidence_for_plan,
    partition_pixel_training_evidence,
    partition_supervised_training_evidence,
    _project_supervised_training_evidence_sources,
    reopen_pixel_training_evidence_set,
    reopen_supervised_training_evidence_entry,
    build_pixel_training_evidence_execution_result,
    build_supervised_training_evidence_execution_result,
    open_pixel_training_evidence_set,
    open_supervised_training_evidence_entry,
    project_pixel_training_evidence_request,
)
from .stage_adapters import (
    TRAINING_EVIDENCE_NODE_KINDS,
    TRAINING_PARENT_NODE_KINDS,
    TRAINING_PLAN_REVIEW_NODE_KINDS,
    build_training_stage_adapters,
)
from .native_session import (
    execute_qupath_training_mutation_session,
    open_qupath_training_mutation_session,
)
from .native_projection import (
    QuPathPixelChannelFeature,
    QuPathPixelChannelFeatures,
    QuPathPixelEntryChannelFeatures,
)
from .parent_execution import open_current_training_parent_execution
from .record_publication import (
    ClassifierRecordSetStore,
    PixelClassifierSampleFacts,
    build_pixel_classifier_record_finalization_input,
    build_supervised_classifier_record_finalization_input,
    materialize_qupath_training_review,
    finalize_reviewed_qupath_training_session,
    _reopen_reviewed_training_records,
)
from .runtime import (
    TrainingPlanSetStore,
    build_training_plan_set_execution_result,
    decode_current_committed_training_plan_set,
    open_held_out_evaluation_target,
)
from .plan_selection import (
    TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION,
    classifier_estimator_settings,
    validate_training_plan_selection_response,
    selection_for_training_settings_revision,
)
from .planning import (
    author_training_plan_set_from_selection,
    primary_plan_review_application_inventory,
    project_training_plan_candidate_participants,
    secondary_output_class_inventory,
    supervised_plan_review_options,
    training_plan_candidate_entry_ids,
)
from ...pipeline_core.config import PrimaryClassifierSettings
from mif_pipeline.pipeline_core import StageResult

# ==============================================================================
#                                  Composition
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TrainingCodecDependencies:
    """Retained stores used by the three Task-11 project-stage codecs."""

    state_store: BatchStateStore
    training_plan_set_store: TrainingPlanSetStore
    classifier_record_set_store: ClassifierRecordSetStore

    def __post_init__(self) -> None:
        if (
            type(self.state_store) is not BatchStateStore
            or type(self.training_plan_set_store) is not TrainingPlanSetStore
            or type(self.classifier_record_set_store) is not ClassifierRecordSetStore
        ):
            raise BatchTrainingError("training codec composition requires exact owner dependencies")


@dataclass(frozen=True, slots=True)
class TrainingRuntimeDependencies:
    """Exact public Task-11 runtime-owner inventory."""

    state_store: BatchStateStore
    project_stage: ProjectStageRuntime
    project_provider: ProjectMutationLeaseProvider
    runner: QuPathRunner
    engine_registry: TrainingEngineProviderRegistry
    training_plan_set_store: TrainingPlanSetStore
    classifier_record_set_store: ClassifierRecordSetStore

    def __post_init__(self) -> None:
        if (
            type(self.state_store) is not BatchStateStore
            or type(self.project_stage) is not ProjectStageRuntime
            or type(self.project_provider) is not ProjectMutationLeaseProvider
            or type(self.runner) is not QuPathRunner
            or type(self.engine_registry) is not TrainingEngineProviderRegistry
            or type(self.training_plan_set_store) is not TrainingPlanSetStore
            or type(self.classifier_record_set_store) is not ClassifierRecordSetStore
        ):
            raise BatchTrainingError(
                "training runtime composition requires exact owner dependencies"
            )


@dataclass(frozen=True, slots=True)
class _AggregateTrainingDependencies:
    """Invocation-local facts consumed by the accepted Task-3D executor."""

    state_store: BatchStateStore
    plan_set_store: TrainingPlanSetStore
    project_stage: ProjectStageRuntime
    evidence_codec_bindings: tuple[ProjectStageCodecBinding, ...]
    project_leases: ProjectMutationLeaseProvider
    engine_registry: TrainingEngineProviderRegistry
    fit_runner: QuPathRunner
    evaluation_runner: QuPathRunner
    record_set_store: ClassifierRecordSetStore
    pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...]
    pixel_sample_facts: tuple[PixelClassifierSampleFacts, ...]
    pixel_scale_tolerance_um: float
    registered_script_bundle_sha256: str
    runtime_versions: tuple[tuple[str, str], ...]
    extension_versions: tuple[tuple[str, str], ...]
    model_versions: tuple[tuple[str, str], ...]
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...]

    def __post_init__(self) -> None:
        if (
            type(self.state_store) is not BatchStateStore
            or type(self.plan_set_store) is not TrainingPlanSetStore
            or type(self.project_stage) is not ProjectStageRuntime
            or type(self.project_leases) is not ProjectMutationLeaseProvider
            or type(self.engine_registry) is not TrainingEngineProviderRegistry
            or type(self.fit_runner) is not QuPathRunner
            or self.evaluation_runner is not self.fit_runner
            or type(self.record_set_store) is not ClassifierRecordSetStore
            or type(self.evidence_codec_bindings) is not tuple
            or not all(
                type(item) is ProjectStageCodecBinding for item in self.evidence_codec_bindings
            )
            or type(self.pixel_entry_channel_features) is not tuple
            or not all(
                type(item) is QuPathPixelEntryChannelFeatures
                for item in self.pixel_entry_channel_features
            )
            or type(self.pixel_sample_facts) is not tuple
            or not all(type(item) is PixelClassifierSampleFacts for item in self.pixel_sample_facts)
            or type(self.secondary_structure_specs) is not tuple
            or not all(
                type(item) is SecondaryStructureSpec for item in self.secondary_structure_specs
            )
        ):
            raise BatchTrainingError("aggregate training requires exact invocation dependencies")


_EVIDENCE_ASSET_BY_NODE = MappingProxyType(
    {
        "primary_training_evidence_snapshot": (
            QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value,
            "training_evidence_snapshot",
        ),
        "secondary_training_evidence_snapshot": (
            QuPathScriptAsset.SNAPSHOT_TRAINING_ANNOTATIONS.value,
            "training_evidence_snapshot",
        ),
        "supervised_training_evidence_snapshot": (
            QuPathScriptAsset.SNAPSHOT_SUPERVISED_TRAINING_EVIDENCE.value,
            "supervised_training_evidence_snapshot",
        ),
    }
)


def _evidence_codec_binding(spec: StageAdapterSpec) -> ProjectStageCodecBinding:
    try:
        asset, wire_stage = _EVIDENCE_ASSET_BY_NODE[spec.node_kind]
    except KeyError as exc:
        raise BatchTrainingError("training evidence codec stage is foreign") from exc
    return ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=asset,
        wire_stage_id=wire_stage,
        terminal_stage_id=spec.node_kind,
        request_codec_version="1",
    )


def _training_evidence_owner_inputs(
    context: BatchNodeExecutionContext,
    dependencies: TrainingCodecDependencies,
) -> tuple[
    object,
    tuple[ValidatedAcceptedAnalysisDomainProjection, ...],
    tuple[ValidatedPrimaryClassifierInferenceResultProjection, ...],
    tuple[ValidatedMeasurementsResultProjection, ...],
    tuple[ValidatedPhenotypingModeProjection, ...],
    Mapping[str, object],
]:
    """Reopen only current committed inputs used by one evidence owner."""

    current = dependencies.state_store.authenticate_current_execution_context(context)
    snapshot = dependencies.state_store.reopen_execution_snapshot(
        expected_head_sha256=current.state_head_sha256
    )
    authorities = {
        item.artifact_ref.artifact_ref_sha256: item
        for item in snapshot.committed_artifact_authorities
    }
    selected = []
    for ref in context.inputs:
        authority = authorities.get(ref.artifact_ref_sha256)
        if authority is None:
            raise BatchTrainingError("training evidence input lacks current committed authority")
        selected.append(authority)
    plan_authorities = tuple(
        item for item in selected if item.artifact_ref.artifact_kind == "training_plan_set"
        and bool(item.artifact_ref.owning_identity["classifier_ids"])
    )
    if len(plan_authorities) != 1:
        raise BatchTrainingError("training evidence requires one current training plan set")
    plan_set = decode_current_committed_training_plan_set(
        plan_authorities[0],
        store=dependencies.training_plan_set_store,
        state_store=dependencies.state_store,
    )
    return (
        plan_set,
        tuple(
            decode_committed_accepted_analysis_domain(item)
            for item in selected
            if item.artifact_ref.artifact_kind == "accepted_analysis_domain"
        ),
        tuple(
            decode_committed_primary_classifier_inference_result(item)
            for item in selected
            if item.artifact_ref.artifact_kind == "primary_inference_result"
        ),
        tuple(
            decode_committed_measurements_result(item)
            for item in selected
            if item.artifact_ref.artifact_kind == "measurements_result"
        ),
        tuple(
            decode_committed_phenotyping_mode(item)
            for item in selected
            if item.artifact_ref.artifact_kind == "phenotyping_mode"
        ),
        MappingProxyType(authorities),
        tuple(item for item in selected if item.artifact_ref.artifact_kind == "training_plan_set"
              and not item.artifact_ref.owning_identity["classifier_ids"]),
    )


def _pixel_evidence_request(
    context: BatchNodeExecutionContext,
    intent: ProjectStageTargetIntent,
    *,
    dependencies: TrainingCodecDependencies,
    exchange_reopener: ProjectStageCommittedExchangeReopener,
) -> Mapping[str, object]:
    plan_set, domains, _primary, _measurements, _modes, authorities, _skips = (
        _training_evidence_owner_inputs(context, dependencies)
    )
    classifier_kind = {
        "primary_training_evidence_snapshot": ClassifierKind.PRIMARY_COMPARTMENT,
        "secondary_training_evidence_snapshot": ClassifierKind.SECONDARY_STRUCTURE,
    }.get(context.adapter_spec.node_kind)
    if classifier_kind is None:
        raise BatchTrainingError("pixel evidence codec has a foreign stage")
    plans = tuple(
        plan
        for plan in plan_set.plan_set.plans
        if plan.request.classifier_kind is classifier_kind
        and any(item.project_entry_id == intent.project_entry_id for item in plan.request.entries)
    )
    domain_authorities = {
        item.artifact_ref.artifact_ref_sha256: item
        for item in authorities.values()
        if item.artifact_ref.artifact_kind == "accepted_analysis_domain"
    }
    revisions: dict[object, tuple[object, object]] = {}
    for domain in domains:
        committed = domain_authorities.get(domain.artifact_ref.artifact_ref_sha256)
        if committed is None:
            raise BatchTrainingError("pixel evidence accepted domain is not currently committed")
        tissue_candidates = tuple(
            authorities[digest]
            for digest in domain.source_input_artifact_ref_sha256s
            if digest in authorities
            and authorities[digest].artifact_ref.artifact_kind == "tissue_acceptance_result"
        )
        if len(tissue_candidates) != 1:
            raise BatchTrainingError("pixel evidence lacks one accepted tissue predecessor")
        tissue = reopen_committed_accepted_tissue_live_authority(
            tissue_candidates[0], exchange_reopener=exchange_reopener
        )
        live = reopen_committed_accepted_analysis_domain_live_authority(
            committed,
            accepted_tissue=tissue,
            exchange_reopener=exchange_reopener,
        )
        revision_by_sha = {
            revision.content_sha256: revision
            for manifest in live.manifests
            for revision in manifest.revisions
        }
        for binding in domain.artifact_ref.scope.analysis_domain_bindings:
            active = tuple(
                item
                for item in live.domain_authorities
                if item.analysis_domain_id == binding.analysis_domain_id
            )
            revision = revision_by_sha.get(binding.analysis_domain_revision_sha256)
            if len(active) != 1 or revision is None or binding in revisions:
                raise BatchTrainingError("pixel evidence domain revision coverage is not exact")
            revisions[binding] = (active[0], revision)
    rows = []
    for plan in plans:
        entries = tuple(
            item
            for item in plan.request.entries
            if item.project_entry_id == intent.project_entry_id
        )
        if not entries:
            raise BatchTrainingError("pixel evidence plan entry role coverage is empty")
        classifier_role, labels, parents = project_pixel_training_evidence_request(
            plan, intent.resolved_sample.config
        )
        for entry in entries:
            for binding in plan.request.analysis_domain_bindings:
                if binding.project_entry_id != intent.project_entry_id:
                    continue
                try:
                    active, revision = revisions[binding]
                except KeyError as exc:
                    raise BatchTrainingError(
                        "pixel evidence lacks an authenticated domain revision"
                    ) from exc
                rows.append(
                    {
                        "training_plan_id": plan.training_plan_id,
                        "classifier_role": classifier_role,
                        "section_id": active.section_id,
                        "generation_id": active.tissue_generation_id,
                        "generation_ids": [active.tissue_generation_id],
                        "run_id": intent.pipeline_run_id,
                        "analysis_domain_id": binding.analysis_domain_id,
                        "analysis_domain_revision": binding.analysis_domain_revision,
                        "analysis_domain_revision_sha256": (
                            binding.analysis_domain_revision_sha256
                        ),
                        "analysis_domain_path_object_id": (active.source_path_object_id),
                        "analysis_domain_revision_evidence": revision.to_mapping(),
                        "domain_set_sha256": binding.domain_set_sha256,
                        "split": entry.split,
                        "training_labels": list(labels),
                        "eligible_parent_ids": list(parents),
                        "expected_evidence_sha256s": [],
                    }
                )
    rows.sort(
        key=lambda item: (
            item["training_plan_id"],
            item["classifier_role"],
            item["split"],
            item["analysis_domain_id"],
            item["analysis_domain_revision_sha256"],
        )
    )
    if not rows:
        raise BatchTrainingError("pixel evidence request matrix is empty")
    return {
        "schema_version": "roi-workflow-invocation/v1",
        "sample_id": intent.sample_id,
        "pixel_training_evidence_requests": rows,
    }


def _supervised_evidence_request(
    context: BatchNodeExecutionContext,
    intent: ProjectStageTargetIntent,
    *,
    dependencies: TrainingCodecDependencies,
) -> Mapping[str, object]:
    plan_set, _domains, _primary, measurements, _modes, _authorities, _skips = (
        _training_evidence_owner_inputs(context, dependencies)
    )
    plans = tuple(
        plan
        for plan in plan_set.plan_set.plans
        if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
        and any(item.project_entry_id == intent.project_entry_id for item in plan.request.entries)
    )
    if len(plans) != 1:
        raise BatchTrainingError("supervised evidence requires one exact selected plan")
    plan = plans[0]
    entry = tuple(
        item for item in plan.request.entries if item.project_entry_id == intent.project_entry_id
    )
    if not entry:
        raise BatchTrainingError("supervised evidence requires selected entry roles")
    section_groups: dict[tuple[str, str, str], set[str]] = {}
    for measurement in measurements:
        if measurement.project_entry_id != intent.project_entry_id:
            continue
        key = (
            measurement.section_id,
            measurement.segmentation_generation_id,
            measurement.measurement_generation_id,
        )
        section_groups.setdefault(key, set()).update(
            item.analysis_domain_id for item in measurement.analysis_domain_bindings
        )
    feature_requirements = plan.request.feature_requirements or {}
    radius = feature_requirements.get("point_selection_radius_level0")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
        raise BatchTrainingError("supervised plan lacks a positive point-selection radius")
    sections = [
        {
            "section_id": key[0],
            "segmentation_generation_id": key[1],
            "measurement_generation_id": key[2],
            "analysis_domain_ids": sorted(domain_ids),
        }
        for key, domain_ids in sorted(section_groups.items())
    ]
    if not sections:
        raise BatchTrainingError("supervised evidence section inventory is empty")
    from .supervised_features import supervised_snapshot_source
    from .supervised_references import reference_selection_sha256
    source_fields = supervised_snapshot_source(plan.request.model_configuration, feature_requirements.get("feature_schema", ()))
    return {
        "schema_version": "roi-workflow-invocation/v1",
        "sample_id": intent.sample_id,
        "supervised_training_evidence_requests": [
            {
                **source_fields,
                "training_plan_id": plan.training_plan_id,
                "reference_selection_sha256": reference_selection_sha256(plan),
                "split": role.split,
                "sections": sections,
                "class_ids": list(plan.request.required_classes),
                "feature_schema": list(feature_requirements.get("feature_schema", ())),
                "point_selection_radius_level0": float(radius),
            }
            for role in entry
        ],
    }


def build_training_project_stage_codecs(
    *, dependencies: TrainingCodecDependencies
) -> tuple[ProjectStageCodec, ...]:
    """Compose exactly the three accepted evidence codecs."""

    if type(dependencies) is not TrainingCodecDependencies:
        raise BatchTrainingError("training codec factory requires typed dependencies")
    specs = {item.node_kind: item for item in build_training_stage_adapters()}
    codecs = []
    for node_kind in TRAINING_EVIDENCE_NODE_KINDS:
        spec = specs[node_kind]
        binding = _evidence_codec_binding(spec)
        if node_kind == "supervised_training_evidence_snapshot":
            request_projector = lambda context, intent, deps=dependencies: (
                _supervised_evidence_request(context, intent, dependencies=deps)
            )

            def result_validator(evidence, deps=dependencies):
                (
                    plan_set,
                    domains,
                    _primary,
                    measurements,
                    modes,
                    _authorities,
                    primary_skips,
                ) = _training_evidence_owner_inputs(evidence.context, deps)
                opened = open_supervised_training_evidence_entry(
                    plan_set=plan_set,
                    measurements=measurements,
                    phenotyping_modes=modes,
                    accepted_domains=domains,
                    execution_evidence=evidence,
                )
                return build_supervised_training_evidence_execution_result(
                    context=evidence.context, evidence=opened
                )

        else:

            def request_projector(
                context,
                intent,
                *,
                committed_exchange_reopener,
                deps=dependencies,
            ):
                return _pixel_evidence_request(
                    context,
                    intent,
                    dependencies=deps,
                    exchange_reopener=committed_exchange_reopener,
                )

            def result_validator(evidence, deps=dependencies):
                (
                    plan_set,
                    domains,
                    primary,
                    _measurements,
                    _modes,
                    _authorities,
                    primary_skips,
                ) = _training_evidence_owner_inputs(evidence.context, deps)
                opened = open_pixel_training_evidence_set(
                    plan_set=plan_set,
                    accepted_domains=domains,
                    execution_evidence=evidence,
                    primary_results=primary,
                    primary_skips=primary_skips,
                )
                return build_pixel_training_evidence_execution_result(
                    context=evidence.context, evidence=opened
                )

        factory = (
            ProjectStageCodec.for_stage_owner
            if node_kind == "supervised_training_evidence_snapshot"
            else ProjectStageCodec.for_exchange_reopening_stage_owner
        )
        codec = factory(
            binding=binding,
            spec=spec,
            request_projector=request_projector,
            result_validator=result_validator,
        )
        codecs.append(codec)
    return tuple(codecs)


def _training_plan_review_slide_id(context: BatchNodeExecutionContext) -> str:
    slide_ids = tuple(
        sorted({item.scope.slide_id for item in context.inputs if item.scope.slide_id is not None})
    )
    if not slide_ids:
        raise BatchTrainingError("training plan review lacks an authenticated slide identity")
    return slide_ids[0]


def _training_revision_replays_selection(node, *, plan, state_store) -> bool:
    """Separate a newly accepted settings change from retained revision history.

    A settled return reopens the existing full plan selector. Its carried
    settings binding remains audit history; a later fit-settings response has
    a new binding and still follows the existing automatic replay path.
    """
    revision = node.training_settings_revision
    if revision is None:
        return False
    returned = node.settled_review_revision
    if returned is None:
        return True
    predecessor = returned.predecessor_node_id
    previous_sha = plan.predecessor_plan_sha256
    while previous_sha is not None:
        previous = state_store._load_plan_snapshot(previous_sha)
        original = previous.node_by_id.get(predecessor)
        if original is not None:
            return original["training_settings_revision"] != revision.to_mapping()
        previous_sha = previous.predecessor_plan_sha256
    raise BatchTrainingError("The previous classifier setup is unavailable. Restore its saved analysis history.")


def _execute_training_plan_review(
    context: BatchNodeExecutionContext,
    *,
    dependencies: TrainingRuntimeDependencies,
    terminal_committer: BatchTerminalCommitter,
) -> ScheduledNodeResult:
    state_store = dependencies.state_store
    current = state_store.authenticate_current_execution_context(context)
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    dispatch = context.authorization.dispatch_binding
    from ..batch_secondary_definitions import resolve_secondary_structures, secondary_definition_selections

    secondary_selections = (secondary_definition_selections(
        snapshot, state_store=dependencies.state_store, participant_ids=dispatch.participant_ids, node_id=context.node_id,
    ) if context.adapter_spec.node_kind == "secondary_classifier_training_plan_review" else {})
    candidates = training_plan_candidate_entry_ids(
        node_kind=context.adapter_spec.node_kind,
        participant_ids=dispatch.participant_ids,
        project_entry_ids=dispatch.project_entry_ids,
        manifest=snapshot.manifest,
        secondary_selections=secondary_selections,
    )
    input_authorities = tuple(
        state_store.authenticate_available_artifact(item) for item in context.inputs
    )
    validation_domains = {item.scope.validation_domain_id for item in context.inputs}
    if len(validation_domains) != 1 or None in validation_domains:
        raise BatchTrainingError("training plan review inputs lack one validation domain")
    validation_domain_id = next(iter(validation_domains))
    candidate_participants = project_training_plan_candidate_participants(
        manifest=snapshot.manifest,
        project_entry_refs=tuple(
            authority.artifact_ref
            for authority in input_authorities
            if authority.artifact_ref.artifact_kind == "project_catalogue_entry"
        ),
        candidate_entry_ids=candidates,
        participant_ids=dispatch.participant_ids,
        validation_domain_id=validation_domain_id,
    )
    primary_inventory = None
    primary_seeds = {}
    supervised_options = None
    secondary_output_classes = {}
    channel_inventory = {}
    channel_indices_by_candidate = {}
    original_pixel_sizes = {}
    if context.adapter_spec.node_kind == "primary_classifier_training_plan_review":
        primary_inventory = primary_plan_review_application_inventory(
            manifest=snapshot.manifest,
            input_authorities=input_authorities,
        )
        if set(dispatch.project_entry_ids) != {
            item.project_entry_id for item in primary_inventory[0]
        } or set(dispatch.participant_ids) != {item.sample_id for item in primary_inventory[0]}:
            raise BatchTrainingError("primary review application members differ from dispatch")
        samples = {item.sample_id: item for item in snapshot.manifest.enabled_samples}
        if any(
            samples[item.sample_id].validation_domain_id != validation_domain_id
            for item in primary_inventory[0]
        ):
            raise BatchTrainingError("primary review domain differs from admitted participants")
        if any(
            candidate_participants[item.project_entry_id]
            != {
                "sample_id": item.sample_id,
                "subject_id": item.subject_id,
            }
            for item in primary_inventory[0]
        ):
            raise BatchTrainingError("primary review candidate participants differ")
        seed = next(
            (
                PrimaryClassifierSettings(
                    samples[item.sample_id].config.primary_classes,
                    samples[item.sample_id].config.primary_classifier,
                )
                for item in primary_inventory[0]
                if samples[item.sample_id].config.primary_classifier is not None
            ),
            None,
        )
        primary_seeds = {key: None if seed is None else seed.to_mapping() for key in candidates}
    elif context.adapter_spec.node_kind == "supervised_phenotype_training_plan_review":
        supervised_options = supervised_plan_review_options(
            manifest=snapshot.manifest,
            input_authorities=input_authorities,
        )
    elif context.adapter_spec.node_kind == "secondary_classifier_training_plan_review":
        samples = {item.sample_id: item for item in snapshot.manifest.enabled_samples}
        secondary_output_classes = dict(
            secondary_output_class_inventory(
                tuple(samples[item] for item in dispatch.participant_ids),
                tuple(candidates),
                secondary_selections=secondary_selections,
            )
        )
    if context.adapter_spec.node_kind in {
        "primary_classifier_training_plan_review",
        "secondary_classifier_training_plan_review",
    }:
        image_inventory = primary_inventory or primary_plan_review_application_inventory(
            manifest=snapshot.manifest, input_authorities=input_authorities
        )
        # The inventory bound each committed, calibrated level-0 frame to this
        # project entry's image digest, sample, slide and configuration.
        for source in image_inventory[2]:
            entry_id = source.project_catalogue_entry.scope.project_entry_id
            if entry_id not in candidate_participants:
                continue
            frame = source.analysis_frame.owning_identity["spatial_frame"]
            original_pixel_sizes[entry_id] = [frame["x_um_per_px"], frame["y_um_per_px"]]
        rows_by_entry = {item.project_entry_id: item for item in image_inventory[0]}
        for candidate_id, entry_ids in candidates.items():
            panels = [
                [channel.to_mapping() for channel in rows_by_entry[entry_id].channels]
                for entry_id in entry_ids
            ]
            if not panels or any(panel != panels[0] for panel in panels[1:]):
                raise BatchTrainingError(
                    "Classifier input panels differ between the selected subjects"
                )
            selected_panel = panels[0]
            if context.adapter_spec.node_kind == "secondary_classifier_training_plan_review":
                channel_indices_by_candidate[candidate_id] = list(range(len(selected_panel)))
            channel_inventory[candidate_id] = selected_panel
    from ..channel_settings import (saved_nuclear_channel_id, saved_prepared_ome,
        kronos_intensity_divisor, shared_kronos_intensity_divisor)
    from ..batch_analysis_preflight import resolve_analysis_channels
    frames = {ref.scope.sample_id: ref for ref in snapshot.accepted_artifacts
              if ref.artifact_kind == "analysis_frame" and ref.scope.sample_id in dispatch.participant_ids}
    nuclear_by_sample = {}
    divisor_by_sample = {}
    for sample in snapshot.manifest.enabled_samples:
        if sample.sample_id not in frames:
            continue
        channels = resolve_analysis_channels(sample.config, frames[sample.sample_id],
            sample_id=sample.sample_id, validation_domain_id=sample.validation_domain_id)
        prepared_ome = saved_prepared_ome(snapshot,
            sample_id=sample.sample_id, frame_ref=frames[sample.sample_id])
        nuclear_by_sample[sample.sample_id] = saved_nuclear_channel_id(snapshot,
            sample_id=sample.sample_id, frame_ref=frames[sample.sample_id], channels=channels,
            prepared_ome=prepared_ome)
        divisor_by_sample[sample.sample_id] = kronos_intensity_divisor(prepared_ome)
    nuclear_by_candidate = {}
    divisor_by_candidate = {}
    for candidate_id, entries in candidates.items():
        divisor = shared_kronos_intensity_divisor(
            divisor_by_sample.get(candidate_participants[entry]["sample_id"])
            for entry in entries)
        if divisor is not None:
            divisor_by_candidate[candidate_id] = divisor
        offered = (supervised_options.get("available_channels", ()) if supervised_options is not None
                   else channel_inventory[candidate_id])
        offered_ids = {row["channel_id"] for row in offered}
        choices = {choice if choice in offered_ids else None for entry in entries
            for choice in (nuclear_by_sample.get(candidate_participants[entry]["sample_id"]),)}
        if len(choices) > 1:
            raise BatchTrainingError("Choose the same nuclear stain in Channels and markers for images fitted together")
        choice = next(iter(choices))
        if choice is not None:
            nuclear_by_candidate[candidate_id] = choice
    if supervised_options is not None and nuclear_by_candidate:
        supervised_options = {**supervised_options,
            "nuclear_channel_id": nuclear_by_candidate["supervised-phenotype"]}
    authorized = state_store.load_authorized_interaction_response(context)
    slide_id = _training_plan_review_slide_id(context)
    if (context.adapter_spec.node_kind == "supervised_phenotype_training_plan_review"
            and authorized is not None
            and authorized.action == InteractionAction.ANNOTATE_SUPERVISED_TRAINING.value):
        from .supervised_references import reopen_reference_plan, validate_reference_completion

        checked = validate_authorized_training_interaction_response(
            authorized, expected_context=context
        )
        previous = state_store.load_attempt(
            checked.accepted_response["prior_terminal_record_sha256"]
        )
        reference_context = previous.stage_result.resume_context
        validate_reference_completion(checked.response, reference_context)
        plan_set = reopen_reference_plan(
            reference_context, store=dependencies.training_plan_set_store
        )
        # The ordinary plan publisher binds the retained settings to this
        # current attempt and its selected scientific inputs before fitting.
        execution_result = build_training_plan_set_execution_result(
            context, plan_set=plan_set, store=dependencies.training_plan_set_store
        )
        return terminal_committer.commit_raw((BatchRawNodeOutcome(
            context=context,
            stage_result=StageResult.completed(
                stage=context.adapter_spec.node_kind, slide_id=slide_id,
                generation_id=plan_set.training_plan_set_sha256,
            ),
            execution_result=execution_result,
            execution_exchange_sha256=None,
        ),))[0]
    node = next(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
    revision = node.training_settings_revision
    replay_revision = _training_revision_replays_selection(node, plan=snapshot.plan, state_store=state_store)
    predecessor_set = None
    initial_saved_selection = None
    if (authorized is None and not replay_revision and node.settled_review_revision is None
            and primary_inventory is not None):
        from .plan_selection import TrainingPlanSelectionResponse, TRAINING_PLAN_SELECTION_SCHEMA_VERSION
        for declaration in snapshot.manifest.manifest.reusable_artifacts:
            if (declaration.artifact_kind != "classifier_record_set"
                    or declaration.owning_identity.get("classifier_kind") != "primary_compartment"
                    or declaration.validation_domain_id != validation_domain_id):
                continue
            configured_targets = declaration.target_sample_ids or tuple(sorted(
                sample.sample_id for sample in snapshot.manifest.enabled_samples
                if sample.config.primary_classifier is not None
                and sample.config.primary_classifier.mode.value == "saved"
                and sample.config.primary_classifier.artifact_path.absolute() == declaration.locator.absolute()))
            if tuple(sorted(dispatch.participant_ids)) == configured_targets:
                if initial_saved_selection is not None:
                    raise BatchTrainingError("Choose one saved classifier for these images")
                initial_saved_selection = TrainingPlanSelectionResponse(TRAINING_PLAN_SELECTION_SCHEMA_VERSION, (),
                    {"export_path": str(declaration.locator), "export_sha256": declaration.sha256})
    if replay_revision:
        if authorized is not None:
            raise BatchTrainingError(
                "A revised training plan cannot accept another plan-selection response."
            )
        authorization = state_store.load_authorization(revision.response_authorization_sha256)
        response = state_store.load_validated_interaction_response(authorization)
        if (
            response is None
            or response["action"] != "retrain_classifier"
            or response["response_sha256"] != revision.accepted_response_sha256
            or response["response"].get("fit_review_sha256") != revision.predecessor_review_sha256
        ):
            raise BatchTrainingError("The saved classifier settings change cannot be confirmed.")
        attempt_ids = snapshot.head.attempt_ids_by_node.get(
            revision.predecessor_plan_review_node_id, ()
        )
        if not attempt_ids:
            raise BatchTrainingError("The previous classifier plan is unavailable.")
        previous_attempt = state_store.load_attempt(
            snapshot.head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]]
        )
        refs = tuple(
            item
            for item in previous_attempt.output_artifacts
            if item.artifact_kind == "training_plan_set"
        )
        if len(refs) != 1 or previous_attempt.node_id != revision.predecessor_plan_review_node_id:
            raise BatchTrainingError(
                "The previous classifier plan does not match this settings change."
            )
        identity = refs[0].owning_identity
        if (
            identity["training_plan_set_sha256"] != revision.predecessor_training_plan_set_sha256
            or identity["store_identity_sha256"]
            != dependencies.training_plan_set_store.store_identity_sha256
        ):
            raise BatchTrainingError(
                "The previous classifier plan belongs to another saved analysis."
            )
        predecessor_set = dependencies.training_plan_set_store.reopen(
            identity["store_relative_locator"],
            sha256=identity["body_sha256"],
            byte_count=identity["body_byte_count"],
        )
        if (
            predecessor_set.training_plan_set_sha256
            != revision.predecessor_training_plan_set_sha256
        ):
            raise BatchTrainingError("The previous classifier settings have changed.")
        measurement_features = None
        if supervised_options is not None:
            measurement = next(
                item
                for item in input_authorities
                if item.artifact_ref.artifact_kind == "measurements_result"
            )
            measurement_features = decode_committed_measurements_result(
                measurement
            ).measurement_plan.features
        selection = selection_for_training_settings_revision(
            predecessor_set,
            response["response"],
            candidate_entry_ids=candidates,
            supervised_options=supervised_options,
            measurement_features=measurement_features,
        )
    elif authorized is None and initial_saved_selection is not None:
        selection = initial_saved_selection
    elif authorized is None:
        outcome = BatchRawNodeOutcome(
            context=context,
            stage_result=StageResult.needs_interaction(
                stage=context.adapter_spec.node_kind,
                slide_id=slide_id,
                reason=InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED,
                message=(
                    "Choose the classifier and subject-level evaluation plan. Reference annotations "
                    "are collected across the included images before model fitting begins."
                ),
                allowed_actions=(InteractionAction.SELECT_TRAINING_PLAN.value,),
                resume_context={
                    "schema_version": TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION,
                    "candidate_entry_ids": {key: list(value) for key, value in candidates.items()},
                    "candidate_participants": {
                        entry_id: dict(participant)
                        for entry_id, participant in candidate_participants.items()
                    },
                    "validation_domain_id": validation_domain_id,
                    "primary_settings_by_candidate_id": primary_seeds,
                    "supervised_options": supervised_options,
                    "secondary_output_classes_by_candidate_id": secondary_output_classes,
                    "available_channels_by_candidate_id": channel_inventory,
                    **({"original_pixel_size_um_by_entry_id": original_pixel_sizes}
                       if original_pixel_sizes else {}),
                    **({"channel_indices_by_candidate_id": channel_indices_by_candidate}
                       if channel_indices_by_candidate else {}),
                    **({"kronos_intensity_divisor_by_candidate_id": divisor_by_candidate}
                       if divisor_by_candidate else {}),
                    **({"nuclear_channel_by_candidate_id": nuclear_by_candidate}
                       if nuclear_by_candidate and supervised_options is None else {}),
                    "application_participants": (
                        [
                            {
                                **item.to_mapping(),
                                "analysis_domain_bindings": [
                                    binding.to_mapping()
                                    for binding in primary_inventory[1]
                                    if binding.project_entry_id == item.project_entry_id
                                ],
                            }
                            for item in primary_inventory[0]
                        ]
                        if primary_inventory is not None
                        else []
                    ),
                },
                checkpoint_id=(
                    "training-plan-selection-" + context.authorization.authorization_sha256[:24]
                ),
            ),
            execution_result=BatchNodeExecutionResult(
                context.authorization.authorization_sha256, ()
            ),
            execution_exchange_sha256=None,
        )
        return terminal_committer.commit_raw((outcome,))[0]
    if not replay_revision and initial_saved_selection is None:
        checked = validate_authorized_training_interaction_response(
            authorized, expected_context=context
        )
        if checked.action != InteractionAction.SELECT_TRAINING_PLAN.value:
            raise BatchTrainingError("training plan retry carries a foreign interaction action")
        selection = validate_training_plan_selection_response(
            checked.response,
            candidate_entry_ids=candidates,
            supervised=(supervised_options is not None),
            allow_skip_primary=(context.adapter_spec.node_kind == "primary_classifier_training_plan_review"),
        )
    if selection.skip_primary or selection.saved_classifier is not None:
        if primary_inventory is None:
            raise BatchTrainingError("This choice requires primary tissue classification setup")
        from .planning import SkippedPrimarySelection, TrainingPlanSet
        if selection.skip_primary:
            selected = SkippedPrimarySelection(
                batch_id=snapshot.head.batch_id, application_entries=primary_inventory[0],
                analysis_domain_bindings=primary_inventory[1],
                source_artifact_ref_sha256s=tuple(sorted(item.artifact_ref_sha256 for item in context.inputs)))
            plan_set = TrainingPlanSet.from_primary_skip(selected,
                normalized_manifest_sha256=snapshot.manifest.normalized_manifest_sha256,
                validation_domain_id=validation_domain_id)
        else:
            from pathlib import Path
            from .accepted_model_reuse import retain_selected_primary_export
            selected = retain_selected_primary_export(
                path=Path(selection.saved_classifier["export_path"]),
                expected_sha256=selection.saved_classifier["export_sha256"],
                workspace_root=state_store.layout.root,
                application_entries=primary_inventory[0], analysis_domain_bindings=primary_inventory[1],
                batch_id=snapshot.head.batch_id, input_refs=context.inputs)
            plan_set = TrainingPlanSet.from_saved_selection(selected,
                normalized_manifest_sha256=snapshot.manifest.normalized_manifest_sha256)
        return terminal_committer.commit_raw((BatchRawNodeOutcome(context=context,
            stage_result=StageResult.completed(stage=context.adapter_spec.node_kind, slide_id=slide_id,
                generation_id=plan_set.training_plan_set_sha256),
            execution_result=build_training_plan_set_execution_result(context, plan_set=plan_set,
                store=dependencies.training_plan_set_store), execution_exchange_sha256=None),))[0]
    if context.adapter_spec.node_kind in {
        "primary_classifier_training_plan_review",
        "secondary_classifier_training_plan_review",
    }:
        from ..annotation_preview import validate_feedback_roles

        validate_feedback_roles(
            state_store.layout.root,
            selection.to_mapping(),
            {
                "candidate_participants": candidate_participants,
                "validation_domain_id": validation_domain_id,
            },
        )
    reference_predecessor_set = None
    if context.adapter_spec.node_kind == "supervised_phenotype_training_plan_review":
        returned = node.settled_review_revision
        if predecessor_set is not None:
            # Settings replay must preserve an already inherited identity, but
            # must not add metadata to an unchanged original selection.
            if any(item.request.inherited_reference_selection_sha256 is not None
                   for item in predecessor_set.plans):
                reference_predecessor_set = predecessor_set
        elif returned is not None:
            previous = state_store.load_attempt(returned.predecessor_terminal_record_sha256)
            refs = tuple(item for item in previous.output_artifacts
                         if item.artifact_kind == "training_plan_set")
            if previous.node_id != returned.predecessor_node_id or len(refs) != 1:
                raise BatchTrainingError("The preceding reference selection is unavailable.")
            identity = refs[0].owning_identity
            if identity["store_identity_sha256"] != dependencies.training_plan_set_store.store_identity_sha256:
                raise BatchTrainingError("The preceding references belong to another analysis.")
            reference_predecessor_set = dependencies.training_plan_set_store.reopen(
                identity["store_relative_locator"], sha256=identity["body_sha256"],
                byte_count=identity["body_byte_count"])
            if reference_predecessor_set.training_plan_set_sha256 != identity["training_plan_set_sha256"]:
                raise BatchTrainingError("The preceding reference selection identity changed.")
    plan_set = author_training_plan_set_from_selection(
        node_kind=context.adapter_spec.node_kind,
        manifest=snapshot.manifest,
        input_authorities=input_authorities,
        selection=selection,
        secondary_selections=secondary_selections,
        supervised_reference_predecessor=reference_predecessor_set,
    )
    if predecessor_set is not None:
        old_by_id = {item.request.classifier_id: item for item in predecessor_set.plans}
        changes = response["response"].get("parameters_by_classifier_id", {})
        output_changes = response["response"].get("output_policy_by_classifier_id", {})
        if set(old_by_id) != {item.request.classifier_id for item in plan_set.plans}:
            raise BatchTrainingError("The classifier set changed while revising settings.")
        for item in plan_set.plans:
            old = old_by_id[item.request.classifier_id]
            configuration = dict(old.request.model_configuration)
            expected_settings = old.request.primary_settings
            expected_classes = old.request.required_classes
            if item.request.classifier_id in output_changes:
                from .plan_selection import apply_primary_ignore_policy
                from .primary_policy import primary_training_required_classes

                choice = output_changes[item.request.classifier_id]
                expected_settings, configuration = apply_primary_ignore_policy(
                    expected_settings, configuration, policy=choice["policy"],
                    boundary_thickness_px=choice["boundary_thickness_px"],
                )
                expected_classes = primary_training_required_classes(expected_settings, configuration)
            if item.request.classifier_id in changes:
                schema = configuration["configuration_schema"]
                estimator = classifier_estimator_settings(configuration)
                key = next(name for name in ("svm", "rtrees", "logistic_regression") if name in estimator)
                if schema in {"qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2"}:
                    configuration["native_configuration"] = {
                        **configuration["native_configuration"],
                        key: changes[item.request.classifier_id],
                    }
                else:
                    configuration[key] = changes[item.request.classifier_id]
            if (
                item.request != replace(
                    old.request, model_configuration=configuration,
                    primary_settings=expected_settings, required_classes=expected_classes,
                )
                or item.authoring_source_artifact_ref_sha256s
                != old.authoring_source_artifact_ref_sha256s
            ):
                raise BatchTrainingError(
                    "The patient roles, image settings or training inputs changed. Reopen the classifier setup."
                )
        plan_set = type(plan_set).create(plan_set.plans, settings_revision=revision)
    elif revision is not None:
        # Preserve the accepted history and its validation-exposure flag without
        # replaying old parameters over the newly reviewed plan selection.
        plan_set = type(plan_set).create(plan_set.plans, settings_revision=revision)
    if context.adapter_spec.node_kind == "supervised_phenotype_training_plan_review":
        from .supervised_references import reference_review_context, validate_saved_reference_coverage

        # Keep the plan unavailable to snapshot/fitting while its ordinary
        # annotation tickets collect labelled points across the chosen roles.
        reference_context = reference_review_context(
            plan_set, store=dependencies.training_plan_set_store,
            measurements=tuple(
                decode_committed_measurements_result(item)
                for item in input_authorities
                if item.artifact_ref.artifact_kind == "measurements_result"
            ),
        )
        if predecessor_set is not None or all(
                item.request.inherited_reference_selection_sha256 is not None for item in plan_set.plans):
            # The settings-only comparison above preserves every reference
            # selection dependency. Recheck its saved cell bindings through the
            # ordinary Freeze reader, then publish the new model plan directly.
            validate_saved_reference_coverage(reference_context, store=state_store,
                head=snapshot.head, runner=dependencies.runner)
        else:
            return terminal_committer.commit_raw((BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.needs_interaction(
                    stage=context.adapter_spec.node_kind, slide_id=slide_id,
                    reason=InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED,
                    message="Label reference cells in the selected images, then freeze the references to fit this classifier.",
                    allowed_actions=(InteractionAction.ANNOTATE_SUPERVISED_TRAINING.value,),
                    resume_context=reference_context,
                    checkpoint_id="supervised-references-" + plan_set.training_plan_set_sha256,
                ),
                execution_result=BatchNodeExecutionResult(context.authorization.authorization_sha256, ()),
                execution_exchange_sha256=None,
            ),))[0]
    execution_result = build_training_plan_set_execution_result(
        context,
        plan_set=plan_set,
        store=dependencies.training_plan_set_store,
    )
    outcome = BatchRawNodeOutcome(
        context=context,
        stage_result=StageResult.completed(
            stage=context.adapter_spec.node_kind,
            slide_id=slide_id,
            generation_id=plan_set.training_plan_set_sha256,
        ),
        execution_result=execution_result,
        execution_exchange_sha256=None,
    )
    return terminal_committer.commit_raw((outcome,))[0]


@dataclass(frozen=True, slots=True)
class _TrainingPlanReviewRuntimeAdapter:
    spec: StageAdapterSpec
    dependencies: TrainingRuntimeDependencies = field(repr=False, compare=False)

    def execute_committed(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> ScheduledNodeResult:
        del workspace_lease
        if context.adapter_spec is not self.spec:
            raise BatchTrainingError("training plan review received a foreign specification")
        return _execute_training_plan_review(
            context,
            dependencies=self.dependencies,
            terminal_committer=terminal_committer,
        )


@dataclass(frozen=True, slots=True)
class _TrainingEvidenceRuntimeAdapter:
    spec: StageAdapterSpec
    plan: BatchPlan
    resolved_samples: tuple[ResolvedBatchSample, ...]
    dependencies: TrainingRuntimeDependencies = field(repr=False, compare=False)

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
            raise BatchTrainingError("training evidence runtime requires one exact context group")
        state_store = self.dependencies.state_store
        head = state_store.load_head()
        snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
        sample_by_id = {item.sample_id: item for item in self.resolved_samples}
        current_by_id = {item.sample_id: item for item in snapshot.manifest.enabled_samples}
        if sample_by_id != current_by_id:
            raise BatchTrainingError("training evidence samples differ from the current manifest")
        catalogue = state_store.project_catalogue_store(workspace_lease).load_current()
        if catalogue is None:
            raise BatchTrainingError("training evidence runtime lacks a project catalogue")
        current_saved_state_by_entry_id = (
            state_store.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=head.sha256,
                lease=workspace_lease,
            )
        )
        node_by_id = {item.node_id: item for item in snapshot.plan.nodes}
        entry_by_id = {item.project_entry_id: item for item in catalogue.entries}
        targets = []
        for context in values:
            participants = context.authorization.dispatch_binding.participant_ids
            entry_ids = context.authorization.dispatch_binding.project_entry_ids
            if (
                len(participants) != 1
                or participants[0] not in sample_by_id
                or len(entry_ids) != 1
                or entry_ids[0] not in entry_by_id
            ):
                raise BatchTrainingError("training evidence dispatch lacks one exact entry")
            sample = sample_by_id[participants[0]]
            entry = entry_by_id[entry_ids[0]]
            node = node_by_id.get(context.node_id)
            if (
                node is None
                or node.kind != self.spec.node_kind
                or node.adapter_spec_sha256 != self.spec.spec_sha256
                or node.participant_ids != participants
                or node.project_entry_ids != entry_ids
                or entry.sample_id != sample.sample_id
                or entry.slide_id != sample.slide_id
                or entry.image_role != "primary_mif"
            ):
                raise BatchTrainingError("training evidence context differs from its current plan")
            targets.append(
                ProjectStageTargetIntent(
                    project_entry_id=entry.project_entry_id,
                    sample_id=sample.sample_id,
                    slide_id=sample.slide_id,
                    # Training evidence participates in the same stable
                    # scientific run as the image-stage artifacts.
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
        targets_tuple = tuple(sorted(targets, key=lambda item: item.project_entry_id))
        target_entry_ids = {item.project_entry_id for item in targets_tuple}
        all_entry_ids = {item.project_entry_id for item in catalogue.entries}
        input_sets = tuple(
            {item.artifact_ref_sha256 for item in context.inputs} for context in values
        )
        shared = tuple(sorted(set.intersection(*input_sets)))
        authority_layout = state_store.authority_layout_sha256
        if authority_layout is None:
            raise BatchTrainingError("training evidence runtime lacks authority-layout identity")
        asset, _wire_stage = _EVIDENCE_ASSET_BY_NODE[self.spec.node_kind]
        intent = ProjectStageRunIntent(
            authority_layout_sha256=authority_layout,
            batch_id=head.batch_id,
            run_plan_id=(
                "training-"
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
            target_script_asset_id=asset,
            guard_script_asset_id=QuPathScriptAsset.BATCH_STAGE_GUARD.value,
            targets=targets_tuple,
            excluded_project_entry_ids=tuple(sorted(all_entry_ids - target_entry_ids)),
            shared_artifact_ref_sha256s=shared,
            launch_record_wait_millis=30_000,
        )
        prepared = prepare_project_stage_run_request(
            intent,
            values,
            self.dependencies.project_stage,
            workspace_lease=workspace_lease,
        )
        return run_project_stage_headless(
            prepared,
            self.dependencies.project_stage,
            workspace_lease,
            terminal_committer,
        )


@dataclass(frozen=True, slots=True)
class _TrainingParentRuntimeAdapter:
    spec: StageAdapterSpec
    dependencies: TrainingRuntimeDependencies = field(repr=False, compare=False)

    def execute_committed(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease,
        terminal_committer: BatchTerminalCommitter,
    ) -> ScheduledNodeResult:
        if context.adapter_spec is not self.spec:
            raise BatchTrainingError("training parent received a foreign specification")
        invocation = _aggregate_training_dependencies(
            context,
            workspace_lease=workspace_lease,
            dependencies=self.dependencies,
        )
        return execute_training_parent(
            context,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
            dependencies=invocation,
        )


def build_training_runtime_adapters(
    *,
    plan: BatchPlan,
    resolved_samples: tuple[ResolvedBatchSample, ...],
    dependencies: TrainingRuntimeDependencies,
) -> tuple[Task15RuntimeAdapterBinding, ...]:
    """Compose the exact nine accepted runtime routes."""

    if (
        type(plan) is not BatchPlan
        or type(dependencies) is not TrainingRuntimeDependencies
        or type(resolved_samples) is not tuple
        or not all(type(item) is ResolvedBatchSample for item in resolved_samples)
        or len({item.sample_id for item in resolved_samples}) != len(resolved_samples)
    ):
        raise BatchTrainingError("training runtime factory requires typed plan and samples")
    result = []
    for spec in build_training_stage_adapters():
        if spec.node_kind in TRAINING_PLAN_REVIEW_NODE_KINDS:
            route = "owner_committed"
            adapter = _TrainingPlanReviewRuntimeAdapter(spec, dependencies)
        elif spec.node_kind in TRAINING_EVIDENCE_NODE_KINDS:
            route = "project_current_image"
            adapter = _TrainingEvidenceRuntimeAdapter(spec, plan, resolved_samples, dependencies)
        elif spec.node_kind in TRAINING_PARENT_NODE_KINDS:
            route = "owner_committed"
            adapter = _TrainingParentRuntimeAdapter(spec, dependencies)
        else:  # pragma: no cover - closed inventory guard
            raise BatchTrainingError("training stage inventory is foreign")
        result.append(Task15RuntimeAdapterBinding(spec, route, adapter))
    if len(result) != 9:
        raise BatchTrainingError("training runtime inventory is incomplete")
    return tuple(result)


def _training_plan_interaction_validator(
    *, dependencies: TrainingRuntimeDependencies, stage: str
) -> Callable[[BatchInteractionTarget, str, Mapping[str, object]], BatchTargetResponseValidation]:
    validator_id = "mif-training-plan-selection-response"

    def validate(
        target: BatchInteractionTarget,
        action: str,
        response: Mapping[str, object],
    ) -> BatchTargetResponseValidation:
        result = target.stage_result
        context = result.resume_context or {}
        if (
            result.stage != stage
            or result.interaction_reason is not InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED
            or action != InteractionAction.SELECT_TRAINING_PLAN.value
            or context.get("schema_version") != TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION
        ):
            raise BatchTrainingError("training plan selection target or action is foreign")
        raw_candidates = context.get("candidate_entry_ids")
        if not isinstance(raw_candidates, Mapping):
            raise BatchTrainingError("training plan selection request lacks candidates")
        candidates = {
            str(key): tuple(str(item) for item in value)
            for key, value in raw_candidates.items()
            if isinstance(value, (tuple, list))
        }
        if set(candidates) != set(raw_candidates):
            raise BatchTrainingError("training plan selection candidate inventory is invalid")
        checked = validate_training_plan_selection_response(
            response,
            candidate_entry_ids=candidates,
            supervised=(stage == "supervised_phenotype_training_plan_review"),
            allow_skip_primary=(stage == "primary_classifier_training_plan_review"),
        )
        state_store = dependencies.state_store
        if checked.saved_classifier is not None:
            if stage != "primary_classifier_training_plan_review":
                raise BatchTrainingError("Saved model selection requires primary tissue classification")
            from pathlib import Path
            from .accepted_model_reuse import read_native_classifier_export
            material = read_native_classifier_export(Path(checked.saved_classifier["export_path"]))
            if (material.sha256 != checked.saved_classifier["export_sha256"]
                    or material.primary_settings is None
                    or material.record_set.validation_domain_id != target.validation_domain_id):
                raise BatchTrainingError("The saved model changed or belongs to a different validation group")
            for row in context["application_participants"]:
                from mif_pipeline.pipeline_core.config import ChannelSpec
                material.primary_settings.validate_channels(tuple(
                    ChannelSpec.from_mapping(item, field="saved model application channel") for item in row["channels"]))
        from ..annotation_preview import validate_feedback_roles

        if not checked.skip_primary and checked.saved_classifier is None and stage in {
            "primary_classifier_training_plan_review",
            "secondary_classifier_training_plan_review",
        }:
            validate_feedback_roles(state_store.layout.root, checked.to_mapping(), context)
        head = state_store.load_head()
        authorization_request = state_store.reopen_interaction_base_authorization_request(
            target.interaction_request_id,
            expected_head_sha256=head.sha256,
        )
        return BatchTargetResponseValidation(
            target_id=target.target_id,
            node_id=target.node_id,
            status="accepted",
            response=checked.to_mapping(),
            authorization_request=authorization_request,
            response_validator_id=validator_id,
            response_validator_version=INTERACTION_ACTION_SCHEMA_VERSION,
            rejection_code=None,
        )

    return validate


def reopen_current_training_fit_review(
    target: BatchInteractionTarget,
    *,
    dependencies: TrainingRuntimeDependencies,
    expected_state_head_sha256: str | None = None,
) -> tuple[Mapping[str, object], ClassifierRecordSet | None, BatchAttemptAuthorizationRequest, TrainingPlanSet]:
    """Reopen the displayed result and its plans; this read grants no approval."""
    from .record_publication import (
        validate_training_fit_review_context,
        _project_pending_training_review_summary,
        validate_training_review_freeze_lineage,
    )

    result = target.stage_result
    if (
        result.stage not in TRAINING_PARENT_NODE_KINDS
        or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
    ):
        raise BatchTrainingError("This request is not a saved classifier review.")
    checked = validate_training_fit_review_context(result.resume_context)
    expected_actions = (
        ("retrain_classifier",)
        if checked["phase"] == "configure_outputs"
        else ("accept", "retrain_classifier")
        if checked["phase"] == "freeze_settings"
        and not any(r.get("model_selection") for r in checked["classifiers"])
        else ("accept",)
    )
    if result.allowed_actions != expected_actions:
        raise BatchTrainingError("This classifier review has unsupported actions.")
    if result.checkpoint_id != "training-fit-review-" + checked["fit_review_sha256"]:
        raise BatchTrainingError("The saved classifier has changed. Reopen Reviews to continue.")
    state_store = dependencies.state_store
    head = state_store.load_head()
    if expected_state_head_sha256 is not None and head.sha256 != expected_state_head_sha256:
        raise BatchTrainingError(
            "The analysis has changed. Reopen Reviews to see the current results."
        )
    publication = state_store.reopen_interaction_request_publication(
        target.interaction_request_id,
        expected_head_sha256=head.sha256,
    )
    state_store.authenticate_interaction_target(publication, head.sha256)
    if (
        state_store.load_interaction_request(publication.interaction_request_id) != result
        or publication.node_id != target.node_id
        or publication.attempt_id != target.attempt_id
        or publication.attempt_authorization_sha256 != target.attempt_authorization_sha256
        or publication.terminal_record_sha256 != target.needs_interaction_terminal_record_sha256
        or publication.interaction_context_sha256 != target.interaction_context_sha256
        or publication.sample_id != target.sample_id
        or publication.slide_id != target.slide_id
        or publication.project_entry_id != target.project_entry_id
        or publication.validation_domain_id != target.validation_domain_id
        or publication.analysis_domain_bindings != target.analysis_domain_bindings
        or publication.compatibility_signature_sha256 != target.compatibility_signature_sha256
    ):
        raise BatchTrainingError(
            "classifier review differs from the current authenticated publication"
        )
    authorization_request = state_store.reopen_interaction_base_authorization_request(
        target.interaction_request_id,
        expected_head_sha256=head.sha256,
    )
    plan_refs = tuple(
        ref
        for ref in authorization_request.input_artifacts
        if ref.artifact_kind == "training_plan_set"
    )
    if len(plan_refs) != 1:
        raise BatchTrainingError("classifier review lacks its exact current plan")
    plan_set = decode_current_committed_training_plan_set(
        state_store.authenticate_available_artifact(plan_refs[0]),
        store=dependencies.training_plan_set_store,
        state_store=state_store,
    ).plan_set
    if checked["phase"] == "configure_outputs":
        from .output_review import build_output_policy_review

        if (
            target.node_id in head.pending_training_fit_set_sha256_by_node
            or build_output_policy_review(plan_set) != checked
            or state_store.load_head().sha256 != head.sha256
        ):
            raise BatchTrainingError("The output review differs from the current unfitted plan.")
        return checked, None, authorization_request, plan_set
    if (
        head.pending_training_fit_set_sha256_by_node.get(target.node_id)
        != checked["checkpoint_set_sha256"]
    ):
        raise BatchTrainingError("classifier review differs from the selected native fit")
    pending = dependencies.classifier_record_set_store.reopen(
        checked["pending_classifier_record_set_sha256"]
    )
    regenerated = _project_pending_training_review_summary(
        plan_set,
        pending,
        checkpoint_set_sha256=checked["checkpoint_set_sha256"],
        training_observations_by_classifier={
            row["classifier_id"]: row["training_observations"]
            for row in checked["classifiers"] if "training_observations" in row
        },
        model_selection_by_classifier={
            row["classifier_id"]: row["model_selection"]
            for row in checked["classifiers"]
            if row.get("model_selection") is not None
        },
    )
    if regenerated != checked or state_store.load_head().sha256 != head.sha256:
        raise BatchTrainingError("classifier review summary differs from current retained results")
    validate_training_review_freeze_lineage(
        state_store=state_store,
        producing_attempt_sha256=publication.terminal_record_sha256,
        review=checked,
        pending_record_set=pending,
    )
    return checked, pending, authorization_request, plan_set


def _training_fit_interaction_validator(
    *, dependencies: TrainingRuntimeDependencies, stage: str
) -> Callable[[BatchInteractionTarget, str, Mapping[str, object]], BatchTargetResponseValidation]:
    def validate(
        target: BatchInteractionTarget,
        action: str,
        response: Mapping[str, object],
    ) -> BatchTargetResponseValidation:
        from .record_publication import (
            validate_training_fit_review_context,
        )

        result = target.stage_result
        if (
            result.stage != stage
            or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
            or action not in ("accept", "retrain_classifier")
        ):
            raise BatchTrainingError("The response does not match this classifier review.")
        checked = validate_training_fit_review_context(result.resume_context)
        if checked["phase"] == "configure_outputs":
            from .output_review import validate_output_policy_change

            if action != "retrain_classifier":
                raise BatchTrainingError("Save the selected output policy before fitting.")
            checked, _, authorization_request, _ = reopen_current_training_fit_review(
                target, dependencies=dependencies,
            )
            return BatchTargetResponseValidation(
                target_id=target.target_id, node_id=target.node_id, status="accepted",
                response=validate_output_policy_change(response, review=checked),
                authorization_request=authorization_request,
                response_validator_id="mif-training-fit-review-response",
                response_validator_version=INTERACTION_ACTION_SCHEMA_VERSION,
                rejection_code=None,
            )
        expected_actions = (
            ("accept", "retrain_classifier")
            if checked["phase"] == "freeze_settings"
            and not any(r.get("model_selection") for r in checked["classifiers"])
            else ("accept",)
        )
        expected_response = {"fit_review_sha256": checked["fit_review_sha256"]}
        if (
            result.checkpoint_id != "training-fit-review-" + checked["fit_review_sha256"]
            or result.allowed_actions != expected_actions
            or action not in expected_actions
            or (action == "accept" and dict(response) != expected_response)
        ):
            raise BatchTrainingError(
                "The saved classifier has changed. Review the current result before continuing."
            )
        _, _, authorization_request, _ = reopen_current_training_fit_review(
            target,
            dependencies=dependencies,
        )
        if action == "retrain_classifier":
            from .plan_selection import validate_training_settings_change_response

            ref = next(
                item
                for item in authorization_request.input_artifacts
                if item.artifact_kind == "training_plan_set"
            )
            plan_set = decode_current_committed_training_plan_set(
                dependencies.state_store.authenticate_available_artifact(ref),
                store=dependencies.training_plan_set_store,
                state_store=dependencies.state_store,
            ).plan_set
            expected_response = validate_training_settings_change_response(
                response,
                expected_review_sha256=checked["fit_review_sha256"],
                model_configurations={
                    item.request.classifier_id: item.request.model_configuration
                    for item in plan_set.plans
                },
            )
        return BatchTargetResponseValidation(
            target_id=target.target_id,
            node_id=target.node_id,
            status="accepted",
            response=expected_response,
            authorization_request=authorization_request,
            response_validator_id="mif-training-fit-review-response",
            response_validator_version=INTERACTION_ACTION_SCHEMA_VERSION,
            rejection_code=None,
        )

    return validate


def _supervised_reference_interaction_validator(*, dependencies):
    """Freeze the displayed immutable plan through the current response issuer."""
    from .supervised_references import (
        reopen_reference_plan, validate_reference_completion, validate_saved_reference_coverage,
    )

    def validate(target, action, response):
        result = target.stage_result
        if (result.stage != "supervised_phenotype_training_plan_review"
                or result.interaction_reason is not InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED
                or action != InteractionAction.ANNOTATE_SUPERVISED_TRAINING.value):
            raise BatchTrainingError("Reopen the current supervised reference review.")
        reopen_reference_plan(result.resume_context, store=dependencies.training_plan_set_store)
        checked = validate_reference_completion(response, result.resume_context)
        store = dependencies.state_store
        head = store.load_head()
        validate_saved_reference_coverage(result.resume_context, store=store, head=head,
                                          runner=dependencies.runner)
        request = store.reopen_interaction_base_authorization_request(
            target.interaction_request_id, expected_head_sha256=head.sha256
        )
        return BatchTargetResponseValidation(
            target_id=target.target_id, node_id=target.node_id, status="accepted",
            response=checked, authorization_request=request,
            response_validator_id="mif-supervised-reference-completion",
            response_validator_version=INTERACTION_ACTION_SCHEMA_VERSION,
            rejection_code=None,
        )
    return validate


def registered_training_interaction_owner_keys() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            [
                (
                    stage,
                    InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED.value,
                    INTERACTION_ACTION_SCHEMA_VERSION,
                )
                for stage in TRAINING_PLAN_REVIEW_NODE_KINDS
            ]
            + [
                (
                    stage,
                    InteractionReason.OTHER_EXPLICIT_REASON.value,
                    INTERACTION_ACTION_SCHEMA_VERSION,
                )
                for stage in TRAINING_PARENT_NODE_KINDS
            ]
            + [("supervised_phenotype_training_plan_review",
                InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED.value,
                INTERACTION_ACTION_SCHEMA_VERSION)]
        )
    )


def build_training_interaction_owner_bindings(
    *, dependencies: TrainingRuntimeDependencies
) -> tuple[BatchInteractionOwnerBinding, ...]:
    """Bind plan selection and saved-fit acceptance to their exact stages."""

    if type(dependencies) is not TrainingRuntimeDependencies:
        raise BatchTrainingError("training interaction factory requires typed dependencies")
    plan_bindings = tuple(
        BatchInteractionOwnerBinding(
            stage=stage,
            interaction_reason=(InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED.value),
            action_schema_version=INTERACTION_ACTION_SCHEMA_VERSION,
            validate_target=_training_plan_interaction_validator(
                dependencies=dependencies, stage=stage
            ),
        )
        for stage in TRAINING_PLAN_REVIEW_NODE_KINDS
    )
    fit_bindings = tuple(
        BatchInteractionOwnerBinding(
            stage=stage,
            interaction_reason=InteractionReason.OTHER_EXPLICIT_REASON.value,
            action_schema_version=INTERACTION_ACTION_SCHEMA_VERSION,
            validate_target=_training_fit_interaction_validator(
                dependencies=dependencies,
                stage=stage,
            ),
        )
        for stage in TRAINING_PARENT_NODE_KINDS
    )
    reference_binding = BatchInteractionOwnerBinding(
        stage="supervised_phenotype_training_plan_review",
        interaction_reason=InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED.value,
        action_schema_version=INTERACTION_ACTION_SCHEMA_VERSION,
        validate_target=_supervised_reference_interaction_validator(dependencies=dependencies),
    )
    return plan_bindings + fit_bindings + (reference_binding,)


def _canonical_secondary_structure_specs(
    samples: tuple[ResolvedBatchSample, ...],
    secondary_selections: Mapping[str, object],
) -> tuple[SecondaryStructureSpec, ...]:
    from ..batch_secondary_definitions import resolve_secondary_structures

    from mif_pipeline.pipeline_core.config import secondary_classifier_groups
    by_structure: dict[str, SecondaryStructureSpec] = {}
    for sample in samples:
        for structure in resolve_secondary_structures(sample, secondary_selections.get(sample.sample_id)):
            previous = by_structure.setdefault(structure.structure_id, structure)
            if previous != structure:
                raise BatchTrainingError("aggregate training has conflicting secondary specifications")
    result = tuple(by_structure.values())
    secondary_classifier_groups(result)
    return result



def _aggregate_training_dependencies(
    context: BatchNodeExecutionContext,
    *,
    workspace_lease: BatchWorkspaceLease,
    dependencies: TrainingRuntimeDependencies,
) -> _AggregateTrainingDependencies:
    """Reopen attempt-local scientific facts from retained owner authorities."""
    from ..batch_secondary_definitions import secondary_definition_selections

    current = dependencies.state_store.authenticate_current_execution_context(context)
    snapshot = dependencies.state_store.reopen_execution_snapshot(
        expected_head_sha256=current.state_head_sha256
    )
    participant_ids = set(current.participant_ids)
    project_entry_ids = set(current.project_entry_ids)
    from ..batch_analysis_preflight import (
        BatchAnalysisPreflightError,
        resolve_analysis_channels,
        validate_analysis_frame_artifact_ref,
    )
    from ..batch_project import reopen_project_catalogue_entry_artifact

    frame_refs_by_sample = {
        ref.scope.sample_id: ref
        for ref in snapshot.accepted_artifacts
        if ref.artifact_kind == "analysis_frame" and ref.scope.sample_id in participant_ids
    }
    frames_by_sample = {
        sample_id: validate_analysis_frame_artifact_ref(ref)
        for sample_id, ref in frame_refs_by_sample.items()
    }
    entries_by_sample = {}
    for ref in snapshot.accepted_artifacts:
        if ref.artifact_kind != "project_catalogue_entry":
            continue
        _catalogue, entry = reopen_project_catalogue_entry_artifact(ref)
        if entry.image_role == "primary_mif" and entry.project_entry_id in project_entry_ids:
            entries_by_sample[entry.sample_id] = entry
    channel_facts = []
    sample_facts = []
    qupath_versions = {frame.qupath_executable_version for frame in frames_by_sample.values()}
    if len(qupath_versions) != 1:
        raise BatchTrainingError("aggregate training requires one authenticated QuPath version")
    script_bundle_sha256 = registered_script_bundle_sha256()
    selected_samples = tuple(
        item for item in snapshot.manifest.enabled_samples if item.sample_id in participant_ids
    )
    if {item.sample_id for item in selected_samples} != participant_ids:
        raise BatchTrainingError("aggregate training participants differ from current manifest")
    catalogue_store = dependencies.state_store.project_catalogue_store(workspace_lease)
    for sample in selected_samples:
        entry = entries_by_sample.get(sample.sample_id)
        frame = frames_by_sample.get(sample.sample_id)
        if entry is None or frame is None:
            raise BatchTrainingError(
                "aggregate training lacks exact project-entry or frame coverage"
            )
        try:
            channels = resolve_analysis_channels(
                sample.config,
                frame_refs_by_sample[sample.sample_id],
                sample_id=sample.sample_id,
                validation_domain_id=sample.validation_domain_id,
            )
        except BatchAnalysisPreflightError as exc:
            raise BatchTrainingError("aggregate training analysis channels are invalid") from exc
        live_channels = reopen_current_project_entry_channel_observation(
            state_store=dependencies.state_store,
            expected_head_sha256=current.state_head_sha256,
            catalogue_store=catalogue_store,
            project_entry_id=entry.project_entry_id,
        )
        channel_facts.append(
            _project_observed_pixel_channel_features(
                project_entry_id=entry.project_entry_id,
                configured_channels=channels,
                observed_channel_names=live_channels.ordered_channel_names,
            )
        )
        spatial = frame.spatial_frame
        if spatial.x_um_per_px != spatial.y_um_per_px:
            raise BatchTrainingError(
                "Tissue classifier training requires equal X and Y pixel sizes"
            )
        sample_facts.append(
            PixelClassifierSampleFacts(
                entry.project_entry_id,
                spatial.x_um_per_px,
            )
        )
    if {item.project_entry_id for item in entries_by_sample.values()} != project_entry_ids:
        raise BatchTrainingError(
            "aggregate training project entries lack exact participant coverage"
        )
    specs = {item.node_kind: item for item in build_training_stage_adapters()}
    return _AggregateTrainingDependencies(
        state_store=dependencies.state_store,
        plan_set_store=dependencies.training_plan_set_store,
        project_stage=dependencies.project_stage,
        evidence_codec_bindings=tuple(
            _evidence_codec_binding(specs[kind]) for kind in TRAINING_EVIDENCE_NODE_KINDS
        ),
        project_leases=dependencies.project_provider,
        engine_registry=dependencies.engine_registry,
        fit_runner=dependencies.runner,
        evaluation_runner=dependencies.runner,
        record_set_store=dependencies.classifier_record_set_store,
        pixel_entry_channel_features=tuple(channel_facts),
        pixel_sample_facts=tuple(sample_facts),
        pixel_scale_tolerance_um=0.0,
        registered_script_bundle_sha256=script_bundle_sha256,
        runtime_versions=(
            ("qupath", next(iter(qupath_versions))),
            ("script_bundle_sha256", script_bundle_sha256),
        ),
        extension_versions=(),
        model_versions=(),
        secondary_structure_specs=(
            _canonical_secondary_structure_specs(selected_samples, secondary_definition_selections(
                snapshot, state_store=dependencies.state_store, participant_ids=participant_ids, node_id=context.node_id,
            )) if current.node_kind.startswith("secondary_") else ()
        ),
    )


def _codec_for_child(dependencies: _AggregateTrainingDependencies, child: object):
    bindings = tuple(dependencies.evidence_codec_bindings)
    if len(bindings) == 1:
        return bindings[0]
    matches = tuple(
        item
        for item in bindings
        if getattr(item, "terminal_stage_id", None) == getattr(child, "producer_node_kind", None)
    )
    if len(matches) != 1:
        raise BatchTrainingError("aggregate training lacks one exact evidence codec binding")
    return matches[0]


def _current_training_projection_inputs(
    parent: object,
    *,
    state_store: BatchStateStore,
) -> tuple[
    tuple[ValidatedAcceptedAnalysisDomainProjection, ...],
    tuple[ValidatedPrimaryClassifierInferenceResultProjection, ...],
    tuple[ValidatedMeasurementsResultProjection, ...],
    tuple[ValidatedPhenotypingModeProjection, ...],
    tuple[object, ...],
]:
    """Reissue state-bound scientific projections at the parent's current head."""

    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=parent.state_head_sha256)
    project_entry_ids = set(parent.project_entry_ids)
    authorities = tuple(
        item
        for item in snapshot.committed_artifact_authorities
        if item.artifact_ref.scope.project_entry_id in project_entry_ids
    )
    return (
        tuple(
            decode_committed_accepted_analysis_domain(item)
            for item in authorities
            if item.artifact_ref.artifact_kind == "accepted_analysis_domain"
        ),
        tuple(
            decode_committed_primary_classifier_inference_result(item)
            for item in authorities
            if item.artifact_ref.artifact_kind == "primary_inference_result"
        ),
        tuple(
            decode_committed_measurements_result(item)
            for item in authorities
            if item.artifact_ref.artifact_kind == "measurements_result"
        ),
        tuple(
            decode_committed_phenotyping_mode(item)
            for item in authorities
            if item.artifact_ref.artifact_kind == "phenotyping_mode"
        ),
        tuple(item for item in snapshot.committed_artifact_authorities
              if item.artifact_ref.artifact_kind == "training_plan_set"
              and not item.artifact_ref.owning_identity["classifier_ids"]
              and any(binding.project_entry_id in project_entry_ids
                      for binding in item.artifact_ref.scope.analysis_domain_bindings)),
    )


def _project_observed_pixel_channel_features(
    *,
    project_entry_id: str,
    configured_channels: tuple[object, ...],
    observed_channel_names: tuple[str, ...],
) -> QuPathPixelEntryChannelFeatures:
    """Bind canonical IDs to exact names and indices observed by live QuPath."""

    configured = tuple(configured_channels)
    observed = tuple(observed_channel_names)
    canonical_ids = tuple(getattr(item, "channel_id", None) for item in configured)
    if (
        not configured
        or len(observed) != len(configured)
        or any(type(item) is not str or not item for item in canonical_ids)
        or len(set(canonical_ids)) != len(canonical_ids)
        or any(type(item) is not str or not item for item in observed)
    ):
        raise BatchTrainingError("live QuPath channel positions lack an exact canonical mapping")
    return QuPathPixelEntryChannelFeatures(
        project_entry_id,
        QuPathPixelChannelFeatures(
            "qupath-pixel-channel-features/v1",
            tuple(
                QuPathPixelChannelFeature(channel_id, index, observed_name)
                for index, (channel_id, observed_name) in enumerate(
                    zip(canonical_ids, observed, strict=True)
                )
            ),
        ),
    )


def _training_projection_inputs_for_child(
    parent: object,
    child: object,
    current: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    """Scope current state-issued projections to one authenticated child."""

    ref = getattr(child, "artifact_ref", None)
    scope = getattr(ref, "scope", None)
    entry_id = getattr(scope, "project_entry_id", None)
    parent_entry_ids = tuple(getattr(parent, "project_entry_ids", ()))
    if (
        type(entry_id) is not str
        or not entry_id
        or entry_id not in parent_entry_ids
        or len(current) != 5
    ):
        raise BatchTrainingError("training evidence child project entry is missing or foreign")
    return tuple(
        tuple(item for item in values if item.artifact_ref.scope.project_entry_id == entry_id
              or (index == 4 and any(binding.project_entry_id == entry_id
                                     for binding in item.artifact_ref.scope.analysis_domain_bindings)))
        for index, values in enumerate(current)
    )


def _required_pixel_channel_ids(plan: object) -> frozenset[str]:
    requirements = plan.request.channel_requirements
    if not isinstance(requirements, Mapping):
        raise BatchTrainingError("pixel plan lacks channel requirements")
    roles = requirements.get("channel_roles")
    if not isinstance(roles, Mapping) or not roles:
        raise BatchTrainingError("pixel plan lacks exact channel roles")
    channel_ids = frozenset(roles.values())
    if not channel_ids or any(type(item) is not str or not item for item in channel_ids):
        raise BatchTrainingError("pixel plan channel roles are ambiguous")
    return channel_ids


def _select_pixel_entry_features(
    dependencies: _AggregateTrainingDependencies,
    plan: object,
    entry_ids: tuple[str, ...],
) -> tuple[QuPathPixelEntryChannelFeatures, ...]:
    by_entry = {item.project_entry_id: item for item in dependencies.pixel_entry_channel_features}
    if set(entry_ids) - set(by_entry):
        raise BatchTrainingError("aggregate training lacks exact pixel channel facts")
    required = _required_pixel_channel_ids(plan)
    selected = []
    for entry_id in entry_ids:
        source = by_entry[entry_id]
        channels = tuple(
            item
            for item in source.channel_features.ordered_channels
            if item.canonical_channel_id in required
        )
        if {item.canonical_channel_id for item in channels} != required:
            raise BatchTrainingError("aggregate training lacks exact required live channels")
        selected.append(
            QuPathPixelEntryChannelFeatures(
                entry_id,
                QuPathPixelChannelFeatures("qupath-pixel-channel-features/v1", channels),
            )
        )
    return tuple(selected)


def _pixel_features_for_plan(
    dependencies: _AggregateTrainingDependencies,
    plan: object,
    *,
    training: bool,
) -> tuple[QuPathPixelEntryChannelFeatures, ...]:
    entry_ids = tuple(
        sorted(
            item.project_entry_id
            for item in plan.request.entries
            if (item.split == "training") is training
        )
    )
    return _select_pixel_entry_features(dependencies, plan, entry_ids)


def _pixel_finalization_facts_for_plan(
    dependencies: _AggregateTrainingDependencies,
    plan: object,
) -> tuple[
    tuple[PixelClassifierSampleFacts, ...],
    tuple[QuPathPixelEntryChannelFeatures, ...],
]:
    entry_ids = tuple(sorted({item.project_entry_id for item in plan.request.entries}))
    if not entry_ids:
        raise BatchTrainingError("aggregate training plan entry facts are not canonical")

    def select_exact(values, field):
        by_entry = {}
        for item in values:
            entry_id = item.project_entry_id
            if entry_id in by_entry:
                raise BatchTrainingError(f"aggregate training has duplicate {field} facts")
            by_entry[entry_id] = item
        if set(entry_ids) - set(by_entry):
            raise BatchTrainingError(f"aggregate training lacks exact {field} facts")
        return tuple(by_entry[item] for item in entry_ids)

    return (
        select_exact(dependencies.pixel_sample_facts, "pixel sample"),
        _select_pixel_entry_features(dependencies, plan, entry_ids),
    )


def _open_session(
    *,
    context: BatchNodeExecutionContext,
    parent: object,
    fit_authority: object,
    workspace_lease: BatchWorkspaceLease,
    terminal_committer: BatchTerminalCommitter,
    dependencies: _AggregateTrainingDependencies,
    pixel_features: tuple[QuPathPixelEntryChannelFeatures, ...],
):
    return open_qupath_training_mutation_session(
        state_store=dependencies.state_store,
        context=context,
        current_parent=parent,
        workspace_lease=workspace_lease,
        checkpoint_writer=dependencies.state_store.native_fit_checkpoint_writer(
            context, workspace_lease
        ),
        terminal_committer=terminal_committer,
        project_leases=dependencies.project_leases,
        engine_registry=dependencies.engine_registry,
        fit_authority=fit_authority,
        plan_set_store=dependencies.plan_set_store,
        runner=dependencies.fit_runner,
        pixel_entry_channel_features=pixel_features,
    )


def execute_training_parent(
    context: BatchNodeExecutionContext,
    *,
    workspace_lease: BatchWorkspaceLease,
    terminal_committer: BatchTerminalCommitter,
    dependencies: _AggregateTrainingDependencies,
) -> ScheduledNodeResult:
    """Run missing fits and publish one complete classifier-record set."""

    if type(dependencies) is not _AggregateTrainingDependencies:
        raise BatchTrainingError("aggregate training requires its exact dependency bundle")
    parent = open_current_training_parent_execution(
        context,
        state_store=dependencies.state_store,
        plan_set_store=dependencies.plan_set_store,
    )
    plan_set = parent.training_plan_set
    plans = tuple(plan_set.plan_set.plans)
    if not plans:
        raise BatchTrainingError("aggregate training has no selected plans")

    from .output_review import build_output_policy_review

    output_review = build_output_policy_review(plan_set.plan_set)
    if output_review is not None:
        head = dependencies.state_store.load_head()
        if context.node_id in head.pending_training_fit_set_sha256_by_node:
            raise BatchTrainingError("Output policy cannot change after a native fit was retained.")
        outcome = BatchRawNodeOutcome(
            context=context,
            stage_result=StageResult.needs_interaction(
                stage=context.adapter_spec.node_kind,
                slide_id=_training_plan_review_slide_id(context),
                reason=InteractionReason.OTHER_EXPLICIT_REASON,
                message="Choose how excluded areas should be handled before fitting the classifier.",
                allowed_actions=("retrain_classifier",),
                resume_context=output_review,
                checkpoint_id="training-fit-review-" + output_review["fit_review_sha256"],
            ),
            execution_result=BatchNodeExecutionResult(context.authorization.authorization_sha256, ()),
            execution_exchange_sha256=None,
        )
        return terminal_committer.commit_raw((outcome,))[0]

    authorized = dependencies.state_store.load_authorized_interaction_response(context)
    reviewed = False
    if authorized is not None:
        checked = validate_authorized_training_interaction_response(
            authorized, expected_context=context
        )
        reviewed = checked.checkpoint_id.startswith("training-fit-review-")
        if reviewed and checked.action != "accept":
            raise BatchTrainingError(
                "Apply the confirmed settings change before starting the next classifier fit."
            )

    complete_by_plan = {}

    def partition(current_parent, plan):
        current_plan_set = current_parent.training_plan_set
        exchange_reopener = dependencies.project_stage.committed_exchange_reopener(
            workspace_lease=workspace_lease
        )
        accepted_domains, primary_results, measurements, phenotyping_modes, primary_skips = (
            _current_training_projection_inputs(
                current_parent,
                state_store=dependencies.state_store,
            )
        )
        if current_parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            reopened = tuple(
                reopen_supervised_training_evidence_entry(
                    child=child,
                    exchange_reopener=exchange_reopener,
                    codec_binding=_codec_for_child(dependencies, child),
                    plan_set=current_plan_set,
                    accepted_domains=child_inputs[0],
                    measurements=child_inputs[2],
                    phenotyping_modes=child_inputs[3],
                )
                for child in current_parent.evidence_child_authorities
                for child_inputs in (
                    _training_projection_inputs_for_child(
                        current_parent,
                        child,
                        (
                            accepted_domains,
                            primary_results,
                            measurements,
                            phenotyping_modes,
                            primary_skips,
                        ),
                    ),
                )
            )
            supervised_sources = tuple(
                source
                for entry in reopened
                for source in _project_supervised_training_evidence_sources(entry)
            )
            complete, fit, held_out = partition_supervised_training_evidence(
                parent_execution=current_parent,
                expected_context=context,
                state_store=dependencies.state_store,
                sources=supervised_sources,
            )
        else:
            pixel_entries = tuple(
                reopen_pixel_training_evidence_set(
                    child=child,
                    exchange_reopener=exchange_reopener,
                    codec_binding=_codec_for_child(dependencies, child),
                    plan_set=current_plan_set,
                    accepted_domains=child_inputs[0],
                    primary_results=child_inputs[1],
                    primary_skips=child_inputs[4],
                )
                for child in current_parent.evidence_child_authorities
                for child_inputs in (
                    _training_projection_inputs_for_child(
                        current_parent,
                        child,
                        (
                            accepted_domains,
                            primary_results,
                            measurements,
                            phenotyping_modes,
                            primary_skips,
                        ),
                    ),
                )
            )
            complete = assemble_pixel_training_evidence_for_plan(
                plan_set=current_plan_set,
                training_plan_id=plan.training_plan_id,
                entry_evidence=pixel_entries,
            )
            fit, held_out = partition_pixel_training_evidence(
                parent_execution=current_parent,
                expected_context=context,
                state_store=dependencies.state_store,
                evidence=complete,
            )
        complete_by_plan[plan.training_plan_id] = complete
        return fit, held_out

    while True:
        parent = open_current_training_parent_execution(
            context,
            state_store=dependencies.state_store,
            plan_set_store=dependencies.plan_set_store,
        )
        current_fit = dependencies.state_store.reopen_current_native_fit_set(
            context,
            current_parent=parent,
            engine_registry=dependencies.engine_registry,
        )
        fitted = (
            frozenset()
            if current_fit is None
            else frozenset(item.classifier_id for item in current_fit.checkpoint_set.entries)
        )
        missing = tuple(plan for plan in plans if plan.request.classifier_id not in fitted)
        if not missing:
            break
        if reviewed:
            raise BatchTrainingError("reviewed native fit is no longer complete")
        plan = missing[0]
        try:
            fit, _held_out = partition(parent, plan)
        except MissingPixelTrainingClassesError as exc:
            # This is an actionable reference-selection failure before native
            # fitting begins. Retire its authorization so ordinary setup review
            # can correct the references without leaving an unfinished attempt.
            outcome = BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.failed(
                    stage=context.adapter_spec.node_kind,
                    slide_id=_training_plan_review_slide_id(context),
                    error_code="training_reference_classes_missing",
                    error_message=str(exc),
                ),
                execution_result=BatchNodeExecutionResult(
                    context.authorization.authorization_sha256, (),
                ),
                execution_exchange_sha256=None,
            )
            return terminal_committer.commit_raw((outcome,))[0]
        pixel_features = (
            ()
            if parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
            else _pixel_features_for_plan(dependencies, plan, training=True)
        )
        with _open_session(
            context=context,
            parent=parent,
            fit_authority=fit,
            workspace_lease=workspace_lease,
            terminal_committer=terminal_committer,
            dependencies=dependencies,
            pixel_features=pixel_features,
        ) as session:
            result = execute_qupath_training_mutation_session(session)
        if type(result) is ScheduledNodeResult:
            return result

    parent = open_current_training_parent_execution(
        context,
        state_store=dependencies.state_store,
        plan_set_store=dependencies.plan_set_store,
    )
    partitions = tuple((plan, *partition(parent, plan)) for plan in plans)
    first_plan, first_fit, _first_held_out = partitions[0]
    finalizable_features = (
        ()
        if parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
        else _pixel_features_for_plan(dependencies, first_plan, training=True)
    )
    with _open_session(
        context=context,
        parent=parent,
        fit_authority=first_fit,
        workspace_lease=workspace_lease,
        terminal_committer=terminal_committer,
        dependencies=dependencies,
        pixel_features=finalizable_features,
    ) as session:
        current_fit = execute_qupath_training_mutation_session(session)
        if type(current_fit) is ScheduledNodeResult:
            return current_fit
        retained_records = {}
        freeze_review_sha256 = None
        if reviewed:
            review, retained, _ = _reopen_reviewed_training_records(
                session,
                current_fit_set=current_fit,
                complete_training_evidence=tuple(
                    complete_by_plan[plan.training_plan_id] for plan in plans
                ),
                record_set_store=dependencies.record_set_store,
                secondary_structure_specs=dependencies.secondary_structure_specs,
            )
            if review["phase"] == "final_results":
                return finalize_reviewed_qupath_training_session(
                    session,
                    current_fit_set=current_fit,
                    complete_training_evidence=tuple(
                        complete_by_plan[plan.training_plan_id] for plan in plans
                    ),
                    record_set_store=dependencies.record_set_store,
                    secondary_structure_specs=dependencies.secondary_structure_specs,
                )
            freeze_review_sha256 = review["fit_review_sha256"]
            retained_records = {
                record.training_plan_id: record for record in retained.classifier_records
            }
        finalization_inputs = []
        for plan, _fit, held_out in partitions:
            training_only = plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY
            if training_only != (held_out is None):
                raise BatchTrainingError(
                    "held-out partition disagrees with accepted evaluation intent"
                )
            target = evaluation = None
            evaluation_split = "test" if reviewed else "validation"
            if plan.split_project_entry_ids[evaluation_split]:
                target = open_held_out_evaluation_target(
                    current_parent=parent,
                    context=context,
                    state_store=dependencies.state_store,
                    engine_registry=dependencies.engine_registry,
                    held_out_authority=held_out,
                    evaluation_split=evaluation_split,
                )
                evaluation = evaluate_held_out_native_classifier(
                    target,
                    project_provider=dependencies.project_leases,
                    runner=dependencies.evaluation_runner,
                    pixel_entry_channel_features=(
                        ()
                        if parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
                        else _pixel_features_for_plan(dependencies, plan, training=False)
                    ),
                )
            retained = retained_records.get(plan.training_plan_id)
            # A saved model keeps the runtime provenance of its fit. The
            # read-only evaluation records its current script independently.
            fit_versions = (retained.runtime_versions if retained is not None
                            else dependencies.runtime_versions)
            common = dict(
                plan=plan,
                complete_training_evidence=complete_by_plan[plan.training_plan_id],
                current_fit_set=current_fit,
                held_out_target=target,
                held_out_evaluation=evaluation,
                retained_record=retained,
                freeze_review_sha256=freeze_review_sha256,
                registered_script_bundle_sha256=(dict(fit_versions)["script_bundle_sha256"]
                    if retained is not None else dependencies.registered_script_bundle_sha256),
                runtime_versions=fit_versions,
                extension_versions=(retained.extension_versions if retained is not None
                                    else dependencies.extension_versions),
                model_versions=(retained.model_versions if retained is not None
                                else dependencies.model_versions),
            )
            if parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
                item = build_supervised_classifier_record_finalization_input(**common)
            else:
                sample_facts, channel_facts = _pixel_finalization_facts_for_plan(dependencies, plan)
                item = build_pixel_classifier_record_finalization_input(
                    **common,
                    sample_facts=sample_facts,
                    pixel_entry_channel_features=channel_facts,
                    pixel_scale_tolerance_um=(dependencies.pixel_scale_tolerance_um),
                    secondary_structure_specs=(dependencies.secondary_structure_specs),
                )
            finalization_inputs.append(item)
        return materialize_qupath_training_review(
            session,
            current_fit_set=current_fit,
            finalization_inputs=tuple(finalization_inputs),
            record_set_store=dependencies.record_set_store,
            secondary_structure_specs=dependencies.secondary_structure_specs,
        )


__all__ = [
    "registered_training_interaction_owner_keys",
    "TrainingCodecDependencies",
    "TrainingRuntimeDependencies",
    "build_training_interaction_owner_bindings",
    "build_training_project_stage_codecs",
    "build_training_runtime_adapters",
    "execute_training_parent",
]
