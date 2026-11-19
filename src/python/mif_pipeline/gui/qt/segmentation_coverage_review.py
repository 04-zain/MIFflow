"""Descriptive nuclear-stain field measurements linked to the shared tissue view."""
from __future__ import annotations

import csv
from io import StringIO
import math

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, Signal, QSignalBlocker, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QSizePolicy, QToolButton, QToolTip, QVBoxLayout, QWidget,
)

from .researcher_widgets import ContentSizedButton, configure_disclosure

COVERED = '#45A1D1'
UNCOVERED = '#E45383'


def coverage_rgba(categories):
    """Colour the supplied categorical pixels without changing their spatial sampling."""
    values = np.asarray(categories)
    if values.ndim != 2 or not np.isin(values, (0, 1, 2)).all():
        raise ValueError('Coverage pixels must be a two-dimensional 0/1/2 category array.')
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    rgba[values == 1] = (69, 161, 209, 115)
    rgba[values == 2] = (228, 83, 131, 180)
    return rgba


class NuclearCoverageReview(QFrame):
    """Collect explicit parameters and plot returned native-pixel area measurements.

    This widget does no image calculation. Each plotted point retains the reader's
    stable field index; selecting it requests its exact field and contribution
    array. Editing a control invalidates the displayed result before a debounced
    current-view calculation. Previous/Next in the containing page also reaches
    coincident points and fields with zero above-threshold signal.
    """
    requested = Signal(object)
    changed = Signal()
    field_selected = Signal(int)
    overlay_changed = Signal(bool)
    result_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('stageInfoPanel', True)
        self.setMinimumWidth(0)
        self._updating = False
        self._busy = False
        self._available = True
        self._fields = []
        self._result = None
        self._selected = None
        self._current_field = None
        self._current_view_status = None
        self._inventory = None
        self.compartment = 'nucleus'
        self._settings = {}
        self._active = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(450)
        self._update_timer.timeout.connect(self._apply)
        panel = QVBoxLayout(self)
        panel.setContentsMargins(12, 12, 12, 12)
        panel.setSpacing(8)
        self.title = QLabel('Nuclear staining coverage', self)
        self.title.setProperty('panelHeading', True)
        self.title.setWordWrap(True)
        panel.addWidget(self.title)
        self.method = QComboBox(self)
        self.method.setAccessibleName('Nuclear inspection method')
        self.method.addItem('Nuclear staining coverage', 'nuclear_coverage')
        self.method.addItem('Cells without nuclear outlines', 'without_nuclear_outline')
        self.method_label = QLabel('Inspection method', self)
        self.method_label.setBuddy(self.method)
        self.method_label.hide()
        self.method.hide()
        method_layout = QVBoxLayout()
        method_layout.setSpacing(4)
        method_layout.addWidget(self.method_label)
        method_layout.addWidget(self.method)
        panel.addLayout(method_layout)
        self.missing_help = QLabel('Use Previous cell and Next cell to compare the nuclear stain with each detected cell that has no saved nuclear outline.', self)
        self.missing_help.setProperty('fieldHelp', True)
        self.missing_help.setWordWrap(True)
        self.missing_help.hide()
        panel.addWidget(self.missing_help)
        self.coverage_body = QWidget(self)
        panel.addWidget(self.coverage_body)
        panel.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout = QHBoxLayout(self.coverage_body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self._content = layout
        self.controls = QWidget(self)
        controls = QVBoxLayout(self.controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(5)
        self.channel = QComboBox(self.controls)
        self.channel.setMinimumWidth(0)
        self.channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.channel.setMinimumContentsLength(12)
        self.channel.setAccessibleName('Nuclear coverage channel')
        self.channel.setPlaceholderText('Choose a stain')
        self.threshold = QLineEdit(self.controls)
        self.threshold.setPlaceholderText('Enter a value')
        self.threshold.setAccessibleName('Coverage threshold in channel units')
        self.threshold.setToolTip('Count finite native pixels strictly greater than this value in the selected channel.')
        self.channel_label = QLabel('Nuclear stain channel', self.controls)
        form.addWidget(self.channel_label, 0, 0, 1, 2)
        form.addWidget(self.channel, 1, 0, 1, 2)
        form.addWidget(QLabel('Threshold · channel units', self.controls), 2, 0, 1, 2)
        form.addWidget(self.threshold, 3, 0, 1, 2)
        controls.addLayout(form)
        self.sampling = QToolButton(self.controls)
        self.sampling.setText('Sampling settings')
        self.sampling.setCheckable(True)
        configure_disclosure(self.sampling)
        controls.addWidget(self.sampling)
        self.sampling_fields = QWidget(self.controls)
        sampling = QGridLayout(self.sampling_fields)
        sampling.setContentsMargins(0, 0, 0, 0)
        sampling.setHorizontalSpacing(10)
        sampling.setVerticalSpacing(5)
        self.field_width = QDoubleSpinBox(self.controls)
        self.field_height = QDoubleSpinBox(self.controls)
        for control, value, name in ((self.field_width, 160., 'Coverage field width'),
                                     (self.field_height, 120., 'Coverage field height')):
            control.setRange(.01, 1_000_000.)
            control.setDecimals(2)
            control.setValue(value)
            control.setSuffix(' µm')
            control.setAccessibleName(name)
            control.setMinimumWidth(0)
        sampling.addWidget(QLabel('Field width', self.sampling_fields), 0, 0)
        sampling.addWidget(QLabel('Field height', self.sampling_fields), 0, 1)
        sampling.addWidget(self.field_width, 1, 0)
        sampling.addWidget(self.field_height, 1, 1)
        sampling_help = QLabel('Each point in the survey represents one field of this size.', self.sampling_fields)
        sampling_help.setProperty('fieldHelp', True)
        sampling_help.setWordWrap(True)
        sampling.addWidget(sampling_help, 2, 0, 1, 2)
        controls.addWidget(self.sampling_fields)
        self.sampling_fields.hide()
        self.sampling.toggled.connect(self.sampling_fields.setVisible)
        self.message = QLabel('Enter a stain intensity. The review measures how much staining above it falls inside the nuclear outlines.', self.controls)
        self.message.setProperty('fieldHelp', True)
        self.message.setWordWrap(True)
        controls.addWidget(self.message)
        self.retry = ContentSizedButton('Retry coverage', self.controls)
        self.retry.clicked.connect(self._apply)
        self.retry.hide()
        controls.addWidget(self.retry)
        controls.addStretch()
        self.controls.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.controls, 0, Qt.AlignmentFlag.AlignTop)

        self.results = QWidget(self)
        results = QVBoxLayout(self.results)
        results.setContentsMargins(0, 0, 0, 0)
        results.setSpacing(7)
        self.canvas = FigureCanvasQTAgg(Figure(figsize=(3.4, 2.6), layout='constrained'))
        self.canvas.setMinimumWidth(0)
        self.canvas.setMinimumHeight(230)
        self.canvas.setMaximumHeight(280)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas.setAccessibleName('Nuclear staining coverage across sampled tissue fields')
        self.canvas.setAccessibleDescription('Horizontal axis: above-threshold stain area. Vertical axis: area outside saved nuclear outlines. Select a point or use Previous and Next to inspect the exact field.')
        self.axis = self.canvas.figure.subplots()
        self.canvas.mpl_connect('button_press_event', self._picked)
        self.canvas.mpl_connect('motion_notify_event', self._hovered)
        self.canvas.mpl_connect('key_press_event', self._key_field)
        results.addWidget(self.canvas)
        self.summary = QLabel(self.results)
        self.summary.setWordWrap(True)
        self.summary.setProperty('fieldHelp', True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        results.addWidget(self.summary)
        self.overlay = QCheckBox('Highlight contributing pixels', self.results)
        self.overlay.setChecked(True)
        self.overlay.toggled.connect(self.overlay_changed)
        results.addWidget(self.overlay)
        self.legend = QLabel(
            f'<span style="color:{COVERED}">■</span> Inside nuclear outlines<br>'
            f'<span style="color:{UNCOVERED}">■</span> Outside nuclear outlines', self.results)
        self.legend.setWordWrap(True)
        self.legend.setAccessibleDescription('Blue pixels: above threshold inside nuclear outlines. Pink pixels: above threshold outside nuclear outlines.')
        results.addWidget(self.legend)
        layout.addWidget(self.results, 1)
        self.results.hide()
        self.channel.currentIndexChanged.connect(self._channel_changed)
        self.threshold.textChanged.connect(self._edited)
        self.field_width.valueChanged.connect(self._edited)
        self.field_height.valueChanged.connect(self._edited)
        self._validity()

    def show_method(self, nuclear_task, metadata):
        missing_available = metadata.get('missing_nucleus_navigation_available', False)
        coverage_available = metadata.get('coverage_available', missing_available)
        choose = nuclear_task and missing_available and coverage_available
        missing = nuclear_task and self.method.currentData() == 'without_nuclear_outline'
        if choose:
            self.title.setText('Nuclear outline review')
        elif missing:
            self.title.setText('Cells without nuclear outlines')
        else:
            self.title.setText('Nuclear staining coverage' if nuclear_task else 'Cell staining coverage')
        self.method_label.setVisible(choose)
        self.method.setVisible(choose)
        self.coverage_body.setVisible(not missing)
        self.missing_help.setVisible(missing)

    def resizeEvent(self, event):
        """Use adjacent controls and plot when the inspector spans a narrow page."""
        horizontal = self.width() >= 620
        self._content.setDirection(QHBoxLayout.Direction.LeftToRight if horizontal else QHBoxLayout.Direction.TopToBottom)
        self.controls.setMaximumWidth(260 if horizontal else 16_777_215)
        super().resizeEvent(event)

    def configure(self, metadata):
        """Populate real image channels while retaining edits for the same inventory."""
        inventory = tuple((int(c['index']), str(c['label'])) for c in metadata['available_channels'])
        self._available = bool(metadata.get('cell_coverage_available', False) if self.compartment == 'cell'
            else metadata.get('coverage_available', metadata.get('missing_nucleus_navigation_available', False)))
        if inventory != self._inventory:
            self._inventory = inventory
            with QSignalBlocker(self.channel):
                self.channel.clear()
                for value, name in inventory:
                    self.channel.addItem(name, value)
                saved = self._settings.get(self.compartment, {})
                channel = saved.get('channel', metadata.get('nuclear_channel') if self.compartment == 'nucleus' else None)
                self.channel.setCurrentIndex(self.channel.findData(channel))
        if not self._available:
            key = 'cell_coverage_unavailable_reason' if self.compartment == 'cell' else 'coverage_unavailable_reason'
            self.message.setText(metadata.get(key) or 'This result has no outlines to compare.')
        self._validity()

    def set_compartment(self, compartment, metadata):
        """Keep separate explicitly chosen channels and thresholds for each task."""
        if compartment == self.compartment:
            self.configure(metadata)
            return
        self._settings[self.compartment] = dict(channel=self.channel.currentData(), threshold=self.threshold.text())
        self._update_timer.stop()
        self._updating = True
        self.retry.hide()
        try:
            self.compartment = compartment
            self._inventory = None
            self.clear_result()
            self.configure(metadata)
            self.threshold.setText(self._settings.get(compartment, {}).get('threshold', ''))
        finally:
            self._updating = False
        noun = 'cell' if compartment == 'cell' else 'nuclear'
        self.channel_label.setText('Cell stain channel' if compartment == 'cell' else 'Nuclear stain channel')
        self.channel.setAccessibleName(f'{noun.capitalize()} coverage channel')
        self.legend.setText(f'<span style="color:{COVERED}">■</span> Inside {noun} outlines<br>'
            f'<span style="color:{UNCOVERED}">■</span> Outside {noun} outlines')
        self.legend.setAccessibleDescription(f'Blue pixels: above threshold inside {noun} outlines. Pink pixels: above threshold outside {noun} outlines.')
        self.canvas.setAccessibleName(f'{noun.capitalize()} staining coverage across sampled tissue fields')
        self.message.setText(f'Choose a stain and threshold to compare with {noun} outlines.')

    def set_active(self, active):
        self._active = bool(active)
        self._update_timer.stop()
        if active and self.options() is not None and self._available and self._result is None:
            self._update_timer.start()

    def reset(self):
        """Forget the prior image's parameter values and measured fields."""
        self._updating = True
        self._update_timer.stop()
        self._active = False
        self._settings = {}
        self.retry.hide()
        try:
            self._inventory = None
            self.channel.clear()
            self.threshold.clear()
            self.field_width.setValue(160.)
            self.field_height.setValue(120.)
            self.clear_result()
            self._busy = False
            self.message.setText('Enter a stain intensity. The review measures how much staining above it falls inside the nuclear outlines.')
        finally:
            self._updating = False
        self._validity()

    def options(self):
        """Return only an explicitly entered finite threshold and current field size."""
        try:
            threshold = float(self.threshold.text())
        except ValueError:
            return None
        if not math.isfinite(threshold) or self.channel.currentData() is None:
            return None
        return dict(channel=int(self.channel.currentData()), threshold=threshold,
                    field_size_um=[self.field_width.value(), self.field_height.value()], compartment=self.compartment)

    def _validity(self):
        self.channel.setEnabled(self._available)
        self.threshold.setEnabled(self._available and self.channel.currentData() is not None)

    def _channel_changed(self):
        if self._updating:
            return
        with QSignalBlocker(self.threshold):
            self.threshold.clear()
        self._edited()

    def _edited(self, *_args):
        if self._updating:
            return
        self._busy = False
        self.retry.hide()
        self._update_timer.stop()
        self.clear_result()
        valid = self.options() is not None
        self.message.setText('Updating current field…' if valid else 'Enter a finite threshold in channel units.')
        self._validity()
        self.changed.emit()
        if self._active and self._available and valid:
            self._update_timer.start()

    def _apply(self):
        self._update_timer.stop()
        options = self.options()
        if options is not None and self._available and self._active:
            self.requested.emit(options)

    def set_busy(self, busy):
        self._update_timer.stop()
        self._busy = bool(busy)
        if busy:
            self.retry.hide()
            self.message.setText('Calculating coverage…')
        self._validity()

    def show_error(self, message):
        self.message.setText(message)
        self.retry.setVisible(self._active and self._available and self.options() is not None)

    def clear_result(self):
        """Remove stale points and selected-field values after parameter/image changes."""
        self._fields = []
        self._selected = None
        self._current_field = None
        self._current_view_status = None
        self._result = None
        self.results.hide()
        self.result_changed.emit(False)

    def set_result(self, result):
        """Display the reader's field records only when they match the current inputs."""
        options = self.options()
        if options is None or any(result.get(key) != value for key, value in options.items()):
            return False
        self._busy = False
        self.retry.hide()
        self._result = dict(result)
        self._fields = list(result['fields'])
        self._selected = result.get('selected_field_index')
        self._current_field = result.get('current_field')
        self._current_view_status = result.get('current_view_status')
        self.message.setText(f'{len(self._fields)} tissue fields · select a point to visit' if self._fields else '')
        self.results.show()
        self.canvas.setVisible(bool(self._fields))
        self._draw()
        self._validity()
        self.result_changed.emit(bool(self._fields))
        return True

    @property
    def fields(self):
        return tuple(self._fields)

    def select_field(self, index):
        """Update the point and values for the field that is actually displayed."""
        self._selected = index
        self._current_field = None
        self._current_view_status = None
        if self._result is not None:
            self._draw()

    def _draw(self):
        axis = self.axis
        noun = 'cell' if self.compartment == 'cell' else 'nuclear'
        axis.clear()
        x = np.asarray([row['above_area_um2'] for row in self._fields], dtype=float)
        y = np.asarray([row['uncovered_area_um2'] for row in self._fields], dtype=float)
        x_high = max(1., float(x.max(initial=0))) * 1.09
        y_high = max(1., float(y.max(initial=0))) * 1.15
        axis.scatter(x, y, s=31, color='#007D83', alpha=.8, edgecolor='white', linewidth=.5, zorder=3)
        selected = next((i for i, row in enumerate(self._fields) if row['field_index'] == self._selected), None)
        if selected is not None:
            row = self._fields[selected]
            axis.scatter([x[selected]], [y[selected]], s=83, facecolor='none', edgecolor='#9F4A14', linewidth=1.8, zorder=4)
            axis.annotate(str(row['field_index']+1), (x[selected], y[selected]), xytext=(7, 6), textcoords='offset points', fontsize=9, color='#9F4A14')
            fraction = row['uncovered_fraction']
            fraction_text = f'{100*fraction:.2f}%' if fraction is not None else 'unavailable: no above-threshold pixels'
            self.summary.setText(f"Field {row['field_index']+1}\nAbove threshold: {row['above_area_um2']:,.2f} µm²\nOutside {noun} outlines: {row['uncovered_area_um2']:,.2f} µm²\nUncovered fraction: {fraction_text}")
        elif self._current_field is not None:
            row = self._current_field
            fraction = row['uncovered_fraction']
            fraction_text = f'{100*fraction:.2f}%' if fraction is not None else 'unavailable: no above-threshold pixels'
            self.summary.setText(f"Current view\nAbove threshold: {row['above_area_um2']:,.2f} µm²\nOutside {noun} outlines: {row['uncovered_area_um2']:,.2f} µm²\nUncovered fraction: {fraction_text}")
        elif self._current_view_status == 'zoom_in':
            self.summary.setText('Zoom in to measure staining coverage in this view.')
        else:
            self.summary.setText('Select a point or use Previous / Next to inspect its field.')
        axis.set_xlim(-x_high*.035, x_high)
        axis.set_ylim(-y_high*.035, y_high)
        axis.set_xlabel('Above-threshold staining (µm²)', fontsize=9)
        axis.set_ylabel(f'Staining outside\n{noun} outlines (µm²)', fontsize=9)
        axis.tick_params(labelsize=8, colors='#526971')
        axis.ticklabel_format(style='sci', scilimits=(-3, 4), axis='both', useOffset=False)
        axis.grid(color='#edf2f2', linewidth=.7)
        axis.set_axisbelow(True)
        for side in ('top', 'right'):
            axis.spines[side].set_visible(False)
        for side in ('left', 'bottom'):
            axis.spines[side].set_color('#b8c8ce')
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        apply_figure_font(self.canvas.figure)
        self.canvas.draw_idle()

    def _nearest_point(self, event):
        if event.inaxes is not self.axis or not self._fields or event.x is None:
            return None
        xy = self.axis.transData.transform([(r['above_area_um2'], r['uncovered_area_um2']) for r in self._fields])
        distance = np.linalg.norm(xy-np.asarray([event.x, event.y]), axis=1)
        index = int(distance.argmin())
        return index if distance[index] <= 14 else None

    def _picked(self, event):
        index = self._nearest_point(event)
        if event.button == 1 and index is not None:
            self.field_selected.emit(int(self._fields[index]['field_index']))

    def _hovered(self, event):
        index = self._nearest_point(event)
        if index is None:
            QToolTip.hideText()
            return
        row = self._fields[index]
        noun = 'cell' if self.compartment == 'cell' else 'nuclear'
        QToolTip.showText(QCursor.pos(), f"Field {row['field_index']+1}\nAbove threshold: {row['above_area_um2']:,.2f} µm²\nOutside {noun} outlines: {row['uncovered_area_um2']:,.2f} µm²", self.canvas)

    def _key_field(self, event):
        if event.key not in ('left', 'right') or not self._fields:
            return
        indices = [row['field_index'] for row in self._fields]
        index = indices.index(self._selected) if self._selected in indices else -1
        self.field_selected.emit(int(indices[(index+(-1 if event.key == 'left' else 1)) % len(indices)]))

    def export_plot(self):
        if self._fields:
            from .figure_export import export_figure
            export_figure(self, self.canvas.figure, f'{self.compartment}-staining-coverage.svg')

    def export_data(self):
        if not self._fields:
            return
        from .figure_export import _choose_path, _save_bytes
        path = _choose_path(self, f'{self.compartment}-staining-coverage.csv', {'CSV table (*.csv)': 'csv'})
        if path is None:
            return
        import json
        output = StringIO()
        common = {key: value for key, value in self._result.items() if key != 'fields'}
        rows = [{**common, **row} for row in self._fields]
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value) if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})
        _save_bytes(output.getvalue().encode('utf-8'), path)
