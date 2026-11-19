"""

Builds the home and software-setup pages used to create or reopen an analysis.

We collect a new analysis name and local parent folder. The form emits a checked creation
request to the controller, which writes the actual analysis. Opening normally
selects the saved local analysis folder.

Software setup occupies a separate scrollable page within the same window.
Image reading, cell segmentation and paired-image alignment receive their
own setup panels and operations. Creation/opening actions follow the current
computer checks, and software changes are restricted while an analysis is open.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis_setup import NewAnalysisRequest
from ..first_run import FirstRunPrerequisiteState
from ..manifest_draft import ManifestDraft
from ..prototype_notice import PrototypeNotice, trusted_local_prototype_notice
from .cpu_setup import CpuSetupPanel, CpuSetupPort
from .phenotyping_setup import PhenotypingSetupPanel
from mif_pipeline.orchestration.setup.instanseg_setup import InstanSegSetupService
from mif_pipeline.orchestration.setup.nimbus_setup import NimbusSetupService
from .image_runtime_setup import ImageRuntimeSetupPanel, ImageRuntimeSetupPort
from mif_pipeline.orchestration.setup.image_runtime_setup import ImageRuntimeSetupStatus
from .forms import apply_widget_accessibility
from .error_copy import diagnostic_details, field_validation_summary
from .jobs import ManagedJobBridge
from .workbench import PageHeading

# ==============================================================================
#                                      Home
# ==============================================================================


@dataclass(frozen=True, slots=True)
class OpenWorkspaceIntent:
    """

    Retains an explicitly selected analysis-manifest path for a caller-supplied opening action.

    Attributes:
        manifest_path: Absolute saved manifest filename. The ordinary desktop
            opens an analysis folder through the separate folder-request signal.
    """

    manifest_path: Path

    def __post_init__(self) -> None:
        """

        Checks that the explicitly selected manifest location is an absolute Path.

        Raises:
            TypeError: The supplied value is not a Path.
            ValueError: Its filename is relative.
        """

        if not isinstance(self.manifest_path, Path):
            raise TypeError("open workspace intent requires a manifest Path")
        if not self.manifest_path.is_absolute():
            raise ValueError("open workspace intent requires an absolute manifest path")


class HomePage(QWidget):
    """

    Displays analysis-opening controls and the selected computer's software setup.

    We retain current form values and emit requests to the desktop controller.
    File creation, software checks and installations run through the supplied
    operations and background-job bridge.

    Attributes:
        new_workspace_requested: Emits the checked NewAnalysisRequest.
        open_workspace_requested: Emits an explicit manifest-opening request
            when a programmatic manifest chooser was supplied.
        open_folder_requested: Emits the selected saved local analysis directory.
        registration_setup_busy: Reports whether the alignment-software panel
            has a pending background operation.
    """

    new_workspace_requested = Signal(object)
    open_workspace_requested = Signal(object)
    open_folder_requested = Signal(object)
    registration_setup_busy = Signal(bool)
    software_setup_return_requested = Signal()

    def __init__(
        self,
        *,
        draft: ManifestDraft | None,
        prerequisite_state: FirstRunPrerequisiteState | None,
        manifest_chooser: Callable[[], Path | None] | None = None,
        folder_chooser: Callable[[], Path | None] | None = None,
        computer_setup: QWidget | None = None,
        admitted: bool = True,
        parent_directory_chooser: Callable[[], Path | None] | None = None,
        cpu_setup_service: CpuSetupPort | None = None,
        nimbus_setup_service: NimbusSetupService | None = None,
        jobs: ManagedJobBridge | None = None,
        confirm_cpu_download: Callable[[str], bool] | None = None,
        instanseg_service_factory: (
            Callable[[Path | None], InstanSegSetupService | None] | None
        ) = None,
        choose_extension_directory: Callable[[], Path | None] | None = None,
        on_extension_installed: Callable[[], None] | None = None,
        on_verified_model: Callable[[Path], None] | None = None,
        warpy_service_factory=None,
        alignment_program_setup_service=None,
        image_runtime_setup_service: ImageRuntimeSetupPort | None = None,
        accept_image_runtime_choice: Callable[[ImageRuntimeSetupStatus], str | None] | None = None,
        refresh_image_runtime_service: Callable[[], ImageRuntimeSetupPort] | None = None,
        notice: PrototypeNotice | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """

        Constructs the new/open analysis forms and independent software-setup panels.

        New analysis reveals the name and parent folder. Open analysis selects
        a saved workspace.
        The software page contains the supplied computer checks followed by image-
        reading, cell-segmentation, cell-phenotyping and alignment controls. These controls
        use the shared jobs object so their slow checks do not block Qt input and painting.

        Args:
            draft: Existing editable analysis setup to display, or None for a new analysis.
            prerequisite_state: Latest computer/QuPath readiness result, or None
                while the initial check is pending.
            manifest_chooser: Optional programmatic chooser for an explicit saved
                manifest. The normal desktop uses folder_chooser.
            folder_chooser: Optional chooser for the saved local analysis directory.
            computer_setup: Optional initial computer/software-selection widget.
            admitted: Whether the initial checks permit creating or opening an analysis.
            parent_directory_chooser: Optional chooser for a new analysis's local parent.
            cpu_setup_service: Native CPU-library check/install operation.
            nimbus_setup_service: Optional Nimbus checkpoint selection and installation.
            jobs: Shared desktop background-job bridge required by software panels.
            confirm_cpu_download: Optional confirmation callback for software/model downloads.
            instanseg_service_factory: Factory for the currently selected extension/model.
            choose_extension_directory: Optional QuPath extension-folder chooser.
            on_extension_installed: Callback refreshing loaded-extension information.
            on_verified_model: Callback retaining the successfully checked model directory.
            warpy_service_factory: Separate Warpy check/install factory.
            alignment_program_setup_service: Elastix/Transformix check, selection and installation.
            image_runtime_setup_service: Java/Bio-Formats setup operations.
            accept_image_runtime_choice: Callback saving a checked image-reader location.
            refresh_image_runtime_service: Factory reflecting updated reader selections.
            notice: Optional prototype-use notice supplied by the application.
            parent: Optional parent QWidget.

        Raises:
            ValueError: A supplied software panel lacks its required background-job bridge.
            TypeError: A supplied initial draft has an unsupported type.
        """

        super().__init__(parent)
        self._draft = draft
        self._admitted = admitted
        self._computer_setup = computer_setup
        self._software_setup_returns_to_review = False
        self.prerequisite_state = prerequisite_state
        self.notice = notice or trusted_local_prototype_notice()
        self._manifest_chooser = manifest_chooser
        self._folder_chooser = folder_chooser or self._choose_analysis_folder
        self._parent_directory_chooser = parent_directory_chooser or self._choose_directory
        if cpu_setup_service is not None and jobs is None:
            raise ValueError("CPU setup requires the managed desktop jobs")
        if nimbus_setup_service is not None and jobs is None:
            raise ValueError("Phenotyping setup requires the managed desktop jobs")
        if warpy_service_factory is not None and alignment_program_setup_service is not None and jobs is None:
            raise ValueError("Registration setup requires the desktop background jobs")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._page_stack = QStackedWidget(self)
        self._page_stack.setObjectName("home-page-stack")
        scroll = QScrollArea(self._page_stack)
        scroll.setObjectName("home-scroll-area")
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        content = QWidget(scroll)
        content.setObjectName("home-content")
        content.setMaximumWidth(860)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(16)
        heading_row = QHBoxLayout()
        heading_row.setSpacing(18)
        heading_row.addWidget(
            PageHeading(
                "home",
                "Your workspace",
                "Start an analysis or continue saved work.",
                content,
            ),
            1,
        )
        classification = QLabel("Prototype", content)
        classification.setObjectName("prototype-classification")
        heading_row.addWidget(classification, 0, Qt.AlignmentFlag.AlignTop)
        content_layout.addLayout(heading_row)
        self._prerequisite_label = QLabel(content)
        self._prerequisite_label.setObjectName("prerequisite-summary")
        self._prerequisite_label.setWordWrap(True)
        content_layout.addWidget(self._prerequisite_label)
        workspace_actions = QHBoxLayout()
        workspace_actions.setSpacing(10)
        self._start_button = QPushButton("New analysis", content)
        self._start_button.setObjectName("home-start-analysis")
        self._start_button.setProperty("actionRole", "primary")
        self._start_button.clicked.connect(self.focus_new_analysis)
        workspace_actions.addWidget(self._start_button)
        self._open_button = QPushButton("Open analysis…", content)
        self._open_button.setObjectName("home-open-workspace")
        self._open_button.clicked.connect(self._emit_open_workspace)
        workspace_actions.addWidget(self._open_button)
        workspace_actions.addStretch(1)
        content_layout.addLayout(workspace_actions)
        form_box = QFrame(content)
        self._new_analysis_panel = form_box
        form_box.setObjectName("home-start-card")
        start_layout = QVBoxLayout(form_box)
        start_layout.setContentsMargins(18, 18, 18, 18)
        start_layout.setSpacing(10)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self._analysis_name = QLineEdit(form_box)
        self._analysis_name.setObjectName("analysis-name")
        self._analysis_name.setPlaceholderText("Name your analysis")
        form.addRow("Analysis name", self._analysis_name)
        self._output_root = self._path_row(
            form,
            "Save analysis in",
            "output-root",
            "Choose a local folder",
            "Browse…",
            self._parent_directory_chooser,
        )
        start_layout.addLayout(form)
        self._analysis_folder = QLabel("Analysis folder: Not yet selected", form_box)
        self._analysis_folder.setObjectName("home-analysis-folder")
        self._analysis_folder.setWordWrap(True)
        self._analysis_folder.setTextFormat(Qt.TextFormat.PlainText)
        self._analysis_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._analysis_folder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        start_layout.addWidget(self._analysis_folder)
        self._request_error = QLabel(form_box)
        self._request_error.setObjectName("home-request-error")
        self._request_error.setProperty("validationMessage", True)
        self._request_error.setWordWrap(True)
        self._request_error.setTextFormat(Qt.TextFormat.PlainText)
        self._request_error.hide()
        start_layout.addWidget(self._request_error)
        self._request_error_toggle = QToolButton(form_box)
        self._request_error_toggle.setObjectName("home-request-error-details-toggle")
        self._request_error_toggle.setText("Details")
        self._request_error_toggle.setCheckable(True)
        from .researcher_widgets import configure_disclosure

        configure_disclosure(self._request_error_toggle)
        self._request_error_toggle.hide()
        start_layout.addWidget(self._request_error_toggle)
        self._request_error_details = QLabel(form_box)
        self._request_error_details.setObjectName("home-request-error-details")
        self._request_error_details.setWordWrap(True)
        self._request_error_details.setTextFormat(Qt.TextFormat.PlainText)
        self._request_error_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._request_error_details.hide()
        start_layout.addWidget(self._request_error_details)
        self._request_error_toggle.toggled.connect(self._request_error_details.setVisible)
        self._new_button = QPushButton("Create analysis", form_box)
        self._new_button.setObjectName("home-new-workspace")
        self._new_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._new_button.clicked.connect(self._emit_new_workspace)
        for field in (self._analysis_name, self._output_root):
            field.textChanged.connect(self._refresh_new_button)
        create_actions = QHBoxLayout()
        create_actions.setSpacing(12)
        create_actions.addStretch(1)
        create_actions.addWidget(self._new_button)
        start_layout.addLayout(create_actions)
        content_layout.addWidget(form_box)
        form_box.hide()

        divider = QFrame(content)
        divider.setObjectName("home-setup-divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        content_layout.addWidget(divider)
        setup_action = QHBoxLayout()
        setup_action.setSpacing(18)
        setup_copy = QVBoxLayout()
        setup_copy.setSpacing(5)
        setup_heading = QLabel("Software setup", content)
        setup_heading.setObjectName("home-setup-heading")
        setup_heading.setProperty("panelHeading", True)
        setup_copy.addWidget(setup_heading)
        setup_help = QLabel("Check or change installed software.", content)
        setup_help.setProperty("supportingText", True)
        setup_help.setWordWrap(True)
        setup_copy.addWidget(setup_help)
        setup_action.addLayout(setup_copy, 1)
        self._setup_button = QPushButton("Software setup", content)
        self._setup_button.setObjectName("home-software-setup")
        self._setup_button.clicked.connect(lambda: self.show_software_setup())
        setup_action.addWidget(self._setup_button)
        content_layout.addLayout(setup_action)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        self._page_stack.addWidget(scroll)

        setup_scroll = QScrollArea(self._page_stack)
        setup_scroll.setObjectName("software-setup-scroll-area")
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        setup_content = QWidget(setup_scroll)
        setup_content.setObjectName("software-setup-content")
        setup_content.setMaximumWidth(860)
        setup_layout = QVBoxLayout(setup_content)
        setup_layout.setContentsMargins(24, 24, 24, 24)
        setup_layout.setSpacing(18)
        self._setup_back = QPushButton("Back to home", setup_content)
        self._setup_back.setObjectName("software-setup-back")
        self._setup_back.setProperty("actionRole", "navigation")
        self._setup_back.clicked.connect(self._leave_software_setup)
        setup_layout.addWidget(self._setup_back, 0, Qt.AlignmentFlag.AlignLeft)
        setup_layout.addWidget(
            PageHeading(
                "software-setup",
                "Software setup",
                "Check the software this computer uses for analysis.",
                setup_content,
            )
        )
        self._software_lock_notice = QLabel("Close the current analysis before changing the paired-image alignment software.", setup_content)
        self._software_lock_notice.setObjectName("software-setup-analysis-notice")
        self._software_lock_notice.setProperty("supportingText", True)
        self._software_lock_notice.setWordWrap(True)
        self._software_lock_notice.hide()
        setup_layout.addWidget(self._software_lock_notice)
        if computer_setup is not None:
            setup_layout.addWidget(computer_setup)
        if jobs is not None:
            from .gpu_setup import GpuSetupPanel

            setup_layout.addWidget(GpuSetupPanel(jobs, parent=setup_content))

        if image_runtime_setup_service is not None:
            if jobs is None:
                raise ValueError("Image-reading setup requires the managed desktop jobs")
            setup_layout.addWidget(
                ImageRuntimeSetupPanel(
                    image_runtime_setup_service,
                    jobs,
                    accept_choice=accept_image_runtime_choice,
                    refresh_service=refresh_image_runtime_service,
                    parent=setup_content,
                )
            )

        if cpu_setup_service is not None:
            setup_layout.addWidget(
                CpuSetupPanel(
                    cpu_setup_service,
                    jobs,
                    confirm_download=confirm_cpu_download,
                    instanseg_service_factory=instanseg_service_factory,
                    choose_extension_directory=choose_extension_directory,
                    on_extension_installed=on_extension_installed,
                    on_verified_model=on_verified_model,
                    parent=setup_content,
                )
            )

        if nimbus_setup_service is not None:
            setup_layout.addWidget(
                PhenotypingSetupPanel(
                    nimbus_setup_service,
                    jobs,
                    confirm_download=confirm_cpu_download,
                    parent=setup_content,
                )
            )

        if warpy_service_factory is not None and alignment_program_setup_service is not None:
            from .registration_setup import RegistrationSetupPanel

            registration_panel = RegistrationSetupPanel(
                jobs,
                warpy_factory=warpy_service_factory,
                tools_service=alignment_program_setup_service,
                on_changed=on_extension_installed,
                parent=setup_content,
            )
            registration_panel.busy_changed.connect(self.registration_setup_busy.emit)
            setup_layout.addWidget(registration_panel)

        setup_layout.addStretch(1)
        setup_scroll.setWidget(setup_content)
        self._page_stack.addWidget(setup_scroll)
        outer.addWidget(self._page_stack)

        self.set_draft(draft)
        self.set_prerequisite_state(prerequisite_state)
        self.set_admitted(admitted)
        apply_widget_accessibility(self, "home")

    @property
    def prerequisite_summary(self) -> str:
        """

        Returns the current computer and QuPath readiness message shown on Home.

        Returns:
            Visible prerequisite-summary text.
        """

        return self._prerequisite_label.text()

    def set_prerequisite_state(
        self,
        state: FirstRunPrerequisiteState | None,
    ) -> None:
        """

        Updates the readiness message and setup permissions after a computer check.

        While the check is pending, image-reader setup remains disabled. Once
        QuPath is ready, the image-reader panel can offer its checks and selections.
        Registration permissions are refreshed using both this result and whether
        an analysis draft is currently open.

        Args:
            state: Latest FirstRunPrerequisiteState, or None while checking.
        """

        self.prerequisite_state = state
        if state is None:
            self._prerequisite_label.setText(
                "Checking computer setup before you create or open an analysis…"
            )
            self._prerequisite_label.show()
            for panel in self.findChildren(ImageRuntimeSetupPanel):
                panel.set_actions_allowed(False)
            self._update_registration_setup()
            return
        readiness = "Ready" if state.ready else "Attention required"
        self._prerequisite_label.setText(
            (
                "Ready to create or open an analysis."
                if state.ready
                else (
                    "Open Software setup to connect QuPath before creating or opening an analysis."
                    if state.authority_v2.state == "ready"
                    else "Complete Software setup to create or open an analysis."
                )
            )
            if self._computer_setup is not None
            else f"{readiness}: {state.qupath.summary}; {state.authority_v2.summary}"
        )
        self._prerequisite_label.setVisible(not state.ready)
        for panel in self.findChildren(ImageRuntimeSetupPanel):
            panel.set_actions_allowed(state.qupath.state == "ready")
        self._update_registration_setup()

    def set_admitted(self, admitted: bool) -> None:
        """

        Enables or disables analysis opening according to the application's current readiness decision.

        Args:
            admitted: Whether creating and opening analyses is currently permitted.
        """

        self._admitted = admitted
        self._start_button.setEnabled(admitted)
        self._open_button.setEnabled(admitted)
        self._refresh_new_button()

    def set_draft(self, draft: ManifestDraft | None) -> None:
        """

        Displays an existing analysis setup or initializes the new-analysis fields.

        With no draft, we clear the name
        and choose an existing Documents folder or the home directory as the initial
        local parent. An existing draft supplies its analysis name and output root.
        We then refresh creation and software-setup permissions.

        Args:
            draft: ManifestDraft to display, or None to initialize a new analysis.

        Raises:
            TypeError: A non-null draft has an unsupported type.
        """

        if draft is not None and not isinstance(draft, ManifestDraft):
            raise TypeError("Home draft must be a ManifestDraft or None")
        self._draft = draft
        if draft is None:
            self._analysis_name.clear()
            documents_location = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DocumentsLocation
            )
            documents = Path(documents_location) if documents_location else None
            self._output_root.setText(
                str(documents if documents is not None and documents.is_dir() else Path.home())
            )
            self._refresh_new_button()
            self._update_registration_setup()
            return
        self._analysis_name.setText(draft.batch_id)
        self._output_root.setText(str(draft.output_root))
        self._refresh_new_button()
        self._update_registration_setup()

    def _update_registration_setup(self) -> None:
        """

        Updates the independent alignment-software panel using the current analysis and QuPath state.

        The panel records whether an analysis is open and permits installation or
        selection only when QuPath is ready and no draft is active.
        """

        from .registration_setup import RegistrationSetupPanel

        self._software_lock_notice.setVisible(self._draft is not None)
        for panel in self.findChildren(RegistrationSetupPanel):
            panel.set_analysis_open(self._draft is not None)
            panel.set_actions_allowed(
                self.prerequisite_state is not None
                and self.prerequisite_state.qupath.state == "ready"
                and self._draft is None
            )

    def _refresh_new_button(self) -> None:
        """

        Shows the current creation location and enables the action when fields contain text.

        We resolve the folder through the same NewAnalysisRequest used by creation.
        Incomplete or invalid names clear the previous preview; submitting an invalid
        name retains the form and reports its corrective message beside the action.
        """

        try:
            request = NewAnalysisRequest(
                name=self._analysis_name.text(),
                parent_directory=Path(self._output_root.text()),
            ) if self._output_root.text().strip() else None
        except (ValueError, OSError):
            request = None
        self._analysis_folder.setText(
            "Analysis folder: " + str(request.analysis_folder)
            if request is not None else "A folder named after the analysis will be created here."
        )
        self._analysis_folder.setToolTip(
            str(request.analysis_folder) if request is not None else ""
        )
        self._new_button.setEnabled(
            self._admitted
            and all(
                field.text().strip()
                for field in (
                    self._analysis_name,
                    self._output_root,
                )
            )
        )

    def _emit_new_workspace(self) -> None:
        """

        Validates the entered analysis name and selected paths before requesting creation.

        We build NewAnalysisRequest from the name and parent folder. A validation error remains beside the form with
        expandable details, retaining all entered values. A successful construction
        clears that error and emits the request for the controller to create the analysis.
        """

        if not self._admitted:
            return
        if not self._analysis_name.text().strip() or not self._output_root.text().strip():
            return
        try:
            request = NewAnalysisRequest(
                name=self._analysis_name.text(),
                parent_directory=Path(self._output_root.text()),
            )
        except ValueError as exc:
            detail = str(exc)
            self._request_error.setText(
                "The analysis name contains a character that cannot be used in a folder name. "
                "Choose a different name."
                if "path separators or reserved characters" in detail
                else field_validation_summary(detail, "the analysis name")
            )
            self._request_error.show()
            self._request_error_details.setText(
                diagnostic_details(detail, self._request_error.text())
            )
            self._request_error_toggle.setChecked(False)
            self._request_error_toggle.show()
            return
        self._request_error.hide()
        self._request_error_toggle.setChecked(False)
        self._request_error_toggle.hide()
        self._request_error_details.clear()
        self.new_workspace_requested.emit(request)

    def _path_row(
        self,
        form: QFormLayout,
        label: str,
        name: str,
        placeholder: str,
        action: str,
        chooser: Callable[[], Path | None],
    ) -> QLineEdit:
        """

        Adds a read-only path field and a button to choose its directory.

        The chooser supplies the path, and the field's tooltip retains its full
        text when the visible box is narrower.

        Args:
            form: QFormLayout receiving the labelled row.
            label: Visible name of the selected path.
            name: Object name used to identify the field and its buttons.
            placeholder: Text displayed while no path is selected.
            action: Chooser button label.
            chooser: Callable returning a selected Path or None after cancellation.

        Returns:
            QLineEdit containing the current selected path text.
        """

        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit(row)
        field.setObjectName(name)
        field.setReadOnly(True)
        field.setPlaceholderText(placeholder)
        field.textChanged.connect(field.setToolTip)
        layout.addWidget(field, 1)
        browse = QPushButton(action, row)
        browse.setObjectName(f"{name}-browse")

        def choose() -> None:
            """

            Updates the selected-path field when its chooser returns a nonempty value.

            Cancelling the chooser leaves the existing field contents in place.
            """

            selected = chooser()
            if selected is not None and str(selected):
                field.setText(str(selected))

        browse.clicked.connect(choose)
        layout.addWidget(browse)
        form.addRow(label, row)
        return field

    def _choose_directory(self) -> Path | None:
        """

        Opens the local parent-folder chooser using the currently selected parent as its starting location.

        Returns:
            Selected Path, or None when the chooser is cancelled.
        """

        selected = QFileDialog.getExistingDirectory(
            self, "Choose local workspace parent folder", self._output_root.text()
        )
        return Path(selected) if selected else None

    def _choose_analysis_folder(self) -> Path | None:
        """

        Opens the saved local analysis-folder chooser.

        Returns:
            Selected directory, or None after cancellation.
        """

        selected = QFileDialog.getExistingDirectory(self, "Open saved local workspace")
        return Path(selected) if selected else None

    def choose_workspace(self) -> None:
        """

        Starts the same saved-analysis opening action used by the Home button.
        """

        self._emit_open_workspace()

    def show_software_setup(self, *, return_to_review: bool = False, component: str | None = None) -> None:
        """

        Displays software setup while retaining the Home form and requested return route.

        A Nimbus request refreshes the independent phenotyping panel and scrolls to its
        complete section. Cell-segmentation checks remain owned by their separate panel.
        """

        self.set_software_setup_return(return_to_review)
        self._page_stack.setCurrentIndex(1)
        if component == "nimbus":
            for panel in self.findChildren(PhenotypingSetupPanel):
                panel.refresh()
            target = self.findChild(PhenotypingSetupPanel)
            scroll = self.findChild(QScrollArea, "software-setup-scroll-area")
            if target is not None and scroll is not None:
                scroll.ensureWidgetVisible(target)

    def set_software_setup_return(self, return_to_review: bool) -> None:
        self._software_setup_returns_to_review = return_to_review
        self._setup_back.setText("Back to marker review" if return_to_review else "Back to home")

    def _leave_software_setup(self) -> None:
        return_to_review = self._software_setup_returns_to_review
        self.set_software_setup_return(False)
        self.show_workspace()
        if return_to_review:
            self.software_setup_return_requested.emit()

    def show_workspace(self) -> None:
        """

        Returns to the new/open analysis page in the same window.
        """

        self._page_stack.setCurrentIndex(0)

    def focus_new_analysis(self) -> None:
        """

        Shows Home and brings the analysis-name field into view for editing.

        We focus the field, select its current text and scroll the Home page enough
        to reveal it. The selected parent folder is retained.
        """

        self.show_workspace()
        self._new_analysis_panel.show()
        self._analysis_name.setFocus()
        self._analysis_name.selectAll()
        scroll = self.findChild(QScrollArea, "home-scroll-area")
        if scroll is not None:
            scroll.ensureWidgetVisible(self._analysis_name)

    def _emit_open_workspace(self) -> None:
        """

        Requests opening the selected saved analysis after the initial checks permit it.

        We invoke the supplied manifest chooser when explicitly configured, otherwise
        the normal analysis-folder chooser. Cancellation leaves the current view
        unchanged. A selected path is expanded and made absolute before emitting
        its corresponding controller request.
        """

        if not self._admitted:
            return
        selected = (self._manifest_chooser or self._folder_chooser)()
        if selected is None:
            return
        path = (selected if isinstance(selected, Path) else Path(selected)).expanduser()
        if self._manifest_chooser is not None:
            self.open_workspace_requested.emit(OpenWorkspaceIntent(path.resolve(strict=False)))
        else:
            self.open_folder_requested.emit(path.absolute())


__all__ = ["HomePage", "OpenWorkspaceIntent"]
