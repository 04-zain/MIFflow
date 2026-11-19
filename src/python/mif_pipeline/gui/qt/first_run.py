"""Embedded computer prerequisites backed by managed native-thread jobs.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..first_run import (
    FirstRunPrerequisiteItem,
    FirstRunPrerequisiteService,
    FirstRunPrerequisiteState,
)
from mif_pipeline.qupath_runtime import QuPathExecutableInfo
from ..prototype_notice import PrototypeNotice, trusted_local_prototype_notice
from .jobs import DesktopJobError, JobAccess, ManagedJobBridge
from .error_copy import diagnostic_details
from .form_navigation import FormNavigation
from .software_details import open_software_details, software_details_button
from .workbench import PageHeading

# ==============================================================================
#                                   First run
# ==============================================================================


class WelcomePage(QWidget):
    """First computer preparation inside the existing desktop window."""

    about_requested = Signal()

    def __init__(self, setup: "ComputerSetupPanel", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("computer-welcome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setObjectName("welcome-scroll-area")
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        content = QWidget(scroll)
        content.setMaximumWidth(680)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        panel = QFrame(content)
        panel.setObjectName("welcome-panel")
        panel.setMaximumWidth(620)
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(28, 24, 28, 24)
        panel_layout.setSpacing(16)
        brand_row = QHBoxLayout()
        brand = QLabel("mIF Pipeline", panel)
        brand.setObjectName("setup-brand")
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        classification = QLabel("Prototype", panel)
        classification.setObjectName("prototype-classification")
        brand_row.addWidget(classification)
        panel_layout.addLayout(brand_row)
        heading = PageHeading(
            "first-run",
            "Welcome",
            "Set up this computer for multiplex immunofluorescence analysis.",
            panel,
        )
        title = heading.findChild(QLabel, "first-run-title")
        assert title is not None
        title_font = title.font()
        title_font.setPixelSize(24)
        title_font.setBold(True)
        title.setFont(title_font)
        panel_layout.addWidget(heading)
        panel_layout.addWidget(setup.authority_controls)
        setup.move_operation_status(panel_layout)
        footer = QHBoxLayout()
        about = QPushButton("About", panel)
        about.setObjectName("welcome-about")
        about.clicked.connect(self.about_requested)
        footer.addWidget(about)
        footer.addStretch(1)
        footer.addWidget(setup.start_button)
        panel_layout.addLayout(footer)
        layout.addWidget(panel)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)


class _PrerequisiteService(Protocol):
    def inspect(self) -> FirstRunPrerequisiteState: ...
    def discover_qupath(self) -> tuple[QuPathExecutableInfo, ...]: ...
    def select_qupath(self, path: Path) -> FirstRunPrerequisiteState: ...
    def install_qupath_extensions(self) -> FirstRunPrerequisiteState: ...
    def initialize_authority_v2(self) -> FirstRunPrerequisiteState: ...


class _SetupStep(QFrame):
    """Keep setup actions beside a short status and optional service details."""

    def __init__(self, number: str, title: str, hint: str, action: str, parent: QWidget,
                 *, details_title: str | None = None, additional_action: str | None = None) -> None:
        super().__init__(parent)
        self._details_title = details_title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading_row = QHBoxLayout()
        heading_row.setSpacing(12)
        number_label = QLabel(number, self)
        number_label.setObjectName("setup-step-number")
        number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number_label.setVisible(bool(number))
        heading_row.addWidget(number_label, 0, Qt.AlignmentFlag.AlignTop)
        heading = QLabel(title, self)
        heading.setProperty("panelHeading", True)
        heading.setWordWrap(True)
        heading_row.addWidget(heading, 1)
        layout.addLayout(heading_row)
        explanation = QLabel(hint, self)
        explanation.setObjectName("setup-caption")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.controls = QFrame(self)
        self.controls.setObjectName("setup-step-controls")
        self.controls.setMinimumHeight(96)
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(20, 16, 20, 16)
        controls_layout.setSpacing(10)
        layout.addWidget(self.controls)
        self.status = QLabel(self.controls)
        self.status.setProperty("setupReadiness", True)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        controls_layout.addWidget(self.status)
        actions = FormNavigation(self.controls) if details_title else QHBoxLayout()
        self.action = QPushButton(action, self.controls)
        self.action.setProperty("actionRole", "primary")
        actions.addWidget(self.action)
        self.additional_action = None
        if additional_action:
            self.additional_action = QPushButton(additional_action, self.controls)
            actions.addWidget(self.additional_action)
        if not details_title:
            actions.addStretch(1)
        self.toggle = (software_details_button(self.controls) if details_title
                       else QToolButton(self.controls))
        self.toggle.setObjectName("setup-details-toggle")
        self.toggle.setText("Details")
        self.toggle.setCheckable(details_title is None)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        actions.addWidget(self.toggle)
        if details_title:
            controls_layout.addWidget(actions)
        else:
            controls_layout.addLayout(actions)
        self.details = QLabel(self.controls)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.hide()
        controls_layout.addWidget(self.details)
        if details_title:
            self.toggle.setAccessibleName(details_title + " details")
            self.toggle.clicked.connect(self._open_details)
        else:
            self.toggle.toggled.connect(self._show_details)

    def _open_details(self) -> None:
        open_software_details(self, self._details_title, self.status.text(), self.details.text())

    def _show_details(self, visible: bool) -> None:
        self.details.setVisible(visible)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def set_item(self, item: FirstRunPrerequisiteItem) -> None:
        label = {
            "ready": "Ready",
            "missing": "Missing",
            "invalid": "Needs attention",
            "error": "Needs attention",
        }[item.state]
        guidance = {
            ("qupath", "ready"): "",
            (
                "qupath",
                "missing",
            ): "No QuPath application selected.",
            (
                "qupath",
                "invalid",
            ): "The selected QuPath cannot be used. Select QuPath to choose another application.",
            (
                "qupath",
                "error",
            ): "QuPath could not be checked. Open Details before changing the selected app.",
            ("authority-v2", "ready"): "",
            (
                "authority-v2",
                "missing",
            ): "Local application records have not been created.",
            (
                "authority-v2",
                "invalid",
            ): "Local workspace setup cannot be used. Open Details and keep existing setup unchanged.",
            (
                "authority-v2",
                "error",
            ): "Local workspace setup could not be checked. Open Details before changing this setup.",
        }[(item.prerequisite_id, item.state)]
        if item.action_id == "install-qupath-extensions":
            guidance = "Install the application extensions for the selected QuPath program."
        elif item.prerequisite_id == "qupath" and item.state == "error" and item.action_id is None:
            guidance = f"{item.summary}. Open Details."
        self.status.setText(f"{label}: {guidance}" if guidance else label)
        self.details.setText(
            item.detail if self._details_title or item.state == "ready"
            else diagnostic_details(item.detail, guidance)
        )
        self.toggle.setVisible(bool(item.detail))
        if (self._details_title is None and item.state in {"error", "invalid"}
                and item.action_id != "install-qupath-extensions"):
            self.toggle.setChecked(True)
        self.details.setVisible(bool(item.detail) and self.toggle.isChecked())


def _initial_error_state(detail: str) -> FirstRunPrerequisiteState:
    return FirstRunPrerequisiteState(
        FirstRunPrerequisiteItem(
            "qupath",
            "error",
            "QuPath requires attention",
            detail,
            "select-qupath",
        ),
        FirstRunPrerequisiteItem(
            "authority-v2",
            "error",
            "Local workspace setup needs attention",
            detail,
            "initialize-authority",
        ),
        False,
    )


class ComputerSetupPanel(QWidget):
    """Render prerequisite state and marshal all prerequisite work off-thread."""

    state_changed = Signal(object)
    prerequisites_ready = Signal(object)
    close_ready = Signal()

    def __init__(
        self,
        service: FirstRunPrerequisiteService | _PrerequisiteService,
        jobs: ManagedJobBridge,
        *,
        initial_state: FirstRunPrerequisiteState | None = None,
        qupath_chooser: Callable[[], Path | None] | None = None,
        notice: PrototypeNotice | None = None,
        auto_inspect: bool = True,
        allow_authority_initialization: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(jobs, ManagedJobBridge):
            raise TypeError("ComputerSetupPanel requires a ManagedJobBridge")
        self._service = service
        self._jobs = jobs
        self._state = initial_state
        self.notice = notice or trusted_local_prototype_notice()
        self._qupath_chooser = qupath_chooser or self._choose_qupath
        self._allow_authority_initialization = allow_authority_initialization
        self._pending: dict[str, str] = {}
        self._discovery_started = False
        self._close_pending = False
        self._close_queued = False

        content = self
        content_layout = QVBoxLayout(self)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(18)
        self._qupath_step = _SetupStep(
            "",
            "Connect QuPath",
            "Choose the QuPath application installed on this computer.",
            "Select QuPath",
            content,
            details_title="QuPath setup",
            additional_action="Install application extensions",
        )
        self._qupath_step.setObjectName("setup-step-qupath")
        self._qupath_step.controls.setObjectName("setup-step-qupath-controls")
        self._qupath_step.controls.setMinimumHeight(124)
        qupath_heading = QLabel("QuPath", self._qupath_step.controls)
        qupath_heading.setProperty("panelHeading", True)
        self._qupath_step.controls.layout().insertWidget(0, qupath_heading)
        self._qupath_status = self._qupath_step.status
        self._qupath_status.setObjectName("qupath-prerequisite-status")
        self._select_button = self._qupath_step.action
        self._select_button.setProperty("actionRole", "secondary")
        self._select_button.setObjectName("select-qupath")
        self._select_button.clicked.connect(self._select_qupath)
        self._install_extensions_button = self._qupath_step.additional_action
        assert self._install_extensions_button is not None
        self._install_extensions_button.setObjectName("install-qupath-extensions")
        self._install_extensions_button.setProperty("actionRole", "primary")
        self._install_extensions_button.clicked.connect(self._install_extensions)
        self._install_extensions_button.hide()
        discovery_layout = QVBoxLayout()
        self._discovery_status = QLabel(self._qupath_step.controls)
        self._discovery_status.setObjectName("qupath-discovery-status")
        self._discovery_status.setWordWrap(True)
        discovery_layout.addWidget(self._discovery_status)
        candidate_row = QHBoxLayout()
        self._discovered_qupath = QComboBox(self._qupath_step.controls)
        self._discovered_qupath.setObjectName("discovered-qupath")
        self._discovered_qupath.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._discovered_qupath.setMinimumWidth(0)
        candidate_row.addWidget(self._discovered_qupath, 1)
        self._use_discovered_button = QPushButton("Use QuPath", self._qupath_step.controls)
        self._use_discovered_button.setProperty("actionRole", "primary")
        self._use_discovered_button.setObjectName("use-discovered-qupath")
        self._use_discovered_button.clicked.connect(self._use_discovered_qupath)
        candidate_row.addWidget(self._use_discovered_button)
        discovery_layout.addLayout(candidate_row)
        self._qupath_step.controls.layout().insertLayout(1, discovery_layout)
        self._discovery_status.hide()
        self._discovered_qupath.hide()
        self._use_discovered_button.hide()
        content_layout.addWidget(self._qupath_step)

        self._workspace_step = _SetupStep(
            "",
            "Application setup",
            "Review the error before retrying local application setup.",
            "Get started",
            content,
        )
        self._workspace_step.setObjectName("setup-step-workspace")
        self._workspace_step.hide()
        self._workspace_step.controls.setObjectName("setup-step-workspace-controls")
        self._authority_status = self._workspace_step.status
        self._authority_status.setObjectName("authority-prerequisite-status")
        self._initialize_button = self._workspace_step.action
        self._initialize_button.setObjectName("initialize-authority")
        self._initialize_button.clicked.connect(self._start_or_retry)
        self._initialize_button.setEnabled(allow_authority_initialization)
        content_layout.addWidget(self._workspace_step)

        self._operation_status = QLabel(content)
        self._operation_status.setObjectName("first-run-operation-status")
        self._operation_status.setWordWrap(True)
        self._operation_status.hide()
        content_layout.addWidget(self._operation_status)
        self._jobs.job_succeeded.connect(self._job_succeeded)
        self._jobs.job_failed.connect(self._job_failed)
        self._jobs.active_jobs_changed.connect(self._jobs_changed)
        if initial_state is not None:
            self.set_state(initial_state)
        elif auto_inspect:
            self.refresh()

    @property
    def state(self) -> FirstRunPrerequisiteState | None:
        return self._state

    @property
    def authority_controls(self) -> QWidget:
        return self._workspace_step

    @property
    def start_button(self) -> QPushButton:
        """The welcome action; local preparation remains an explicit user choice."""
        return self._initialize_button

    def move_operation_status(self, layout: QVBoxLayout) -> None:
        """Move the single job status to the current prerequisite host."""
        layout.addWidget(self._operation_status)

    def show_home_controls(self) -> None:
        self.move_operation_status(self.layout())

    @property
    def has_pending_operation(self) -> bool:
        return bool(self._pending)

    @property
    def close_pending(self) -> bool:
        return self._close_pending

    def begin_close(self) -> None:
        """Drain joined setup outcomes before the owning shell closes."""
        self._close_pending = True
        self._select_button.setEnabled(False)
        self._use_discovered_button.setEnabled(False)
        self._discovered_qupath.setEnabled(False)
        self._initialize_button.setEnabled(False)
        self._set_operation_status("Waiting for active work to finish before closing")
        self._queue_close_if_idle()

    def _set_operation_status(self, detail: str) -> None:
        self._operation_status.setText(detail)
        self._operation_status.setVisible(bool(detail))

    def _show_setup_failure(
        self,
        message: str,
        *,
        technical_detail: str,
        operation: str,
    ) -> None:
        self._set_operation_status(message)
        step = self._workspace_step if operation == "initialize-authority" else self._qupath_step
        step.details.setText(diagnostic_details(technical_detail, message))
        step.toggle.show()
        if operation == "initialize-authority":
            step.show()
            step.toggle.setChecked(True)
            self._initialize_button.setText("Retry")
            self._update_action_controls()

    def _jobs_changed(self, _active_job_ids: object) -> None:
        self._queue_close_if_idle()

    def _queue_close_if_idle(self) -> None:
        if (
            self._close_pending
            and not self._close_queued
            and not self._jobs.has_active_jobs
            and not self._pending
        ):
            self._close_queued = True
            QTimer.singleShot(0, self._finish_close)

    def _finish_close(self) -> None:
        self._close_queued = False
        if self._close_pending and not self._jobs.has_active_jobs and not self._pending:
            self.close_ready.emit()

    def _choose_qupath(self) -> Path | None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Select QuPath executable",
        )
        return None if not selected else Path(selected)

    def set_state(self, state: FirstRunPrerequisiteState) -> None:
        if not isinstance(state, FirstRunPrerequisiteState):
            raise TypeError("first-run state must be FirstRunPrerequisiteState")
        self._state = state
        self._qupath_step.set_item(state.qupath)
        if state.qupath.state == "ready":
            self._discovery_status.hide()
            self._discovered_qupath.hide()
            self._use_discovered_button.hide()
        elif self._discovery_status.text():
            self._discovery_status.show()
        self._workspace_step.set_item(state.authority_v2)
        self._workspace_step.setVisible(state.authority_v2.state in {"invalid", "error"})
        self._initialize_button.setText(
            "Retry" if state.authority_v2.state in {"invalid", "error"} else "Get started")
        self._update_action_controls()
        self.state_changed.emit(state)
        if state.ready and not self._close_pending:
            self.prerequisites_ready.emit(state)
        if state.qupath.action_id == "select-qupath" and not self._discovery_started:
            self._discovery_started = True
            self._discovery_status.setText("Looking for QuPath installations…")
            self._discovery_status.show()
            self._submit(
                "discover-qupath",
                self._service.discover_qupath,
                access=JobAccess.READ,
                coalesce_key="first-run:discover-qupath",
            )

    def _update_action_controls(self) -> None:
        state = self._state
        if state is None:
            return
        self._select_button.setEnabled(
            not self._close_pending
            and not self._pending
            and state.qupath.action_id in {"select-qupath", "install-qupath-extensions"}
        )
        repair_extensions = state.qupath.action_id == "install-qupath-extensions"
        self._install_extensions_button.setVisible(repair_extensions)
        self._install_extensions_button.setEnabled(repair_extensions and not self._pending and not self._close_pending)
        self._initialize_button.setEnabled(
            self._allow_authority_initialization
            and not self._close_pending
            and not self._pending
            and state.authority_v2.state != "ready"
        )
        self._update_discovery_controls()

    def _update_discovery_controls(self) -> None:
        available = (
            self._state is not None
            and self._state.qupath.action_id == "select-qupath"
            and self._discovered_qupath.count() > 0
            and not self._pending
            and not self._close_pending
        )
        self._discovered_qupath.setEnabled(available)
        self._use_discovered_button.setEnabled(available)

    def _use_discovered_qupath(self) -> None:
        path = self._discovered_qupath.currentData()
        if not isinstance(path, Path):
            return
        self._submit_selection(path)

    def refresh(self) -> None:
        self._submit(
            "inspect-prerequisites",
            self._service.inspect,
            access=JobAccess.READ,
            coalesce_key="first-run:inspect",
        )

    def _select_qupath(self) -> None:
        selected = self._qupath_chooser()
        if selected is None:
            return
        path = selected if isinstance(selected, Path) else Path(selected)
        self._submit_selection(path)

    def _install_extensions(self) -> None:
        self._submit("install-qupath-extensions", self._service.install_qupath_extensions,
                     access=JobAccess.MUTATION)

    def _submit_selection(self, path: Path) -> None:
        select_qupath = self._service.select_qupath
        self._submit(
            "select-qupath",
            lambda: select_qupath(path),
            access=JobAccess.MUTATION,
        )

    def _start_or_retry(self) -> None:
        """Retry failed checks before offering explicit preparation of missing records."""
        if self._state is None:
            return
        if self._state.authority_v2.state == "missing":
            self._initialize_authority()
        else:
            self.refresh()

    def _initialize_authority(self) -> None:
        if (
            not self._allow_authority_initialization
            or self._state is None
            or self._state.authority_v2.state != "missing"
        ):
            return
        self._submit(
            "initialize-authority",
            self._service.initialize_authority_v2,
            access=JobAccess.MUTATION,
        )

    def _submit(
        self,
        operation: str,
        callable_: Callable[[], object],
        *,
        access: JobAccess,
        coalesce_key: str | None = None,
    ) -> None:
        if self._close_pending:
            return
        try:
            job_id = self._jobs.submit(
                operation,
                callable_,
                access=access,
                coalesce_key=coalesce_key,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            guidance = {
                "inspect-prerequisites": "Setup could not be checked. Open Details before changing this setup.",
                "discover-qupath": "Could not search for QuPath. Select QuPath to choose its file.",
                "select-qupath": "QuPath could not be checked. Open Details and check the selected installation.",
                "install-qupath-extensions": "Extension installation could not start. See Details, then try again.",
                "initialize-authority": "Workspace setup could not start. Open Details before changing this setup.",
            }[operation]
            self._show_setup_failure(
                guidance,
                technical_detail=str(exc),
                operation=operation,
            )
            return
        self._pending[job_id] = operation
        self._select_button.setEnabled(False)
        self._install_extensions_button.setEnabled(False)
        self._update_discovery_controls()
        self._initialize_button.setEnabled(False)
        self._set_operation_status(
            {
                "inspect-prerequisites": "Checking setup…",
                "discover-qupath": "Looking for QuPath installations…",
                "select-qupath": "Checking QuPath…",
                "install-qupath-extensions": "Installing application extensions…",
                "initialize-authority": "Setting up your workspace…",
            }[operation]
        )

    def _job_succeeded(self, job_id: str, value: object) -> None:
        operation = self._pending.pop(job_id, None)
        if operation is None:
            return
        if operation == "discover-qupath":
            if not isinstance(value, tuple) or not all(
                isinstance(info, QuPathExecutableInfo) for info in value
            ):
                self._discovery_status.setText(
                    "Could not read the QuPath search results. Select QuPath to choose its file."
                )
                self._qupath_step.details.setText(
                    diagnostic_details(
                        "QuPath discovery returned invalid candidates",
                        "QuPath search results could not be used. Select QuPath to choose its file.",
                    )
                )
            else:
                self._discovered_qupath.clear()
                for info in value:
                    self._discovered_qupath.addItem(
                        f"QuPath {info.version} — {info.path}", info.path
                    )
                found = bool(value)
                self._discovery_status.setText(
                    "Choose a verified installation below, or select another QuPath file."
                    if found
                    else "No compatible QuPath installation found. Select QuPath to browse."
                )
                self._discovered_qupath.setVisible(found)
                self._use_discovered_button.setVisible(found)
            self._set_operation_status("")
            self._update_action_controls()
            self._queue_close_if_idle()
            return
        if not isinstance(value, FirstRunPrerequisiteState):
            detail = f"{operation} returned an invalid prerequisite state"
            if self._state is None:
                self.set_state(_initial_error_state(detail))
            else:
                self.set_state(self._state)
            self._show_setup_failure(
                (
                    "Workspace setup could not be checked. Open Details before changing this setup."
                    if operation == "initialize-authority"
                    else (
                        "Setup could not be checked. Open Details before changing this setup."
                        if operation == "inspect-prerequisites"
                        else "QuPath could not be checked. Open Details and check the selected installation."
                    )
                ),
                technical_detail=detail,
                operation=operation,
            )
            self._queue_close_if_idle()
            return
        self._set_operation_status("")
        self.set_state(value)
        self._queue_close_if_idle()

    def _job_failed(self, job_id: str, error: DesktopJobError) -> None:
        operation = self._pending.pop(job_id, None)
        if operation is None:
            return
        if operation == "discover-qupath":
            self._discovery_status.setText(
                "Could not search for QuPath. Select QuPath to choose its file."
            )
            self._qupath_step.details.setText(
                diagnostic_details(
                    error.diagnostic,
                    "QuPath could not be found automatically. Select QuPath to choose its file.",
                )
            )
            self._set_operation_status("")
            self._update_action_controls()
            self._queue_close_if_idle()
            return
        detail = f"{operation} failed: {error.message}"
        guidance = {
            "inspect-prerequisites": (
                "Setup could not be checked. Open Details before changing this setup."
            ),
            "select-qupath": (
                "QuPath could not be checked. Open Details and check the selected "
                "QuPath installation."
            ),
            "install-qupath-extensions": "Extension installation did not finish. See Details, then try again.",
            "initialize-authority": "Workspace setup could not finish. Review the error before continuing.",
        }[operation]
        if self._state is None:
            self.set_state(_initial_error_state(detail))
        else:
            self.set_state(self._state)
        self._show_setup_failure(
            guidance,
            technical_detail=error.diagnostic,
            operation=operation,
        )
        self._queue_close_if_idle()


__all__ = ["ComputerSetupPanel", "WelcomePage"]
