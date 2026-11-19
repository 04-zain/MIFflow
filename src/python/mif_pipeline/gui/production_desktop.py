"""Local prototype composition, deferred until first-run readiness.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
import tempfile
from threading import RLock

from mif_pipeline.io.bioformats_prerequisite import BioFormatsRuntimeInventory, InventoryMissing
from mif_pipeline.io import selected_offline_bioformats_inventory
from mif_pipeline.orchestration.setup.batch_prerequisites import BatchPrerequisiteOwnerInputs
from mif_pipeline.orchestration.batch_service import BatchServiceError
from mif_pipeline.orchestration.prototype_owner_inputs import prototype_owner_inputs_supplier
from mif_pipeline.orchestration.batch_settled_review_revision import requires_paired_alignment
from mif_pipeline.pipeline_core.runtime_lifecycle import RuntimeLifecycleOwner
from mif_pipeline.qupath_runtime.class_origin_candidate import probe_qupath_class_origins
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding, qupath_command
from mif_pipeline.qupath_runtime.payload_lock import MODEL_VERSION

from .batch_services import ProductionBatchServiceProvider
from .desktop_services import BatchServiceProvider, DesktopFileOperations
from .file_operations import LocalDesktopFileOperations
from .first_run import FirstRunPrerequisiteService, FirstRunPrerequisiteState
from .source_series import LocalSourceSeriesInspectionService, SourceSeriesInspectionPort
from .successor_services import SuccessorServiceRegistry

DesktopServices = tuple[
    BatchServiceProvider,
    SourceSeriesInspectionPort,
    DesktopFileOperations,
    SuccessorServiceRegistry,
]


# ==============================================================================
#                               Production desktop
# ==============================================================================


class ProductionDesktopDependencies:
    """Retain one provider for the local session after prerequisite readiness.

    The installed launcher discovers exact class origins when owner inputs are
    needed. Programmatic clients may still supply exact paths explicitly.
    Readiness remains with the full prerequisite owner.
    """

    def __init__(
        self,
        prerequisites: FirstRunPrerequisiteService,
        *,
        instanseg_jar: Path | None = None,
        djl_jar: Path | None = None,
        pytorch_engine_jar: Path | None = None,
        native_root: Path | None = None,
        cache_root: Path | None = None,
        bioformats_inventory: BioFormatsRuntimeInventory | None = None,
        discover_class_origins: bool = False,
        cpu_native_root: Path | None = None,
        image_runtime_selection_guard: Callable[[], None] | None = None,
        launch_binding: QuPathProfileLaunchBinding | None = None,
        software_preferences_path: Path | None = None,
    ) -> None:
        payload_paths = (instanseg_jar, djl_jar, pytorch_engine_jar, native_root, cache_root)
        if any(path is not None for path in payload_paths):
            if any(path is None for path in payload_paths):
                raise ValueError("QuPath payload paths must be supplied together")
            if any(
                not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
                for path in payload_paths
            ):
                raise ValueError("QuPath payload paths must be absolute Paths without traversal")
            self._payload_paths = payload_paths
        else:
            self._payload_paths = None
        if type(discover_class_origins) is not bool:
            raise TypeError("discover_class_origins must be bool")
        if discover_class_origins:
            if self._payload_paths is not None:
                raise ValueError("class origin discovery cannot replace explicit payload paths")
            if (
                not isinstance(cpu_native_root, Path)
                or not cpu_native_root.is_absolute()
                or ".." in cpu_native_root.parts
            ):
                raise ValueError("class origin discovery requires an absolute CPU native root")
        elif cpu_native_root is not None:
            raise ValueError("CPU native root requires class origin discovery")
        if bioformats_inventory is not None and not isinstance(
            bioformats_inventory, BioFormatsRuntimeInventory
        ):
            raise TypeError("bioformats_inventory must be BioFormatsRuntimeInventory")
        self.prerequisites = prerequisites
        self._software_preferences_path = software_preferences_path
        if launch_binding is not None:
            if type(launch_binding) is not QuPathProfileLaunchBinding:
                raise TypeError("launch_binding must be a QuPathProfileLaunchBinding")
        self._launch_binding = launch_binding
        self._profile_lifecycle = RuntimeLifecycleOwner.open()
        self._bioformats_inventory = bioformats_inventory
        self._discover_class_origins = discover_class_origins
        self._cpu_native_root = cpu_native_root
        self._image_runtime_selection_guard = image_runtime_selection_guard
        self._origin_lock = RLock()
        self._origin_paths: tuple[Path | None, Path | None, Path | None, Path | None] | None = None
        self._session_cache: tempfile.TemporaryDirectory[str] | None = None
        self.successors = SuccessorServiceRegistry()
        self._services: DesktopServices | None = None
        self._provider: ProductionBatchServiceProvider | None = None
        self._closed = False

    def invalidate_class_origin_cache(self) -> None:
        """Require the next owner-input request to rediscover live class origins."""
        with self._origin_lock:
            self._origin_paths = None

    def select_qupath_launch_binding(self, binding: QuPathProfileLaunchBinding) -> None:
        """Apply checked extension setup before creating the analysis services."""
        if self._services is not None or self._closed:
            raise BatchServiceError("Close the analysis and restart the application before changing QuPath setup.")
        self._launch_binding = binding
        self.invalidate_class_origin_cache()

    def ready_services(self, state: FirstRunPrerequisiteState) -> DesktopServices:
        if self._closed:
            raise BatchServiceError("production desktop dependencies are closed")
        info = self.prerequisites.qupath_executable_info
        if not state.ready or info is None:
            raise BatchServiceError("desktop dependencies require first-run readiness")
        qupath_command(info.path, launch_binding=self._launch_binding)
        if self._launch_binding is not None and info.version != self._launch_binding.qupath_version:
            raise BatchServiceError("QuPath version differs from the selected profile.")
        if self._services is None:
            cpu_native_root = self._cpu_native_root
            if self._discover_class_origins:
                if sys.platform not in {"linux", "win32"}:
                    raise BatchServiceError("unsupported platform for prototype payload")
                owner_inputs = self._discovered_owner_inputs(info.path)
            elif self._payload_paths is None:
                owner_inputs = lambda manifest, plan: BatchPrerequisiteOwnerInputs(
                    self._bioformats_inventory,
                    {},
                )
            else:
                if sys.platform not in {"linux", "win32"}:
                    raise BatchServiceError("unsupported platform for prototype payload")
                instanseg_jar, djl_jar, pytorch_engine_jar, native_root, cache_root = (
                    self._payload_paths
                )
                cpu_native_root = native_root
                owner_inputs = prototype_owner_inputs_supplier(
                    qupath_executable=info.path,
                    instanseg_jar=instanseg_jar,
                    djl_jar=djl_jar,
                    pytorch_engine_jar=pytorch_engine_jar,
                    native_root=native_root,
                    cache_root=cache_root,
                    platform="win-x86_64" if sys.platform == "win32" else "linux-x86_64",
                    bioformats_inventory=self._bioformats_inventory,
                    launch_binding=self._launch_binding,
                )
            self._provider = ProductionBatchServiceProvider(
                qupath_executable=info.path,
                prerequisite_inputs=self._guarded_owner_inputs(owner_inputs),
                linux_containment_profile=(
                    "prototype-local-process-group" if sys.platform == "linux" else None
                ),
                runtime_lifecycle=self._profile_lifecycle.borrower,
                launch_binding=self._launch_binding,
                cpu_native_root=cpu_native_root,
                software_preferences_path=self._software_preferences_path,
            )
            self._services = (
                self._provider,
                LocalSourceSeriesInspectionService(self._image_runtime_selection_guard),
                LocalDesktopFileOperations(
                    lambda: info,
                    launch_binding=self._launch_binding,
                    runtime_lifecycle=self._profile_lifecycle.borrower,
                    cpu_native_root=cpu_native_root,
                ),
                self.successors,
            )
        return self._services

    def _guarded_owner_inputs(self, owner_inputs):
        guard = self._image_runtime_selection_guard
        if guard is None:
            return owner_inputs

        def guarded(manifest, plan):
            guard()
            return owner_inputs(manifest, plan)

        return guarded

    def _discovered_owner_inputs(self, executable: Path):
        """Defer bounded class discovery and cache creation to the worker call."""
        platform = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
        native_root = self._cpu_native_root
        assert native_root is not None

        def inputs(manifest, plan):
            from mif_pipeline.orchestration.batch_segmentation_settings import current_segmentation_settings
            if plan.normalized_manifest_sha256 != manifest.normalized_manifest_sha256:
                raise ValueError("prerequisite plan differs from validated manifest")
            inventory = self._bioformats_inventory
            if inventory is None:
                try:
                    inventory = selected_offline_bioformats_inventory()
                except InventoryMissing:
                    pass
            if not any(
                any(model is not None and model.model_version == MODEL_VERSION
                    for model in current_segmentation_settings(manifest, plan, sample.sample_id))
                or any(node.kind=='segmentation' and node.section_target is not None
                       and sample.sample_id in node.participant_ids for node in plan.nodes)
                or requires_paired_alignment(sample, plan)
                for sample in manifest.enabled_samples
            ):
                return BatchPrerequisiteOwnerInputs(inventory, {})
            with self._origin_lock:
                if self._closed:
                    raise BatchServiceError("production desktop dependencies are closed")
                if self._origin_paths is None:
                    observed = probe_qupath_class_origins(
                        executable, launch_binding=self._launch_binding
                    )
                    paths = tuple(
                        (
                            item.code_source
                            if item.status == "candidate" and isinstance(item.code_source, Path)
                            else None
                        )
                        for item in (
                            observed.instanseg,
                            observed.djl,
                            observed.pytorch_engine,
                            observed.warpy,
                        )
                    )
                    if all(paths[:3]) or paths[3] is not None:
                        self._origin_paths = paths
                    else:
                        return BatchPrerequisiteOwnerInputs(inventory, {})
                if self._session_cache is None:
                    self._session_cache = tempfile.TemporaryDirectory(prefix="mif-qupath-probe-")
                cache_root = Path(self._session_cache.name)
                instanseg_jar, djl_jar, pytorch_engine_jar, warpy_jar = self._origin_paths
            return prototype_owner_inputs_supplier(
                qupath_executable=executable,
                instanseg_jar=instanseg_jar,
                djl_jar=djl_jar,
                pytorch_engine_jar=pytorch_engine_jar,
                native_root=native_root,
                cache_root=cache_root,
                platform=platform,
                bioformats_inventory=inventory,
                launch_binding=self._launch_binding,
                warpy_jar=warpy_jar,
            )(manifest, plan)

        return inputs

    def close(self) -> None:
        """Release retained backend contexts after managed desktop work ends."""
        if self._closed:
            return
        if self._services is not None:
            self._services[2].require_qupath_stopped()
        self._closed = True
        if self._provider is not None:
            self._provider.close()
        permit = self._profile_lifecycle.begin_teardown()
        self._profile_lifecycle.finish_teardown(permit, failure=None)
        with self._origin_lock:
            if self._session_cache is not None:
                self._session_cache.cleanup()
                self._session_cache = None


def build_production_desktop_dependencies(
    preferences_path: Path,
    *,
    prerequisites: FirstRunPrerequisiteService | None = None,
    instanseg_jar: Path | None = None,
    djl_jar: Path | None = None,
    pytorch_engine_jar: Path | None = None,
    native_root: Path | None = None,
    cache_root: Path | None = None,
    bioformats_inventory: BioFormatsRuntimeInventory | None = None,
    discover_class_origins: bool = False,
    cpu_native_root: Path | None = None,
    image_runtime_selection_guard: Callable[[], None] | None = None,
    launch_binding: QuPathProfileLaunchBinding | None = None,
) -> ProductionDesktopDependencies:
    """Build first-run even when preferences and authority do not yet exist."""
    return ProductionDesktopDependencies(
        prerequisites or FirstRunPrerequisiteService(preferences_path=preferences_path),
        instanseg_jar=instanseg_jar,
        djl_jar=djl_jar,
        pytorch_engine_jar=pytorch_engine_jar,
        native_root=native_root,
        cache_root=cache_root,
        bioformats_inventory=bioformats_inventory,
        discover_class_origins=discover_class_origins,
        cpu_native_root=cpu_native_root,
        image_runtime_selection_guard=image_runtime_selection_guard,
        launch_binding=launch_binding,
        software_preferences_path=preferences_path,
    )
