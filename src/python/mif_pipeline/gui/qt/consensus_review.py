"""Compare cached regional reference shapes before accepting their consensus."""
import math
import numpy as np
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView, QToolTip,
    QSizePolicy,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from mif_pipeline.preprocessing.unmixing.purification import compare_consensus_spectra
from .spectral_review import SpectralPlotToolbar, SpectralLegend, show_spectral_hover
from .reference_workspace import text_label, reference_table, decision_combo
from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis, plot_spectral_profile, label_spectral_axis


class ConsensusReviewPanel(QWidget):
    def __init__(self, review, parent=None):
        super().__init__(parent)
        self.setObjectName("consensus-review-panel")
        self._regions = tuple(review["regions"])
        self._region_labels = {region["roi_id"]: region.get("label", f"Reference region {index + 1}")
                               for index, region in enumerate(self._regions)}
        self._spectra = tuple(tuple(r["values"]) for r in self._regions)
        self._names = tuple(review["measurement_names"])
        from mif_pipeline._defaults import default_section
        self._threshold = default_section("unmixing.reference_consensus")["outlier_review_angle_degrees"]
        self._curve_visibility = {}
        self._x, self._groups, self._unit = spectral_axis(self._names, wavelengths=review.get("measurement_wavelengths"), wavelength_unit=review.get("wavelength_unit"), optical_settings=review.get("measurement_optical_settings"))
        self._included = tuple(bool(r["included"]) for r in self._regions)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.overview_actions = header
        header.addWidget(text_label(review["component"], self, heading=True), 1)
        layout.addLayout(header)
        figure = Figure(figsize=(9, 3.2), layout="constrained")
        figure.set_facecolor(self.palette().window().color().name())
        self._axes = figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setObjectName("consensus-spectral-canvas")
        self.canvas.setMinimumHeight(320)
        self.toolbar = SpectralPlotToolbar(self.canvas, self, coordinates=False)
        self.toolbar.setObjectName("consensus-plot-controls")
        self.toolbar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.toolbar.setStyleSheet(
            f"QToolBar {{ background: {self.palette().window().color().name()}; "
            "border: none; padding: 0; spacing: 6px; }")
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        reset = self.toolbar._actions["home"]
        reset.setIcon(QIcon())
        self.toolbar.widgetForAction(reset).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        header.addWidget(self.toolbar)
        self.canvas.setMinimumWidth(0)
        layout.addWidget(self.canvas)
        self.legend = SpectralLegend(self.canvas, self,
            changed=lambda name, visible: self._curve_visibility.__setitem__(name, visible))
        layout.addWidget(self.legend)
        layout.addWidget(text_label("Regional agreement", self, heading=True))
        self.table = QTableWidget(len(self._regions), 4, self)
        self.table.setObjectName("consensus-region-decisions")
        self.table.setHorizontalHeaderLabels(("Reference region", "Angle (°)", "Review", "Decision"))
        self.table.setToolTip("Select a region to emphasize its curve. Excluding a region updates the combined reference.")
        reference_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setFixedHeight(min(320, 50 + 44 * len(self._regions)))
        self._choices = []
        for row, region in enumerate(self._regions):
            item = QTableWidgetItem(self._region_labels[region["roi_id"]])
            item.setData(Qt.ItemDataRole.UserRole, region["roi_id"])
            source = "\n".join(filter(None, (self._region_labels[region["roi_id"]], region.get("filename"))))
            item.setToolTip(source)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, source)
            self.table.setItem(row, 0, item)
            choice = decision_combo(self.table, (("Include", "include"), ("Exclude", "exclude"),
                                                  ("Retain difference", "retain")))
            choice.setCurrentIndex(0 if region["included"] else 1)
            self.table.setCellWidget(row, 3, choice)
            self._choices.append(choice)
            choice.currentIndexChanged.connect(self._selection_changed)
        layout.addWidget(self.table)
        self.status = text_label("", self, supporting=True)
        layout.addWidget(self.status)
        self._footer = QWidget(self)
        footer = QVBoxLayout(self._footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(0)
        layout.addWidget(self._footer)
        self.table.currentCellChanged.connect(self._focus_region)
        self.canvas.mpl_connect("motion_notify_event", self._hover_profile)
        self.canvas.mpl_connect("figure_leave_event", lambda _event: QToolTip.hideText())
        self._refresh(reset_view=True)
        self.table.setCurrentCell(0, 0)

    def add_review_actions(self, settings, navigation):
        """Keep the final decision directly below regional agreement."""
        self._footer.layout().addLayout(navigation)

    def set_threshold(self, text):
        try:
            value = float(text)
            if not math.isfinite(value) or not 0 <= value <= 90:
                value = None
        except (TypeError, ValueError):
            value = None
        if value != self._threshold:
            self._threshold = value
            self._clear_retention()
        self._refresh()

    def _clear_retention(self):
        for choice in self._choices:
            if choice.currentData() == "retain":
                choice.blockSignals(True)
                choice.setCurrentIndex(0)
                choice.blockSignals(False)

    def _selection_changed(self, *_args):
        included = tuple(c.currentData() != "exclude" for c in self._choices)
        changed = included != self._included
        if changed:
            self._clear_retention()
            self._included = included
        self._refresh(reset_view=changed)

    def _refresh(self, *, reset_view=False):
        """Redraw cached spectra and decisions, preserving the inspected view when possible."""
        limits = None if reset_view or not self._axes.lines else (
            self._axes.get_xlim(), self._axes.get_ylim())
        self._axes.clear()
        self.legend.set_lines(())
        self._hover_profiles = []
        if not any(self._included):
            self.status.setText("Include at least one region, or reselect the reference ROIs.")
            for row in range(len(self._regions)):
                angle_item = QTableWidgetItem("—")
                angle_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, 1, angle_item)
                self.table.setItem(row, 2, QTableWidgetItem("Excluded"))
                
            self.toolbar.update()
            self.canvas.draw_idle()
            return
        self._consensus, self._comparisons = compare_consensus_spectra(self._spectra, self._included)
        unresolved = []
        for row, (region, spectrum, keep, comparison, choice) in enumerate(zip(
            self._regions, self._spectra, self._included, self._comparisons, self._choices, strict=True)):
            angle = comparison[0]
            flagged = keep and self._threshold is not None and angle > self._threshold
            text = ("Excluded" if not keep else "Enter threshold" if self._threshold is None else
                    "Difference retained" if flagged and choice.currentData() == "retain" else
                    "Review difference" if flagged else "Within threshold")
            if flagged and choice.currentData() != "retain":
                unresolved.append(self._region_labels[region["roi_id"]])
            angle_item = QTableWidgetItem(f"{angle:.3g}")
            angle_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 1, angle_item)
            status = QTableWidgetItem("—" if keep and self._threshold is None else text)
            status.setToolTip(text)
            if flagged:
                status.setForeground(Qt.GlobalColor.darkRed)
            self.table.setItem(row, 2, status)
            line = plot_spectral_profile(self._axes, self._x, spectrum, self._groups, ordinal=True, label=self._region_labels[region["roi_id"]],
                                  linestyle="-" if keep else "--", alpha=.8 if keep else .35, linewidth=1.4)
            self._hover_profiles.append((line, self._region_labels[region["roi_id"]], spectrum, spectrum))
        combined_line = plot_spectral_profile(self._axes, self._x, self._consensus, self._groups, ordinal=True,
                              color="black", linewidth=1.8, linestyle="--", alpha=.8, label="Combined reference")
        self._hover_profiles.append((combined_line, "Combined reference", self._consensus, self._consensus))
        label_spectral_axis(self._axes, self._names, self._x, self._groups, self._unit, ordinal=True)
        self._axes.set_ylabel("Unit-L2 reference value")
        self._axes.set_facecolor(self.palette().window().color().name())
        self._axes.spines[["top", "right"]].set_visible(False)
        self._axes.set_axisbelow(True)
        self._axes.grid(axis="y", color="#d5dfe5", linewidth=.7, linestyle=(0, (3, 4)))
        self._axes.set_ylim(bottom=0)
        if limits is not None:
            self._axes.set_xlim(limits[0])
            self._axes.set_ylim(limits[1])
        else:
            self.toolbar.update()
            self.toolbar.push_current()
        for line in self._axes.lines:
            line.set_visible(self._curve_visibility.get(line.get_label(), True))
        self.legend.set_lines(self._axes.lines)
        self.status.setText(
            "Enter the spectral-difference threshold to identify regions requiring review."
            if self._threshold is None else
            "Explicitly retain or exclude the flagged regions: " + ", ".join(unresolved)
            if unresolved else ("Flagged spectra are explicitly retained." if any(
                keep and comparison[0] > self._threshold for keep, comparison in zip(self._included, self._comparisons, strict=True))
                else f"No included spectrum exceeds {self._threshold:g}°."))
        self._focus_region(self.table.currentRow())
        self.canvas.draw_idle()

    def _focus_region(self, row, *_):
        for index, profile in enumerate(self._hover_profiles[:-1]):
            profile[0].set_linewidth(2.4 if index == row else 1.2)
        self.canvas.draw_idle()

    def _hover_profile(self, event):
        show_spectral_hover(self.canvas, self._axes, event, self._names,
            np.arange(1, len(self._names) + 1), self._hover_profiles, toolbar=self.toolbar)

    def value(self, *, validate=True):
        if validate:
            if not any(self._included):
                raise ValueError("Include at least one region, or reselect the reference ROIs")
            if self._threshold is None:
                raise ValueError("Enter a spectral-difference threshold from 0 to 90 degrees")
            unresolved = [self._region_labels[r["roi_id"]] for r, keep, comparison, choice in zip(
                self._regions, self._included, self._comparisons, self._choices, strict=True)
                if keep and comparison[0] > self._threshold and choice.currentData() != "retain"]
            if unresolved:
                raise ValueError("Explicitly retain or exclude the differing regions: " + ", ".join(unresolved))
        return {r["roi_id"]: c.currentData() for r, c in zip(self._regions, self._choices, strict=True)}

    def set_value(self, values):
        if not isinstance(values, dict) or set(values) != {r["roi_id"] for r in self._regions}:
            return
        for region, choice in zip(self._regions, self._choices, strict=True):
            index = choice.findData(values[region["roi_id"]])
            if index >= 0:
                choice.blockSignals(True)
                choice.setCurrentIndex(index)
                choice.blockSignals(False)
        self._included = tuple(c.currentData() != "exclude" for c in self._choices)
        self._refresh(reset_view=True)
