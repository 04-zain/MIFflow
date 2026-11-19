"""Display the pending control-derived AF proposal without changing its authority."""
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHeaderView, QTableWidget, QTableWidgetItem,
    QSizePolicy, QToolButton, QToolTip, QVBoxLayout, QWidget,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from .researcher_widgets import configure_disclosure
from .reference_workspace import reference_table, text_label
from .spectral_review import SpectralLegend, show_spectral_hover
from mif_pipeline.preprocessing.unmixing.spectral_plots import (
    spectral_axis, plot_spectral_profile, label_spectral_axis,
)


class TargetAfReviewPanel(QWidget):
    def __init__(self, review, parent=None):
        super().__init__(parent)
        self.setObjectName("target-af-review-panel")
        self._regions = tuple(review["regions"])
        self._names = tuple(review["measurement_names"])
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        kind = ("Shared unstained control" if review["source_kind"] == "reuse_reference_unstained_control"
                else "Unstained control for this image")
        source_panel = QFrame(self)
        source_panel.setObjectName("target-af-source-evidence")
        source_panel.setProperty("workbenchSection", True)
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(16, 14, 16, 14)
        source_layout.setSpacing(6)
        source_kind = text_label(kind, source_panel)
        source_kind.setProperty("fieldCaption", True)
        source_layout.addWidget(source_kind)
        self.source = text_label(review["control_filename"], source_panel)
        self.source.setObjectName("target-af-control-source")
        source_layout.addWidget(self.source)
        layout.addWidget(source_panel)

        spectrum_heading = QWidget(self)
        spectrum_layout = QVBoxLayout(spectrum_heading)
        spectrum_layout.setContentsMargins(0, 4, 0, 0)
        spectrum_layout.setSpacing(4)
        title = "Proposed spectrum" if len(self._regions) == 1 else "Proposed spectra"
        spectrum_layout.addWidget(text_label(title, spectrum_heading, heading=True))
        spectrum_layout.addWidget(text_label(
            "Image background subtracted; each spectrum normalized to sum to one (L1).",
            spectrum_heading, supporting=True))
        layout.addWidget(spectrum_heading)
        figure = Figure(figsize=(9, 3.6), layout="constrained")
        figure.set_facecolor(self.palette().window().color().name())
        self._axes = figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setObjectName("target-af-spectral-canvas")
        self.canvas.setMinimumHeight(350)
        layout.addWidget(self.canvas)
        x, groups, unit = spectral_axis(self._names, wavelengths=review.get("measurement_wavelengths"), wavelength_unit=review.get("wavelength_unit"), optical_settings=review.get("measurement_optical_settings"))
        self._hover_profiles = []
        for region in self._regions:
            label = f"{region['component']} · {region['label']}"
            line = plot_spectral_profile(self._axes, x, region["values"], groups,
                                         ordinal=True, label=label, linewidth=1.8)
            self._hover_profiles.append((line, label, region["values"], region["values"]))
        label_spectral_axis(self._axes, self._names, x, groups, unit, ordinal=True)
        self._axes.set_ylabel("Fraction of total reference signal")
        self._axes.set_ylim(bottom=0)
        self._axes.set_facecolor(self.palette().window().color().name())
        self._axes.spines[["top", "right"]].set_visible(False)
        self._axes.set_axisbelow(True)
        self._axes.grid(axis="y", color="#d5dfe5", linewidth=.7, linestyle=(0, (3, 4)))
        self._axes.spines[["left", "bottom"]].set_color("#a7b8c3")
        self._axes.tick_params(color="#a7b8c3", labelcolor="#344957")
        self.legend = SpectralLegend(self.canvas, self)
        self.legend.set_lines(self._axes.lines)
        layout.addWidget(self.legend)
        toggle = QToolButton(self)
        toggle.setObjectName("target-af-background-disclosure")
        toggle.setText("Background subtraction")
        toggle.setCheckable(True)
        configure_disclosure(toggle)
        layout.addWidget(toggle)
        details = QFrame(self)
        details.setProperty("workbenchSection", True)
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(16, 14, 16, 14)
        details_layout.setSpacing(10)
        units = {"full": "Fully corrected intensity",
                 "exposure_only": "Exposure-normalized intensity",
                 "as_stored": "Stored calibrated intensity"}[review["correction_mode"]]
        explanation = text_label(
            f"Region mean minus reviewed image background, in {units.lower()}. "
            "Negative values are set to zero before normalization.", details, supporting=True)
        details_layout.addWidget(explanation)
        self.region_selector = QComboBox(details)
        self.region_selector.setObjectName("target-af-background-region")
        for region in self._regions:
            self.region_selector.addItem(f"{region['component']} · {region['label']}", region["roi_id"])
        self.region_selector.setVisible(len(self._regions) > 1)
        details_layout.addWidget(self.region_selector)
        self.band_table = QTableWidget(len(self._names), 3, details)
        self.band_table.setObjectName("target-af-background-values")
        self.band_table.setHorizontalHeaderLabels(("Band", "Signed difference", "After clipping"))
        reference_table(self.band_table)
        self.band_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            self.band_table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.band_table.setMinimumHeight(220)
        details_layout.addWidget(self.band_table)
        details.hide()
        toggle.toggled.connect(details.setVisible)
        layout.addWidget(details)
        self.region_selector.currentIndexChanged.connect(self._show_background)
        self._show_background(0)
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.canvas.mpl_connect("figure_leave_event", lambda _event: QToolTip.hideText())

    def _show_background(self, index):
        region = self._regions[index]
        for row, (name, signed, corrected) in enumerate(zip(
                self._names, region["signed_values"], region["corrected_values"], strict=True)):
            for column, value in enumerate((name, f"{signed:.8g}", f"{corrected:.8g}")):
                item = QTableWidgetItem(value)
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    item.setToolTip(repr((signed, corrected)[column - 1]))
                self.band_table.setItem(row, column, item)

    def _hover(self, event):
        show_spectral_hover(self.canvas, self._axes, event, self._names,
                            np.arange(1, len(self._names) + 1), self._hover_profiles)
