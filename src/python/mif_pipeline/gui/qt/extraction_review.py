"""Review regional extraction decisions beside their retained subtraction evidence."""
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox,QGridLayout,QHBoxLayout,QHeaderView,QSizePolicy,QTableWidget,QTableWidgetItem,QToolButton,QToolTip,QVBoxLayout,QWidget
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from .researcher_widgets import configure_disclosure
from .reference_workspace import text_label,reference_table,decision_combo
from .spectral_review import SpectralLegend,show_spectral_hover
from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis,plot_spectral_profile,label_spectral_axis

class ExtractionReviewPanel(QWidget):
    def __init__(self,review,parent=None):
        super().__init__(parent)
        self.setObjectName('extraction-review-panel'); self._regions=tuple(review['regions'])
        self._component=review.get('component','Fluorophore extraction')
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(10)
        header=QHBoxLayout(); header.setSpacing(10)
        self.plot_title=text_label(self._component,self,heading=True)
        self.plot_title.setWordWrap(False)
        self.plot_title.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Maximum)
        header.addWidget(self.plot_title); header.addSpacing(10)
        region_label=text_label('Region',self)
        region_label.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Maximum)
        header.addWidget(region_label)
        self.region_selector=QComboBox(self); self.region_selector.setObjectName('extraction-reference-region')
        self.region_selector.setAccessibleName('Reference region shown in plot')
        for index,region in enumerate(self._regions):
            self.region_selector.addItem(region.get('label',f'Reference region {index+1}'),region['region'])
        self.region_selector.setMaximumWidth(260)
        header.addWidget(self.region_selector)
        header.addSpacing(10)
        view_label=text_label('View',self)
        view_label.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Maximum)
        header.addWidget(view_label)
        self.view_selector=QComboBox(self); self.view_selector.setObjectName('extraction-plot-view')
        self.view_selector.setAccessibleName('Extraction plot view')
        if all('positive_values' in r for r in self._regions): self.view_selector.addItem('Autofluorescence subtraction','subtraction')
        self.view_selector.addItem('Extracted signal','residual')
        header.addWidget(self.view_selector); header.addStretch(1); layout.addLayout(header)
        figure=Figure(figsize=(9,2.6),layout='constrained'); figure.set_facecolor(self.palette().window().color().name())
        self.canvas=FigureCanvasQTAgg(figure); self.canvas.setObjectName('extraction-residual-canvas')
        self.canvas.setMinimumHeight(260); self.canvas.setMinimumWidth(0); self._axes=figure.add_subplot(111)
        layout.addWidget(self.canvas); self.legend=SpectralLegend(self.canvas,self); layout.addWidget(self.legend)
        self.metrics=text_label('',self,supporting=True); self.metrics.setObjectName('extraction-clipping-metrics'); layout.addWidget(self.metrics)
        self.table=QTableWidget(len(self._regions),3,self); self.table.setObjectName('extraction-region-decisions')
        self.table.setHorizontalHeaderLabels(('Reference region','Bands below zero','Use in reference')); reference_table(self.table)
        self.table.setToolTip('Select a region to inspect its extraction above.')
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.table.setFixedHeight(min(390,50+44*len(self._regions))); self._choices=[]
        for row,region in enumerate(self._regions):
            label=region.get('label',f'Reference region {row+1}')
            item=QTableWidgetItem(label); item.setToolTip(region['region']); self.table.setItem(row,0,item)
            count=QTableWidgetItem(f"{region['clipped_count']} / {len(region['measurement_names'])}")
            count.setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row,1,count)
            choice=decision_combo(self.table,(('Include','include'),('Exclude','exclude')))
            if region['disposition']=='failed':choice.setCurrentIndex(1); choice.setEnabled(False)
            self._choices.append(choice); self.table.setCellWidget(row,2,choice)
            choice.currentIndexChanged.connect(lambda _,r=row:self.table.setCurrentCell(r,0))
        layout.addWidget(self.table)
        toggle=QToolButton(self); toggle.setText('Correction details and band values'); toggle.setCheckable(True)
        configure_disclosure(toggle)
        self.overview_actions=QHBoxLayout(); self.overview_actions.setContentsMargins(0,0,0,0)
        self.overview_actions.addWidget(toggle); self.overview_actions.addStretch(1)
        layout.addLayout(self.overview_actions)
        self.details = QWidget(self)
        details = QVBoxLayout(self.details)
        details.setContentsMargins(12, 0, 0, 6)
        details.setSpacing(8)
        self.fit_values = {}
        metadata = QGridLayout()
        metadata.setColumnStretch(1, 1)
        metadata.setHorizontalSpacing(16)
        metadata.setVerticalSpacing(5)
        for row, title in enumerate(("Stained image", "AF control", "Signal band", "Bands used to estimate AF", "Control multiplier")):
            caption = text_label(title, self.details, supporting=True)
            value = text_label("", self.details)
            metadata.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
            metadata.addWidget(value, row, 1)
            self.fit_values[title] = (caption, value)
        details.addLayout(metadata)
        self.fit_help = text_label("The multiplier matches the control to the stained region in the bands selected for AF estimation. The scaled control is subtracted from every band.", self.details, supporting=True)
        details.addWidget(self.fit_help)
        self.clipped_magnitude = text_label("", self.details, supporting=True)
        details.addWidget(self.clipped_magnitude)
        layout.addWidget(self.details)
        self.details.hide()
        toggle.toggled.connect(self.details.setVisible)
        self.band_table=QTableWidget(0,4,self); self.band_table.setObjectName('extraction-band-values')
        self.band_table.setHorizontalHeaderLabels(('Band','Signed residual','After clipping','Clipped amount'))
        reference_table(self.band_table); self.band_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for column in (1,2,3):
            self.band_table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
        self.band_table.setMinimumHeight(220); self.band_table.hide(); toggle.toggled.connect(self.band_table.setVisible)
        layout.addWidget(self.band_table)
        self.table.currentCellChanged.connect(lambda row,*_:self._show_region(max(0,row)))
        self.region_selector.currentIndexChanged.connect(lambda row:self.table.setCurrentCell(row,0))
        self.view_selector.currentIndexChanged.connect(lambda _:self._show_region(max(0,self.table.currentRow())))
        self.canvas.mpl_connect('motion_notify_event',self._hover_profile)
        self.canvas.mpl_connect('figure_leave_event',lambda _:QToolTip.hideText())
        self.table.setCurrentCell(0,0)

    def values(self):
        return {name:tuple(r['region'] for r,c in zip(self._regions,self._choices,strict=True) if c.currentData()==decision)
                for name,decision in (('included_roi_ids','include'),('excluded_roi_ids','exclude'))}

    def set_values(self,values):
        included=set(values.get('included_roi_ids') or ()); excluded=set(values.get('excluded_roi_ids') or ())
        if not included and not excluded:return
        for region,choice in zip(self._regions,self._choices,strict=True):
            choice.blockSignals(True)
            choice.setCurrentIndex(0 if region['region'] in included else 1)
            choice.blockSignals(False)

    def _show_region(self, index):
        region = self._regions[index]
        self.region_selector.blockSignals(True)
        self.region_selector.setCurrentIndex(index)
        self.region_selector.blockSignals(False)
        factor = region.get("af_subtraction_factor")
        values = {"Stained image": region.get("filename"), "AF control": region.get("control_filename")}
        if region.get("measurement_ids"):
            labels = dict(zip(region["measurement_ids"], region["measurement_names"], strict=True))
            values["Signal band"] = labels[region["signal_measurement_id"]]
            values["Bands used to estimate AF"] = ", ".join(labels[value] for value in region["dark_measurement_ids"])
        if factor is not None:
            values["Control multiplier"] = f"{factor:.5g}"
        for title, (caption, value) in self.fit_values.items():
            content = values.get(title)
            caption.setVisible(bool(content))
            value.setVisible(bool(content))
            value.setText(content or "")
        self.fit_help.setVisible(factor is not None)
        mode_labels = {
            "full": "Fully corrected intensity",
            "exposure_only": "Exposure-normalized intensity",
            "as_stored": "Stored calibrated intensity",
        }
        units = mode_labels[region["correction_mode"]]
        signed = region["signed_residual"]
        clipped = region["clipped_residual"]
        names = region["measurement_names"]
        self._axes.clear()
        self.legend.set_lines(())
        self._hover_profiles = []
        self._hover_names = tuple(names)
        self._hover_x = np.arange(len(names))
        self.band_table.setRowCount(0)
        if signed is None:
            self.metrics.setText("The correction is unavailable because extraction stopped before autofluorescence subtraction was completed. This region cannot be used for the reference spectrum.")
            self.clipped_magnitude.setText("The L2 correction cannot be calculated without the spectrum after autofluorescence subtraction.")
            self._axes.text(.5, .5, "No post-AF residual available", ha="center", va="center",
                            transform=self._axes.transAxes)
        else:
            def number(value):
                return "unavailable" if value is None else f"{value:.5g}"

            ratio = region["clipped_to_retained_l2_ratio"]
            ratio_text = (
                "Relative correction (L2): unavailable."
                if ratio is None else
                f"Relative correction (L2): {100 * ratio:.3g}%."
            )
            if ratio is None and not any(value > 0 for value in signed):
                ratio_text = "Relative correction (L2): undefined because no positive residual remains."
            self.metrics.setText(
                f"{region['clipped_count']} of {len(names)} bands were below zero after AF subtraction and were set to zero."
                + "  " + ratio_text
                + ("\nThis region cannot be used for the reference spectrum."
                   if region["disposition"] == "failed" else "")
            )
            self.clipped_magnitude.setText(
                f"Clipping removed an L2 magnitude of {number(region['clipped_l2'])} in {units.lower()} units. "
                "Relative correction expresses this magnitude as a percentage of the retained positive signal’s L2 magnitude."
            )
            x, groups, unit = spectral_axis(names, wavelengths=region.get("measurement_wavelengths"), wavelength_unit=region.get("wavelength_unit"), optical_settings=region.get("measurement_optical_settings"))
            if self.view_selector.currentData() == "subtraction" and region.get("af_subtraction_factor") is not None:
                factor = region["af_subtraction_factor"]
                scaled = tuple(value * factor for value in region["control_values"])
                profiles = ((region["positive_values"], "Stained region", "#157fa3", "-"),
                            (scaled, "Estimated AF", "#a16a37", "-"),
                            (signed, "Remaining signal (signed)", "#287a80", "--"))
            else:
                profiles = ((signed, "After AF subtraction (signed)", "#b75b37", "-"),
                            (clipped, "After clipping to zero", "#287a80", "--"))
            lines = []
            for values, label, color, style in profiles:
                line = plot_spectral_profile(self._axes, x, values, groups, ordinal=True,
                    color=color, linestyle=style, linewidth=1.7, label=label)
                lines.append(line)
                self._hover_profiles.append((line, label, values, values))
            self._axes.axhline(0, color="#637381", linewidth=.8, zorder=0)
            band_positions = np.arange(1, len(names) + 1)
            self._axes.fill_between(band_positions, signed, 0, where=np.asarray(signed) < 0,
                                    color="#b75b37", alpha=.15, interpolate=True)
            label_spectral_axis(self._axes, names, x, groups, unit, ordinal=True)
            self.legend.set_lines(lines)
            self._hover_x = band_positions
            subtraction = self.view_selector.currentData() == "subtraction" and factor is not None
            headers = (("Band", "Stained region", "Estimated AF", "Remaining signal") if subtraction else
                       ("Band", "Remaining signal", "After clipping", "Amount clipped"))
            self.band_table.setHorizontalHeaderLabels(headers)
            self.band_table.setRowCount(len(names))
            for row, (name, raw, retained) in enumerate(zip(names, signed, clipped, strict=True)):
                numbers = (region["positive_values"][row], scaled[row], raw) if subtraction else (raw, retained, max(0.0, -raw))
                for column, value in enumerate((name, *(f"{v:.8g}" for v in numbers))):
                    item=QTableWidgetItem(value)
                    if column:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
                    self.band_table.setItem(row, column, item)
        self._axes.set_ylabel(units.rsplit(' ', 1)[0] + '\nintensity')
        self._axes.set_facecolor(self.palette().window().color().name())
        self._axes.spines[["top", "right"]].set_visible(False)
        self._axes.grid(axis="y", color="#d5dfe5", linewidth=.7, linestyle=(0, (3, 4)))
        self.canvas.draw_idle()

    def _hover_profile(self, event):
        show_spectral_hover(self.canvas, self._axes, event, self._hover_names,
            self._hover_x, self._hover_profiles)
