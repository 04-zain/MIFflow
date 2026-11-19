"""Show included GPU libraries and install QuPath's matching native bridge."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .jobs import JobAccess
from .researcher_widgets import ContentSizedButton
from .software_details import open_software_details, software_details_button


class _LibraryReport(QLabel):
    """Keep wrapped library explanations visible at the setup page's width."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMinimumHeight(0)
        self.setMinimumHeight(max(0, self.heightForWidth(self.width())))

    def setText(self, text):
        super().setText(text)
        self.setMinimumHeight(0)
        self.setMinimumHeight(max(0, self.heightForWidth(self.width())))


class GpuSetupPanel(QWidget):
    """Use the existing job bridge for library checks and explicit installation."""

    def __init__(self, jobs, parent=None, *, service=None):
        from mif_pipeline.orchestration.setup.native_gpu_setup import NativeGpuSetupService

        super().__init__(parent)
        self._jobs = jobs
        self._service = service or NativeGpuSetupService()
        self._pending = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading = QLabel("NVIDIA processing libraries", self)
        heading.setProperty("panelHeading", True)
        layout.addWidget(heading)
        purpose = QLabel("Connect the included NVIDIA libraries for InstanSeg and KRONOS in QuPath.", self)
        purpose.setWordWrap(True)
        layout.addWidget(purpose)
        section = QFrame(self)
        section.setObjectName("gpu-setup-controls")
        body = QVBoxLayout(section)
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(10)
        adapter_heading = QLabel("QuPath GPU adapter", section)
        adapter_heading.setProperty("panelHeading", True)
        body.addWidget(adapter_heading)
        self.message = _LibraryReport(section)
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.message.setText("Reading installed library versions…")
        self.message.setProperty("setupReadiness", True)
        body.addWidget(self.message)
        self._details = "The library check has not finished."
        actions = QHBoxLayout()
        self.install = ContentSizedButton("Install…", section)
        self.install.setToolTip("Download the 3.4 MB adapter that connects QuPath to the included NVIDIA libraries.")
        self.install.setEnabled(False)
        self.install.clicked.connect(lambda: self._submit(True))
        self.refresh = ContentSizedButton("Check again", section)
        self.refresh.clicked.connect(lambda: self._submit(False))
        actions.addWidget(self.install)
        actions.addWidget(self.refresh)
        self.details_toggle = software_details_button(section)
        self.details_toggle.setObjectName("gpu-setup-details")
        self.details_toggle.setAccessibleName("QuPath GPU adapter details")
        self.details_toggle.clicked.connect(lambda: open_software_details(
            self, "QuPath GPU adapter", self.message.text(), self._details))
        actions.addWidget(self.details_toggle)
        actions.addStretch()
        body.addLayout(actions)
        layout.addWidget(section)
        jobs.job_succeeded.connect(self._success)
        jobs.job_failed.connect(self._failure)
        QTimer.singleShot(0, lambda: self._submit(False))

    def _submit(self, install):
        if self._pending is not None:
            return
        try:
            self._pending = self._jobs.submit(
                "native-gpu-install" if install else "native-gpu-status",
                self._service.install if install else self._service.inspect,
                access=JobAccess.MUTATION if install else JobAccess.READ)
            self.install.setEnabled(False)
            self.refresh.setEnabled(False)
            self.message.setText("Downloading and checking the 3.4 MB adapter…"
                                 if install else "Reading installed library versions…")
        except (RuntimeError, ValueError) as error:
            self.message.setText("Could not check GPU libraries. See Details.")
            self._details = str(error)

    def _success(self, identity, value):
        if identity != self._pending:
            return
        self._pending = None
        self.refresh.setEnabled(True)
        self.install.setVisible(value.state != "ready")
        self.refresh.setVisible(value.state != "ready")
        self.install.setEnabled(value.state == "missing")
        self.message.setText({
            "ready": "Connected: NVIDIA libraries are available to InstanSeg and KRONOS in QuPath.",
            "missing": "Setup needed: select Install to connect the included NVIDIA libraries.",
            "unavailable": "NVIDIA support for InstanSeg and KRONOS is not installed. See Details.",
            "incompatible": "Needs attention: the NVIDIA library files do not match the required installation.",
        }.get(value.state, "Check the GPU library details."))
        self._details = "\n\n".join(filter(None,
            (value.python_detail, value.native_detail)))

    def _failure(self, identity, error):
        if identity != self._pending:
            return
        self._pending = None
        self.refresh.setEnabled(True)
        self.refresh.show()
        self.message.setText("GPU setup failed. See Details, then check again.")
        self._details = getattr(error, "diagnostic", str(error))
