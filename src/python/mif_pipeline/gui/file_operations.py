"""Open the files and native editors requested by the current desktop workflow.

The service validates local paths and obtains the managed QuPath runner before starting an
annotation, landmark or detector draft. It releases that runner when the operation ends and
returns only the result associated with the current request. Ordinary file-opening actions
use the supplied result location."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
from threading import Lock
import webbrowser

from mif_pipeline.qupath_runtime import (
    LinuxContainmentProfile,
    QuPathExecutableInfo,
    QuPathProfileLaunchBinding,
    QuPathRunner,
    qupath_command,
)
from mif_pipeline.pipeline_core.runtime_lifecycle import RuntimeLifecycleBorrower

# ==============================================================================
#                                File operations
# ==============================================================================


def _local_path(path: Path, label: str, *, file_only: bool) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} requires an absolute local path")
    if not path.exists():
        raise ValueError(f"{label} does not exist: {path}")
    if file_only and not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    if not file_only and not (path.is_file() or path.is_dir()):
        raise ValueError(f"{label} is not a file or directory: {path}")
    return path


def _local_image(path: Path, label: str) -> Path:
    """

    Keeps a mounted image locator while checking its regular identity member.

    File-backed images identify themselves through the image file. A processed
    OME-Zarr directory uses its root metadata member for identity; QuPath still
    receives the directory locator. The image reader owns metadata and generation
    validation, so this desktop check does not open pixels or hash the store.

    Args:
        path: Absolute local image file or processed-image directory.
        label: Researcher-facing description used for an invalid local path.

    Returns:
        The original image locator accepted by the shared image identity owner.
    """

    from mif_pipeline.io.backends.base import image_identity_member

    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} requires an absolute local path")
    image_identity_member(path)
    return path


class LocalDesktopFileOperations:
    """Open exact local locators without creating workflow or project authority."""

    def __init__(
        self,
        executable_resolver: Callable[[], QuPathExecutableInfo],
        *,
        launch_binding: QuPathProfileLaunchBinding | None = None,
        runtime_lifecycle: RuntimeLifecycleBorrower | None = None,
        linux_containment_profile: LinuxContainmentProfile = "prototype-local-process-group",
        cpu_native_root: Path | None = None,
    ) -> None:
        if not callable(executable_resolver):
            raise TypeError("QuPath executable resolver must be callable")
        self._executable_resolver = executable_resolver
        if launch_binding is not None:
            if type(launch_binding) is not QuPathProfileLaunchBinding:
                raise TypeError("launch_binding must be a QuPathProfileLaunchBinding")
            qupath_command(launch_binding.executable, launch_binding=launch_binding)
        self._launch_binding = launch_binding
        self._cpu_native_root = cpu_native_root
        if linux_containment_profile not in ("hardened-broker", "prototype-local-process-group"):
            raise ValueError("Linux containment profile is unsupported")
        self._runtime_lifecycle = runtime_lifecycle
        self._linux_containment_profile = linux_containment_profile
        self._qupath_lock = Lock()
        self._qupath_runner: QuPathRunner | None = None
        self._qupath_selection: tuple[Path, str] | None = None
        self._qupath_active = False

    def require_qupath_stopped(self) -> None:
        """Fast close guard for the retained application-launched window."""

        with self._qupath_lock:
            if self._qupath_active:
                raise RuntimeError("QuPath window is still open")
            runner = self._qupath_runner
        if runner is not None:
            runner.require_interactive_stopped()

    def _claim_qupath_runner(self, project: Path | None = None):
        info = self._executable_resolver()
        if not isinstance(info, QuPathExecutableInfo):
            raise ValueError("a probed QuPath executable is required")
        executable = info.path
        qupath_command(executable, *((str(project),) if project is not None else ()), launch_binding=self._launch_binding)
        if self._launch_binding is not None and info.version != self._launch_binding.qupath_version:
            raise ValueError("probed QuPath version differs from the selected launch binding")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"probed QuPath executable is unavailable: {executable}")
        selection = (executable, info.version)
        with self._qupath_lock:
            if self._qupath_active:
                raise RuntimeError("QuPath window is still open")
            if self._qupath_runner is not None:
                self._qupath_runner.require_interactive_stopped()
            if self._qupath_runner is None or self._qupath_selection != selection:
                self._qupath_runner = QuPathRunner(
                    executable,
                    launch_binding=self._launch_binding,
                    runtime_lifecycle=self._runtime_lifecycle,
                    linux_containment_profile=self._linux_containment_profile,
                    cpu_native_root=self._cpu_native_root,
                )
                self._qupath_selection = selection
            runner = self._qupath_runner
            self._qupath_active = True
        return runner

    def open_qupath(
        self,
        project_path: Path,
        *,
        paired_views: tuple[dict, ...] = (),
        reference_display=None,
        tissue_authoring=None,
        image_appearance_path=None,
        classifier_review=None,
        feature_inspection=None,
        inspection=None,
    ) -> object:
        if reference_display is not None and reference_display.get("mode") not in {"overlay", "side_by_side"}:
            raise ValueError("Open alignment editing and candidate review in the embedded registration editor.")
        project = _local_path(project_path, "QuPath project", file_only=True)
        runner = self._claim_qupath_runner(project)
        try:
            if feature_inspection is not None:
                import json
                from tempfile import TemporaryDirectory
                from mif_pipeline.orchestration.batch_training.qupath_feature_settings import PixelImageFeatures

                # A preview edits the form's existing recipe. Its temporary return
                # is consumed when the owned QuPath window closes.
                log_directory = project.parent / "logs"
                log_directory.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(prefix="image-features-", dir=log_directory) as folder:
                    result_path = Path(folder) / "features.json"
                    request = dict(feature_inspection, result_path=str(result_path))
                    completed = runner.run_interactive_project(project, log_directory=log_directory,
                        feature_inspection=request, image_appearance_path=image_appearance_path, inspection={})
                    if (completed.returncode != 0
                            or completed.drain_evidence.termination_reason != "normal_exit"):
                        raise RuntimeError("The image-feature preview could not finish. Reopen it to try again.")
                    result = {"status": "feature_settings_cancelled",
                              "classifier_index": feature_inspection["classifier_index"]}
                    if result_path.is_file():
                        recipe = PixelImageFeatures.from_mapping(json.loads(result_path.read_text()))
                        result.update(status="feature_settings_saved", pixel_features=recipe.to_mapping())
                    return result
            if tissue_authoring is not None:
                from mif_pipeline.orchestration.tissue_authoring_draft import edit_tissue_detector

                return edit_tissue_detector(runner, project, tissue_authoring,
                                            image_appearance_path=image_appearance_path)
            if reference_display is not None:
                return runner.run_interactive_project(
                    project,
                    log_directory=project.parent / "logs",
                    paired_views=paired_views,
                    reference_display=reference_display,
                    image_appearance_path=image_appearance_path, classifier_review=classifier_review, inspection=inspection,
                )
            if paired_views:
                return runner.run_interactive_project(
                    project, log_directory=project.parent / "logs", paired_views=paired_views, image_appearance_path=image_appearance_path, classifier_review=classifier_review, inspection=inspection
                )
            return runner.run_interactive_project(project, log_directory=project.parent / "logs",
                                                  image_appearance_path=image_appearance_path, classifier_review=classifier_review, inspection=inspection)
        except OSError as exc:
            raise RuntimeError(f"could not open QuPath project: {project}") from exc
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def run_registration_editor(self, session) -> object:
        """Retain the existing native process gate for the embedded registration page."""
        from mif_pipeline.qupath_runtime.registration_editor import RegistrationEditorSession
        if not isinstance(session, RegistrationEditorSession):
            raise TypeError("A prepared registration session is required")
        runner = self._claim_qupath_runner(session.project)
        try:
            completed = runner.run_registration_editor(session)
            if completed.returncode != 0 or completed.drain_evidence.termination_reason != "normal_exit":
                raise RuntimeError("The registration reader could not finish. Inspect its native log.")
            if session.draft_error is not None:
                raise RuntimeError(session.draft_error)
            return completed
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def select_saved_figure_region(self, service, batch_id: str, expected_head: str,
            artifact_ref: str, figure_id: str, options) -> dict[str, int] | None:
        """Use the existing native process gate while choosing a disposable export rectangle."""
        runner = self._claim_qupath_runner()
        try:
            return service.select_saved_figure_region(batch_id, expected_head_sha256=expected_head,
                artifact_ref_sha256=artifact_ref, figure_id=figure_id, figure_options=options, runner=runner)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def edit_spectral_reference_regions(self, service, batch_id, request_id, expected_head, *, component=None,
                                       preparation_phase=None):
        """Hold the existing native process gate through reference drawing and snapshot."""
        view = service.reference_region_action(batch_id, interaction_request_id=request_id,
            expected_state_head_sha256=expected_head, action="load",
            **({'component': component, 'preparation_phase': preparation_phase} if component is not None else {}))
        runner = self._claim_qupath_runner(Path(view["project_path"]))
        try:
            return service.reference_region_action(batch_id, interaction_request_id=request_id,
                expected_state_head_sha256=expected_head, action="edit", runner=runner,
                **({'component': component, 'preparation_phase': preparation_phase} if component is not None else {}))
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def edit_local_annotations(
        self,
        service,
        batch_id,
        request_id,
        expected_head,
        entry_id,
        folder,
        *,
        preview_selection=None,
        reference_role=None,
        image_appearance_path=None,
        cell_removal_node_id=None,
    ):
        from mif_pipeline.orchestration.local_annotation_review import edit_annotations

        project = _local_path(Path(folder), "Local analysis folder", file_only=False)
        if not project.is_dir():
            raise ValueError("Choose the local analysis folder for this annotation draft.")
        runner = self._claim_qupath_runner(project)
        try:
            return edit_annotations(
                service,
                runner,
                batch_id,
                request_id,
                expected_head,
                entry_id,
                project / "annotation-drafts",
                preview_selection=preview_selection,
                reference_role=reference_role,
                image_appearance_path=image_appearance_path,
                cell_removal_node_id=cell_removal_node_id,
            )
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def edit_local_cell_correction(self, service, batch_id, *, ticket, folder, image_appearance_path=None, recovery=None):
        from mif_pipeline.orchestration.local_annotation_review import edit_cell_correction
        project = _local_path(Path(folder), "Local analysis folder", file_only=False)
        runner = self._claim_qupath_runner(project)
        try:
            return edit_cell_correction(service, runner, batch_id, ticket=ticket,
                draft_folder=project / "annotation-drafts", image_appearance_path=image_appearance_path, recovery=recovery)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def edit_local_cell_corrections(self, service, batch_id, *, tickets, folder, image_appearance_path=None, image_names=None,
                                    recovery=None):
        from mif_pipeline.orchestration.local_annotation_review import edit_cell_corrections
        project = _local_path(Path(folder), "Local analysis folder", file_only=False)
        runner = self._claim_qupath_runner(project)
        try:
            return edit_cell_corrections(service, runner, batch_id, tickets=tickets,
                draft_folder=project / "annotation-drafts", image_appearance_path=image_appearance_path,
                image_names=image_names, recovery=recovery)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def prepare_cluster_annotation_project(self, folder: Path, image: Path, ticket: dict, *, mounted_images=None) -> Path:
        from mif_pipeline.orchestration.cluster.cluster_annotations import prepare_local_project

        source = _local_image(image, "Mounted image")
        if not isinstance(folder, Path) or not folder.is_absolute():
            raise ValueError("Choose an absolute local annotation draft folder")
        runner = self._claim_qupath_runner(source)
        try:
            return prepare_local_project(runner, folder, source, ticket, mounted_images=mounted_images)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def collect_cluster_annotations(self, project: Path):
        from mif_pipeline.orchestration.cluster.cluster_annotations import collect_local_annotations

        source = _local_path(project, "Local annotation project", file_only=True)
        runner = self._claim_qupath_runner(source)
        try:
            return collect_local_annotations(runner, source)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def prepare_cluster_landmark_project(self, folder: Path, images: dict, ticket: dict) -> Path:
        from mif_pipeline.orchestration.cluster.cluster_landmark_exchange import prepare_local_project

        sources = {
            role: _local_image(Path(path), "Mounted registration image")
            for role, path in images.items()
        }
        runner = self._claim_qupath_runner(sources["fixed"])
        try:
            return prepare_local_project(runner, folder, sources, ticket)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def prepare_cluster_registration_editor(self, project: Path):
        """Verify the mounted pair, then prepare its embedded manual editor."""
        from mif_pipeline.orchestration.cluster.cluster_landmark_exchange import verify_local_project
        from mif_pipeline.qupath_runtime.registration_editor import create_registration_session

        project = _local_path(project, "Manual alignment draft", file_only=True)
        runner = self._claim_qupath_runner(project)
        try:
            session = verify_local_project(runner, project)
            view = {
                "mode": "alignment", "method": "manual",
                "fixed_entry_id": session["entries"]["fixed"],
                "moving_entry_id": session["entries"]["moving"],
            }
            return create_registration_session(project, view,
                remote_ticket_sha256=session["ticket"]["ticket_sha256"])
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def prepare_cluster_global_alignment(self, project: Path, result: dict, mounted) -> Path:
        """

        Prepares the returned global fit in the existing local alignment project.

        The exchange owner verifies the ticket and transform through the mounted
        cluster location, then records the global fit beside the local draft. This
        step prepares review and optional local refinement; it does not publish a
        final alignment or accept the registration review. The managed runner is
        released before the controls reopen the same project interactively.

        Args:
            project: Existing local QuPath project for this manual alignment.
            result: Completed cluster global-affine result and its exact identity.
            mounted: Mapping between cluster and mounted local image locations.

        Returns:
            Verified local project path ready to reopen for global review.
        """

        from mif_pipeline.orchestration.cluster.cluster_landmark_exchange import prepare_global_view

        project = _local_path(project, "Manual alignment project", file_only=True)
        runner = self._claim_qupath_runner(project)
        try:
            return prepare_global_view(runner, project, result, mounted)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def prepare_cluster_alignment_candidate(self, project: Path, pending, mounted):
        from mif_pipeline.orchestration.cluster.cluster_landmark_exchange import prepare_candidate_view
        from mif_pipeline.qupath_runtime.registration_editor import create_registration_session

        project = _local_path(project, "Alignment viewing project", file_only=True)
        runner = self._claim_qupath_runner(project)
        try:
            view = prepare_candidate_view(runner, project, pending, mounted)
            return create_registration_session(project, view)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def collect_cluster_landmarks(self, project: Path):
        from mif_pipeline.orchestration.cluster.cluster_landmark_exchange import collect_landmarks

        project = _local_path(project, "Manual alignment draft", file_only=True)
        runner = self._claim_qupath_runner(project)
        try:
            return collect_landmarks(runner, project)
        finally:
            with self._qupath_lock:
                self._qupath_active = False

    def open_plot(self, plot_path: Path) -> bool:
        plot = _local_path(plot_path, "plot", file_only=True)
        return self._open_local_uri(plot)

    def reveal_path(self, path: Path) -> bool:
        target = _local_path(path, "reveal path", file_only=False)
        directory = target if target.is_dir() else target.parent
        return self._open_local_uri(directory)

    @staticmethod
    def _open_local_uri(path: Path) -> bool:
        try:
            opened = webbrowser.open(path.as_uri())
        except (OSError, webbrowser.Error) as exc:
            raise RuntimeError(f"could not open local path: {path}") from exc
        if not opened:
            raise RuntimeError(f"could not open local path: {path}")
        return True


__all__ = ["LocalDesktopFileOperations"]
