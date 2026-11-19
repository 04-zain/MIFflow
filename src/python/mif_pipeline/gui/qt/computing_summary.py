"""Read-only computing choices shared by local and streaming Plans.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import PurePosixPath
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QGridLayout, QWidget
from .form_sections import FormSection, label

# ==============================================================================
#                               Computing summary
# ==============================================================================


class ComputingSummary(FormSection):
    def __init__(self, parent=None):
        super().__init__("Computing", parent=parent)
        self.setObjectName("plan-processing-section")
        self.values = {}
        self.rows = {}
        self._columns = 0
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(24)
        self._grid.setVerticalSpacing(14)
        self.body.addLayout(self._grid)
        self._reflow = QTimer(self)
        self._reflow.setSingleShot(True)
        self._reflow.timeout.connect(self._arrange)
        for key, title in (
            ("mode", "Processing location"),
            ("processor", "Preferred processor"),
            ("workspace", "Analysis folder"),
            ("cluster", "Cluster analysis folder"),
            ("resources", "Job resources"),
        ):
            row = QWidget(self)
            layout = QVBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(3)
            layout.addWidget(label(title, row, role="fieldCaption"))
            value = label("Not yet selected", row, name="plan-computing-" + key)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
            layout.addWidget(value)
            self._grid.addWidget(row, len(self.rows), 0)
            self.values[key] = value
            self.rows[key] = row
        self.rows["cluster"].hide()
        self.rows["resources"].hide()

    def _arrange(self):
        columns = 2 if self.width() >= 800 and self.fontMetrics().height() < 27 else 1
        if columns == self._columns:
            return
        self._columns = columns
        for row in self.rows.values():
            self._grid.removeWidget(row)
        for index, (key, row) in enumerate(self.rows.items()):
            self._grid.addWidget(row, index // columns, index % columns)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1 if columns == 2 else 0)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow.start(0)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
            self, "_reflow"
        ):
            self._columns = 0
            self._reflow.start(0)

    def set_location(self, settings, workspace, *, valid=True):
        mode = settings.get("mode", "local")
        remote = settings.get("remote") or {}
        processing = remote if mode == "streaming" else settings
        device = {
            "auto": "Automatic CPU or GPU",
            "cpu": "CPU",
            "cuda": "NVIDIA GPU",
            "mps": "Apple Metal GPU",
        }.get(processing.get("device", "auto"), "Not selected")
        values = {
            "mode": (
                {
                    "local": "This computer",
                    "desktop": "Cluster desktop",
                    "streaming": "Cluster streaming",
                }[mode]
                if valid
                else "Review Computing settings"
            ),
            "processor": device if valid else "Not confirmed",
            "workspace": str(workspace),
        }
        streaming = mode == "streaming" and bool(remote) and valid
        self.rows["cluster"].setVisible(streaming)
        self.rows["resources"].setVisible(streaming)
        if streaming:
            values["cluster"] = str(PurePosixPath(remote["output_root"]) / remote["batch_id"])
            values["resources"] = (
                f"{remote['cpus']} CPU cores · {remote['memory_gib']} GiB memory · "
                f"{remote['hours']} hours · {remote['gpus']} GPUs"
            )
            if remote.get("partition"):
                values["resources"] += f" · Queue: {remote['partition']}"
            if remote.get("account"):
                values["resources"] += f" · Account: {remote['account']}"
        for key, widget in self.values.items():
            widget.setText(values.get(key, ""))
            widget.setToolTip(values.get(key, ""))
