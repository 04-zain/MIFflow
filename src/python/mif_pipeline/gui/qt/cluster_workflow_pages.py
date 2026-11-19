"""Plan and Run views for an explicitly selected cluster analysis."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QSizePolicy,
)
from .form_sections import FormSection, label, scroll_content, WorkbenchMessage
from .researcher_widgets import ContentSizedButton
from .action_bar import WorkflowActionBar
from .workbench import style_scientific_table, fit_scientific_columns

# ==============================================================================
#                             Cluster workflow pages
# ==============================================================================


class ExecutionRoute(QWidget):
    def __init__(self, local, remote, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self)
        self.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.stack.addWidget(local)
        self.stack.addWidget(remote)
        layout.addWidget(self.stack)

    def use_remote(self, enabled):
        self.stack.setCurrentIndex(1 if enabled else 0)


class ClusterPlanPage(QWidget):
    start_requested = Signal()
    configure_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, body, layout = scroll_content(self)
        layout.addWidget(label("Plan", body, role="dialogHeading", name="cluster-plan-title"))
        self.summary = label("", body, role="choiceHelp")
        layout.addWidget(self.summary)
        from .computing_summary import ComputingSummary

        self.computing_summary = ComputingSummary(body)
        layout.addWidget(self.computing_summary)
        section = FormSection(
            "Samples and cluster locations",
            "These are the same images and identifiers selected on Samples.",
            body,
        )
        self.samples = QTableWidget(0, 3, section)
        self.samples.setObjectName("cluster-plan-samples")
        self.samples.setHorizontalHeaderLabels(["Sample", "Patient or animal", "Image on cluster"])
        self.samples.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        style_scientific_table(self.samples)
        section.body.addWidget(self.samples)
        layout.addWidget(section)
        self.message = WorkbenchMessage(body)
        layout.addWidget(self.message)
        layout.addStretch(1)
        outer.addWidget(scroll, 1)
        bar = WorkflowActionBar(self)
        self.start = ContentSizedButton("Start analysis on cluster", self)
        self.start.setObjectName("cluster-plan-start")
        self.start.setProperty("actionRole", "primary")
        self.start.clicked.connect(self.start_requested.emit)
        bar.add_primary(self.start)
        outer.addWidget(bar)
        self.start.setEnabled(False)

    def set_computing(self, settings, workspace, *, valid=True):
        self.computing_summary.set_location(settings, workspace, valid=valid)

    def set_samples(self, rows, profile, *, prepared=False):
        self.samples.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, key in enumerate(("sample_id", "subject_id", "source_mif_path")):
                item = QTableWidgetItem(str(row[key]))
                item.setToolTip(str(row[key]))
                self.samples.setItem(index, column, item)
        fit_scientific_columns(self.samples)
        self.samples.setMinimumHeight(min(330, 65 + 42 * len(rows)))
        self.summary.setText(f"{len(rows)} samples · {profile.host}")
        self.message.setPlainText(
            "The analysis setup is already stored on the cluster."
            if prepared
            else "Start analysis sends this sample list, creates the cluster setup file and submits the processing job. Open the current review from Run to complete forms and annotations."
        )
        self.start.setEnabled(bool(rows) and not prepared)


class ClusterRunPage(QWidget):
    start_requested = Signal()
    resume_requested = Signal()
    reviews_requested = Signal()
    refresh_requested = Signal()
    options_requested = Signal()
    retry_requested = Signal()
    continue_requested = Signal()
    reconcile_requested = Signal()
    processors_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(16)
        layout.addWidget(
            label("Cluster analysis", self, role="dialogHeading", name="cluster-run-title")
        )
        self.description = label(
            "Image processing runs on the cluster. Complete each pending form and annotation review from this application.",
            self,
            role="choiceHelp",
        )
        layout.addWidget(self.description)
        self.status = WorkbenchMessage(self)
        layout.addWidget(self.status)
        self.start = ContentSizedButton("Start analysis on cluster", self)
        self.start.setObjectName("cluster-run-start")
        self.start.setProperty("actionRole", "primary")
        self.start.clicked.connect(self.start_requested.emit)
        self.resume = ContentSizedButton("Resume analysis", self)
        self.resume.setObjectName("cluster-run-resume")
        self.resume.setProperty("actionRole", "primary")
        self.resume.setEnabled(False)
        self.resume.clicked.connect(self.resume_requested.emit)
        self.reviews = ContentSizedButton("Open current review", self)
        self.reviews.clicked.connect(self.reviews_requested.emit)
        self.refresh = ContentSizedButton("Refresh progress", self)
        self.refresh.clicked.connect(self.refresh_requested.emit)
        self.processors = ContentSizedButton("Check processors", self)
        self.processors.clicked.connect(self.processors_requested.emit)
        self.options = ContentSizedButton("Change a completed step…", self)
        self.options.clicked.connect(self.options_requested.emit)
        self.retry = ContentSizedButton("Retry a failed step…", self)
        self.retry.clicked.connect(self.retry_requested.emit)
        self.continue_saved = ContentSizedButton("Continue saved work", self)
        self.continue_saved.clicked.connect(self.continue_requested.emit)
        self.reconcile = ContentSizedButton("Check submission", self)
        self.reconcile.clicked.connect(self.reconcile_requested.emit)
        from .form_navigation import FormNavigation

        bar = FormNavigation(self)
        bar.addWidget(self.start)
        bar.addWidget(self.resume)
        bar.addWidget(self.reviews)
        bar.addWidget(self.refresh)
        bar.addWidget(self.processors)
        controls = FormNavigation(self)
        controls.addWidget(self.continue_saved)
        controls.addWidget(self.options)
        controls.addWidget(self.retry)
        controls.addWidget(self.reconcile)
        layout.addStretch(1)
        layout.addWidget(controls)
        layout.addWidget(bar)
