"""Optional coordinate-view controls, independent of cluster-label acceptance."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLabel,
    QSpinBox, QDoubleSpinBox, QComboBox, QToolButton, QSizePolicy,
)

from .researcher_widgets import ContentSizedButton, configure_disclosure


class UmapReviewControls(QWidget):
    """Keep optional fitting and colouring separate from biological cluster names."""

    requested = Signal()
    changed = Signal()
    cell_selected = Signal(object)

    def __init__(self, parent=None, *, collapsible=False):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._body = QWidget(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        if collapsible:
            self.toggle = QToolButton(self)
            self.toggle.setText('UMAP view'); self.toggle.setCheckable(True)
            configure_disclosure(self.toggle)
            self.toggle.toggled.connect(self._body.setVisible)
            layout.addWidget(self.toggle,alignment=Qt.AlignmentFlag.AlignLeft); self._body.hide()
        layout.addWidget(self._body)
        layout = body_layout
        text = self.introduction = QLabel("UMAP shows cells with similar marker measurements near one another. Calculate this optional view to inspect the existing clusters.")
        text.setWordWrap(True)
        text.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Maximum)
        layout.addWidget(text)
        self.parameters=QWidget(self._body)
        body_layout.addWidget(self.parameters)
        layout=QVBoxLayout(self.parameters);layout.setContentsMargins(0,0,0,0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        form = QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(6)
        self.maximum = QSpinBox()
        self.maximum.setRange(3, 1_000_000)
        self.maximum.setValue(50_000)
        self.maximum.setMaximumWidth(130)
        self.colour = QComboBox()
        self.colour.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.colour.setMinimumContentsLength(12)
        form.addWidget(QLabel('Colour by',self),0,0)
        form.addWidget(self.colour,1,0)
        form.addWidget(QLabel('Maximum cells',self),0,1)
        form.addWidget(self.maximum,1,1)
        form.setColumnStretch(0,1)
        layout.addLayout(form)
        self.advanced = QToolButton()
        self.advanced.setText("Advanced settings")
        self.advanced.setCheckable(True)
        configure_disclosure(self.advanced)
        layout.addWidget(self.advanced,alignment=Qt.AlignmentFlag.AlignLeft)
        panel = QWidget()
        details = QFormLayout(panel)
        self.neighbours = QSpinBox()
        self.neighbours.setRange(2, 200)
        self.neighbours.setValue(15)
        self.distance = QDoubleSpinBox()
        self.distance.setRange(0, 1)
        self.distance.setSingleStep(.05)
        self.distance.setValue(.1)
        self.seed = QSpinBox()
        self.seed.setRange(0, 2**31 - 1)
        for name, widget in (("Neighbours", self.neighbours), ("Minimum distance", self.distance), ("Seed", self.seed)):
            details.addRow(name, widget)
        panel.hide()
        self.advanced.toggled.connect(panel.setVisible)
        layout.addWidget(panel)
        self.calculate = ContentSizedButton("Calculate UMAP view")
        self.calculate.clicked.connect(self.requested)
        actions = QHBoxLayout()
        actions.addWidget(self.calculate)
        self.open_view = ContentSizedButton("Fit view")
        self.open_view.setEnabled(False)
        self.open_view.clicked.connect(self._fit_view)
        actions.addWidget(self.open_view)
        self.export_view = ContentSizedButton("Export figure…")
        self.export_view.setEnabled(False)
        actions.addWidget(self.export_view)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.point_view = None
        for control in (self.maximum, self.neighbours, self.distance, self.seed):
            control.valueChanged.connect(self._invalidate_view)
        self.colour.currentIndexChanged.connect(self._invalidate_view)
        self.hide()

    def set_integrated(self):
        """Use the linked review's shared group and tissue selection."""
        self._integrated=True
        self.introduction.hide();self.open_view.hide();self.export_view.hide()
        self.parameters.setVisible(self.point_view is None)
        self.layout().setContentsMargins(0,0,0,0)

    def configure(self, group):
        """Offer only measurements present in the accepted clustering evidence."""
        self.set_measurements((profile.measurement, profile.feature_id)
                              for profile in group.evidence.clusters[0].marker_profiles)

    def set_measurements(self, measurements):
        """Use the same colour choices for pending review and saved results."""
        self._invalidate_view()
        self.colour.clear()
        self.colour.addItem("Clusters", "cluster_id")
        for label, feature_id in measurements:
            self.colour.addItem(label, feature_id)
        self.show()

    def _invalidate_view(self, *_args):
        self.open_view.setEnabled(False)
        self.export_view.setEnabled(False)
        if self.point_view is not None:
            self.point_view.hide()
        self.changed.emit()

    def set_plots(self, plots):
        """Render the exact coordinate CSV returned for the current display choices."""
        from pathlib import Path
        if not plots:
            return False
        request = (plots[0].display_options or {}).get('umap_request')
        if request is not None and request != self.options():
            return False
        source = next((plot for plot in plots if Path(plot.path).suffix == '.csv'), None)
        if source is None:
            return False
        if self.point_view is None:
            from .umap_points import UmapPointView
            self.point_view = UmapPointView(self._body)
            self.point_view.cell_selected.connect(self.cell_selected)
            self._body.layout().addWidget(self.point_view)
        self.point_view.set_csv(source.path)
        self.open_view.setText('Fit view'); self.open_view.setEnabled(True)
        self.export_view.setEnabled(True)
        if getattr(self,'_integrated',False):
            self.parameters.hide();self.point_view.canvas.setFixedHeight(500);self.point_view.caption.hide()
        return True

    def _fit_view(self):
        if self.point_view is not None:
            self.point_view.fit_view()

    def options(self):
        """Return view settings without adding them to the label-review response."""
        return {"umap_settings": {"max_cells": self.maximum.value(), "neighbors": self.neighbours.value(),
                                  "min_dist": self.distance.value(), "seed": self.seed.value()},
                "attribute": self.colour.currentData() or "cluster_id"}
