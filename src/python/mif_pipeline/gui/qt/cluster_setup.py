"""Confirm cluster locations from Samples; this dialog never authors identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
)
from .form_sections import label, WorkbenchMessage
from .researcher_widgets import ContentSizedButton
from .jobs import JobAccess

# ==============================================================================
#                                 Cluster setup
# ==============================================================================


class ClusterSetupDialog(QDialog):
    def __init__(self, client, jobs, mounted_images=None, parent=None, *, samples=()):
        super().__init__(parent)
        self.client = client
        self.jobs = jobs
        self._pending = None
        self._samples = tuple(samples)
        self.setWindowTitle("Confirm cluster image locations")
        self.resize(1020, 570)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        layout.addWidget(label("Use the samples already selected", self, role="dialogHeading"))
        layout.addWidget(
            label(
                f"{len(self._samples)} included samples will be prepared on the cluster. Sample names, patient or animal identifiers and grouping are taken from Samples.",
                self,
                role="choiceHelp",
            )
        )
        self.table = QTableWidget(len(self._samples), 3, self)
        self.table.setObjectName("cluster-setup-samples")
        self.table.setHorizontalHeaderLabels(
            ["Sample", "mIF image on cluster", "Paired image on cluster"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        from .workbench import style_scientific_table
        from .channel_table import WrappedTableHeader

        style_scientific_table(self.table)
        self.table.setHorizontalHeader(WrappedTableHeader(self.table))
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        for index, sample in enumerate(self._samples):
            reference = sample.get("paired_reference_image") or {}
            for column, text in enumerate(
                (sample["sample_id"], sample["source_mif_path"], reference.get("source_path", ""))
            ):
                cell = QTableWidgetItem(str(text))
                cell.setToolTip(str(text))
                self.table.setItem(index, column, cell)
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)
        layout.addWidget(
            label(
                "To change which samples are included, close this window and edit Samples. The cluster will check that the mapped files exist before creating the setup.",
                self,
                role="choiceHelp",
            )
        )
        self.message = WorkbenchMessage(self)
        layout.addWidget(self.message)
        from .action_bar import WorkflowActionBar

        footer = WorkflowActionBar(self)
        self.prepare = ContentSizedButton("Prepare these samples on cluster", self)
        self.prepare.setProperty("actionRole", "primary")
        self.prepare.setEnabled(bool(self._samples))
        self.prepare.clicked.connect(self.submit)
        footer.add_primary(self.prepare)
        close = ContentSizedButton("Close", self)
        close.clicked.connect(self.reject)
        footer.add_utility(close)
        layout.addWidget(footer)
        jobs.job_succeeded.connect(self.completed)
        jobs.job_failed.connect(self.failed)

    def submit(self):
        if self._pending or self.jobs.has_active_jobs:
            return
        try:
            if not self._samples:
                raise ValueError("Include samples on the Samples page first.")
            rows = list(self._samples)
            self._pending = self.jobs.submit(
                "prepare-cluster-analysis",
                lambda: self.client.prepare_analysis(rows),
                access=JobAccess.MUTATION,
            )
            self.prepare.setEnabled(False)
            self.message.setPlainText(
                "Checking the mapped image paths and preparing the cluster analysis…"
            )
        except (ValueError, RuntimeError) as error:
            self.message.setPlainText(str(error))

    def completed(self, identity, result):
        if identity != self._pending:
            return
        self._pending = None
        self.prepare.setEnabled(True)
        self.message.setPlainText(
            f"Prepared {result['sample_count']} samples from Samples. The cluster analysis is ready to start."
        )

    def failed(self, identity, error):
        if identity != self._pending:
            return
        self._pending = None
        self.prepare.setEnabled(True)
        self.message.setPlainText(getattr(error, "message", str(error)))

    def reject(self):
        if self._pending:
            self.message.setPlainText("Wait for the setup operation to finish before closing.")
            return
        super().reject()

    def closeEvent(self, event):
        if self._pending:
            event.ignore()
            return
        super().closeEvent(event)
