"""Private retained owner composition for the workspace batch service.

The `BatchRuntimeDependencies` record carries `runtime`, `training`, `image`, `phenotyping`,
`preprocessing_response_resolver` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import MappingProxyType

from mif_containment.client import ContainmentCompatibilityExpectationV1
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.qupath_runtime.runner import LinuxContainmentProfile
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding

from .batch_artifacts import StageAdapterRegistry
from .batch_dag import BatchPlan
from .batch_interactions import BatchInteractionOwnerRegistry
from .batch_manifest import ResolvedBatchSample, ValidatedBatchManifest
from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs
from .batch_phenotyping import (
    PhenotypingCodecDependencies,
    PhenotypingRuntimeDependencies,
    SupervisedNativePredictionStore,
    build_phenotyping_interaction_owner_bindings,
    build_phenotyping_project_stage_codecs,
    build_phenotyping_runtime_adapters,
)
from .batch_preprocessing_adapters import build_preprocessing_stage_adapters
from .batch_preprocessing_interactions import (
    build_preprocessing_interaction_owner_bindings,
)
from .batch_preprocessing_wire import (
    AuthorizedInteractionResponseResolver,
    PreprocessingJobRunner,
    SpawnPreprocessingJobRunner,
)
from .batch_runtime import (
    BatchProjectRuntime, _runtime_workspace_manifest, open_batch_project_runtime,
)
from .batch_service_registry import (
    Task15RuntimeAdapterBinding,
    Task15RuntimeStageRegistry,
)
from .batch_stage_adapters import (
    ImageCodecDependencies,
    ImageRuntimeDependencies,
    build_image_interaction_owner_bindings,
    build_image_project_stage_codecs,
    build_image_stage_adapters,
)
from .batch_state import BatchWorkspaceLayout
from .batch_training import (
    ClassifierRecordSetStore,
    NativeClassifierApplicationAuthority,
    TrainingCodecDependencies,
    TrainingPlanSetStore,
    TrainingRuntimeDependencies,
    build_prototype_training_engine_provider_registry,
    build_training_interaction_owner_bindings,
    build_training_project_stage_codecs,
    build_training_runtime_adapters,
    provision_classifier_record_set_store_root,
    provision_training_plan_set_store_root,
)
from .phenotype_views import PhenotypeViewStore
from .project_stage_codecs import ProjectStageCodec, ProjectStageCodecRegistry
from .project_stage_owner_capabilities import (
    create_project_stage_owner_capability_seam,
)

# ==============================================================================
#                           Batch service dependencies
# ==============================================================================


class BatchServiceDependencyError(RuntimeError):
    """The retained application owner graph is incomplete or foreign."""


def registered_batch_interaction_owner_inventory() -> tuple[tuple[str, str, str], ...]:
    """Return immutable keys from the same owners that bind runtime validators."""
    from .batch_preprocessing_interactions import registered_preprocessing_interaction_owner_keys
    from .batch_training.composition import registered_training_interaction_owner_keys
    from .batch_stage_adapters import registered_image_interaction_owner_keys
    from .batch_phenotyping.composition import registered_phenotyping_interaction_owner_keys
    from .batch_secondary_definitions import NODE_KIND
    from mif_pipeline.pipeline_core import InteractionReason, INTERACTION_ACTION_SCHEMA_VERSION

    return tuple(
        sorted(
            (
                *registered_preprocessing_interaction_owner_keys(),
                *registered_training_interaction_owner_keys(),
                *registered_image_interaction_owner_keys(),
                *registered_phenotyping_interaction_owner_keys(),
                (NODE_KIND, InteractionReason.OTHER_EXPLICIT_REASON.value, INTERACTION_ACTION_SCHEMA_VERSION),
            )
        )
    )


@dataclass(frozen=True, slots=True)
class _StateInteractionResponseResolver:
    state_store: object

    def __call__(self, context):
        response = self.state_store.load_validated_interaction_response(  # type: ignore[attr-defined]
            context.authorization
        )
        if context.adapter_spec.node_kind == "inspection":
            from .batch_acquisition_organization import inspection_response_dispatch
            if response is None:
                from .batch_inspection_recovery import load_inspection_retry_response
                response = load_inspection_retry_response(self.state_store, context)
            return inspection_response_dispatch(self.state_store, context, response)
        return response


class _BatchRuntimeDependencyPrivate:
    __slots__ = ("_phenotyping_codec_dependencies",)


@dataclass(frozen=True, slots=True)
class BatchRuntimeDependencies(_BatchRuntimeDependencyPrivate):
    """Exact retained owners used by one workspace-scoped service provider."""

    runtime: BatchProjectRuntime
    training: TrainingRuntimeDependencies
    image: ImageRuntimeDependencies
    phenotyping: PhenotypingRuntimeDependencies
    preprocessing_response_resolver: AuthorizedInteractionResponseResolver
    preprocessing_job_runner: PreprocessingJobRunner
    interaction_owners: BatchInteractionOwnerRegistry
    project_codecs_by_spec_sha256: Mapping[str, ProjectStageCodec]

    def __post_init__(self) -> None:
        runtime = self.runtime
        if (
            type(runtime) is not BatchProjectRuntime
            or type(self.training) is not TrainingRuntimeDependencies
            or type(self.image) is not ImageRuntimeDependencies
            or type(self.phenotyping) is not PhenotypingRuntimeDependencies
            or not isinstance(
                self.preprocessing_response_resolver,
                AuthorizedInteractionResponseResolver,
            )
            or not callable(self.preprocessing_job_runner)
            or type(self.interaction_owners) is not BatchInteractionOwnerRegistry
            or self.project_codecs_by_spec_sha256 is not runtime.project_codecs_by_spec_sha256
        ):
            raise BatchServiceDependencyError(
                "batch runtime dependencies require exact retained owners"
            )
        for nested in (self.training, self.image, self.phenotyping):
            if (
                nested.state_store is not runtime.state_store
                or nested.project_stage is not runtime.project_stage
                or nested.project_provider is not runtime.project_provider
                or nested.runner is not runtime.runner
            ):
                raise BatchServiceDependencyError("nested dependencies substituted a Task-9 owner")


def _adapter_spec_set_sha256(registry: StageAdapterRegistry) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {"stage_adapter_spec_sha256s": [item.spec_sha256 for item in registry.specs]}
        )
    ).hexdigest()


def build_batch_stage_registry(
    *,
    plan: BatchPlan,
    resolved_samples: tuple[ResolvedBatchSample, ...],
    dependencies: BatchRuntimeDependencies,
) -> Task15RuntimeStageRegistry:
    """Compose the exact four plus nine plus twelve plus twenty routes."""

    if type(plan) is not BatchPlan or type(dependencies) is not BatchRuntimeDependencies:
        raise BatchServiceDependencyError("batch stage registry requires typed plan dependencies")
    samples = tuple(resolved_samples)
    if type(resolved_samples) is not tuple or not all(
        type(item) is ResolvedBatchSample for item in samples
    ):
        raise BatchServiceDependencyError("batch stage registry requires resolved samples")
    preprocessing = tuple(
        Task15RuntimeAdapterBinding(item.spec, "raw_direct", item)
        for item in build_preprocessing_stage_adapters(
            plan=plan,
            resolved_samples=samples,
            response_resolver=dependencies.preprocessing_response_resolver,
            job_runner=dependencies.preprocessing_job_runner,
        )
    )
    from .batch_secondary_definitions import (
        SecondaryDefinitionRuntimeAdapter, build_secondary_definition_stage_spec,
        plan_has_secondary_definition_choice,
    )
    definition_spec = build_secondary_definition_stage_spec() if plan_has_secondary_definition_choice(plan) else None
    bindings = (
        *preprocessing,
        *build_training_runtime_adapters(
            plan=plan,
            resolved_samples=samples,
            dependencies=dependencies.training,
        ),
        *build_image_stage_adapters(
            plan=plan,
            resolved_samples=samples,
            dependencies=dependencies.image,
        ),
        *build_phenotyping_runtime_adapters(
            plan=plan,
            resolved_samples=samples,
            dependencies=dependencies.phenotyping,
            codec_dependencies=dependencies._phenotyping_codec_dependencies,  # type: ignore[attr-defined]
        ),
        *((Task15RuntimeAdapterBinding(definition_spec, "owner_committed",
            SecondaryDefinitionRuntimeAdapter(definition_spec, dependencies.training)),)
          if definition_spec is not None else ()),
    )
    stage_registry = StageAdapterRegistry(tuple(item.spec for item in bindings))
    if plan.adapter_spec_set_sha256 != _adapter_spec_set_sha256(stage_registry):
        raise BatchServiceDependencyError(
            "The saved analysis and this application's processing steps do not match. "
            "Create a new analysis with the current application build."
        )
    return Task15RuntimeStageRegistry(stage_registry, tuple(bindings))


def _add_cleanup_note(primary: BaseException, label: str, failure: BaseException) -> None:
    try:
        primary.add_note(f"{label} cleanup failed: {failure}")
    except (AttributeError, TypeError):
        pass


def _close_retained(resources: list[tuple[str, object]], primary: BaseException | None) -> None:
    first = primary
    for label, resource in reversed(resources):
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as failure:
            if first is None:
                first = failure
            else:
                _add_cleanup_note(first, label, failure)
    if primary is None and first is not None:
        raise first


@contextmanager
def open_batch_runtime_dependencies(
    *,
    workspace_layout: BatchWorkspaceLayout,
    qupath_executable: Path,
    linux_containment_profile: LinuxContainmentProfile | None,
    containment_expectation: ContainmentCompatibilityExpectationV1 | None,
    launch_binding: QuPathProfileLaunchBinding | None = None,
    cpu_native_root: Path | None = None,
    software_preferences_path: Path | None = None,
    prerequisite_inputs: (
        Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs] | None
    ) = None,
) -> Iterator[BatchRuntimeDependencies]:
    """Open retained stores before and close last."""

    origin = _runtime_workspace_manifest(workspace_layout)
    retained: list[tuple[str, object]] = []
    runtime_manager = None
    runtime_entered = False
    primary: BaseException | None = None
    try:
        # Select one installed format definition while composing the service owners.
        from mif_pipeline.pipeline_core.schema_registry import schema_validation_snapshot

        with schema_validation_snapshot():
            training_root = provision_training_plan_set_store_root(
                workspace_layout.state / "training-plan-sets"
            )
            training_store = TrainingPlanSetStore(training_root)
            retained.append(("training-plan-sets", training_store))
            classifier_store = ClassifierRecordSetStore(
                provision_classifier_record_set_store_root(
                    workspace_layout.state / "classifier-record-sets"
                )
            )
            retained.append(("classifier-record-sets", classifier_store))
            phenotype_store = PhenotypeViewStore(workspace_layout.state / "phenotype-views")
            retained.append(("phenotype-views", phenotype_store))
            prediction_store = SupervisedNativePredictionStore(
                workspace_layout.state / "supervised-native-predictions"
            )
            retained.append(("supervised-native-predictions", prediction_store))

            phase: dict[str, object] = {}
            callback_called = False

            def codec_factory(owners):
                nonlocal callback_called
                if callback_called:
                    raise BatchServiceDependencyError(
                        "Task-9 codec callback was invoked more than once"
                    )
                callback_called = True
                authority = NativeClassifierApplicationAuthority(
                    owners.state_store, owners.project_provider
                )
                publisher, resolver = create_project_stage_owner_capability_seam()
                training_codec = TrainingCodecDependencies(
                    owners.state_store, training_store, classifier_store
                )
                image_codec = ImageCodecDependencies(
                    owners.state_store,
                    owners.project_provider,
                    classifier_store,
                    authority,
                    owners.project_operations,
                    training_plan_set_store=training_store,
                    prerequisite_inputs=prerequisite_inputs,
                )
                phenotyping_codec = PhenotypingCodecDependencies(
                    owners.state_store,
                    classifier_store,
                    phenotype_store,
                    prediction_store,
                    authority,
                    resolver,
                )
                phase.update(
                    authority=authority,
                    publisher=publisher,
                    phenotyping_codec=phenotyping_codec,
                )
                codecs = (
                    *build_training_project_stage_codecs(dependencies=training_codec),
                    *build_image_project_stage_codecs(dependencies=image_codec),
                    *build_phenotyping_project_stage_codecs(dependencies=phenotyping_codec),
                )
                return ProjectStageCodecRegistry.for_application_composition(codecs)

            runtime_manager = open_batch_project_runtime(
                workspace_layout=workspace_layout,
                qupath_executable=qupath_executable,
                project_stage_codec_registry_factory=codec_factory,
                _workspace_origin_manifest=origin,
                linux_containment_profile=linux_containment_profile,
                containment_expectation=containment_expectation,
                launch_binding=launch_binding,
                cpu_native_root=cpu_native_root,
            )
            runtime = runtime_manager.__enter__()
            runtime_entered = True
            if not callback_called:
                raise BatchServiceDependencyError("Task-9 omitted its codec callback")
            authority = phase["authority"]
            phenotyping_codec = phase["phenotyping_codec"]
            training = TrainingRuntimeDependencies(
                runtime.state_store,
                runtime.project_stage,
                runtime.project_provider,
                runtime.runner,
                build_prototype_training_engine_provider_registry(),
                training_store,
                classifier_store,
            )
            image = ImageRuntimeDependencies(
                runtime.state_store,
                runtime.project_stage,
                runtime.project_provider,
                runtime.runner,
                runtime.project_operations,
                runtime.registration_executor,
                classifier_store,
                authority,
            )
            phenotyping = PhenotypingRuntimeDependencies(
                runtime.state_store,
                runtime.project_stage,
                runtime.project_provider,
                runtime.runner,
                runtime.project_operations,
                classifier_store,
                phenotype_store,
                prediction_store,
                authority,
                phase["publisher"],
                software_preferences_path=software_preferences_path,
            )
            resolver = _StateInteractionResponseResolver(runtime.state_store)
            job_runner = SpawnPreprocessingJobRunner()
            from .batch_secondary_definitions import build_secondary_definition_interaction_owner

            interaction_owners = BatchInteractionOwnerRegistry(
                (
                    *build_preprocessing_interaction_owner_bindings(state_store=runtime.state_store),
                    *build_training_interaction_owner_bindings(dependencies=training),
                    *build_image_interaction_owner_bindings(
                        dependencies=ImageCodecDependencies(
                            runtime.state_store,
                            runtime.project_provider,
                            classifier_store,
                            authority,
                            runtime.project_operations,
                            training_plan_set_store=training_store,
                            prerequisite_inputs=prerequisite_inputs,
                        )
                    ),
                    *build_phenotyping_interaction_owner_bindings(dependencies=phenotyping_codec),
                    build_secondary_definition_interaction_owner(dependencies=training),
                )
            )
            interaction_keys = tuple(
                (binding.stage, binding.interaction_reason, binding.action_schema_version)
                for binding in interaction_owners.bindings
            )
            if interaction_keys != registered_batch_interaction_owner_inventory():
                raise BatchServiceDependencyError(
                    "application interaction owners do not match the registered stages"
                )
            dependencies = BatchRuntimeDependencies(
                runtime,
                training,
                image,
                phenotyping,
                resolver,
                job_runner,
                interaction_owners,
                runtime.project_codecs_by_spec_sha256,
            )
            # The codec dependencies are service-private and needed only to prove
            # the publisher/resolver pair when composing runtime owners.
            object.__setattr__(dependencies, "_phenotyping_codec_dependencies", phenotyping_codec)
        yield dependencies
    except BaseException as exc:
        primary = exc
        raise
    finally:
        body_primary = primary
        try:
            _close_retained(retained, primary)
        except BaseException as failure:
            if primary is None:
                primary = failure
            else:
                _add_cleanup_note(primary, "retained stores", failure)
        if runtime_entered and runtime_manager is not None:
            try:
                runtime_manager.__exit__(
                    type(primary) if primary is not None else None,
                    primary,
                    primary.__traceback__ if primary is not None else None,
                )
            except BaseException as failure:
                if primary is None:
                    primary = failure
                else:
                    _add_cleanup_note(primary, "Task-9 runtime", failure)
        if body_primary is None and primary is not None:
            raise primary


__all__ = ["BatchRuntimeDependencies", "build_batch_stage_registry"]
