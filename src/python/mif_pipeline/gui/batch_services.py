"""Desktop lifetime management for the public batch service composition.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from threading import RLock

from mif_containment.client import ContainmentCompatibilityExpectationV1
from mif_pipeline.orchestration import batch_service
from mif_pipeline.orchestration.batch_dag import BatchPlan
from mif_pipeline.orchestration.batch_manifest import (
    BatchManifest,
    ValidatedBatchManifest,
    validate_manifest,
)
from mif_pipeline.orchestration.setup.batch_prerequisites import BatchPrerequisiteOwnerInputs
from mif_pipeline.orchestration.batch_service import BatchService, BatchServiceError
from mif_pipeline.orchestration.batch_state import BatchWorkspaceLayout
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeLifecycleBorrower,
    RuntimeUseToken,
)
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding
from mif_pipeline.qupath_runtime.runner import LinuxContainmentProfile

# ==============================================================================
#                                 Batch services
# ==============================================================================


class ProductionBatchServiceProvider:
    """Retain one backend provider per workspace until explicit desktop teardown.

    Call ``close`` only after managed desktop jobs have ended. If called during
    a live service operation, the backend refuses immediate teardown and keeps
    dependencies alive until that operation finishes. No worker is stopped.

    The selected CPU-library directory is passed to each workspace runtime so
    production model launches use the same installation as prerequisite checks.

    A workspace-only authority cannot subsequently be replaced by a manifest
    authority in this provider. Multiple manifest authorities for one workspace
    require callers to retain their exact service; workspace lookup is ambiguous.
    """

    def __init__(
        self,
        *,
        qupath_executable: Path,
        prerequisite_inputs: Callable[
            [ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs
        ],
        linux_containment_profile: LinuxContainmentProfile | None = None,
        containment_expectation: ContainmentCompatibilityExpectationV1 | None = None,
        runtime_lifecycle: RuntimeLifecycleBorrower | None = None,
        launch_binding: QuPathProfileLaunchBinding | None = None,
        cpu_native_root: Path | None = None,
        software_preferences_path: Path | None = None,
    ) -> None:
        if not isinstance(qupath_executable, Path) or not qupath_executable.is_absolute():
            raise BatchServiceError("QuPath executable must be an exact absolute Path")
        if not callable(prerequisite_inputs):
            raise BatchServiceError("prerequisite_inputs must be an owner-input supplier")
        if runtime_lifecycle is not None and not isinstance(
            runtime_lifecycle, RuntimeLifecycleBorrower
        ):
            raise TypeError("runtime_lifecycle must be a RuntimeLifecycleBorrower")
        if launch_binding is not None:
            if type(launch_binding) is not QuPathProfileLaunchBinding:
                raise TypeError("launch_binding must be a QuPathProfileLaunchBinding")
            if launch_binding.executable != qupath_executable:
                raise BatchServiceError("QuPath executable differs from selected launch binding")
        self._qupath_executable = qupath_executable
        self._prerequisite_inputs = prerequisite_inputs
        self._linux_containment_profile = linux_containment_profile
        self._containment_expectation = containment_expectation
        self._launch_binding = launch_binding
        self._cpu_native_root = cpu_native_root
        self._software_preferences_path = software_preferences_path
        self._profile_reservation: RuntimeUseToken | None = (
            runtime_lifecycle.acquire("qupath-profile-provider")
            if runtime_lifecycle is not None
            else None
        )
        self._contexts = ExitStack()
        self._lock = RLock()
        self._closed = False
        self._providers: dict[tuple[Path, str], batch_service.BatchServiceProvider] = {}
        self._manifests: dict[Path, BatchService] = {}
        self._workspace_manifests: dict[tuple[Path, str], dict[Path, BatchService]] = {}
        self._workspace_only: set[tuple[Path, str]] = set()

    def for_manifest(self, manifest_path: Path) -> BatchService:
        with self._lock:
            self._require_open()
            if not isinstance(manifest_path, Path):
                raise BatchServiceError("manifest_path must be a Path")
            path = manifest_path.expanduser().resolve(strict=True)
            if path in self._manifests:
                return self._manifests[path]
            # Public parsing locates the workspace; validation authority and
            # remembered manifest bytes stay with the returned backend service.
            validated = validate_manifest(
                BatchManifest.from_json_bytes(path.read_bytes()),
                allow_materialized_create_target=True,
            )
            key = (validated.manifest.output_root.resolve(), validated.manifest.batch_id)
            if key in self._workspace_only:
                raise BatchServiceError(
                    "workspace-only authority already acquired; cannot replace it with a manifest authority"
                )
            provider = self._provider(key)
            service = provider.for_manifest(path)
            self._manifests[path] = service
            self._workspace_manifests.setdefault(key, {})[path] = service
            return service

    def for_workspace(self, output_root: Path, batch_id: str) -> BatchService:
        with self._lock:
            self._require_open()
            if not isinstance(output_root, Path):
                raise BatchServiceError("output_root must be a Path")
            key = (output_root.expanduser().resolve(), batch_id)
            manifests = self._workspace_manifests.get(key, {})
            if len(manifests) > 1:
                raise BatchServiceError("workspace service authority is ambiguous across manifests")
            if manifests:
                return next(iter(manifests.values()))
            service = self._provider(key).for_workspace(*key)
            self._workspace_only.add(key)
            return service

    def _provider(self, key: tuple[Path, str]) -> batch_service.BatchServiceProvider:
        provider = self._providers.get(key)
        if provider is None:
            layout = BatchWorkspaceLayout.resolve(*key)
            provider = self._contexts.enter_context(
                batch_service.open_batch_service_provider(
                    workspace_layout=layout,
                    qupath_executable=self._qupath_executable,
                    prerequisite_inputs=self._prerequisite_inputs,
                    linux_containment_profile=self._linux_containment_profile,
                    containment_expectation=self._containment_expectation,
                    launch_binding=self._launch_binding,
                    cpu_native_root=self._cpu_native_root,
                    software_preferences_path=self._software_preferences_path,
                )
            )
            self._providers[key] = provider
        return provider

    def _require_open(self) -> None:
        if self._closed:
            raise BatchServiceError("production batch service provider is closed")

    def close(self) -> None:
        self.__exit__(None, None, None)

    def __enter__(self) -> ProductionBatchServiceProvider:
        with self._lock:
            self._require_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            result = self._contexts.__exit__(exc_type, exc_value, traceback)
            if self._profile_reservation is not None:
                self._profile_reservation.release()
            return result


__all__ = ["ProductionBatchServiceProvider"]
