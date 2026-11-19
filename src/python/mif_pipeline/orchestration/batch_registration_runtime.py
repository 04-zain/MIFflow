"""Compose existing-project registration through the retained batch owners.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import url2pathname

from mif_pipeline.pipeline_core.config import AnalysisConfig
from mif_pipeline.pipeline_core.stage_result import StageResult, StageStatus
from mif_pipeline.qupath_runtime.project import ProjectLayout
from mif_pipeline.qupath_runtime.registration_session import CurrentRegistrationProjectSnapshot

from .batch_artifacts import (
    BatchArtifactError,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
)
from .batch_registration import (
    build_warpy_fallback_registration_result_execution_result,
    build_warpy_registration_result_execution_result,
    route_registration_execution,
    registration_options_for_route,
)
from .batch_registration_session import (
    build_registration_transform_interaction,
    prepare_registration_project_preflight,
)
from .batch_state import (
    BatchRawNodeOutcome,
    BatchStateStore,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    ScheduledNodeResult,
)
from .batch_tissue_artifacts import reopen_committed_accepted_tissue_live_authority

if TYPE_CHECKING:
    from .batch_stage_adapters import ImageRuntimeDependencies


# ==============================================================================
#                           Batch registration runtime
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _RegistrationInvocation:
    config: AnalysisConfig
    state: Mapping[str, object]
    action: str | None
    action_context: Mapping[str, object]


def _interaction_fields(
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
) -> tuple[str | None, Mapping[str, object]]:
    """Project the state owner's validated response into the executor envelope."""
    accepted = state_store.load_validated_interaction_response(context.authorization)
    if accepted is None:
        return None, MappingProxyType({})
    action = accepted.get("action")
    validated = accepted.get("response")
    predecessor = context.authorization.interaction_predecessor_terminal_record_sha256
    if (
        action
        not in {
            "import_warpy_transform",
            "accept_registration",
            "reject",
            "side_by_side",
            "skip_moving_reference",
        }
        or predecessor is None
        or not isinstance(validated, Mapping)
        or validated.get("action") != action
        or not isinstance(validated.get("response"), Mapping)
    ):
        raise BatchArtifactError("registration retry action lacks validated response material")
    # The public mapping restores JSON arrays from immutable retained tuples;
    # snapshot parsing requires the original array representation.
    saved = state_store.load_attempt(predecessor).stage_result.to_mapping()["resume_context"]
    if not isinstance(saved, Mapping):
        raise BatchArtifactError("registration retry lacks saved service context")
    # Retained results may already carry the shared service envelope.
    service_context = saved.get("service_context", saved)
    if not isinstance(service_context, Mapping):
        raise BatchArtifactError("registration retry service context is invalid")
    return action, MappingProxyType(
        {
            "service_context": service_context,
            "response": validated["response"],
        }
    )


def _retained_project_layout(project_uri: str) -> ProjectLayout:
    """Read-only layout matching the existing project-operations owner."""
    project_file = Path(url2pathname(urlsplit(project_uri).path))
    root = project_file.parent
    return ProjectLayout(
        root=root,
        project_file=project_file,
        scripts_dir=root / "scripts",
        pixel_classifiers_dir=root / "classifiers" / "pixel_classifiers",
        object_classifiers_dir=root / "classifiers" / "object_classifiers",
        data_dir=root / "data",
        exchange_dir=root / "exchange",
        checkpoints_dir=root / "checkpoints",
        provenance_dir=root / "provenance",
        runs_dir=root / "runs",
        config_path=root / "data" / "analysis-config.json",
        config_json="",
    )


def _one_result(results: tuple[ScheduledNodeResult, ...]) -> ScheduledNodeResult:
    if len(results) != 1:
        raise BatchArtifactError("registration owner returned a foreign terminal set")
    return results[0]


