"""Set up optional phenotyping models independently of cell-segmentation software.

Nimbus checkpoint inspection, selection and installation retain the same managed-job and
verified-download services. The panel keeps model readiness separate from the scientific
method selection. KRONOS access uses the existing browser guide; no restricted package is
downloaded by this page.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mif_pipeline.orchestration.setup.nimbus_setup import (
    NimbusSetupService,
    NimbusSetupStatus,
    nimbus_download_info,
)

from mif_pipeline.orchestration.setup.nimbus_runtime import (
    NimbusRuntimeStatus,
    inspect_nimbus_runtime,
)

from .jobs import DesktopJobError, JobAccess, ManagedJobBridge
from .model_access import KronosAccessGuide
from .researcher_widgets import ContentSizedButton, configure_disclosure
from .software_details import open_software_details, software_details_button

# ==============================================================================
#                              Phenotyping setup
# ==============================================================================


class PhenotypingSetupPanel(QWidget):
    """Keep Nimbus model actions and optional KRONOS guidance in the phenotyping section."""

    def __init__(
        self,
        service: NimbusSetupService,
        jobs: ManagedJobBridge,
        *,
        confirm_download: Callable[[str], bool] | None = None,
        choose_nimbus_checkpoint: Callable[[], Path | None] | None = None,
        inspect_runtime: Callable[[], NimbusRuntimeStatus] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Create model controls and start a local checkpoint check without downloading.

        Only explicit install or existing-file selection actions can mutate the saved model
        choice. Completion callbacks ignore other panels' jobs on the shared bridge.
        """
        super().__init__(parent)
        self.setObjectName("phenotyping-setup-panel")
        self._service = service
        self._jobs = jobs
        self._confirm = confirm_download or self._confirm_download
        self._choose_nimbus = choose_nimbus_checkpoint or self._choose_nimbus_checkpoint
        self._nimbus_state: NimbusSetupStatus | None = None
        self._nimbus_jobs: dict[str, str] = {}
        self._inspect_runtime = inspect_runtime or inspect_nimbus_runtime
        self._runtime_jobs: set[str] = set()
        self._kronos_jobs: set[str] = set()
        self._marker_text_jobs: set[str] = set()
        self._errors: dict[str, str] = {}
        self._reports: dict[str, str] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QLabel("Cell phenotyping", self)
        heading.setProperty("panelHeading", True)
        outer.addWidget(heading)
        purpose = self._label(
            "Install the software needed to evaluate markers and phenotype cells.", self
        )
        outer.addWidget(purpose)
        controls = QFrame(self)
        controls.setObjectName("phenotyping-setup-controls")
        controls.setProperty("workbenchSection", True)
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        outer.addWidget(controls)
        nimbus_heading = QLabel("Nimbus", controls)
        nimbus_heading.setProperty("panelHeading", True)
        layout.addWidget(nimbus_heading)
        self._nimbus_status = self._label("Checking: Nimbus model weights…", controls)
        self._nimbus_status.setObjectName("nimbus-model-status")
        self._nimbus_status.setProperty("setupReadiness", True)
        layout.addWidget(self._nimbus_status)
        actions = QHBoxLayout()
        self._nimbus_install = ContentSizedButton("Install model…", controls)
        self._nimbus_install.setObjectName("nimbus-model-install")
        self._nimbus_install.setAccessibleName("Install Nimbus model weights")
        self._nimbus_install.setProperty("actionRole", "secondary")
        self._nimbus_install.clicked.connect(self._offer_nimbus_install)
        actions.addWidget(self._nimbus_install)
        self._nimbus_select = ContentSizedButton("Use existing…", controls)
        self._nimbus_select.setObjectName("nimbus-model-select")
        self._nimbus_select.setAccessibleName("Use existing Nimbus model weights")
        self._nimbus_select.setProperty("actionRole", "secondary")
        self._nimbus_select.clicked.connect(self._offer_nimbus_select)
        actions.addWidget(self._nimbus_select)
        self._nimbus_check = ContentSizedButton("Check again", controls)
        self._nimbus_check.setObjectName("nimbus-model-check")
        self._nimbus_check.setAccessibleName("Check Nimbus model weights again")
        self._nimbus_check.setProperty("actionRole", "primary")
        self._nimbus_check.clicked.connect(self.refresh_nimbus)
        actions.addWidget(self._nimbus_check)
        actions.addWidget(self._details_button("model", "nimbus-model-details", controls))
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addSpacing(8)
        runtime_heading = QLabel("Nimbus scoring software", controls)
        runtime_heading.setProperty("panelHeading", True)
        layout.addWidget(runtime_heading)
        self._runtime_status = self._label("Checking: Nimbus scoring software…", controls)
        self._runtime_status.setObjectName("nimbus-runtime-status")
        self._runtime_status.setProperty("setupReadiness", True)
        layout.addWidget(self._runtime_status)
        self._runtime_check = ContentSizedButton("Check again", controls)
        self._runtime_check.setObjectName("nimbus-runtime-check")
        self._runtime_check.setAccessibleName("Check Nimbus scoring software again")
        self._runtime_check.setProperty("actionRole", "primary")
        self._runtime_check.clicked.connect(self.refresh_runtime)
        runtime_actions = QHBoxLayout()
        runtime_actions.addWidget(self._runtime_check)
        runtime_actions.addWidget(self._details_button("runtime", "nimbus-runtime-details", controls))
        runtime_actions.addStretch(1)
        layout.addLayout(runtime_actions)
        layout.addSpacing(8)
        kronos_heading = QLabel("KRONOS", controls)
        kronos_heading.setProperty("panelHeading", True)
        layout.addWidget(kronos_heading)
        from mif_pipeline.orchestration.setup.kronos_setup import selected_package
        selected = selected_package(preferences_path=service.preferences_path)
        self._kronos_status = self._label(
            "Selected: KRONOS2 model." if selected else "Select or download the KRONOS2 model.", controls)
        self._kronos_status.setToolTip(str(selected) if selected else "")
        self._reports["kronos"] = f"Selected folder: {selected}" if selected else "No KRONOS2 model folder is selected."
        layout.addWidget(self._kronos_status)
        kronos_actions = QHBoxLayout()
        self._kronos_install = ContentSizedButton("Install model…", controls)
        self._kronos_install.setObjectName("kronos-install-model")
        self._kronos_install.clicked.connect(self._show_kronos_install)
        kronos_actions.addWidget(self._kronos_install)
        self._kronos_select = ContentSizedButton("Use existing…", controls)
        self._kronos_select.setObjectName("kronos-select-model")
        self._kronos_select.clicked.connect(self._select_kronos)
        kronos_actions.addWidget(self._kronos_select)
        kronos_actions.addWidget(self._details_button("kronos", "kronos-model-details", controls))
        kronos_actions.addStretch(1)
        layout.addLayout(kronos_actions)

        self._kronos_dialog = QDialog(self)
        self._kronos_dialog.setObjectName("kronos-install-dialog")
        self._kronos_dialog.setWindowTitle("Install KRONOS model")
        self._kronos_dialog.resize(640, 440)
        dialog_layout = QVBoxLayout(self._kronos_dialog)
        dialog_layout.setContentsMargins(0, 0, 0, 16)
        scroll = QScrollArea(self._kronos_dialog)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget(scroll)
        kronos_layout = QVBoxLayout(page)
        kronos_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        kronos_layout.setContentsMargins(24, 24, 24, 20)
        kronos_layout.setSpacing(16)
        kronos_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(page)
        dialog_layout.addWidget(scroll)
        title = QLabel("Download and select KRONOS2", self._kronos_dialog)
        title.setProperty("panelHeading", True)
        kronos_layout.addWidget(title)
        kronos_layout.addWidget(KronosAccessGuide("phenotyping-setup", self._kronos_dialog))
        kronos_layout.addWidget(self._label(
            "Keep the downloaded files together in one folder, including config.json, "
            "the model weights, marker_metadata.csv and model code.", self._kronos_dialog))
        self._kronos_downloaded = ContentSizedButton("Select downloaded folder…", self._kronos_dialog)
        self._kronos_downloaded.setProperty("actionRole", "primary")
        self._kronos_downloaded.clicked.connect(self._select_kronos)
        kronos_layout.addWidget(self._kronos_downloaded, alignment=Qt.AlignmentFlag.AlignLeft)
        self._kronos_dialog_status = self._label("", self._kronos_dialog)
        self._kronos_dialog_status.hide()
        kronos_layout.addWidget(self._kronos_dialog_status)
        marker_toggle = QToolButton(self._kronos_dialog)
        marker_toggle.setText("Additional markers")
        marker_toggle.setCheckable(True)
        configure_disclosure(marker_toggle)
        kronos_layout.addWidget(marker_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        kronos = QWidget(self._kronos_dialog)
        marker_layout = QVBoxLayout(kronos)
        marker_layout.setContentsMargins(0, 0, 0, 0)
        marker_layout.setSpacing(10)
        from mif_pipeline.orchestration.setup.kronos_setup import selected_marker_text_model
        text_model = selected_marker_text_model(preferences_path=service.preferences_path)
        marker_layout.addWidget(self._label(
            "To add a marker absent from KRONOS, install BioLinkBERT-large. "
            "It converts the marker’s biological description into the marker embedding used by KRONOS.", kronos))
        self._marker_text_status = self._label(
            "Selected: BioLinkBERT-large marker model." if text_model else
            "Marker-description model is not selected. Install it only if adding new markers.", kronos)
        self._reports["marker"] = self._marker_report(text_model) if text_model else "No marker-description model folder is selected."
        self._marker_text_status.setObjectName("kronos-marker-text-status")
        marker_layout.addWidget(self._marker_text_status)
        text_actions = QHBoxLayout()
        self._marker_text_install = ContentSizedButton("Install marker model…", kronos)
        self._marker_text_install.setObjectName("kronos-marker-text-install")
        self._marker_text_install.clicked.connect(self._install_marker_text)
        text_actions.addWidget(self._marker_text_install)
        self._marker_text_select = ContentSizedButton("Use existing marker model…", kronos)
        self._marker_text_select.setObjectName("kronos-marker-text-select")
        self._marker_text_select.clicked.connect(self._select_marker_text)
        text_actions.addWidget(self._marker_text_select)
        text_actions.addWidget(self._details_button("marker", "kronos-marker-text-details", kronos))
        text_actions.addStretch(1)
        marker_layout.addLayout(text_actions)

        kronos.hide()
        marker_toggle.toggled.connect(kronos.setVisible)
        marker_toggle.toggled.connect(lambda expanded: self._kronos_dialog.resize(
            self._kronos_dialog.width(), 560 if expanded else 440))
        kronos_layout.addWidget(kronos)
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self._kronos_dialog)
        close_buttons.setContentsMargins(24, 0, 24, 0)
        close_buttons.rejected.connect(self._kronos_dialog.reject)
        dialog_layout.addWidget(close_buttons)
        jobs.job_succeeded.connect(self._job_succeeded)
        jobs.job_failed.connect(self._job_failed)
        self.refresh()

    def _show_kronos_install(self) -> None:
        """Open the download guide without changing the selected model or starting a download."""
        self._kronos_dialog.show()
        self._kronos_dialog.raise_()
        self._kronos_dialog.activateWindow()

    def _select_kronos(self) -> None:
        """Inspect the chosen package in a managed job and retain its location."""
        if self._kronos_jobs:
            return
        directory = QFileDialog.getExistingDirectory(self, "Select KRONOS model folder")
        if not directory:
            return
        from mif_pipeline.orchestration.setup.kronos_setup import select_package
        self._kronos_select.setEnabled(False)
        self._kronos_downloaded.setEnabled(False)
        self._kronos_status.setText("Checking KRONOS model files…")
        self._kronos_dialog_status.setText("Checking KRONOS model files…")
        self._kronos_dialog_status.show()
        try:
            job = self._jobs.submit("select-kronos-model", lambda: select_package(Path(directory),
                preferences_path=self._service.preferences_path), access=JobAccess.MUTATION)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._kronos_select.setEnabled(True)
            self._kronos_downloaded.setEnabled(True)
            self._show_error("KRONOS model selection could not start. See Details.",
                             str(exc), component="kronos")
            self._kronos_dialog_status.setText(self._kronos_status.text())
            return
        self._kronos_jobs.add(job)

    def _select_marker_text(self) -> None:
        """Inspect an ordinary pinned helper snapshot off the Qt thread."""
        if self._marker_text_jobs or not self.isEnabled():
            return
        directory = QFileDialog.getExistingDirectory(self, "Select BioLinkBERT-large folder")
        if directory:
            from mif_pipeline.orchestration.setup.kronos_setup import select_marker_text_model
            self._submit_marker_text(lambda: select_marker_text_model(
                Path(directory), preferences_path=self._service.preferences_path))

    def _install_marker_text(self) -> None:
        """Show the helper source, size and licence before explicit installation."""
        from mif_pipeline.orchestration.setup.kronos_setup import (
            marker_text_download_info, marker_text_target, install_marker_text_model,
        )
        if self._marker_text_jobs or not self.isEnabled():
            return
        try:
            pin = marker_text_download_info()
            target = marker_text_target()
        except (OSError, ValueError, KeyError) as exc:
            self._show_error("Marker model download information is unavailable. See Details.",
                             str(exc), component="marker")
            return
        detail = (
            "Install the marker-description model?\n\n"
            f"Source: https://huggingface.co/{pin['repository']}\n"
            f"Revision: {pin['revision']}\n"
            f"Download: {sum(row['size_bytes'] for row in pin['files'].values()):,} bytes\n"
            f"License: {pin['license']}\n{pin['license_url']}\n"
            f"Target: {target}\n\n"
            "This downloads the text model only. KRONOS weights remain a separate access-controlled selection."
        )
        accepted = QMessageBox.question(
            self, "Install KRONOS marker-description model", detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes
        if accepted:
            self._submit_marker_text(lambda: install_marker_text_model(
                consent=True, preferences_path=self._service.preferences_path))

    def _submit_marker_text(self, operation) -> None:
        """Keep the selected helper usable while a managed setup job runs."""
        if self._marker_text_jobs:
            return
        self._marker_text_status.setText("Preparing and verifying marker-description model…")
        self._marker_text_install.setEnabled(False)
        self._marker_text_select.setEnabled(False)
        try:
            job = self._jobs.submit("prepare-kronos-marker-text", operation, access=JobAccess.MUTATION)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._show_error("Marker model setup could not start. See Details.",
                             str(exc), component="marker")
            self._marker_text_install.setEnabled(True)
            self._marker_text_select.setEnabled(True)
            return
        self._marker_text_jobs.add(job)

    @staticmethod
    def _label(text: str, parent: QWidget) -> QLabel:
        """Keep model guidance and diagnostic text readable without rich-text interpretation."""
        label = QLabel(text, parent)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setProperty("supportingText", True)
        return label

    def _confirm_download(self, detail: str) -> bool:
        """Show the exact checkpoint source and destination before requesting installation."""
        return QMessageBox.question(
            self, "Install Nimbus model weights", detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _choose_nimbus_checkpoint(self) -> Path | None:
        """Choose an existing checkpoint for service-owned verification and selection."""
        selected, _ = QFileDialog.getOpenFileName(
            self, "Select Nimbus V1 model checkpoint", "", "Nimbus checkpoint (*.pt);;All files (*)"
        )
        return Path(selected) if selected else None

    def _show_error(self, summary: str, detail: str, *, component: str = "model") -> None:
        """Retain each component's diagnostic until that component is checked successfully."""
        self._component_label(component).setText(summary)
        self._errors[component] = detail
        if component == "runtime":
            self._runtime_check.show()

    def _component_label(self, component: str) -> QLabel:
        return {"model": self._nimbus_status, "runtime": self._runtime_status,
                "kronos": self._kronos_status, "marker": self._marker_text_status}[component]

    @staticmethod
    def _marker_report(model) -> str:
        return (f"Selected folder: {model.directory}\nRepository: {model.repository}\n"
                f"Revision: {model.revision}\nSHA-256: {model.sha256}")

    def _details_button(self, component: str, object_name: str, parent: QWidget) -> QToolButton:
        names = {"model": "Nimbus model weights", "runtime": "Nimbus scoring software",
                 "kronos": "KRONOS2 model", "marker": "BioLinkBERT-large marker model"}
        button = software_details_button(parent)
        button.setObjectName(object_name)
        button.setAccessibleName(names[component] + " details")
        button.clicked.connect(lambda: open_software_details(
            self, names[component], self._component_label(component).text(),
            "\n\n".join(filter(None, (self._errors.get(component), self._reports.get(component))))
            or "The setup check has not finished."))
        return button

    def refresh(self) -> None:
        """Check the selected model and the bundled scoring software independently."""
        self.refresh_nimbus()
        self.refresh_runtime()

    def refresh_runtime(self) -> None:
        """Probe the bundled helper off the Qt thread without altering the installation."""
        if self._runtime_jobs:
            return
        try:
            job_id = self._jobs.submit(
                "inspect-nimbus-runtime", self._inspect_runtime, access=JobAccess.READ
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            self._show_error(
                "Nimbus software could not be checked. Open Details to review the problem.",
                str(exc), component="runtime",
            )
            return
        self._runtime_jobs.add(job_id)
        self._runtime_status.setText("Checking: Nimbus scoring software…")
        self._runtime_check.setEnabled(False)

    def _show_runtime(self, status: NimbusRuntimeStatus) -> None:
        """Report verified bundled-software readiness without offering an environment installer."""
        self._errors.pop("runtime", None)
        self._reports["runtime"] = f"Last check: {status.state}\nLocation: {status.location}\n\n{status.detail}"
        repair = (
            "Reinstall the complete desktop application."
            if getattr(sys, "frozen", False) else
            "Build the Nimbus worker using the source-installation instructions, then select Check again."
        )
        self._runtime_status.setText({
            "ready": "Ready: Nimbus scoring software is included and compatible.",
            "missing": (
                "Missing: Nimbus scoring software is not installed."
            ),
            "incompatible": (
                "Needs attention: Nimbus scoring software is incompatible."
            ),
        }[status.state])
        self._runtime_check.setVisible(status.state != "ready")
        if status.state != "ready":
            self._reports["runtime"] += "\n\n" + repair
        self._runtime_check.setEnabled(True)

    def _update_nimbus_buttons(self) -> None:
        """Prevent overlapping model changes and offer installation only for missing weights."""
        busy = bool(self._nimbus_jobs)
        ready = self._nimbus_state is not None and self._nimbus_state.state == "installed" and "model" not in self._errors
        self._nimbus_install.setVisible(not ready)
        self._nimbus_check.setVisible(not ready)
        self._nimbus_select.setText("Change model…" if ready else "Use existing…")
        self._nimbus_select.setAccessibleName("Change Nimbus model weights" if ready else "Use existing Nimbus model weights")
        self._nimbus_install.setEnabled(
            not busy and (self._nimbus_state is None or self._nimbus_state.state == "missing")
        )
        self._nimbus_select.setEnabled(not busy)
        self._nimbus_check.setEnabled(not busy)

    def refresh_nimbus(self) -> None:
        """Refresh this computer's checkpoint selection without starting another pending check."""
        if not self._nimbus_jobs:
            self._submit_nimbus("inspect", self._service.inspect, JobAccess.READ)

    def _submit_nimbus(
        self, action: str, operation: Callable[[], object], access: JobAccess
    ) -> None:
        """Run checkpoint I/O off the Qt thread and retain the returned job's ownership."""
        try:
            job_id = self._jobs.submit(f"nimbus-{action}", operation, access=access)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._show_error(
                "Nimbus setup could not start. Open Details before changing these files.", str(exc)
            )
            return
        self._nimbus_jobs[job_id] = action
        self._nimbus_status.setText(
            "Installing and verifying Nimbus model weights…" if action == "install"
            else "Checking: Nimbus model weights…"
        )
        self._update_nimbus_buttons()

    def _offer_nimbus_select(self) -> None:
        """Select an existing checkpoint only while no model operation is pending."""
        if self._nimbus_jobs or not self.isEnabled():
            return
        location = self._choose_nimbus()
        if location is not None:
            self._submit_nimbus(
                "select", lambda: self._service.select(location), JobAccess.MUTATION
            )

    def _offer_nimbus_install(self) -> None:
        """Request the pinned weight download after the researcher reviews its exact source."""
        if self._nimbus_jobs or not self.isEnabled():
            return
        if self._nimbus_state is not None and self._nimbus_state.state != "missing":
            return
        try:
            info = nimbus_download_info()
            detail = (
                f"Install Nimbus model {info.version}?\n\n"
                f"Source: {', '.join(info.sources)}\n"
                f"Approximate download: {info.approximate_size_bytes:,} bytes\n"
                f"License: {info.license}\n"
                f"Target: {self._service.target}\n\nDownload and verify these files?"
            )
        except (ValueError, RuntimeError) as exc:
            self._show_error(
                "Download information is unavailable. Open Details before changing this setup.",
                str(exc),
            )
            return
        if not self._confirm(detail):
            self._nimbus_status.setText("Setup not started. No files were downloaded.")
            return
        self._submit_nimbus(
            "install", lambda: self._service.install(consent=True), JobAccess.MUTATION
        )

    def _show_nimbus(self, status: NimbusSetupStatus) -> None:
        """Display verified weight readiness without claiming the inference runtime was checked."""
        self._nimbus_state = status
        self._errors.pop("model", None)
        self._reports["model"] = f"Last check: {status.state}\nLocation: {status.location}\n\n{status.detail}"
        self._nimbus_status.setText({
            "installed": "Installed: Nimbus V1 model weights are available.",
            "missing": "Model weights are missing. Choose Install model or Use existing.",
            "incompatible": (
                "The Nimbus model file cannot be used. Open Details to review the problem."
            ),
        }[status.state])
        self._update_nimbus_buttons()

    def _job_succeeded(self, job_id: str, value: object) -> None:
        """Apply only this panel's completed model or bundled-software check."""
        if job_id in self._marker_text_jobs:
            self._marker_text_jobs.remove(job_id)
            self._marker_text_install.setEnabled(True)
            self._marker_text_select.setEnabled(True)
            self._marker_text_status.setText("Selected: BioLinkBERT-large marker model.")
            self._errors.pop("marker", None)
            self._reports["marker"] = self._marker_report(value)
            return
        if job_id in self._kronos_jobs:
            self._kronos_jobs.remove(job_id)
            self._kronos_select.setEnabled(True)
            self._kronos_downloaded.setEnabled(True)
            self._kronos_status.setText("Selected: KRONOS2 model.")
            self._kronos_status.setToolTip(str(value))
            self._errors.pop("kronos", None)
            self._reports["kronos"] = f"Selected folder: {value}"
            self._kronos_dialog_status.hide()
            self._kronos_dialog.accept()
            return
        if job_id in self._runtime_jobs:
            self._runtime_jobs.remove(job_id)
            if isinstance(value, NimbusRuntimeStatus):
                self._show_runtime(value)
            else:
                self._show_error(
                    "Nimbus software could not be checked. Open Details to review the problem.",
                    "Nimbus software check returned an invalid status.", component="runtime",
                )
                self._runtime_check.setEnabled(True)
            return
        if job_id not in self._nimbus_jobs:
            return
        self._nimbus_jobs.pop(job_id)
        if isinstance(value, NimbusSetupStatus):
            self._show_nimbus(value)
        else:
            self._show_error(
                "Nimbus setup could not be checked. Open Details before changing these files.",
                "Nimbus setup returned an invalid status.",
            )
            self._update_nimbus_buttons()

    def _job_failed(self, job_id: str, error: DesktopJobError) -> None:
        """Retain each failed component's diagnostic and allow another explicit check."""
        if job_id in self._marker_text_jobs:
            self._marker_text_jobs.remove(job_id)
            self._marker_text_install.setEnabled(True)
            self._marker_text_select.setEnabled(True)
            self._show_error("Marker model setup failed. See Details.",
                             error.diagnostic, component="marker")
            return
        if job_id in self._kronos_jobs:
            self._kronos_jobs.remove(job_id)
            self._kronos_select.setEnabled(True)
            self._kronos_downloaded.setEnabled(True)
            self._show_error("KRONOS model selection failed. See Details.",
                             error.diagnostic, component="kronos")
            self._kronos_dialog_status.setText(self._kronos_status.text())
            self._kronos_dialog_status.show()
            return
        if job_id in self._runtime_jobs:
            self._runtime_jobs.remove(job_id)
            self._show_error(
                "Nimbus software could not be checked. Open Details to review the problem.",
                error.diagnostic, component="runtime",
            )
            self._runtime_check.setEnabled(True)
            return
        if job_id not in self._nimbus_jobs:
            return
        action = self._nimbus_jobs.pop(job_id)
        if error.message.startswith("download_http_error: HTTP 503"):
            summary = (
                "The download server is temporarily unavailable (HTTP 503). "
                "Try later or select existing files."
            )
        else:
            verb = "installed" if action == "install" else "checked"
            summary = (
                f"Nimbus model files could not be {verb}. "
                "Open Details before changing these files."
            )
        self._show_error(summary, error.diagnostic)
        self._update_nimbus_buttons()


__all__ = ["PhenotypingSetupPanel"]
