"""Sole retained-authority composition root for batch project work.

The `BatchProjectRuntime` record carries `authority_layout_sha256`,
`workspace_storage_scope_sha256`, `state_store`, `project_provider`, `runner` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
from threading import RLock
from types import MappingProxyType
from typing import Iterator, Literal, Mapping, Protocol, runtime_checkable

from mif_containment.client import ContainmentCompatibilityExpectationV1
from mif_pipeline.pipeline_core.authority_provisioning import (
    RetainedAuthorityLayoutV2,
    open_application_authority_layout_v2,
)
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeCompositionBinding,
    RuntimeLifecycleBorrower,
    RuntimeLifecycleError,
    RuntimeLifecycleOwner,
    RuntimeTeardownPermit,
)
from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseProvider
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding, qupath_command
from mif_pipeline.qupath_runtime.catalogue_operations import (
    QuPathRunnerProjectOperations,
)
from mif_pipeline.qupath_runtime.project_authority import ProjectAuthorityV2
from mif_pipeline.qupath_runtime.runner import LinuxContainmentProfile, QuPathRunner
from mif_pipeline.registration.executor import ProductionRegistrationExecutor

from .batch_state import (
    BatchSecurePublicationProvider,
    BatchStateStore,
    BatchWorkspaceLayout,
    WindowsBatchSecurePublicationProvider,
    batch_project_workspace_storage_scope_sha256,
)
from .batch_manifest import ValidatedBatchManifest
from .project_stage_plan import ProjectStageRuntime
from .project_stage_codecs import ProjectStageCodec, ProjectStageCodecRegistry

# ==============================================================================
#                                 Batch runtime
# ==============================================================================


class BatchProjectRuntimeError(RuntimeError):
    """Raised when the composed project runtime cannot open or close safely."""


@dataclass(frozen=True, slots=True)
class BatchProjectRuntimeOwners:
    """Exact phase-one owners issued before project-stage runtime creation."""

    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    state_store: BatchStateStore
    project_provider: ProjectMutationLeaseProvider
    runner: QuPathRunner
    project_operations: QuPathRunnerProjectOperations
    registration_executor: ProductionRegistrationExecutor


@runtime_checkable
class ProjectStageCodecRegistryFactory(Protocol):
    def __call__(self, owners: BatchProjectRuntimeOwners, /) -> "ProjectStageCodecRegistry": ...


class _BatchRuntimeFactories(Protocol):
    def open_authority_layout(self) -> RetainedAuthorityLayoutV2: ...

    def build_runner(
        self,
        executable: Path,
        *,
        containment_expectation: ContainmentCompatibilityExpectationV1 | None,
        linux_containment_profile: LinuxContainmentProfile | None,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        launch_binding: QuPathProfileLaunchBinding | None,
        cpu_native_root: Path | None = None,
    ) -> QuPathRunner: ...

    def build_project_operations(
        self,
        runner: QuPathRunner,
        *,
        authority_layout: RetainedAuthorityLayoutV2,
        workspace_storage_scope_sha256: str,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
    ) -> QuPathRunnerProjectOperations: ...

    def build_project_provider(
        self,
        *,
        project_authority: ProjectAuthorityV2,
        project_inspector: QuPathRunnerProjectOperations,
        runtime_lifecycle: RuntimeLifecycleBorrower,
    ) -> ProjectMutationLeaseProvider: ...

    def build_state_store(
        self,
        layout: BatchWorkspaceLayout,
        *,
        publication_provider: BatchSecurePublicationProvider | None,
        project_mutation_provider: ProjectMutationLeaseProvider,
        authority_layout: RetainedAuthorityLayoutV2,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
        workspace_storage_scope_sha256: str,
    ) -> BatchStateStore: ...

    def build_registration_executor(
        self,
        *,
        runner: QuPathRunner,
        runtime_lifecycle: RuntimeLifecycleBorrower,
    ) -> ProductionRegistrationExecutor: ...

    def build_windows_publication_provider(
        self, layout: BatchWorkspaceLayout
    ) -> BatchSecurePublicationProvider: ...


class _ProductionBatchRuntimeFactories:
    def open_authority_layout(self) -> RetainedAuthorityLayoutV2:
        return open_application_authority_layout_v2()

    def build_runner(
        self,
        executable: Path,
        *,
        containment_expectation: ContainmentCompatibilityExpectationV1 | None,
        linux_containment_profile: LinuxContainmentProfile | None,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        launch_binding: QuPathProfileLaunchBinding | None,
        cpu_native_root: Path | None = None,
    ) -> QuPathRunner:
        # On Windows the executor selects Job Objects from the host; the Linux
        # profile value is never used by that platform-specific launch path.
        runner_profile: LinuxContainmentProfile = (
            "hardened-broker" if linux_containment_profile is None else linux_containment_profile
        )
        return QuPathRunner(
            executable,
            containment_expectation=containment_expectation,
            linux_containment_profile=runner_profile,
            runtime_lifecycle=runtime_lifecycle,
            launch_binding=launch_binding,
            cpu_native_root=cpu_native_root,
        )

    def build_project_operations(
        self,
        runner: QuPathRunner,
        *,
        authority_layout: RetainedAuthorityLayoutV2,
        workspace_storage_scope_sha256: str,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
    ) -> QuPathRunnerProjectOperations:
        return QuPathRunnerProjectOperations(
            runner,
            authority_layout=authority_layout,
            workspace_storage_scope_sha256=workspace_storage_scope_sha256,
            runtime_lifecycle=runtime_lifecycle,
            runtime_binding=runtime_binding,
        )

    def build_project_provider(
        self,
        *,
        project_authority: ProjectAuthorityV2,
        project_inspector: QuPathRunnerProjectOperations,
        runtime_lifecycle: RuntimeLifecycleBorrower,
    ) -> ProjectMutationLeaseProvider:
        return ProjectMutationLeaseProvider(
            project_authority=project_authority,
            project_inspector=project_inspector,
            runtime_lifecycle=runtime_lifecycle,
        )

    def build_state_store(
        self,
        layout: BatchWorkspaceLayout,
        *,
        publication_provider: BatchSecurePublicationProvider | None,
        project_mutation_provider: ProjectMutationLeaseProvider,
        authority_layout: RetainedAuthorityLayoutV2,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
        workspace_storage_scope_sha256: str,
    ) -> BatchStateStore:
        return BatchStateStore(
            layout,
            publication_provider=publication_provider,
            project_mutation_provider=project_mutation_provider,
            authority_layout=authority_layout,
            runtime_lifecycle=runtime_lifecycle,
            runtime_binding=runtime_binding,
            workspace_storage_scope_sha256=workspace_storage_scope_sha256,
        )

    def build_registration_executor(
        self,
        *,
        runner: QuPathRunner,
        runtime_lifecycle: RuntimeLifecycleBorrower,
    ) -> ProductionRegistrationExecutor:
        return ProductionRegistrationExecutor(
            runner=runner,
            runtime_lifecycle=runtime_lifecycle,
            allow_object_transfer=False,
        )

    def build_windows_publication_provider(
        self, layout: BatchWorkspaceLayout
    ) -> BatchSecurePublicationProvider:
        return WindowsBatchSecurePublicationProvider(workspace_root=layout.root)


def _bounded_cleanup_note(primary: BaseException, resource: str, failure: BaseException) -> None:
    try:
        primary.add_note(
            f"{resource} cleanup failed: {type(failure).__name__}: " f"{str(failure)[:240]}"
        )
    except BaseException:
        pass


@dataclass(slots=True)
class _BatchProjectRuntimeCloseController:
    owner: RuntimeLifecycleOwner
    authority_layout: RetainedAuthorityLayoutV2
    registration_executor: ProductionRegistrationExecutor
    project_provider: ProjectMutationLeaseProvider
    _lock: RLock = field(default_factory=RLock, repr=False)
    cleanup_failures: tuple[tuple[str, BaseException], ...] = ()

    def close(self) -> None:
        with self._lock:
            if self.owner.state == "closed":
                return
            if self.owner.state == "failed":
                failure = self.owner.terminal_failure
                assert failure is not None
                raise failure
            if self.owner.state == "tearing_down":
                raise BatchProjectRuntimeError("runtime teardown is already in progress")
            try:
                permit = self.owner.begin_teardown()
            except RuntimeLifecycleError as exc:
                raise BatchProjectRuntimeError(
                    "runtime close refused because live operations remain"
                ) from exc

        # Do not retain this controller lock while closing OS resources.  The
        # lifecycle state is already ``tearing_down``, so a concurrent close
        # can observe that terminal transition-in-progress and refuse instead
        # of waiting and then accidentally replaying the completed outcome.
        failures: list[tuple[str, BaseException]] = []
        resources = (
            (
                "registration executor",
                lambda: self.registration_executor.close(teardown_permit=permit),
            ),
            (
                "project provider",
                lambda: self.project_provider.close(teardown_permit=permit),
            ),
            ("retained authority layout", self.authority_layout.close),
        )
        for name, cleanup in resources:
            try:
                cleanup()
            except BaseException as exc:
                failures.append((name, exc))

        with self._lock:
            self.cleanup_failures = tuple(failures)
            failure = failures[0][1] if failures else None
            if failure is not None:
                for name, additional in failures[1:]:
                    _bounded_cleanup_note(failure, name, additional)
            self.owner.finish_teardown(permit, failure=failure)
            if failure is not None:
                raise failure

    def attach_cleanup_notes(self, primary: BaseException) -> None:
        for name, failure in self.cleanup_failures:
            _bounded_cleanup_note(primary, name, failure)


@dataclass(frozen=True, slots=True)
class BatchProjectRuntime:
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    state_store: BatchStateStore
    project_provider: ProjectMutationLeaseProvider
    runner: QuPathRunner
    project_operations: QuPathRunnerProjectOperations
    registration_executor: ProductionRegistrationExecutor
    project_stage_codec_registry: "ProjectStageCodecRegistry"
    project_codecs_by_spec_sha256: Mapping[str, "ProjectStageCodec"]
    project_stage: ProjectStageRuntime
    _close_controller: _BatchProjectRuntimeCloseController = field(repr=False, compare=False)

    def close(self) -> None:
        self._close_controller.close()

    @property
    def project_stage_active_session_count(self) -> int:
        """Return the ephemeral active session count owned by the facade."""

        return self.project_stage.active_session_count

    @property
    def project_stage_is_quiescent(self) -> bool:
        """Report whether project-stage in-process authority is fully released."""

        return self.project_stage.is_quiescent

    def wait_for_project_stage_quiescence(self, timeout_seconds: float | None = None) -> bool:
        """Wait on the facade condition without creating durable pause proof."""

        return self.project_stage.wait_for_quiescence(timeout_seconds)


def _validate_platform_configuration(
    *,
    platform: Literal["linux", "windows"],
    linux_containment_profile: LinuxContainmentProfile | None,
    containment_expectation: ContainmentCompatibilityExpectationV1 | None,
) -> None:
    if platform == "linux":
        if linux_containment_profile == "prototype-local-process-group":
            if containment_expectation is not None:
                raise BatchProjectRuntimeError(
                    "local Linux containment forbids a broker expectation"
                )
            return
        if linux_containment_profile == "hardened-broker":
            if type(containment_expectation) is not ContainmentCompatibilityExpectationV1:
                raise BatchProjectRuntimeError(
                    "hardened Linux containment requires its exact expectation"
                )
            return
        raise BatchProjectRuntimeError(
            "Linux runtime requires an explicit local or hardened profile"
        )
    if platform == "windows":
        if linux_containment_profile is not None:
            raise BatchProjectRuntimeError("Windows runtime rejects Linux containment profiles")
        if containment_expectation is not None:
            raise BatchProjectRuntimeError("Windows runtime rejects Linux broker expectations")
        return
    raise BatchProjectRuntimeError(f"unsupported runtime platform {platform!r}")


def _cleanup_partial_runtime(
    *,
    owner: RuntimeLifecycleOwner,
    authority_layout: RetainedAuthorityLayoutV2 | None,
    registration_executor: ProductionRegistrationExecutor | None,
    project_provider: ProjectMutationLeaseProvider | None,
    primary: BaseException,
) -> None:
    try:
        permit = owner.begin_teardown()
    except BaseException as exc:
        _bounded_cleanup_note(primary, "partial runtime lifecycle", exc)
        return
    failures: list[tuple[str, BaseException]] = []
    resources: tuple[tuple[str, object], ...] = (
        ("registration executor", registration_executor),
        ("project provider", project_provider),
        ("retained authority layout", authority_layout),
    )
    for name, resource in resources:
        if resource is None:
            continue
        try:
            if name == "retained authority layout":
                resource.close()  # type: ignore[attr-defined]
            else:
                resource.close(teardown_permit=permit)  # type: ignore[attr-defined]
        except BaseException as exc:
            failures.append((name, exc))
            _bounded_cleanup_note(primary, name, exc)
    owner.finish_teardown(permit, failure=failures[0][1] if failures else None)


@dataclass(slots=True)
class _PartialRuntimeCleanup:
    owner: RuntimeLifecycleOwner
    authority_layout: RetainedAuthorityLayoutV2 | None = None
    registration_executor: ProductionRegistrationExecutor | None = None
    project_provider: ProjectMutationLeaseProvider | None = None

    def __call__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, traceback
        if exc is not None:
            _cleanup_partial_runtime(
                owner=self.owner,
                authority_layout=self.authority_layout,
                registration_executor=self.registration_executor,
                project_provider=self.project_provider,
                primary=exc,
            )
        return False


def _runtime_workspace_manifest(
    workspace_layout: BatchWorkspaceLayout,
) -> ValidatedBatchManifest | None:
    """Read the head-selected original manifest before composing a saved workspace.

    Reopening binds the runtime to the saved analysis identity. A new analysis
    has no retained manifest and uses its current workspace layout.
    """
    import json
    from .batch_manifest import read_retained_manifest

    retained = None
    head_path = workspace_layout.state_head_path
    if head_path.is_symlink():
        raise BatchProjectRuntimeError("The saved analysis head must be a regular file.")
    if head_path.exists():
        # Full execution-history validation remains with BatchStateStore. This
        # initial read selects only the digest-checked manifest needed to bind
        # that store, the runner and native operations to the same origin.
        selected = json.loads(head_path.read_bytes())
        retained = read_retained_manifest(
            workspace_layout.manifest, selected["normalized_manifest_sha256"],
        )
        if (retained.original_manifest_sha256 != selected["original_manifest_sha256"]
                or retained.manifest.batch_id != workspace_layout.batch_id
                or selected["batch_id"] != workspace_layout.batch_id):
            raise BatchProjectRuntimeError("The saved manifest belongs to another analysis.")
    return retained


@contextmanager
def _open_batch_project_runtime(
    *,
    workspace_layout: BatchWorkspaceLayout,
    qupath_executable: Path,
    linux_containment_profile: LinuxContainmentProfile | None,
    containment_expectation: ContainmentCompatibilityExpectationV1 | None,
    platform: Literal["linux", "windows"],
    factories: _BatchRuntimeFactories,
    project_stage_codec_registry_factory: ProjectStageCodecRegistryFactory,
    workspace_origin_manifest: ValidatedBatchManifest | None = None,
    launch_binding: QuPathProfileLaunchBinding | None = None,
    cpu_native_root: Path | None = None,
) -> Iterator[BatchProjectRuntime]:
    from .project_stage_codecs import ProjectStageCodecError

    _validate_platform_configuration(
        platform=platform,
        linux_containment_profile=linux_containment_profile,
        containment_expectation=containment_expectation,
    )
    if not isinstance(workspace_layout, BatchWorkspaceLayout):
        raise BatchProjectRuntimeError("runtime workspace layout is invalid")
    if not isinstance(qupath_executable, Path):
        raise BatchProjectRuntimeError("QuPath executable must be a Path")
    qupath_command(qupath_executable, launch_binding=launch_binding)

    owner = RuntimeLifecycleOwner.open()
    borrower = owner.borrower
    partial_cleanup = _PartialRuntimeCleanup(owner=owner)
    with ExitStack() as construction_stack:
        construction_stack.push(partial_cleanup)
        authority_layout = factories.open_authority_layout()
        partial_cleanup.authority_layout = authority_layout
        authority_layout.validate()
        scope = batch_project_workspace_storage_scope_sha256(
            batch_id=workspace_layout.batch_id,
            original_output_root=(workspace_layout.output_root if workspace_origin_manifest is None
                                  else workspace_origin_manifest.manifest.output_root.resolve()),
        )
        runner = factories.build_runner(
            qupath_executable,
            containment_expectation=containment_expectation,
            linux_containment_profile=linux_containment_profile,
            runtime_lifecycle=borrower,
            launch_binding=launch_binding,
            cpu_native_root=cpu_native_root,
        )
        binding = owner.issue_binding(
            authority_layout=authority_layout,
            runner=runner,
            workspace_storage_scope_sha256=scope,
        )
        project_operations = factories.build_project_operations(
            runner,
            authority_layout=authority_layout,
            workspace_storage_scope_sha256=scope,
            runtime_lifecycle=borrower,
            runtime_binding=binding,
        )
        project_authority = ProjectAuthorityV2(authority_layout, runtime_lifecycle=borrower)
        project_provider = factories.build_project_provider(
            project_authority=project_authority,
            project_inspector=project_operations,
            runtime_lifecycle=borrower,
        )
        partial_cleanup.project_provider = project_provider
        publication_provider = (
            factories.build_windows_publication_provider(workspace_layout)
            if platform == "windows"
            else None
        )
        state_store = factories.build_state_store(
            workspace_layout,
            publication_provider=publication_provider,
            project_mutation_provider=project_provider,
            authority_layout=authority_layout,
            runtime_lifecycle=borrower,
            runtime_binding=binding,
            workspace_storage_scope_sha256=scope,
        )
        state_store.authenticate_project_operations(project_operations, runner=runner)
        registration_executor = factories.build_registration_executor(
            runner=runner, runtime_lifecycle=borrower
        )
        partial_cleanup.registration_executor = registration_executor
        owners = BatchProjectRuntimeOwners(
            authority_layout_sha256=authority_layout.layout.layout_sha256,
            workspace_storage_scope_sha256=scope,
            state_store=state_store,
            project_provider=project_provider,
            runner=runner,
            project_operations=project_operations,
            registration_executor=registration_executor,
        )
        factory_invoked = False

        def build_registry_once() -> ProjectStageCodecRegistry:
            nonlocal factory_invoked
            if factory_invoked:
                raise BatchProjectRuntimeError(
                    "application project-stage codec registry factory was invoked twice"
                )
            factory_invoked = True
            if not callable(project_stage_codec_registry_factory):
                raise BatchProjectRuntimeError(
                    "application project-stage codec registry factory is invalid"
                )
            registry = project_stage_codec_registry_factory(owners)
            if type(registry) is not ProjectStageCodecRegistry:
                raise BatchProjectRuntimeError(
                    "application project-stage codec registry is invalid"
                )
            try:
                registry.require_application_composition()
            except ProjectStageCodecError as exc:
                raise BatchProjectRuntimeError(
                    "application project-stage codec registry is incomplete or untrusted"
                ) from exc
            return registry

        # Use one installed format definition for this complete registry.
        from mif_pipeline.pipeline_core.schema_registry import schema_validation_snapshot

        with schema_validation_snapshot():
            project_stage_codec_registry = build_registry_once()
        codecs_by_spec_sha256: dict[str, ProjectStageCodec] = {}
        for codec in project_stage_codec_registry.application_codecs():
            spec_sha256 = codec.binding.adapter_spec_sha256
            if spec_sha256 in codecs_by_spec_sha256:
                raise BatchProjectRuntimeError(
                    "application project-stage codecs duplicate an adapter specification"
                )
            codecs_by_spec_sha256[spec_sha256] = codec
        project_codecs_by_spec_sha256 = MappingProxyType(codecs_by_spec_sha256)
        project_stage = state_store.project_stage_runtime(
            provider=project_provider,
            runner=runner,
            project_operations=project_operations,
            codec_registry=project_stage_codec_registry,
        )
        controller = _BatchProjectRuntimeCloseController(
            owner=owner,
            authority_layout=authority_layout,
            registration_executor=registration_executor,
            project_provider=project_provider,
        )
        runtime = BatchProjectRuntime(
            authority_layout_sha256=authority_layout.layout.layout_sha256,
            workspace_storage_scope_sha256=scope,
            state_store=state_store,
            project_provider=project_provider,
            runner=runner,
            project_operations=project_operations,
            registration_executor=registration_executor,
            project_stage_codec_registry=project_stage_codec_registry,
            project_codecs_by_spec_sha256=project_codecs_by_spec_sha256,
            project_stage=project_stage,
            _close_controller=controller,
        )
        construction_stack.pop_all()

    body_failure: BaseException | None = None
    try:
        yield runtime
    except BaseException as exc:
        body_failure = exc
        raise
    finally:
        try:
            runtime.close()
        except BaseException as close_failure:
            if body_failure is None:
                raise
            if runtime._close_controller.cleanup_failures:
                runtime._close_controller.attach_cleanup_notes(body_failure)
            else:
                _bounded_cleanup_note(body_failure, "runtime close refusal", close_failure)


@contextmanager
def open_batch_project_runtime(
    *,
    workspace_layout: BatchWorkspaceLayout,
    qupath_executable: Path,
    project_stage_codec_registry_factory: ProjectStageCodecRegistryFactory,
    linux_containment_profile: LinuxContainmentProfile | None = None,
    containment_expectation: ContainmentCompatibilityExpectationV1 | None = None,
    launch_binding: QuPathProfileLaunchBinding | None = None,
    cpu_native_root: Path | None = None,
    _workspace_origin_manifest: ValidatedBatchManifest | None = None,
) -> Iterator[BatchProjectRuntime]:
    origin = (_workspace_origin_manifest if _workspace_origin_manifest is not None
              else _runtime_workspace_manifest(workspace_layout))
    if origin is not None and origin.manifest.batch_id != workspace_layout.batch_id:
        raise BatchProjectRuntimeError("The runtime origin belongs to another analysis.")
    if sys.platform == "linux" and os.name == "posix":
        platform: Literal["linux", "windows"] = "linux"
    elif sys.platform == "win32" and os.name == "nt":
        platform = "windows"
    else:
        raise BatchProjectRuntimeError("batch project runtime requires native Linux or Windows")
    with _open_batch_project_runtime(
        workspace_layout=workspace_layout,
        qupath_executable=qupath_executable,
        linux_containment_profile=linux_containment_profile,
        containment_expectation=containment_expectation,
        platform=platform,
        factories=_ProductionBatchRuntimeFactories(),
        workspace_origin_manifest=origin,
        project_stage_codec_registry_factory=project_stage_codec_registry_factory,
        launch_binding=launch_binding,
        cpu_native_root=cpu_native_root,
    ) as runtime:
        yield runtime


__all__ = [
    "BatchProjectRuntime",
    "BatchProjectRuntimeOwners",
    "BatchProjectRuntimeError",
    "ProjectStageCodecRegistryFactory",
    "open_batch_project_runtime",
]
