"""Inspect authoritative pooled AF spectra and their retained regional evidence."""
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QHeaderView, QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton, QToolTip, QVBoxLayout, QWidget
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from .reference_workspace import INTENSITY_UNITS, reference_table, text_label
from .researcher_widgets import configure_disclosure
from .spectral_review import SpectralLegend, show_spectral_hover
from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis, plot_spectral_profile, label_spectral_axis
from .af_control_composition import ControlSourceComposition


class AfControlSpectrumPanel(ControlSourceComposition, QWidget):
    """All displayed spectral arrays come from the service projection, unmodified."""
    revision_requested = Signal(str)
    changed = Signal()

    def __init__(self, review, parent=None, *, target=False, source=False, editable=False):
        super().__init__(parent)
        self.setObjectName('target-af-review-panel' if target else 'af-control-set-review-panel')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        if source:
            self._init_source_composition(review, editable=editable)
            return
        self._review, self._target, self._source_review = review, target, source
        self._matched_negative = review.get('review_kind') == 'matched_negative'
        self._combined_label = 'Matched negative control' if self._matched_negative else 'Combined AF spectrum'
        self._names = tuple(review['measurement_names'])
        self.can_continue = self._matched_negative or all(row.get('status', 'pass') == 'pass'
                                for row in review.get('sets', review.get('components', ())))
        if target:
            regions = {row['roi_id']: row for row in review['regions']}
            self._sets = tuple(dict(component, regions=[regions[roi_id] for roi_id in component['roi_ids']],
                                    label=component['component']) for component in review['components'])
        else:
            # Several positives may reuse exactly the same control evidence.
            sets = []
            for control in review['sets']:
                same = next((row for row in sets if tuple(row['roi_ids']) == tuple(control['roi_ids'])
                             and row['values'] == control['values']
                             and tuple(row['signed_values']) == tuple(control['signed_values'])), None)
                if same is None:
                    sets.append(dict(control, label=(f'Negative control {len(sets) + 1}' if self._matched_negative
                        else f'AF control set {len(sets) + 1}'),
                                     positive_labels=[control['positive_label']]))
                else: same['positive_labels'].append(control['positive_label'])
            self._sets = tuple(sets)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)
        header = QHBoxLayout(); header.setSpacing(10)
        heading = text_label('Autofluorescence spectrum' if target else
            'Negative control spectrum' if self._matched_negative else 'Control spectrum', self, heading=True)
        heading.setWordWrap(False)
        heading.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum); header.addWidget(heading)
        self.set_selector = QComboBox(self); self.set_selector.setObjectName('af-spectrum-control-set')
        self.set_selector.setAccessibleName('AF spectrum shown in plot')
        for index, control in enumerate(self._sets):
            self.set_selector.addItem(control['label'], control['set_id'] if source else index)
        self.set_selector.setVisible(len(self._sets) > 1); header.addWidget(self.set_selector); header.addStretch(1)
        layout.addLayout(header)
        self.status = text_label('', self); self.status.setProperty('validationMessage', True)
        layout.addWidget(self.status)
        self.source = text_label('', self, supporting=True); layout.addWidget(self.source)
        figure = Figure(figsize=(9, 3.1), layout='constrained')
        figure.set_facecolor(self.palette().window().color().name())
        self._axes = figure.add_subplot(111); self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setMinimumWidth(0); self.canvas.setFixedHeight(280); layout.addWidget(self.canvas)
        self.legend = SpectralLegend(self.canvas, self); layout.addWidget(self.legend)
        normalization = 'sum to one (L1)' if target else 'unit L2 length'
        self._method = ('Regional means are background-corrected and averaged equally. '
            f'Negative pooled values are set to zero, then the result is normalized to {normalization}.')
        if self._matched_negative:
            self._method = ('The matched negative region is background-corrected. Negative values are set to zero, '
                'then the result is normalized to unit L2 length.')
        regional_heading = text_label('Regional evidence', self, heading=True)
        layout.addWidget(regional_heading)
        self.table = QTableWidget(0, 6, self); self.table.setObjectName('af-control-regional-evidence')
        self.table.setHorizontalHeaderLabels(('Region', 'Weight', 'Pixels', 'Signal (L2)', 'Below zero', 'Angle (°)'))
        reference_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
            self.table.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setToolTip('Select a region to emphasize its exact shape. Signal is the L2 length after background correction and clipping. Angle compares its shape with the combined spectrum; no acceptance threshold is applied.')
        if self._matched_negative:
            self.table.setColumnHidden(1, True)
            self.table.setColumnHidden(5, True)
            self.table.setToolTip('Measured spectrum of the matched negative-control region.')
        elif source:
            self.table.setColumnHidden(4, True)
            self.table.setColumnHidden(5, True)
            self.table.setToolTip('Retained background-corrected regional spectra and their equal contribution to this AF set.')
        layout.addWidget(self.table)
        self.details_toggle = QToolButton(self); self.details_toggle.setText('Band values and calculation details')
        self.details_toggle.setObjectName('target-af-background-disclosure' if target else 'af-control-band-values')
        self.details_toggle.setCheckable(True); configure_disclosure(self.details_toggle); layout.addWidget(self.details_toggle)
        details = QWidget(self); detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 0, 0, 0); detail_layout.setSpacing(8)
        self.method = text_label(self._method, self, supporting=True); detail_layout.addWidget(self.method)
        self.details_source = text_label('', self, supporting=True); detail_layout.addWidget(self.details_source)
        self.region_selector = QComboBox(self); self.region_selector.setAccessibleName('Spectrum whose exact band values are shown')
        detail_layout.addWidget(self.region_selector)
        self.band_table = QTableWidget(len(self._names), 4, self)
        self.band_table.setHorizontalHeaderLabels(('Acquired band', 'Signed difference', 'After clipping', 'Normalized'))
        reference_table(self.band_table); self.band_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self.band_table.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.band_table.setFixedHeight(min(270, 48 + 44 * len(self._names))); detail_layout.addWidget(self.band_table)
        layout.addWidget(details); details.hide(); self.details_toggle.toggled.connect(details.setVisible)
        self.set_selector.currentIndexChanged.connect(self._show_set)
        self.table.currentCellChanged.connect(self._select_region)
        self.region_selector.currentIndexChanged.connect(self._show_values)
        self.canvas.mpl_connect('motion_notify_event', self._hover)
        self.canvas.mpl_connect('figure_leave_event', lambda _: QToolTip.hideText())
        self._show_set(0)

    def set_revision_option(self, option):
        if not self._source_review:
            return
        if option not in (None, 'retry_shared_control_preparation', 'revise_shared_control'):
            raise ValueError('Unknown AF preparation revision')
        self._revision_option = option
        self.edit_regions_button.setVisible(option is not None)

    def _show_set(self, index):
        self._current = self._sets[max(0, index)]
        control = self._current
        diagnostic = control.get('diagnostic')
        self.status.setText((diagnostic or 'Another control set has insufficient AF signal').rstrip('. ') +
            '. Change the AF regions or image background before continuing.' if not self.can_continue else '')
        if self._matched_negative and control['values'] is None:
            self.status.setText('No signal remains after background subtraction. '
                'Review the control region and background selection.')
        self.status.setVisible(bool(self.status.text()))
        if self._target:
            filenames = tuple(dict.fromkeys(row.get('filename', row.get('control_filename', ''))
                for row in control['regions']))
            self.source.setText(' · '.join(name for name in filenames if name) or self._review.get('control_filename', ''))
        elif self._source_review:
            self.source.setText(control.get('control_filename', self._review.get('control_filename', '')))
        elif self._matched_negative:
            self.source.setText(control['control_filename'] + '\nMatched to ' + ', '.join(control['positive_labels']))
        else:
            self.source.setText('Used for ' + ', '.join(control['positive_labels']))
        self.table.blockSignals(True); self.table.setRowCount(len(control['regions']))
        self.table.clearSelection(); self.table.setCurrentCell(-1, -1)
        for row, region in enumerate(control['regions']):
            def number(key, suffix=''):
                value = region.get(key)
                return '—' if value is None else f'{value:.5g}{suffix}'
            count = region.get('clipped_count')
            label = region['label'].replace('Autofluorescence region', 'AF region')
            if self._target and len(self._review.get('sources', ())) > 1:
                label += '\n' + region.get('filename', region.get('control_filename', ''))
            if region.get('status') == 'failed': label += '\nInsufficient control signal'
            values = (label, f"{control['roi_weights'][row]:.4g}",
                      str(region.get('accepted_pixel_count', '—')), number('corrected_l2'),
                      '—' if count is None else f'{count} / {len(self._names)}', number('angle_to_combined_degrees'))
            for col, value in enumerate(values):
                item = QTableWidgetItem(value); item.setToolTip(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
            self.table.setRowHeight(row, 60 if '\n' in label else 44)
        self.table.setFixedHeight(min(270, 48 + sum(self.table.rowHeight(row) for row in range(self.table.rowCount()))))
        self.table.blockSignals(False)
        self.region_selector.blockSignals(True); self.region_selector.clear(); self.region_selector.addItem(self._combined_label, -1)
        for row, region in enumerate(control['regions']): self.region_selector.addItem(region['label'], row)
        self.region_selector.blockSignals(False); self.region_selector.setCurrentIndex(0); self._show_values()
        if self._source_review:
            self.set_heading.setText(control['label'] if len(self._sets) == 1 else 'AF control set')
            count = len(control['regions'])
            self.set_summary.setText(f'{count} region' + ('' if count == 1 else 's') + ' · equal contribution')
            self.combined_button.setText('Show combined spectrum' if control['values'] is not None else 'Show all regions')
            self.combined_button.setEnabled(False)
        self._plot_spectra()

    def _plot_spectra(self):
        """Plot only retained arrays, preserving the service's calculation order."""
        control = self._current
        self._axes.clear(); self._hover_profiles = []
        x, groups, unit = spectral_axis(self._names, wavelengths=self._review.get('measurement_wavelengths'),
            wavelength_unit=self._review.get('wavelength_unit'), optical_settings=self._review.get('measurement_optical_settings'))
        signal_view = self._source_review and self.plot_view.currentData() == 'signal'
        self._region_lines = [None] * len(control['regions'])
        lines = []

        def add(values, label, **style):
            if values is None:
                return None
            line = plot_spectral_profile(self._axes, x, values, groups, ordinal=True, label=label, **style)
            self._hover_profiles.append((line, label, values, values))
            lines.append(line)
            return line

        if signal_view:
            row = self.table.currentRow()
            selected = control['regions'][row] if row >= 0 else control
            label = selected['label'].replace('Autofluorescence region', 'AF region') if row >= 0 else self._combined_label
            add(selected['signed_values'], 'After subtraction (signed)', color='#5b7c8c', linewidth=2.0, linestyle='--')
            add(selected['corrected_values'], 'After clipping', color='#006b70', linewidth=2.4)
            units = INTENSITY_UNITS.get(self._review.get('correction_mode', control.get('correction_mode')), 'Corrected intensity')
            self._axes.set_ylabel(units)
            self.plot_title.setText(label)
        else:
            add(control['values'], self._combined_label, color='#164f5c', linewidth=2.6, zorder=5)
            for index, region in enumerate(control['regions']):
                if self._matched_negative:
                    continue
                label = region['label'].replace('Autofluorescence region', 'AF region')
                self._region_lines[index] = add(region['values'], label, linewidth=1.3, alpha=.6, linestyle='--')
            self._axes.set_ylabel('Fraction of total\nreference signal' if self._target else
                'Control spectrum (L2-normalized)' if self._matched_negative else 'AF spectrum (L2-normalized)')
            self._axes.set_ylim(bottom=0)
            if self._source_review:
                self.plot_title.setText('AF spectrum')
        self._label_axis()
        self._axes.set_facecolor(self.palette().window().color().name())
        self._axes.spines[['top', 'right']].set_visible(False)
        self._axes.spines[['bottom', 'left']].set_color('#bdceca')
        self._axes.grid(axis='y', color='#d5dfe5', linewidth=.7, linestyle=(0, (3, 4)))
        self.legend.set_lines(lines)
        if not self._hover_profiles:
            self._axes.text(.5, .5, 'No usable AF spectrum after background correction',
                ha='center', va='center', transform=self._axes.transAxes, color='#536878')
        if not signal_view:
            self._emphasize_region(self.table.currentRow())
        self.canvas.draw_idle()

    def _label_axis(self):
        x, groups, unit = spectral_axis(self._names, wavelengths=self._review.get('measurement_wavelengths'),
            wavelength_unit=self._review.get('wavelength_unit'), optical_settings=self._review.get('measurement_optical_settings'))
        label_spectral_axis(self._axes, self._names, x, groups, unit if groups else None, ordinal=True)
        if unit and not groups:
            self._axes.set_xlabel((f'Acquired band\nDetection wavelengths: {min(x):g}–{max(x):g} {unit}'
                if self._source_review and self._source_compact else
                f'Acquired band (detection wavelengths: {min(x):g}–{max(x):g} {unit})'))

    def _emphasize_region(self, row):
        for index, line in enumerate(self._region_lines):
            if line is None:
                continue
            line.set_linewidth(2.4 if index == row else 1.3)
            line.set_alpha(1 if index == row else .3 if row >= 0 else .6)

    def _select_region(self, row, *_):
        self.region_selector.setCurrentIndex(row + 1 if row >= 0 else 0)
        if self._source_review:
            self.combined_button.setEnabled(row >= 0)
        if self._source_review and self.plot_view.currentData() == 'signal':
            self._plot_spectra()
        else:
            self._emphasize_region(row)
            self.canvas.draw_idle()

    def _show_values(self, *_):
        row = self.region_selector.currentData()
        region = self._current if row in (None, -1) else self._current['regions'][row]
        units = INTENSITY_UNITS.get(self._review.get('correction_mode', self._current.get('correction_mode')), 'Corrected intensity')
        filename = region.get('control_filename', region.get('filename',
            self.source.text() if self._target else self._review.get('control_filename', '')))
        self.details_source.setText(('' if self._source_review else filename + '\n' if filename else '') + units +
            '. Signed values are after image-background subtraction, before clipping or normalization.')
        if self._source_review:
            self.details_caption.setText(self._combined_label if row in (None, -1) else
                region['label'].replace('Autofluorescence region', 'AF region'))
        normalized = region['values'] if region['values'] is not None else (None,) * len(self._names)
        signed = region['signed_values'] if region['signed_values'] is not None else (None,) * len(self._names)
        corrected = region['corrected_values'] if region['corrected_values'] is not None else (None,) * len(self._names)
        for row, values in enumerate(zip(self._names, signed, corrected, normalized, strict=True)):
            for col, value in enumerate(values):
                item = QTableWidgetItem('—' if value is None else str(value) if col == 0 else f'{value:.8g}')
                item.setToolTip(str(value) if col == 0 else repr(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.band_table.setItem(row, col, item)

    def _hover(self, event):
        show_spectral_hover(self.canvas, self._axes, event, self._names,
            np.arange(1, len(self._names) + 1), self._hover_profiles)