def execute_registration_committed(
    context: BatchNodeExecutionContext,
    *,
    dependencies: ImageRuntimeDependencies,
    workspace_lease: BatchWorkspaceLease,
    terminal_committer: BatchTerminalCommitter,
) -> ScheduledNodeResult:
    """Run one registration decision and commit through its existing owners."""
    state_store = dependencies.state_store
    current = state_store.authenticate_current_execution_context(context)
    execution_snapshot = state_store.reopen_execution_snapshot(
        expected_head_sha256=current.state_head_sha256,
    )
    catalogue_store = state_store.project_catalogue_store(workspace_lease)
    route = route_registration_execution(
        context,
        manifest=execution_snapshot.manifest,
        state_store=state_store,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
    )

    def raw(stage_result: StageResult, execution_result: BatchNodeExecutionResult):
        return BatchRawNodeOutcome(context, stage_result, execution_result, None)

    def empty_result() -> BatchNodeExecutionResult:
        return BatchNodeExecutionResult(
            authorization_sha256=context.authorization.authorization_sha256,
            outputs=(),
        )

    if route.disposition == "direct_r1":
        return _one_result(
            terminal_committer.commit_raw((raw(route.stage_result, route.execution_result),))
        )
    action, action_context = _interaction_fields(context, state_store)
    if action in {"side_by_side", "skip_moving_reference"}:
        stage_result, execution_result = build_warpy_fallback_registration_result_execution_result(
            route,
            state_store=state_store,
            catalogue_store=catalogue_store,
        )
        return _one_result(terminal_committer.commit_raw((raw(stage_result, execution_result),)))

    live_tissue = reopen_committed_accepted_tissue_live_authority(
        state_store.authenticate_available_artifact(route.tissue_ref),
        exchange_reopener=dependencies.project_stage.committed_exchange_reopener(
            workspace_lease=workspace_lease,
        ),
    )
    project = _retained_project_layout(route.catalogue.project_uri)
    service_context = action_context.get("service_context", {})
    transform_snapshot = None
    registration_predecessor = None
    attempt_kind = "initial_registration_import"
    if action == "import_warpy_transform":
        attempt_kind = "import_warpy_transform"
        transform_snapshot = CurrentRegistrationProjectSnapshot.from_mapping(
            service_context.get("batch_registration_snapshot"),
        )
    elif action in {"accept_registration", "reject"}:
        attempt_kind = "review_registration"
        registration_predecessor = service_context.get("pending_registration_sha256")
    preflight = prepare_registration_project_preflight(
        route,
        state_store=state_store,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        project_provider=dependencies.project_provider,
        runner=dependencies.runner,
        project=project,
        attempt_kind=attempt_kind,
        interaction_response_sha256=context.authorization.interaction_response_sha256,
        predecessor_terminal_record_sha256=(
            context.authorization.interaction_predecessor_terminal_record_sha256
        ),
        predecessor_transform_snapshot=transform_snapshot,
        registration_predecessor_sha256=registration_predecessor,
    )
    if preflight.disposition == "transform_choice_required":
        interaction = build_registration_transform_interaction(route, preflight)
        return _one_result(terminal_committer.commit_raw((raw(interaction, empty_result()),)))

    invocation = _RegistrationInvocation(
        config=replace(
            route.sample.config,
            input_image=Path(url2pathname(urlsplit(route.primary_entry.canonical_uri).path)),
            registration=registration_options_for_route(route),
        ),
        state=MappingProxyType(
            {
                "tissue_acceptance": MappingProxyType(
                    {
                        "generation": live_tissue.generation.to_mapping(),
                    }
                )
            }
        ),
        action=action,
        action_context=action_context,
    )
    provider = dependencies.project_provider
    acquisition = (
        provider.acquire(preflight.acquisition_session)
        if preflight.recovery_record_sha256 is None
        else provider.recover(
            preflight.acquisition_session,
            stale_active_record_sha256=preflight.recovery_record_sha256,
        )
    )
    with acquisition as project_lease:
        try:
            session = provider.issue_existing_registration_project_session(
                lease=project_lease,
                resolver=preflight.snapshot_resolver,
                runner=dependencies.runner,
                project=project,
                target_plan=preflight.runtime_target_plan,
            )
            recovered = (
                None
                if preflight.recovery_record_sha256 is None
                else dependencies.runner.recover_registration_process(session)
            )
            stage_result, completion = dependencies.registration_executor.execute_existing_project(
                invocation,
                project=project,
                session=session,
                recovered_completion=recovered,
            )
        except Exception as primary:
            try:
                provider.abort_unlaunched_registration(project_lease, resolver=preflight.snapshot_resolver)
            except Exception as cleanup:
                primary.add_note(f"Registration preparation could not be archived: {cleanup}"[:512])
            raise
        if completion.disposition != "completed":
            raise BatchArtifactError(
                "registration native completion is not normal and cannot commit"
            )
        execution_result = empty_result()
        if stage_result.status is StageStatus.COMPLETED:
            if action == "reject":
                stage_result = StageResult.failed(
                    stage="registration",
                    slide_id=route.sample.slide_id,
                    generation_id=route.tissue_result.generation_id,
                    error_code="registration_rejected",
                    error_message="The reviewed registration was rejected.",
                )
            else:
                stage_result, execution_result = build_warpy_registration_result_execution_result(
                    route,
                    stage_result,
                    snapshot=preflight.snapshot,
                    state_store=state_store,
                    catalogue_store=catalogue_store,
                )
        elif stage_result.status not in {StageStatus.NEEDS_INTERACTION, StageStatus.FAILED}:
            raise BatchArtifactError("registration scientific terminal status is unsupported")
        outcome = raw(stage_result, execution_result)
        prepared = state_store.prepare_project_mutation_node_result(
            context,
            stage_result,
            execution_result,
            execution_exchange_sha256=None,
            project_lease=project_lease,
            expected_head_sha256=route.state_head.sha256,
            lease=workspace_lease,
        )
        terminal_digest = state_store.stage_project_terminal_attempt(
            context,
            prepared,
            project_lease=project_lease,
            expected_head_sha256=route.state_head.sha256,
            lease=workspace_lease,
        )
        resolution = provider.finalize_registration_runtime(
            session,
            completion,
            terminal_record_sha256=terminal_digest,
        )
        material = state_store.prepare_registration_material_successor(
            context,
            prepared,
            project_lease=project_lease,
            session=session,
            completion=completion,
            resolution=resolution,
            terminal_record_sha256=terminal_digest,
            expected_head_sha256=route.state_head.sha256,
            lease=workspace_lease,
        )
        committed, receipt = terminal_committer.commit_project_stage((outcome,), material)
        result = _one_result(committed)
        project_lease.complete(receipt)
        return result
