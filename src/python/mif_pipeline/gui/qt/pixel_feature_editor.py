"""Direct per-scale recipe editing when native image inspection is unavailable."""
from copy import deepcopy
import math
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QVBoxLayout, QWidget
from .researcher_widgets import ContentSizedButton
from mif_pipeline.gui.pixel_image_features import FILTER_LABELS, image_features_from_values


class PixelFeatureEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.intensity = QCheckBox("Include channel intensity", self)
        layout.addWidget(self.intensity)
        self.scales = QListWidget(self)
        self.scales.setAccessibleName("Image filter scales")
        self.scales.setMaximumHeight(120)
        layout.addWidget(self.scales)
        actions = QHBoxLayout()
        self.add = ContentSizedButton("Add scale", self)
        self.remove = ContentSizedButton("Remove selected scale", self)
        actions.addWidget(self.add); actions.addWidget(self.remove); actions.addStretch()
        layout.addLayout(actions)
        self.scale_details = QWidget(self)
        details = QVBoxLayout(self.scale_details)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(12)
        sigma_row = QHBoxLayout()
        label = QLabel("Gaussian σ (µm)", self.scale_details)
        self.sigma = QLineEdit(self.scale_details)
        self.sigma.setAccessibleName("Gaussian sigma in micrometres")
        label.setBuddy(self.sigma)
        sigma_row.addWidget(label); sigma_row.addWidget(self.sigma)
        details.addLayout(sigma_row)
        filter_grid = QGridLayout()
        filter_grid.setHorizontalSpacing(20)
        filter_grid.setVerticalSpacing(10)
        details.addLayout(filter_grid)
        self.filters = {}
        for index, (key, title) in enumerate(FILTER_LABELS.items()):
            control = QCheckBox(title, self.scale_details)
            self.filters[key] = control
            filter_grid.addWidget(control, index // 2, index % 2)
            control.toggled.connect(self._store)
        layout.addWidget(self.scale_details)
        self.scales.currentRowChanged.connect(self._select)
        self.sigma.textChanged.connect(self._store)
        self.intensity.toggled.connect(self.changed)
        self.add.clicked.connect(self._add)
        self.remove.clicked.connect(self._remove)
        self.set_value({"include_intensity": True, "scales": []})

    def set_value(self, value):
        """Restore one current recipe or a same-form draft, including unfinished rows."""
        self._loading = True
        self._rows = [{"sigma_um": row["sigma_um"], "filters": list(row["filters"])} for row in value["scales"]]
        self.intensity.setChecked(value["include_intensity"])
        self.scales.clear()
        self.scales.addItems([self._label(row) for row in self._rows])
        self._loading = False
        self._fit_scales()
        self.scales.setCurrentRow(0 if self._rows else -1)
        self._select(self.scales.currentRow())

    def _fit_scales(self):
        self.scales.setFixedHeight(8 + 24 * min(4, max(2, len(self._rows))))

    @staticmethod
    def _label(row):
        try:
            value = float(row["sigma_um"])
            scale = f"σ {value:.6g} µm" if math.isfinite(value) and value > 0 else "Enter a positive σ"
        except (TypeError, ValueError):
            scale = "Enter a positive σ"
        count = len(row["filters"])
        return f"{scale} · {count} filter{'s' if count != 1 else ''}"

    def _select(self, index):
        if self._loading:
            return
        self._loading = True
        row = self._rows[index] if 0 <= index < len(self._rows) else None
        self.sigma.setText(str(row["sigma_um"]) if row else "")
        self.sigma.setEnabled(row is not None)
        self.remove.setEnabled(row is not None)
        self.scale_details.setVisible(row is not None)
        self.scales.setVisible(bool(self._rows))
        self.remove.setVisible(bool(self._rows))
        for key, control in self.filters.items():
            control.setChecked(row is not None and key in row["filters"])
            control.setEnabled(row is not None)
        self._loading = False

    def _store(self, *_):
        index = self.scales.currentRow()
        if self._loading or index < 0:
            return
        row = self._rows[index]
        chosen = [key for key, control in self.filters.items() if control.isChecked()]
        row["filters"] = [key for key in row["filters"] if key in chosen] + [key for key in chosen if key not in row["filters"]]
        row["sigma_um"] = self.sigma.text()
        self.scales.item(index).setText(self._label(row))
        self.changed.emit()

    def _add(self):
        index = self.scales.currentRow()
        previous = self._rows[index] if index >= 0 else None
        try:
            sigma = float(previous["sigma_um"]) * 2 if previous else 1.0
            if not math.isfinite(sigma) or sigma <= 0:
                sigma = 1.0
        except (TypeError, ValueError):
            sigma = 1.0
        used = set()
        for row in self._rows:
            try:
                used.add(float(row["sigma_um"]))
            except (TypeError, ValueError):
                pass
        while math.isfinite(sigma) and sigma in used:
            sigma *= 2
        row = {"sigma_um": sigma if math.isfinite(sigma) else "", "filters": list(previous["filters"]) if previous else []}
        self._rows.append(row)
        self.scales.addItem(self._label(row))
        self._fit_scales()
        self.scales.setCurrentRow(len(self._rows) - 1)
        self.changed.emit()

    def _remove(self):
        index = self.scales.currentRow()
        if index >= 0:
            self._loading = True
            self._rows.pop(index)
            self.scales.takeItem(index)
            self._loading = False
            self._fit_scales()
            self._select(self.scales.currentRow())
            self.changed.emit()

    def capture_draft(self):
        return {"include_intensity": self.intensity.isChecked(), "scales": deepcopy(self._rows)}

    def value(self):
        return image_features_from_values({"editor_image_features": self.capture_draft()}, "editor")
