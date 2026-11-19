"""Optional segmentation downloads on the existing Software setup job bridge."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QVBoxLayout

from mif_pipeline.orchestration.setup.alternative_segmentation_setup import AlternativeSegmentationSetupService
from .jobs import JobAccess
from .researcher_widgets import ContentSizedButton
from .software_details import open_software_details, software_details_button


class AlternativeSegmentationSetupPanel(QFrame):
    def __init__(self, method, jobs, parent=None):
        super().__init__(parent)
        self.setObjectName(f"{method}-setup-panel")
        self._service = AlternativeSegmentationSetupService(method)
        self._jobs = jobs
        self._pending = None
        self._method = method
        self._title = "Cellpose-SAM" if method == "cellpose" else "StarDist"
        self._details = "The model check has not finished."
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QLabel("Cellpose-SAM model" if method == "cellpose" else "StarDist model and extension", self)
        heading.setProperty("panelHeading", True)
        layout.addWidget(heading)
        self.status = QLabel("Checking model installation…", self)
        self.status.setObjectName(f"{method}-model-status")
        self.status.setProperty("setupReadiness", True)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.install = ContentSizedButton("Install…", self)
        self.install.setObjectName(f"{method}-model-install")
        self.install.setAccessibleName("Install Cellpose-SAM model" if method == "cellpose" else "Install StarDist model and extension")
        self.install.setProperty("actionRole", "secondary")
        self.install.clicked.connect(self._install)
        actions.addWidget(self.install)
        self.check = ContentSizedButton("Check again", self)
        self.check.setObjectName(f"{method}-model-check")
        self.check.setProperty("actionRole", "primary")
        self.check.clicked.connect(self.refresh)
        actions.addWidget(self.check)
        self.details = software_details_button(self)
        self.details.setObjectName(f"{method}-model-details")
        self.details.setAccessibleName(f"{self._title} setup details")
        self.details.clicked.connect(lambda: open_software_details(
            self, f"{self._title} setup", self.status.text(), self._details))
        actions.addWidget(self.details)
        actions.addStretch()
        layout.addLayout(actions)
        jobs.job_succeeded.connect(self._succeeded)
        jobs.job_failed.connect(self._failed)
        self.refresh()

    def _submit(self, operation, action, access):
        if self._pending is not None:
            return
        self._pending = self._jobs.submit(f"{self._method}-{operation}", action, access=access)
        self.install.setEnabled(False)
        self.check.setEnabled(False)
        self.status.setText("Installing selected model…" if operation == "install" else "Checking installed model…")

    def refresh(self):
        self._submit("inspect", self._service.inspect, JobAccess.READ)

    def _install(self):
        release = self._service.release
        size = sum(asset["size_bytes"] for asset in release["files"]) / 1024**2
        sources = "\n".join(asset["url"] for asset in release["files"] if not asset["filename"].startswith("LICENSE"))
        text = (f"Install {release['version']} ({size:,.1f} MiB)?\n\n"
                f"Code: {release['code_license']}. Model: {release['model_license']}.\n\n"
                f"Destination: {self._service.directory}\n\nSources:\n{sources}")
        if QMessageBox.question(self, "Install optional segmentation model", text) != QMessageBox.StandardButton.Yes:
            return
        self._submit("install", lambda: self._service.install(consent=True), JobAccess.MUTATION)

    def _succeeded(self, job_id, status):
        if job_id != self._pending:
            return
        self._pending = None
        self._details = f"Location: {status.location}\n\n{status.detail}"
        self.status.setText({
            "installed": f"Installed: {self._title} model files are available.",
            "missing": f"Missing: {self._title} model files are not fully installed.",
            "incompatible": f"Needs attention: {self._title} files do not match the required version.",
        }.get(status.state, f"{self._title} setup could not be checked."))
        if status.state == "incompatible" and status.detail.startswith("Cellpose runtime is unavailable"):
            self.status.setText("Needs attention: Cellpose scoring software is unavailable.")
        self.install.setVisible(status.state != "installed")
        self.check.setVisible(status.state != "installed")
        self.install.setEnabled(status.state == "missing")
        self.check.setEnabled(True)

    def _failed(self, job_id, error):
        if job_id != self._pending:
            return
        self._pending = None
        self.install.show()
        self.check.show()
        self.status.setText(f"{self._title} setup failed. See Details.")
        self._details = error.diagnostic
        self.install.setEnabled(True)
        self.check.setEnabled(True)
