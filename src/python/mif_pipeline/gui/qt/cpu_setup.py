"""Install and check the native software used for cell segmentation.

InstanSeg extension, model and CPU-library operations use the shared managed-job bridge.
We retain independent readiness and diagnostics for those three components; phenotyping
models have their own setup panel and do not affect these segmentation checks.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mif_pipeline.orchestration.setup.instanseg_setup import InstanSegSetupService
from mif_pipeline.orchestration.setup.instanseg_setup import InstanSegSetupStatus
from mif_pipeline.orchestration.setup.instanseg_setup import instanseg_download_info
from mif_pipeline.orchestration.setup.prerequisite_setup import CpuSetupStatus
from mif_pipeline.qupath_runtime.payload_lock import SOURCE_ARCHIVE_LOCK

from .jobs import DesktopJobError, JobAccess, ManagedJobBridge
from .software_details import software_details_button

# ==============================================================================
#                                   Cpu setup
# ==============================================================================


class CpuSetupPort(Protocol):
    platform: str

    def inspect(self) -> CpuSetupStatus: ...
    def install(self, *, consent: bool) -> CpuSetupStatus: ...


class CpuSetupPanel(QWidget):
    """Inspect locally and download only after an explicit user decision."""

    def __init__(
        self,
        service: CpuSetupPort,
        jobs: ManagedJobBridge,
        *,
        confirm_download: Callable[[str], bool] | None = None,
        instanseg_service_factory: (
            Callable[[Path | None], InstanSegSetupService | None] | None
        ) = None,
        choose_extension_directory: Callable[[], Path | None] | None = None,
        on_extension_installed: Callable[[], None] | None = None,
        on_verified_model: Callable[[Path], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Create the three segmentation checks and route their work through managed jobs.

        Installation remains an explicit action for the named component. Existing verified
        models and the connected QuPath extension directory retain their supplied callbacks.
        """
        super().__init__(parent)
        self.setObjectName("cpu-setup-panel")
        self._service = service
        self._jobs = jobs
        self._confirm = confirm_download or self._confirm_download
        self._pending: set[str] = set()
        self._state: CpuSetupStatus | None = None
        self._instanseg_factory = instanseg_service_factory
        self._choose_extensions = choose_extension_directory or self._choose_extension_folder
        self._extension_installed = on_extension_installed
        self._verified_model = on_verified_model
        self._extension_directory: Path | None = None
        self._instanseg_state: InstanSegSetupStatus | None = None
        self._bundled_model = False
        self._instanseg_jobs: dict[str, str] = {}
        self._error_details_by_component: dict[str, str] = {}
        self._error_details_buttons = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        heading = QLabel("Cell segmentation", self)
        heading.setProperty("panelHeading", True)
        outer.addWidget(heading)
        purpose = QLabel("Install the software needed to detect cells", self)
        purpose.setWordWrap(True)
        outer.addWidget(purpose)
        controls = QFrame(self)
        controls.setObjectName("cpu-setup-controls")
        outer.addWidget(controls)
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        extension_heading = QLabel("InstanSeg extension", controls)
        extension_heading.setProperty("panelHeading", True)
        layout.addWidget(extension_heading)
        self._extension_status = QLabel(
            "Not checked: Connect QuPath to check its InstanSeg extension.", self
        )
        self._extension_status.setObjectName("instanseg-extension-status")
        self._extension_status.setProperty("setupReadiness", True)
        self._extension_status.setWordWrap(True)
        self._extension_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._extension_status)
        self._extension_install = QPushButton("Install…", self)
        self._extension_install.setProperty("actionRole", "secondary")
        self._extension_install.setObjectName("instanseg-extension-install")
        self._extension_install.setAccessibleName("Install InstanSeg extension")
        self._extension_install.setEnabled(False)
        self._extension_install.clicked.connect(lambda: self._offer_instanseg("extension"))
        extension_actions = QHBoxLayout()
        extension_actions.addWidget(self._extension_install)
        self._extension_check = QPushButton("Check again", controls)
        self._extension_check.setObjectName("instanseg-extension-check")
        self._extension_check.setAccessibleName("Check InstanSeg extension again")
        self._extension_check.setProperty("actionRole", "primary")
        self._extension_check.clicked.connect(self.refresh_instanseg)
        extension_actions.addWidget(self._extension_check)
        extension_actions.addWidget(self._details_button("extension"))
        extension_actions.addStretch(1)
        layout.addLayout(extension_actions)
        layout.addSpacing(10)
        model_heading = QLabel("InstanSeg model", controls)
        model_heading.setProperty("panelHeading", True)
        layout.addWidget(model_heading)
        self._model_status = QLabel("Not checked: Check the InstanSeg model.", self)
        self._model_status.setObjectName("instanseg-model-status")
        self._model_status.setProperty("setupReadiness", True)
        self._model_status.setWordWrap(True)
        self._model_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._model_status)
        self._model_install = QPushButton("Install…", self)
        self._model_install.setProperty("actionRole", "secondary")
        self._model_install.setObjectName("instanseg-model-install")
        self._model_install.setAccessibleName("Install InstanSeg model")
        self._model_install.setEnabled(False)
        self._model_install.clicked.connect(lambda: self._offer_instanseg("model"))
        model_actions = QHBoxLayout()
        model_actions.addWidget(self._model_install)
        self._check_again = QPushButton("Check again", self)
        self._check_again.setProperty("actionRole", "primary")
        self._check_again.setObjectName("instanseg-check-again")
        self._check_again.clicked.connect(self.refresh_instanseg)
        model_actions.addWidget(self._check_again)
        self._model_source = QLabel("Included with the app", controls)
        self._model_source.setObjectName("instanseg-model-source")
        self._model_source.setProperty("supportingText", True)
        self._model_source.hide()
        model_actions.addWidget(self._model_source)
        model_actions.addWidget(self._details_button("model"))
        model_actions.addStretch(1)
        layout.addLayout(model_actions)
        layout.addSpacing(10)
        cpu_heading = QLabel("Cell-segmentation libraries", controls)
        cpu_heading.setProperty("panelHeading", True)
        layout.addWidget(cpu_heading)
        self._status = QLabel("Checking: cell-segmentation libraries…", self)
        self._status.setObjectName("cpu-setup-status")
        self._status.setProperty("setupReadiness", True)
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._status)
        self._install = QPushButton("Install…", self)
        self._install.setProperty("actionRole", "secondary")
        self._install.setObjectName("cpu-setup-install")
        self._install.setAccessibleName("Install cell-segmentation libraries")
        self._install.setEnabled(False)
        self._install.clicked.connect(self._offer_download)
        cpu_actions = QHBoxLayout()
        cpu_actions.addWidget(self._install)
        cpu_actions.addWidget(self._details_button("cpu"))
        cpu_actions.addStretch(1)
        layout.addLayout(cpu_actions)
        from .alternative_segmentation_setup import AlternativeSegmentationSetupPanel
        for method in ("cellpose", "stardist"):
            layout.addSpacing(10)
            layout.addWidget(AlternativeSegmentationSetupPanel(method, jobs, controls))
        jobs.job_succeeded.connect(self._job_succeeded)
        jobs.job_failed.connect(self._job_failed)
        self.refresh()
        self.refresh_instanseg()

    @property
    def status_text(self) -> str:
        return self._status.text()

    def _show_error(self, component: str, label: QLabel, summary: str, detail: str) -> None:
        label.setText(summary)
        if component == "extension":
            self._extension_check.show()
        elif component == "model":
            self._check_again.show()
        elif component == "cpu":
            self._install.show()
        self._error_details_by_component[component] = detail
        self._sync_error_details()

    def _clear_error(self, component: str) -> None:
        self._error_details_by_component.pop(component, None)
        self._sync_error_details()

    def _details_button(self, component):
        button = software_details_button(self)
        button.setObjectName("cpu-setup-details-" + component)
        names = {"cpu": "Cell-segmentation libraries", "extension": "InstanSeg extension", "model": "InstanSeg model"}
        button.setAccessibleName(names[component] + " details")
        button.hide()
        button.clicked.connect(lambda: self._open_details(component))
        self._error_details_buttons[component] = button
        return button

    def _open_details(self, component):
        from .software_details import open_software_details
        names = {"cpu": "Cell-segmentation libraries", "extension": "InstanSeg extension", "model": "InstanSeg model"}
        explanations = {
            "cpu": "This check verifies the installed CPU library files. Plan also checks whether QuPath can load the tensor engine required by InstanSeg.",
            "extension": "This check verifies the extension files and which extension QuPath loaded. The model and processing libraries are checked separately.",
            "model": "This check verifies the installed InstanSeg model files against the supported release.",
        }
        self._details_dialog = open_software_details(self, names[component], explanations[component],
            self._error_details_by_component.get(component, ""))

    def _sync_error_details(self) -> None:
        for component, button in self._error_details_buttons.items():
            button.setVisible(bool(self._error_details_by_component.get(component)))

    def refresh(self) -> None:
        self._submit("inspect-cpu-setup", self._service.inspect, JobAccess.READ)

    def _choose_extension_folder(self) -> Path | None:
        selected = QFileDialog.getExistingDirectory(self, "Choose QuPath extensions folder")
        return Path(selected) if selected else None

    def refresh_instanseg(self) -> None:
        if self._instanseg_factory is None or self._instanseg_jobs:
            return
        self._submit_instanseg("inspect", lambda: self._inspect_instanseg(), JobAccess.READ)

    def _inspect_instanseg(self) -> InstanSegSetupStatus | None:
        service = self._instanseg_factory(self._extension_directory)
        return service.inspect() if service is not None else None

    def _submit_instanseg(self, kind: str, action: Callable[[], object], access: JobAccess) -> None:
        try:
            job_id = self._jobs.submit(f"instanseg-{kind}", action, access=access)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._show_error(
                "extension" if kind == "inspect" else kind,
                self._model_status if kind == "model" else self._extension_status,
                "InstanSeg setup could not start. Open Details before changing these files.",
                str(exc),
            )
            return
        self._instanseg_jobs[job_id] = kind
        if kind == "inspect":
            self._extension_status.setText(
                "Checking: InstanSeg extension files and QuPath runtime…"
            )
            self._model_status.setText("Checking: InstanSeg model files…")
        else:
            (self._extension_status if kind == "extension" else self._model_status).setText(
                "Checking: installing and verifying files…"
            )
        self._extension_install.setEnabled(False)
        self._model_install.setEnabled(False)
        self._check_again.setEnabled(False)
        self._extension_check.setEnabled(False)

    def _offer_instanseg(self, component: str) -> None:
        if (
            self._instanseg_jobs
            or self._pending
            or not self.isEnabled()
            or self._instanseg_state is None
        ):
            return
        status = getattr(self._instanseg_state, component)
        if status.state != "missing":
            return
        if component == "extension" and status.location is None:
            directory = self._choose_extensions()
            if directory is None:
                return
            if not directory.is_absolute() or ".." in directory.parts:
                self._extension_status.setText("Choose an absolute QuPath extensions folder.")
                return
            self._extension_directory = directory
            self._extension_status.setText(
                "Configure this same extensions folder in QuPath, then check again before installation."
            )
            self.refresh_instanseg()
            return
        info = instanseg_download_info(component)
        target = status.location
        detail = (
            f"Install InstanSeg {component} version {info.version}?\n\n"
            f"Source: {', '.join(info.sources)}\n"
            f"Approximate download: {info.approximate_size_bytes:,} bytes\n"
            f"Target: {target}\n\nDownload and verify these files?"
        )
        if not self._confirm(detail):
            label = self._extension_status if component == "extension" else self._model_status
            label.setText("Setup not started. No files were downloaded.")
            return

        def install():
            service = self._instanseg_factory(self._extension_directory)
            if service is None:
                raise RuntimeError("Connect QuPath before installing InstanSeg files")
            installed = (
                service.install_extension(consent=True)
                if component == "extension"
                else service.install_model(consent=True)
            )
            return installed, service.inspect()

        self._submit_instanseg(component, install, JobAccess.MUTATION)

    def _show_instanseg(self, status: InstanSegSetupStatus) -> None:
        self._clear_error("extension")
        self._clear_error("model")
        self._instanseg_state = status
        extension = status.extension
        model = status.model
        labels = {
            "installed": "Installed",
            "missing": "Missing",
            "incompatible": "Needs attention",
            "not_checked": "Not checked",
        }
        runtime = status.origins.instanseg
        live = {
            "candidate": "QuPath loaded the extension.",
            "missing": "QuPath has not loaded the extension. Open Details to review the check.",
            "incompatible": "QuPath loaded different extension files. Open Details to review the check.",
            "error": "QuPath's loaded extension could not be checked. Open Details to review the error.",
        }[runtime.status]
        if runtime.status != "candidate" or extension.state != "installed":
            self._error_details_by_component["extension"] = "\n".join(
                part
                for part in (
                    extension.detail,
                    runtime.diagnostic if runtime.status != "candidate" else "",
                )
                if part
            )
        extension_label = (
            "Needs attention"
            if extension.state == "installed" and runtime.status != "candidate"
            else labels[extension.state]
        )
        extension_guidance = {
            "installed": "InstanSeg extension files installed.",
            "missing": "InstanSeg extension files are missing. Choose Install or connect QuPath.",
            "incompatible": "Resolve the conflicting extension files listed in Details, then restart the application and select Check again.",
            "not_checked": "Connect QuPath, then check again.",
        }[extension.state]
        if extension.state == "installed" and runtime.status == "candidate":
            self._extension_status.setText(
                "Ready: QuPath loaded the verified InstanSeg 0.1.7 extension."
            )
        else:
            self._extension_status.setText(
                f"{extension_label}: {extension_guidance}"
                + (
                    " " + live
                    if runtime.status != "candidate" or extension.state == "installed"
                    else ""
                )
            )
        bundled = (
            getattr(
                self._instanseg_factory(self._extension_directory), "bundled_payload_root", None
            )
            is not None
        )
        self._bundled_model = bundled
        if model.state != "installed":
            self._error_details_by_component["model"] = model.detail
        self._sync_error_details()
        model_guidance = (
            "InstanSeg model files installed."
            if model.state == "installed"
            else (
                "Connect QuPath, then check again."
                if model.state == "not_checked"
                else (
                    "The included InstanSeg model cannot be used. Reinstall the app."
                    if bundled
                    else (
                        "InstanSeg model files are missing. Choose Install."
                        if model.state == "missing"
                        else "InstanSeg model files cannot be used. Open Details and keep these files unchanged."
                    )
                )
            )
        )
        self._model_status.setText(f"{labels[model.state]}: {model_guidance}")
        if model.state == "installed" and model.location is not None and self._verified_model:
            self._verified_model(model.location)
        extension_ready = extension.state == "installed" and runtime.status == "candidate"
        for button in (self._extension_check, self._extension_install):
            button.setVisible(not extension_ready)
        for button in (self._check_again, self._model_install):
            button.setVisible(model.state != "installed")
        self._model_source.setVisible(bundled)
        self._extension_install.setEnabled(extension.state == "missing")
        self._model_install.setEnabled(model.state == "missing" and not bundled)
        self._check_again.setEnabled(True)
        self._extension_check.setEnabled(True)

    def _confirm_download(self, detail: str) -> bool:
        """Confirm the named segmentation component and its exact download description."""
        return (
            QMessageBox.question(
                self,
                "Install cell-segmentation software",
                detail,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _offer_download(self) -> None:
        if self._pending or not self.isEnabled() or (self._state is not None and self._state.ready):
            return
        native = SOURCE_ARCHIVE_LOCK[f"pytorch-native-cpu-{self._service.platform}"]
        jni = SOURCE_ARCHIVE_LOCK["pytorch-jni"]
        detail = (
            "These libraries let InstanSeg run on this computer's CPU.\n\n"
            "Download and verify these exact files before analysis?\n\n"
            f"{native[0]}\n{native[1]} bytes; SHA-256 {native[2]}\n\n"
            f"{jni[0]}\n{jni[1]} bytes; SHA-256 {jni[2]}\n\n"
            "No download occurs during an analysis run."
        )
        if not self._confirm(detail):
            self._status.setText("Cell-segmentation setup not started. No files were downloaded.")
            return
        self._submit(
            "install-cpu-setup", lambda: self._service.install(consent=True), JobAccess.MUTATION
        )

    def _submit(
        self, operation: str, action: Callable[[], CpuSetupStatus], access: JobAccess
    ) -> None:
        try:
            job_id = self._jobs.submit(operation, action, access=access)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._show_error(
                "cpu",
                self._status,
                "Cell-segmentation setup could not start. Open Details before changing these files.",
                str(exc),
            )
            return
        self._pending.add(job_id)
        self._install.setEnabled(False)

    def _job_succeeded(self, job_id: str, value: object) -> None:
        """Apply only this panel's completed segmentation check or installation result."""
        if job_id in self._instanseg_jobs:
            kind = self._instanseg_jobs.pop(job_id)
            if kind == "inspect" and value is None:
                self._clear_error("extension")
                self._clear_error("model")
                self._extension_status.setText(
                    "Not checked: Connect QuPath to check its InstanSeg extension."
                )
                self._model_status.setText("Not checked: Connect QuPath to check its model.")
            elif kind == "inspect" and isinstance(value, InstanSegSetupStatus):
                self._show_instanseg(value)
            elif (
                kind in {"extension", "model"}
                and isinstance(value, tuple)
                and len(value) == 2
                and isinstance(value[1], InstanSegSetupStatus)
            ):
                if (
                    kind == "extension"
                    and value[0].state == "installed"
                    and self._extension_installed
                ):
                    self._extension_installed()
                self._show_instanseg(value[1])
            else:
                self._show_error(
                    "extension" if kind == "inspect" else kind,
                    self._model_status if kind == "model" else self._extension_status,
                    "InstanSeg setup could not be checked. Open Details before changing these files.",
                    "InstanSeg setup returned an invalid status.",
                )
            self._check_again.setEnabled(True)
            self._extension_check.setEnabled(True)
            return
        if job_id not in self._pending:
            return
        self._pending.remove(job_id)
        if not isinstance(value, CpuSetupStatus):
            self._show_error(
                "cpu",
                self._status,
                "Cell-segmentation setup could not be checked. "
                "Open Details before changing these files.",
                "Cell-segmentation setup returned an invalid status.",
            )
            return
        self._state = value
        self._clear_error("cpu")
        label = {"ready": "Installed", "missing": "Missing", "incompatible": "Needs attention"}[
            value.state
        ]
        guidance = {
            "ready": "Cell-segmentation library files are installed.",
            "missing": "Cell-segmentation libraries are missing. Choose Install to add them.",
            "incompatible": "Cell-segmentation libraries cannot be used. Open Details and keep these files unchanged.",
        }[value.state]
        self._status.setText(f"{label}: {guidance}")
        if value.state != "ready":
            self._error_details_by_component["cpu"] = value.detail
            self._sync_error_details()
        self._install.setVisible(not value.ready)
        self._install.setEnabled(not value.ready and not self._pending)

    def _job_failed(self, job_id: str, error: DesktopJobError) -> None:
        """Restore the affected segmentation action and retain its original diagnostic."""
        if job_id in self._instanseg_jobs:
            kind = self._instanseg_jobs.pop(job_id)
            label = self._model_status if kind == "model" else self._extension_status
            self._show_error(
                "extension" if kind == "inspect" else kind,
                label,
                (
                    "InstanSeg files could not be checked. "
                    "Open Details before changing these files."
                    if kind == "inspect"
                    else f"InstanSeg {kind} could not be installed. "
                    "Open Details before changing these files."
                ),
                error.diagnostic,
            )
            self._check_again.setEnabled(True)
            self._extension_check.setEnabled(True)
            if self._instanseg_state is not None:
                self._extension_install.setEnabled(
                    self._instanseg_state.extension.state == "missing"
                )
                self._model_install.setEnabled(
                    self._instanseg_state.model.state == "missing" and not self._bundled_model
                )
            return
        if job_id not in self._pending:
            return
        self._pending.remove(job_id)
        self._show_error(
            "cpu",
            self._status,
            (
                "Cell-segmentation libraries could not be checked. "
                "Open Details before changing these files."
                if error.operation == "inspect-cpu-setup"
                else "Cell-segmentation libraries could not be installed. "
                "Open Details before changing these files."
            ),
            error.diagnostic,
        )
        self._install.setEnabled(not self._pending)


__all__ = ["CpuSetupPanel", "CpuSetupPort"]
