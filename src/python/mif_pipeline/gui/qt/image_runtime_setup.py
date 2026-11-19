"""Home controls for the shared Java and Bio-Formats image-reading setup.

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

from mif_pipeline.orchestration.setup.image_runtime_setup import ImageRuntimeSetupStatus
from mif_pipeline.orchestration.setup.image_runtime_setup import image_runtime_download_info

from .jobs import DesktopJobError, JobAccess, ManagedJobBridge
from .software_details import open_software_details, software_details_button

_BUNDLE_POLICY_GUIDANCE = {
    "bundled_runtime_unsupported": (
        "This application includes unsupported Bio-Formats files. Reinstall the app."
    ),
    "payload_inside_bundle": ("Select or install Bio-Formats outside the application folder."),
}


# ==============================================================================
#                              Image runtime setup
# ==============================================================================


def _bundle_policy_guidance(detail: str) -> str | None:
    code, separator, _explanation = detail.partition(": ")
    return _BUNDLE_POLICY_GUIDANCE.get(code) if separator else None


class ImageRuntimeSetupPort(Protocol):
    platform: str

    def inspect(self) -> tuple[ImageRuntimeSetupStatus, ...]: ...
    def select(self, component: str, location: Path) -> ImageRuntimeSetupStatus: ...
    def install(self, component: str, *, consent: bool) -> ImageRuntimeSetupStatus: ...


class ImageRuntimeSetupPanel(QWidget):
    """Offer explicit local selection and consented installation off-thread."""

    def __init__(
        self,
        service: ImageRuntimeSetupPort,
        jobs: ManagedJobBridge,
        *,
        choose_directory: Callable[[str], Path | None] | None = None,
        confirm_download: Callable[[str], bool] | None = None,
        accept_choice: Callable[[ImageRuntimeSetupStatus], str | None] | None = None,
        refresh_service: Callable[[], ImageRuntimeSetupPort] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("image-runtime-setup-panel")
        self._service = service
        self._jobs = jobs
        self._choose = choose_directory or self._choose_directory
        self._confirm = confirm_download or self._confirm_download
        self._accept = accept_choice or (lambda _status: None)
        self._refresh_service = refresh_service
        self._pending: dict[str, tuple[str, str | None]] = {}
        self._actions_allowed = True
        self._statuses: dict[str, ImageRuntimeSetupStatus] = {}
        self._labels: dict[str, QLabel] = {}
        self._select: dict[str, QPushButton] = {}
        self._install: dict[str, QPushButton] = {}
        self._advisory: dict[str, str] = {}
        self._error_details_by_component: dict[str, str] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        heading = QLabel("Image-reading setup", self)
        heading.setProperty("panelHeading", True)
        outer.addWidget(heading)
        purpose = QLabel("Install the software needed to read slide images and their metadata.", self)
        purpose.setWordWrap(True)
        outer.addWidget(purpose)
        self._permission_guidance = QLabel(
            "Select QuPath before choosing image-reading software.", self
        )
        self._permission_guidance.setWordWrap(True)
        self._permission_guidance.hide()
        outer.addWidget(self._permission_guidance)
        controls = QFrame(self)
        controls.setObjectName("image-runtime-setup-controls")
        outer.addWidget(controls)
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        for index, (component, title) in enumerate(
            (("java", "Java 21"), ("bioformats", "Bio-Formats"))
        ):
            if index:
                layout.addSpacing(10)
            section = QWidget(self)
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(8)
            heading = QLabel(title, section)
            heading.setProperty("panelHeading", True)
            section_layout.addWidget(heading)
            status = QLabel("Checking: local files and runtime…", section)
            status.setWordWrap(True)
            status.setTextFormat(Qt.TextFormat.PlainText)
            status.setObjectName(f"image-runtime-status-{component}")
            status.setProperty("setupReadiness", True)
            section_layout.addWidget(status)
            self._labels[component] = status
            row = QHBoxLayout()
            install = QPushButton("Install…", section)
            install.setProperty("actionRole", "secondary")
            install.setObjectName(f"image-runtime-install-{component}")
            install.setAccessibleName(f"Install {title}")
            install.clicked.connect(
                lambda _checked=False, name=component: self._offer_install(name)
            )
            row.addWidget(install)
            self._install[component] = install
            select = QPushButton("Use existing…", section)
            select.setProperty("actionRole", "primary")
            select.setObjectName(f"image-runtime-select-{component}")
            select.setAccessibleName(f"Use existing {title}")
            select.clicked.connect(lambda _checked=False, name=component: self._offer_select(name))
            row.addWidget(select)
            self._select[component] = select
            details = software_details_button(section)
            details.setObjectName(f"image-runtime-details-{component}")
            details.setAccessibleName(f"{title} setup details")
            details.clicked.connect(lambda _checked=False, name=component: self._show_details(name))
            row.addWidget(details)
            row.addStretch(1)
            section_layout.addLayout(row)
            layout.addWidget(section)
        jobs.job_succeeded.connect(self._job_succeeded)
        jobs.job_failed.connect(self._job_failed)
        self._update_buttons()
        self.refresh()

    def status_text(self, component: str) -> str:
        return self._labels[component].text()

    def _show_error(self, components: tuple[str, ...], summary: str, detail: str) -> None:
        for component in components:
            self._labels[component].setText(summary)
            self._error_details_by_component[component] = detail

    def _clear_error(self, component: str) -> None:
        self._error_details_by_component.pop(component, None)

    def _show_details(self, component: str) -> None:
        labels = {"java": "Java 21", "bioformats": "Bio-Formats"}
        sections = []
        if error := self._error_details_by_component.get(component):
            sections.append(error)
        if status := self._statuses.get(component):
            sections.append(
                f"Last check: {status.state}\n"
                f"Location: {status.location or 'Not selected'}\n"
                f"Included with application: {'Yes' if status.included else 'No'}\n\n{status.detail}"
            )
        open_software_details(self, labels[component] + " setup", self.status_text(component),
                              "\n\n".join(sections) or "The setup check has not finished.")

    def _choose_directory(self, component: str) -> Path | None:
        title = "Select Java home" if component == "java" else "Select Bio-Formats files"
        selected = QFileDialog.getExistingDirectory(self, title)
        return Path(selected) if selected else None

    def _confirm_download(self, detail: str) -> bool:
        return (
            QMessageBox.question(
                self,
                "Install image-reading files",
                detail,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _update_buttons(self) -> None:
        busy = bool(self._pending) or not self._actions_allowed
        for component in ("java", "bioformats"):
            self._select[component].setEnabled(not busy)
            status = self._statuses.get(component)
            ready = status is not None and status.state == "ready"
            self._select[component].setText("Change location…" if ready else "Use existing…")
            self._select[component].setAccessibleName(f"Change {component} location" if ready else f"Use existing {component}")
            self._install[component].setVisible(not ready)
            self._install[component].setEnabled(
                not busy and (status is None or status.state != "ready")
            )

    def set_actions_allowed(self, allowed: bool) -> None:
        self._actions_allowed = allowed
        self._permission_guidance.setVisible(not allowed)
        self.setToolTip("" if allowed else "Select QuPath before choosing image-reading software.")
        self._update_buttons()

    def refresh(self) -> None:
        if self._pending:
            return
        self._submit("inspect", None, self._service.inspect, JobAccess.READ)

    def _offer_select(self, component: str) -> None:
        if self._pending or not self._actions_allowed or not self.isEnabled():
            return
        location = self._choose(component)
        if location is None:
            return
        if not location.is_absolute():
            self._labels[component].setText("Select an absolute local directory.")
            return
        self._submit(
            "select", component, lambda: self._service.select(component, location), JobAccess.READ
        )

    def _offer_install(self, component: str) -> None:
        if self._pending or not self._actions_allowed or not self.isEnabled():
            return
        try:
            info = image_runtime_download_info(component, self._service.platform)
        except (ValueError, RuntimeError) as exc:
            self._show_error(
                (component,),
                "Download information is unavailable. Open Details before changing this setup.",
                str(exc),
            )
            return
        size_mb = info.approximate_size_bytes / 1_000_000
        detail = (
            f"Download and verify {component} {info.version}?\n\n"
            f"Approximate download: {size_mb:.1f} MB\n"
            f"Source: {', '.join(info.sources)}\n\n"
            "Files are installed only for this user."
        )
        if not self._confirm(detail):
            self._labels[component].setText("Installation not started. No files were downloaded.")
            return
        self._submit(
            "install",
            component,
            lambda: self._service.install(component, consent=True),
            JobAccess.MUTATION,
        )

    def _submit(
        self, action: str, component: str | None, operation: Callable[[], object], access: JobAccess
    ) -> None:
        try:
            job_id = self._jobs.submit(f"image-runtime-{action}", operation, access=access)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._show_error(
                (component,) if component is not None else ("java", "bioformats"),
                "Image-reading setup could not start. Open Details before changing this setup.",
                str(exc),
            )
            return
        self._pending[job_id] = (action, component)
        if component is not None:
            self._labels[component].setText(
                "Checking: selected files…"
                if action == "select"
                else "Checking: installing and verifying files…"
            )
        self._update_buttons()

    def _show_status(self, status: ImageRuntimeSetupStatus) -> None:
        self._clear_error(status.component)
        self._statuses[status.component] = status
        guidance = {
            ("java", "ready"): "Java 21 is available.",
            ("java", "missing"): "Java 21 files are unavailable.",
            ("java", "incompatible"): "Choose a compatible Java 21 installation.",
            ("java", "not_checked"): "Java 21 has not been selected.",
            ("bioformats", "ready"): "Bio-Formats is available.",
            ("bioformats", "missing"): "Bio-Formats files are unavailable.",
            ("bioformats", "incompatible"): "Choose supported Bio-Formats files.",
            ("bioformats", "not_checked"): "Complete Java setup to check Bio-Formats.",
        }[(status.component, status.state)]
        if status.component == "bioformats" and status.state == "incompatible":
            guidance = _bundle_policy_guidance(status.detail) or guidance
        if status.component == "java" and status.state == "not_checked":
            if status.detail.startswith("Image-reading check failed"):
                guidance = (
                    "Java could not start. Select Java 21."
                    if "Java could not start" in status.detail
                    else "Image-reading setup could not be checked. Open Details before changing this setup."
                )
                self._error_details_by_component[status.component] = status.detail
            elif status.location is not None:
                guidance = "Image-reading check is pending."
        label = {
            "ready": "Ready",
            "missing": "Missing",
            "incompatible": "Needs attention",
            "not_checked": "Not checked",
        }[status.state]
        advisory = self._advisory.get(status.component)
        self._labels[status.component].setText(
            f"{label}: {guidance}" + (f" {advisory}" if advisory else "")
        )

    def _job_succeeded(self, job_id: str, value: object) -> None:
        pending = self._pending.pop(job_id, None)
        if pending is None:
            return
        action, component = pending
        if (
            action == "inspect"
            and isinstance(value, tuple)
            and len(value) == 2
            and all(isinstance(item, ImageRuntimeSetupStatus) for item in value)
        ):
            for status in value:
                self._show_status(status)
        elif component is not None and isinstance(value, ImageRuntimeSetupStatus):
            self._show_status(value)
            if value.state in {"ready", "not_checked"} and value.location is not None:
                try:
                    advisory = self._accept(value)
                    if self._refresh_service is not None:
                        self._service = self._refresh_service()
                except (OSError, TypeError, ValueError, RuntimeError) as exc:
                    self._show_error(
                        (component,),
                        "The selected image-reading files could not be applied. "
                        "Open Details before changing this setup.",
                        str(exc),
                    )
                else:
                    if advisory:
                        self._advisory[component] = advisory
                    else:
                        self._advisory.pop(component, None)
                    self.refresh()
        else:
            self._show_error(
                (component,) if component is not None else ("java", "bioformats"),
                "Image-reading setup could not be checked. "
                "Open Details before changing this setup.",
                "Image-reading setup returned an invalid status.",
            )
        self._update_buttons()

    def _job_failed(self, job_id: str, error: DesktopJobError) -> None:
        pending = self._pending.pop(job_id, None)
        if pending is None:
            return
        action, component = pending
        components = ("java", "bioformats") if component is None else (component,)
        if error.error_type == "ImageRuntimeSetupError" and error.message.startswith(
            "download_http_error: HTTP 503"
        ):
            summary = (
                "The download server is temporarily unavailable (HTTP 503). "
                "Try later or select existing files."
            )
        elif (
            error.error_type == "InventoryIncompatible"
            and (policy_guidance := _bundle_policy_guidance(error.message)) is not None
        ):
            summary = policy_guidance
        else:
            action_text = {
                "inspect": "checked",
                "select": "checked",
                "install": "installed",
            }[action]
            summary = (
                f"Image-reading files could not be {action_text}. "
                "Open Details before changing this setup."
            )
        self._show_error(components, summary, error.diagnostic)
        self._update_buttons()


__all__ = ["ImageRuntimeSetupPanel", "ImageRuntimeSetupPort"]
