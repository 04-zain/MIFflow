"""

Opens the desktop, restores selected software and connects setup results to the analysis pages.

We construct one window containing the welcome, home and analysis views.
The shared background-job bridge runs slow setup and processing operations;
Qt signals deliver their results to the pages. Analysis services are created
after the computer and selected QuPath installation pass the initial checks.

Local launch restores Java, Bio-Formats, QuPath and model locations from the
saved preferences, with explicit environment selections taking precedence
for image reading. The setup controllers receive separate InstanSeg, Warpy,
Elastix and image-reader operations. When the application session finishes,
its surrounding context closes the shared processing dependencies.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import sys
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Iterator

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from ..desktop_services import BatchServiceProvider, DesktopFileOperations
from ..desktop_preferences import desktop_preferences_path, load_desktop_preferences, write_desktop_preferences
from mif_pipeline.qupath_runtime.managed_profile import (
    load_managed_qupath_profile, BundledExtensionUpdateRequired,
)
from mif_pipeline.qupath_runtime.class_origin_candidate import probe_qupath_class_origins
from ..first_run import FirstRunPrerequisiteService, FirstRunPrerequisiteState
from ..production_desktop import (
    DesktopServices,
    ProductionDesktopDependencies,
    build_production_desktop_dependencies,
)
from ..source_series import SourceSeriesInspectionPort
from ..successor_services import SuccessorServiceRegistry
from mif_pipeline.orchestration.setup.prerequisite_setup import CpuSetupService
from mif_pipeline.orchestration.setup.prerequisite_setup import default_cpu_setup_target
from mif_pipeline.orchestration.setup.image_runtime_setup import ImageRuntimeSetupService
from mif_pipeline.orchestration.setup.image_runtime_setup import ImageRuntimeSetupStatus
from mif_pipeline.io import configure_offline_runtime_selection, DependencyVersionError
from mif_pipeline.orchestration.setup.instanseg_setup import InstanSegSetupService
from mif_pipeline.orchestration.setup.instanseg_setup import default_instanseg_model_target
from mif_pipeline.orchestration.setup.nimbus_setup import NimbusSetupService
from .controller import QtDesktopController, SetupDiscardCloseRequest
from .cpu_setup import CpuSetupPanel, CpuSetupPort
from .error_copy import diagnostic_details, operation_error_summary
from .operation_error import OperationErrorDialog
from .first_run import ComputerSetupPanel, WelcomePage
from .home import HomePage
from .jobs import ManagedJobBridge
from .image_runtime_setup import ImageRuntimeSetupPort, ImageRuntimeSetupPanel
from .shell import WorkspaceShell
from .theme import apply_desktop_theme

# ==============================================================================
#                                  Application
# ==============================================================================


class DesktopApplicationComposition(QObject):
    """

    Connects the initial computer setup, home page and active analysis controller in one window.

    We show the welcome/setup view until the computer is prepared. When its
    required checks pass, the selected analysis services are attached and the
    normal controller handles image selection, processing and reviews. The
    same background-job bridge is used before and after this transition.

    Attributes:
        shell_created: Signal emitted when the analysis controller and its
            prepared window become available.
    """

    shell_created = Signal(object)

    def __init__(
        self,
        prerequisites: FirstRunPrerequisiteService,
        jobs: ManagedJobBridge,
        *,
        batch_services: BatchServiceProvider | None = None,
        source_series_inspection: SourceSeriesInspectionPort | None = None,
        file_operations: DesktopFileOperations | None = None,
        successors: SuccessorServiceRegistry = SuccessorServiceRegistry(),
        services_factory: Callable[[FirstRunPrerequisiteState], DesktopServices] | None = None,
        cpu_setup_service: CpuSetupPort | None = None,
        nimbus_setup_service: NimbusSetupService | None = None,
        instanseg_service_factory: (
            Callable[[Path | None], InstanSegSetupService | None] | None
        ) = None,
        invalidate_class_origins: Callable[[], None] | None = None,
        image_runtime_setup_service: ImageRuntimeSetupPort | None = None,
        accept_image_runtime_choice: Callable[[ImageRuntimeSetupStatus], str | None] | None = None,
        refresh_image_runtime_service: Callable[[], ImageRuntimeSetupPort] | None = None,
        warpy_service_factory=None,
        alignment_program_setup_service=None,
        manifest_chooser: Callable[[], Path | None] | None = None,
        shell_factory: Callable[[HomePage], WorkspaceShell] | None = None,
        allow_authority_initialization: bool = True,
        parent: QObject | None = None,
    ) -> None:
        """

        Constructs the initial pages and connects their setup, close and readiness signals.

        We accept either already constructed processing services or a factory that
        builds them after setup. The source-series inspector accompanies supplied
        batch services. The home page receives the independent software setup
        operations, while the computer-setup panel performs the initial check and
        reports when the analysis controller can be created.

        Args:
            prerequisites: Service checking the selected QuPath program and local
                support required to create and open analyses.
            jobs: Shared background-job bridge for setup and later processing.
            batch_services: Optional prepared analysis-service provider.
            source_series_inspection: Source-image inspector supplied with batch_services.
            file_operations: Optional analysis file-opening and creation operations.
            successors: Registry of supported transitions between completed analysis steps.
            services_factory: Optional factory called after prerequisite checks succeed.
            cpu_setup_service: Native CPU-library inspection and installation operations.
            nimbus_setup_service: Optional marker-expression checkpoint selection and installation.
            instanseg_service_factory: Factory using the currently selected QuPath
                program and optional extension directory.
            invalidate_class_origins: Callback clearing cached loaded-extension information.
            image_runtime_setup_service: Selected Java/Bio-Formats setup operations.
            accept_image_runtime_choice: Callback saving a checked Java or Bio-Formats location.
            refresh_image_runtime_service: Factory reflecting newly selected image-reader paths.
            warpy_service_factory: Factory for the separate Warpy extension check/install action.
            alignment_program_setup_service: Elastix/Transformix check, selection and installation.
            manifest_chooser: Optional programmatic chooser used to open an explicit
                analysis manifest; normal desktop opening chooses a saved analysis folder.
            shell_factory: Optional constructor receiving the home page and returning
                a WorkspaceShell, used with explicitly supplied services.
            allow_authority_initialization: Whether the welcome/setup page may prepare
                the local filesystem support required for analysis storage.
            parent: Optional Qt parent of this controller object.

        Raises:
            ValueError: Required paired services are incomplete or construction modes conflict.
            TypeError: The supplied window factory returns an unsupported window type.
        """

        super().__init__(parent)
        if (batch_services is None) != (source_series_inspection is None):
            raise ValueError(
                "batch services and source-series inspection must be supplied together"
            )
        if services_factory is not None and (
            batch_services is not None or shell_factory is not None
        ):
            raise ValueError("deferred services cannot replace injected desktop composition")
        if shell_factory is None and batch_services is None and services_factory is None:
            raise ValueError("desktop composition requires injected batch services")
        self._services_factory = services_factory
        self._cpu_setup_service = cpu_setup_service
        self._nimbus_setup_service = nimbus_setup_service
        self._instanseg_service_factory = instanseg_service_factory
        self._invalidate_class_origins = invalidate_class_origins
        self._verified_model_directory: Path | None = None
        self._image_runtime_setup_service = image_runtime_setup_service
        self._accept_image_runtime_choice = accept_image_runtime_choice
        self._refresh_image_runtime_service = refresh_image_runtime_service
        self._jobs = jobs
        self._batch_services = batch_services
        self._source_series_inspection = source_series_inspection
        self._file_operations = file_operations
        self._successors = successors
        self._manifest_chooser = manifest_chooser
        self._last_observed_prepared: bool | None = None
        self._registration_setup_busy = False
        self.controller: QtDesktopController | None = None
        self.computer_setup = ComputerSetupPanel(
            prerequisites,
            jobs,
            auto_inspect=False,
            allow_authority_initialization=allow_authority_initialization,
        )
        self.welcome = WelcomePage(self.computer_setup)
        self.home = HomePage(
            draft=None,
            prerequisite_state=None,
            admitted=False,
            computer_setup=self.computer_setup,
            manifest_chooser=self._manifest_chooser,
            cpu_setup_service=self._cpu_setup_service,
            nimbus_setup_service=self._nimbus_setup_service,
            instanseg_service_factory=self._instanseg_service_factory,
            on_extension_installed=self._extension_installed,
            on_verified_model=self._verified_model,
            image_runtime_setup_service=self._image_runtime_setup_service,
            accept_image_runtime_choice=self._accept_image_runtime_choice,
            refresh_image_runtime_service=self._refresh_image_runtime_service,
            warpy_service_factory=warpy_service_factory,
            alignment_program_setup_service=alignment_program_setup_service,
            jobs=self._jobs,
        )
        self.shell = (
            shell_factory(self.home)
            if shell_factory
            else WorkspaceShell(home_page=self.home, admitted=False)
        )
        if not isinstance(self.shell, WorkspaceShell):
            raise TypeError("configured desktop shell must be a WorkspaceShell")
        self.shell.set_admitted(False)
        self.shell.install_welcome(self.welcome)
        self.home.registration_setup_busy.connect(self._set_registration_setup_busy)
        self.shell.show_welcome(True)
        self.welcome.about_requested.connect(self.shell._show_about)
        self.shell.close_requested.connect(self._bootstrap_close)
        self.computer_setup.close_ready.connect(self._bootstrap_closed)
        self.computer_setup.state_changed.connect(self._prerequisite_state_changed)
        self.computer_setup.prerequisites_ready.connect(self._prerequisites_ready)
        self.computer_setup.refresh()

    @property
    def current_window(self) -> WorkspaceShell:
        """

        Returns the shared desktop window containing the current welcome, setup or analysis page.

        Returns:
            WorkspaceShell created for this application session.
        """

        return self.shell


    def _bootstrap_close(self) -> None:
        """

        Starts closing the application while it is still on the initial setup pages.

        We disable Home and Welcome and ask the computer-setup panel to finish its
        close sequence. Its close_ready signal triggers the final window close
        after that panel has released its background setup work.
        """

        self.home.setEnabled(False)
        self.welcome.setEnabled(False)
        self.computer_setup.begin_close()

    def _prerequisite_state_changed(self, state: FirstRunPrerequisiteState) -> None:
        """

        Updates the home/software controls when the computer-setup check returns a new state.

        We defer these changes while registration software setup is active. Otherwise
        we update the home summary and, when QuPath is ready, refresh the InstanSeg
        information. The first transition to a prepared computer opens Software
        setup. The welcome page is shown or hidden when that preparation state changes.

        Args:
            state: Latest computer and QuPath prerequisite result.
        """

        if self._registration_setup_busy:
            return
        self.home.set_prerequisite_state(state)
        self.shell.set_admitted(state.ready and self.controller is not None)
        if self.computer_setup.close_pending:
            return
        if state.qupath.state == "ready":
            for panel in self.home.findChildren(CpuSetupPanel):
                panel.refresh_instanseg()
        prepared = state.authority_v2.state == "ready"
        if prepared:
            self.computer_setup.show_home_controls()
        previous_prepared = self._last_observed_prepared
        first_preparation = previous_prepared is False and prepared
        self._last_observed_prepared = prepared
        if first_preparation:
            self.home.show_software_setup()
        if previous_prepared is None or previous_prepared != prepared:
            self.shell.show_welcome(not prepared)

    def _bootstrap_closed(self) -> None:
        """

        Closes the initial desktop window after its setup panel reports that closing is ready.

        This handles the pre-analysis window. Once the analysis controller exists,
        its own close sequence is connected to the window's close request.
        """

        if self.controller is None and self.computer_setup.close_pending:
            self.shell.allow_close()
            self.shell.close()

    def _prerequisites_ready(self, state: FirstRunPrerequisiteState) -> None:
        """

        Creates the analysis controller once the required setup checks have passed.

        We wait while registration setup or closing is active and construct this
        controller only once. A deferred service factory supplies the processing,
        source inspection, file and transition operations at this point. We then
        connect processing errors and unsaved-close confirmation, replace the initial
        close handler, and enable the prepared analysis window.

        Args:
            state: Successful prerequisite result used by the service factory.
        """

        if (
            self._registration_setup_busy
            or self.controller is not None
            or self.computer_setup.close_pending
            or not state.ready
        ):
            return
        if self._services_factory is not None:
            (
                self._batch_services,
                self._source_series_inspection,
                self._file_operations,
                self._successors,
            ) = self._services_factory(state)
        if self._batch_services is None:
            return
        self.controller = QtDesktopController(
            shell=self.shell,
            jobs=self._jobs,
            batch_services=self._batch_services,
            source_series_inspection=self._source_series_inspection,
            file_operations=self._file_operations,
            successors=self._successors,
            segmentation_model_directory=self._verified_model_directory,
        )
        self.controller.operation_failed.connect(self._show_operation_error)
        self.controller.discard_setup_close_requested.connect(self._confirm_discard_setup_close)
        self.shell.close_requested.disconnect(self._bootstrap_close)
        self.shell.set_admitted(True)
        self.shell_created.emit(self.shell)


    def _set_registration_setup_busy(self, busy: bool) -> None:
        """

        Prevents overlapping software changes and analysis opening while registration setup is active.

        We update the window's analysis availability and the computer, cell-segmentation
        and image-reader setup panels together. When the operation finishes, an existing
        analysis controller can become available again.

        Args:
            busy: Whether the registration setup panel has a pending background operation.
        """

        self._registration_setup_busy = busy
        self.shell.set_admitted(not busy and self.controller is not None)
        self.computer_setup.setEnabled(not busy)
        for kind in (CpuSetupPanel, ImageRuntimeSetupPanel):
            for panel in self.home.findChildren(kind):
                panel.setEnabled(not busy)
        if not busy and self.computer_setup.state is not None:
            # Registration checks can begin while first preparation emits its
            # ready state. Apply the retained result once those checks finish.
            self._prerequisite_state_changed(self.computer_setup.state)
            self._prerequisites_ready(self.computer_setup.state)

    def _extension_installed(self) -> None:
        """

        Clears cached extension-source information after a setup check or change requests it.

        The next native prerequisite observation can then report the files currently
        loaded by the selected QuPath installation.
        """

        if self._invalidate_class_origins is not None:
            self._invalidate_class_origins()

    def _verified_model(self, directory: Path) -> None:
        """

        Retains the checked segmentation-model directory and supplies it to an active analysis controller.

        Args:
            directory: Model location that passed the setup file checks.
        """

        self._verified_model_directory = directory
        if self.controller is not None:
            self.controller.set_verified_segmentation_model_directory(directory)

    def _show_operation_error(self, detail: str) -> None:
        """

        Displays a failed action with a brief explanation and its complete diagnostic text.

        We obtain the operation-specific summary where available and place the
        remaining detail in the dialog's expandable section. Text is interpreted
        as plain text so filenames and error output retain their literal contents.
        The dialog opens without blocking another nested Qt event loop and is
        released when it closes.

        Args:
            detail: Diagnostic text received from the failed analysis operation.
        """

        if not isinstance(self.shell, QWidget):
            return
        if isinstance(self.shell, WorkspaceShell):
            self.shell.statusBar().clearMessage()
        run_page = None if self.controller is None else getattr(self.controller, "run_page", None)
        summary = operation_error_summary(detail)
        explanation = summary or (
            "We could not determine why this action stopped. "
            "Open Details to inspect the original error, or copy the details to report the problem."
        )
        diagnostic = detail
        job_error = None if self.controller is None else getattr(self.controller, "current_job_error", None)
        if job_error is not None and job_error.diagnostic:
            diagnostic = (job_error.diagnostic if detail in job_error.diagnostic
                          else detail + "\n\n" + job_error.diagnostic)
        run_failure = detail.startswith(("Analysis could not start or continue.", "Revised options could not be applied."))
        if run_failure and run_page is not None:
            diagnostic += "\n\n" + run_page.operation_diagnostic()
        box = OperationErrorDialog(self.shell, explanation, diagnostic)
        if run_failure and run_page is not None and not self.controller.jobs.has_active_jobs:
            def open_stopped_step() -> None:
                self.shell.show_route("run")
                run_page.open_stopped_step()

            retry_label = run_page.retry_action_label()
            if retry_label is not None:
                def select_retry() -> None:
                    self.shell.show_route("run")
                    run_page.open_retry_selection()
                box.add_recovery_action(retry_label, select_retry)
            elif run_page.has_stopped_step():
                box.add_recovery_action("Open stopped step", open_stopped_step)
            elif run_page.can_continue_saved_work():
                box.add_recovery_action("Continue saved work", self.controller.continue_saved_work)
        box.open()

    def _confirm_discard_setup_close(self, request: SetupDiscardCloseRequest) -> None:
        """

        Requests an explicit decision when the current setup cannot be saved while closing.

        We show the failure details and offer Cancel or Exit without saving, with
        Cancel as the default and escape action. The selected response is sent to
        the analysis controller using the current close-request ID. Closing the
        dialog without choosing Exit retains editing.

        Args:
            request: Current SetupDiscardCloseRequest containing its ID and failed-save reason.
        """

        if type(request) is not SetupDiscardCloseRequest or self.controller is None:
            return
        box = QMessageBox(self.shell)
        box.setObjectName("discard-setup-close")
        box.setWindowTitle("Exit without saving?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        explanation = (
            "The app could not save the current setup. You can keep editing or close "
            "and discard unsaved changes."
        )
        box.setText(explanation)
        box.setInformativeText(
            "Closing will discard unsaved setup changes. Your analysis folder and "
            "original images will be kept."
        )
        box.setDetailedText(diagnostic_details(request.reason, explanation))
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        exit_button = box.addButton("Exit without saving", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        controller = self.controller

        def choose(clicked: object) -> None:
            """

            Sends the selected close decision to the controller that issued the request.

            Args:
                clicked: Cancel or Exit button selected in this confirmation dialog.
            """

            if clicked is exit_button:
                controller.confirm_discard_setup_close(request.request_id)
            elif clicked is cancel:
                controller.cancel_discard_setup_close(request.request_id)

        box.buttonClicked.connect(choose)
        box.finished.connect(
            lambda _result: controller.cancel_discard_setup_close(request.request_id)
        )
        box.open()


def launch(
    *,
    batch_services: BatchServiceProvider | None = None,
    source_series_inspection: SourceSeriesInspectionPort | None = None,
    prerequisites: FirstRunPrerequisiteService,
    file_operations: DesktopFileOperations | None = None,
    successors: SuccessorServiceRegistry = SuccessorServiceRegistry(),
    services_factory: Callable[[FirstRunPrerequisiteState], DesktopServices] | None = None,
    cpu_setup_service: CpuSetupPort | None = None,
    nimbus_setup_service: NimbusSetupService | None = None,
    instanseg_service_factory: Callable[[Path | None], InstanSegSetupService | None] | None = None,
    invalidate_class_origins: Callable[[], None] | None = None,
    image_runtime_setup_service: ImageRuntimeSetupPort | None = None,
    accept_image_runtime_choice: Callable[[ImageRuntimeSetupStatus], str | None] | None = None,
    refresh_image_runtime_service: Callable[[], ImageRuntimeSetupPort] | None = None,
    warpy_service_factory=None,
    alignment_program_setup_service=None,
    argv: Sequence[str] = (),
) -> int:
    """

    Runs the desktop using the supplied processing and software-setup operations.

    We create or reuse QApplication, apply the shared theme, construct the
    background-job bridge and create the application controller. The current
    window is shown before entering Qt's event loop. Completion returns the
    event loop's exit code to the command-line launcher.

    Args:
        batch_services: Optional prepared analysis-service provider.
        source_series_inspection: Inspector supplied with prepared batch services.
        prerequisites: Required computer and QuPath setup-check service.
        file_operations: Optional analysis file operations.
        successors: Supported analysis-step transitions.
        services_factory: Optional factory creating processing services after setup.
        cpu_setup_service: CPU inference-library setup operations.
        nimbus_setup_service: Optional Nimbus checkpoint setup operations.
        instanseg_service_factory: Factory for the current extension/model selection.
        invalidate_class_origins: Callback refreshing loaded-extension information.
        image_runtime_setup_service: Java/Bio-Formats setup operations.
        accept_image_runtime_choice: Callback saving checked image-reader locations.
        refresh_image_runtime_service: Factory reflecting newly chosen locations.
        warpy_service_factory: Separate Warpy extension setup factory.
        alignment_program_setup_service: Elastix/Transformix setup operations.
        argv: Additional command arguments supplied to a new QApplication.

    Returns:
        Integer exit status returned by the Qt application event loop.

    Raises:
        RuntimeError: A non-GUI Qt application is already active in the process.
        ValueError or TypeError: The supplied application-service construction is invalid.
    """

    existing = QApplication.instance()
    if existing is not None and not isinstance(existing, QApplication):
        raise RuntimeError("Qt desktop launch requires a QApplication")
    qt = existing or QApplication([sys.argv[0], *argv])
    apply_desktop_theme(qt)
    jobs = ManagedJobBridge()
    composition = DesktopApplicationComposition(
        prerequisites,
        jobs,
        batch_services=batch_services,
        source_series_inspection=source_series_inspection,
        file_operations=file_operations,
        successors=successors,
        services_factory=services_factory,
        cpu_setup_service=cpu_setup_service,
        nimbus_setup_service=nimbus_setup_service,
        instanseg_service_factory=instanseg_service_factory,
        invalidate_class_origins=invalidate_class_origins,
        image_runtime_setup_service=image_runtime_setup_service,
        accept_image_runtime_choice=accept_image_runtime_choice,
        refresh_image_runtime_service=refresh_image_runtime_service,
        warpy_service_factory=warpy_service_factory,
        alignment_program_setup_service=alignment_program_setup_service,
    )
    composition.current_window.show()
    return qt.exec()


@dataclass(frozen=True, slots=True)
class LocalDesktopLaunchContext:
    """

    Retains the restored software selections and services for one local desktop session.

    Attributes:
        dependencies: Shared processing services and prerequisite information.
        cpu_setup_service: Selected CPU-library directory and installation operations.
        instanseg_service_factory: Factory using the current QuPath program and model roots.
        invalidate_class_origins: Callback clearing cached loaded-extension information.
        image_runtime_setup_service: Java/Bio-Formats setup constructed from current selections.
        accept_image_runtime_choice: Callback persisting a newly checked reader location.
        refresh_image_runtime_service: Factory rebuilding setup from the latest selected paths.
        warpy_service_factory: Factory for independent Warpy installation and checks.
        alignment_program_setup_service: Elastix/Transformix selection and installation service.
    """

    dependencies: ProductionDesktopDependencies
    cpu_setup_service: CpuSetupService
    instanseg_service_factory: Callable[[Path | None], InstanSegSetupService | None]
    invalidate_class_origins: Callable[[], None]
    image_runtime_setup_service: ImageRuntimeSetupService
    accept_image_runtime_choice: Callable[[ImageRuntimeSetupStatus], str | None]
    refresh_image_runtime_service: Callable[[], ImageRuntimeSetupService]
    nimbus_setup_service: NimbusSetupService | None = None
    warpy_service_factory: object | None = None
    alignment_program_setup_service: object | None = None


@contextmanager
def open_local_desktop_launch_context(
    *,
    preference_path: Path | None = None,
) -> Iterator[LocalDesktopLaunchContext]:
    """

    Restores selected software and supplies the services used throughout a local desktop session.

    We locate the saved preferences and versioned model/native-library directories.
    Explicit JAVA_HOME and MIF_BIOFORMATS_PAYLOAD_ROOT selections take precedence
    over saved paths and are checked before image reading is configured. A saved
    selection that needs correction is retained as a setup error that prevents
    analysis preparation until it is resolved.

    The context provides separate factories for Java/Bio-Formats, InstanSeg and
    Warpy, plus the Elastix setup service. A previously selected managed QuPath
    launch description is reopened with the selected executable, so extension
    checks use that same configured QuPath environment. Extension checks run
    through Software setup after the window opens. Its installation action
    prepares the bundled MIF extension and compatible dependencies.

    Args:
        preference_path: Optional absolute preference-file location; omission
            uses the platform's normal desktop preference path.

    Yields:
        LocalDesktopLaunchContext used by the application while the with block
        remains open. Its shared dependencies are closed when the block exits.

    Raises:
        ValueError or DependencyVersionError: An explicit environment path,
            selected QuPath description or reader configuration is invalid.
    """

    cpu_platform = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
    cpu_target = default_cpu_setup_target(cpu_platform)
    model_target = default_instanseg_model_target(cpu_platform)
    preference_path = desktop_preferences_path() if preference_path is None else preference_path
    try:
        saved = load_desktop_preferences(preference_path)
    except (ValueError, OSError):
        # The first-run page reports invalid saved preferences and offers
        # the relevant setup controls before processing can be prepared.
        saved = None
    launch_binding = None

    def inspect_extensions(info) -> None:
        """Check saved QuPath extensions in the setup worker after the window opens."""
        nonlocal launch_binding
        from mif_pipeline.qupath_runtime.native_image_reader import validate_native_reader

        validate_native_reader()
        current = load_desktop_preferences(preference_path)
        if current is None or current.qupath_profile_manifest is None:
            raise ValueError(
                "QuPath is selected, but its application extension folder has not been prepared. "
                "Install application extensions copies the MIF extension included with this application "
                "and adds the compatible InstanSeg and Warpy extensions, reusing checked local files "
                "or downloading missing ones. The InstanSeg and registration sections also manage "
                "those extensions and provide their additional models and processing software."
            )
        manifest = Path(current.qupath_profile_manifest)
        if (launch_binding is not None and launch_binding.executable == info.path
                and launch_binding.profile_root / "manifest.json" == manifest):
            return
        try:
            selected_binding = load_managed_qupath_profile(manifest, expected_executable=info.path)
        except BundledExtensionUpdateRequired:
            # An application update already supplies this JAR. Reuse the selected
            # dependencies locally; downloads remain an explicit setup action.
            install_extensions(info, consent=False)
            return
        dependencies.select_qupath_launch_binding(selected_binding)
        launch_binding = selected_binding

    def install_extensions(info, *, consent: bool = True) -> None:
        """Prepare a separate extension folder, reusing compatible installed files.

        We keep the user's ordinary QuPath preferences and extensions unchanged.
        The existing installer verifies reused files and downloads missing
        dependencies only after the explicit installation action.
        """
        nonlocal launch_binding
        from mif_pipeline.orchestration.setup.qupath_profile_setup import (
            ManagedQuPathSetupService,
        )
        from mif_pipeline.qupath_runtime import payload_lock
        from mif_pipeline.qupath_runtime.native_image_reader import validate_native_reader

        validate_native_reader()
        previous = load_desktop_preferences(preference_path)
        if previous is None or Path(previous.qupath_executable) != info.path:
            raise ValueError("Select QuPath before installing its application extensions.")
        existing = {}
        if previous.qupath_profile_manifest is not None:
            directory = Path(previous.qupath_profile_manifest).parent / "extensions"
            for key, pin in (("instanseg", payload_lock.SOURCE_ARCHIVE_LOCK["instanseg"]),
                             ("warpy", payload_lock.WARPY_SOURCE_ARCHIVE)):
                candidate = directory / pin[0].rsplit("/", 1)[-1]
                if candidate.is_file():
                    existing[key] = candidate
        else:
            observed = probe_qupath_class_origins(info.path)
            for key in ("instanseg", "warpy"):
                candidate = getattr(observed, key)
                if candidate.status == "candidate" and candidate.code_source is not None:
                    existing[key] = candidate.code_source
        profiles_root = preference_path.parent / "qupath-profiles"
        profiles_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        selected_binding = ManagedQuPathSetupService(
            executable=info.path, profiles_root=profiles_root, platform=cpu_platform,
        ).install(consent=consent, existing_instanseg=existing.get("instanseg"),
                  existing_warpy=existing.get("warpy"))
        if load_desktop_preferences(preference_path) != previous:
            raise ValueError("Software setup changed during installation. Check the selected QuPath program and try again.")
        dependencies.select_qupath_launch_binding(selected_binding)
        write_desktop_preferences(preference_path,
            replace(previous, qupath_profile_manifest=str(selected_binding.profile_root / "manifest.json")))
        launch_binding = selected_binding

    selected: dict[str, Path | None] = {
        "java": (
            Path(os.environ["JAVA_HOME"])
            if "JAVA_HOME" in os.environ
            else Path(saved.java_home) if saved and saved.java_home else None
        ),
        "bioformats": (
            Path(os.environ["MIF_BIOFORMATS_PAYLOAD_ROOT"])
            if "MIF_BIOFORMATS_PAYLOAD_ROOT" in os.environ
            else Path(saved.bioformats_root) if saved and saved.bioformats_root else None
        ),
    }
    frozen_root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else None
    for component, path in selected.items():
        if path is not None and (not path.is_absolute() or ".." in path.parts):
            raise ValueError(f"{component} selection must be an absolute path")
    selection_error: str | None = None
    if selected["java"] is not None or selected["bioformats"] is not None:
        try:
            # Environment selections already take precedence in selected.
            # Missing software is reported by setup, not as a launch failure.
            configure_offline_runtime_selection(
                java_home=selected["java"], bioformats_root=selected["bioformats"],
            )
        except DependencyVersionError as exc:
            selection_error = str(exc)

    def make_image_service() -> ImageRuntimeSetupService:
        """

        Builds image-reader setup from the latest selected Java and Bio-Formats directories.

        Returns:
            ImageRuntimeSetupService with its application-data installation root
            and frozen-application location when present.
        """

        return ImageRuntimeSetupService(
            cpu_platform,
            java_home=selected["java"],
            bioformats_root=selected["bioformats"],
            managed_root=cpu_target.parents[2] / "image-runtime",
            frozen_root=frozen_root,
        )

    def require_current_image_selection() -> None:
        """

        Stops analysis preparation while the selected image reader still needs correction.

        Raises:
            BatchServiceError: The saved reader selection has an unresolved setup error.
        """

        if selection_error is not None:
            from mif_pipeline.orchestration.batch_service import BatchServiceError

            raise BatchServiceError(
                f"Image-reading setup needs attention before planning: {selection_error}"
            )

    dependencies = build_production_desktop_dependencies(
        preference_path,
        discover_class_origins=True,
        cpu_native_root=cpu_target,
        image_runtime_selection_guard=require_current_image_selection,
        launch_binding=launch_binding,
        prerequisites=FirstRunPrerequisiteService(
            preferences_path=preference_path, check_extensions=inspect_extensions,
            install_extensions=install_extensions,
        ),
    )

    def make_instanseg_service(extension_directory: Path | None) -> InstanSegSetupService | None:
        """

        Creates InstanSeg setup for the QuPath program currently reported by the prerequisite service.

        We supply the versioned managed-model root and, for a frozen application,
        its included model directory. When a managed QuPath launch was selected,
        the native inspection uses that same launch description.

        Args:
            extension_directory: Optional explicit extensions folder to compare with QuPath.

        Returns:
            InstanSegSetupService, or None until a QuPath program has been selected.
        """

        info = dependencies.prerequisites.qupath_executable_info
        if info is None:
            return None
        return InstanSegSetupService(
            executable=info.path,
            managed_payload_root=model_target,
            extension_directory=extension_directory,
            bundled_payload_root=(
                Path(sys._MEIPASS) / "qupath-assets"
                if getattr(sys, "_MEIPASS", None) is not None
                else None
            ),
            origin_probe=(
                (lambda path: probe_qupath_class_origins(path, launch_binding=launch_binding))
                if launch_binding is not None
                else None
            ),
        )

    def accept_image_choice(status: ImageRuntimeSetupStatus) -> str | None:
        """

        Saves a checked Java or Bio-Formats location and determines whether it can take effect immediately.

        We try to configure the selected pair in the current process, then save the
        component location in preferences and the context's selected-path mapping.
        A JVM already opened with other settings requires restarting. An explicit
        environment variable pointing elsewhere is reported because it takes
        precedence over the saved choice on the next launch.

        Args:
            status: Ready or file-checked ImageRuntimeSetupStatus with a component location.

        Returns:
            Required restart or environment-correction message, or None when the
            selected location can be used without another action.

        Raises:
            ValueError: The component has no acceptable checked location.
            DependencyVersionError: A new incompatible reader choice cannot be saved
                through the supported restart/correction path.
        """

        nonlocal selection_error
        if status.state not in {"ready", "not_checked"} or status.location is None:
            raise ValueError("Only verified local choices can be saved")
        next_selected = dict(selected)
        next_selected[status.component] = status.location
        restart = False
        try:
            configure_offline_runtime_selection(
                java_home=next_selected["java"],
                bioformats_root=next_selected["bioformats"],
            )
        except DependencyVersionError as exc:
            if "runtime_selection_frozen" in str(exc):
                restart = True
                selection_error = str(exc)
            elif selection_error is not None:
                selection_error = str(exc)
            else:
                raise
        else:
            selection_error = None
        dependencies.prerequisites.save_image_runtime_selection(
            status.component,
            status.location,
        )
        selected.update(next_selected)
        environment_key = (
            "JAVA_HOME" if status.component == "java" else "MIF_BIOFORMATS_PAYLOAD_ROOT"
        )
        environment_path = os.environ.get(environment_key)
        if environment_path and Path(environment_path) != status.location:
            return f"{environment_key} overrides this saved choice on the next launch."
        if restart:
            return "Restart the app to use this selection."
        if selection_error is not None:
            return "Complete the remaining image-reading setup before planning."
        return None



    from mif_pipeline.orchestration.setup.warpy_setup import WarpySetupService
    from mif_pipeline.orchestration.setup.elastix_setup import ElastixSetupService

    def make_warpy_service():
        """

        Creates the independent Warpy setup operation for the selected QuPath program.

        Returns:
            WarpySetupService using any recorded managed launch selection, or None
            until QuPath has been selected.
        """

        info = dependencies.prerequisites.qupath_executable_info
        return (
            WarpySetupService(executable=info.path, launch_binding=launch_binding)
            if info is not None
            else None
        )

    try:
        yield LocalDesktopLaunchContext(
            dependencies=dependencies,
            cpu_setup_service=CpuSetupService(
                cpu_target,
                cpu_platform,
            ),
            instanseg_service_factory=make_instanseg_service,
            nimbus_setup_service=NimbusSetupService(
                platform=cpu_platform, preferences_path=preference_path,
            ),
            invalidate_class_origins=dependencies.invalidate_class_origin_cache,
            image_runtime_setup_service=make_image_service(),
            accept_image_runtime_choice=accept_image_choice,
            refresh_image_runtime_service=make_image_service,
            warpy_service_factory=make_warpy_service,
            alignment_program_setup_service=ElastixSetupService(
                platform=cpu_platform, preferences_path=preference_path
            ),
        )
    finally:
        dependencies.close()


def launch_from_local_preferences(argv: Sequence[str] = ()) -> int:
    """

    Starts the ordinary desktop using the saved local software selections.

    We keep the local launch context open while Qt runs and pass its separate
    setup factories and deferred processing services into launch(). Exiting
    the application then closes those shared dependencies.

    Args:
        argv: Additional desktop command arguments.

    Returns:
        Integer Qt application exit status.
    """

    with open_local_desktop_launch_context() as context:
        return launch(
            prerequisites=context.dependencies.prerequisites,
            services_factory=context.dependencies.ready_services,
            cpu_setup_service=context.cpu_setup_service,
            nimbus_setup_service=context.nimbus_setup_service,
            instanseg_service_factory=context.instanseg_service_factory,
            invalidate_class_origins=context.invalidate_class_origins,
            image_runtime_setup_service=context.image_runtime_setup_service,
            accept_image_runtime_choice=context.accept_image_runtime_choice,
            refresh_image_runtime_service=context.refresh_image_runtime_service,
            warpy_service_factory=context.warpy_service_factory,
            alignment_program_setup_service=context.alignment_program_setup_service,
            argv=argv,
        )


_application_launcher: Callable[[Sequence[str]], int] = launch_from_local_preferences


def main(argv: Sequence[str] | None = None) -> int:
    """

    Passes the selected command arguments to the configured local desktop launcher.

    Args:
        argv: Optional command arguments. Omission uses sys.argv after the program name.

    Returns:
        Integer exit status from the configured application launcher.
    """

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    return _application_launcher(arguments)
