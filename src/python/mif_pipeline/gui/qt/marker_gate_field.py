"""Link a marker intensity interval to the shared continuous tissue viewer."""
from math import isfinite, nextafter

from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QMenu, QMessageBox

from .cohort_image_navigation import CohortImageNavigation
from .cell_image_field import CellImageFieldReview
from .researcher_widgets import ContentSizedButton


class _MarkerImageField(CellImageFieldReview):
    def _cell_key(self, row):
        keys = ('sample_id','project_entry_id','slide_id','section_id','cell_id')
        if all(key in row for key in keys):
            import json
            return json.dumps([row[key] for key in keys], separators=(',', ':'))
        return super()._cell_key(row)


class MarkerGateField(QWidget):
    requested = Signal()
    range_changed = Signal(float, float)

    def __init__(self, parent=None, *, histogram_selection=False):
        super().__init__(parent)
        self.revision = 0
        self._identity = None
        self._gate = None
        self._extent = None
        self._interval = None
        self._bundle = None
        self._display = {}
        self._viewport = {}
        self._channels = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.requested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        choices = QHBoxLayout()
        self.image_navigation = CohortImageNavigation(self)
        self.images = self.image_navigation.images
        self.images.setAccessibleName('Image for marker tissue review')
        choices.addWidget(self.image_navigation, 1)
        if not histogram_selection:
            choices.addWidget(QLabel('Inspect', self))
        self.population = QComboBox(self)
        self.population.setAccessibleName('Marker intensity selection')
        for label, value in (('At or above gate', 'positive'), ('Below gate', 'negative'), ('Selected intensity range', 'range')):
            self.population.addItem(label, value)
        self.population.setVisible(not histogram_selection)
        choices.addWidget(self.population)
        self.restore_gate = ContentSizedButton("Use current gate", self)
        self.restore_gate.setProperty("actionRole", "secondary")
        self.restore_gate.clicked.connect(lambda: self.population.setCurrentIndex(0))
        self.restore_gate.hide()
        self._histogram_selection = histogram_selection
        choices.addStretch(1)
        choices.addWidget(self.restore_gate)
        layout.addLayout(choices)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.cell_view = _MarkerImageField(self)
        self.cell_view.view.setAccessibleName('Marker intensity selection in tissue')
        layout.addWidget(self.cell_view)
        self.cell_value = QLabel(self)
        self.cell_value.setWordWrap(True)
        self.cell_value.setMinimumHeight(self.cell_value.fontMetrics().height())
        self.cell_value.hide()
        actions = QHBoxLayout()
        actions.addWidget(self.cell_value)
        self.retry = ContentSizedButton('Retry tissue view', self)
        self.retry.clicked.connect(self._schedule)
        self.retry.hide()
        actions.addWidget(self.retry)
        actions.addStretch()
        self.export = ContentSizedButton('Export tissue view…', self)
        menu = QMenu(self.export)
        menu.addAction('Figure…', lambda: self._export(False))
        menu.addAction('Field data…', lambda: self._export(True))
        self.export.setMenu(menu)
        self.export.setEnabled(False)
        actions.addWidget(self.export)
        layout.addLayout(actions)
        self.images.currentIndexChanged.connect(self._image_changed)
        self.population.currentIndexChanged.connect(self._selection_changed)
        self.cell_view.field_requested.connect(self._field_changed)
        self.cell_view.field_cell_selected.connect(self._inspect_cell)
        self.cell_view.appearance_requested.connect(lambda: self.cell_view.open_appearance(available_channels=self._channels))
        self.cell_view.display_changed.connect(self._display_changed)
        self.cell_view.load_requested.connect(self._load_appearance)

    def configure(self, marker, source, cutoff, extent, images):
        identity = (marker, source, tuple(tuple(row[k] for k in ('sample_id','project_entry_id','slide_id')) for row in images))
        if identity != self._identity:
            self._identity = identity
            self._bundle = None
            self._display = {}
            self._viewport = {}
            self.cell_view.clear()
            self.cell_value.hide()
            self.export.setEnabled(False)
            self.image_navigation.set_images([
                {"label": " · ".join(dict.fromkeys((row["sample_id"], row["slide_id"]))),
                 "key": [row[k] for k in ("sample_id", "project_entry_id", "slide_id")]}
                for row in images
            ])
            with QSignalBlocker(self.population):
                self.population.setCurrentIndex(0)
        self._gate = dict(marker_id=marker, source=source, cutoff=cutoff)
        score = source == 'nimbus'
        self.population.setAccessibleName('Nimbus score selection' if score else 'Marker intensity selection')
        self.population.setItemText(2, 'Selected score range' if score else 'Selected intensity range')
        self.cell_view.view.setAccessibleName('Nimbus score selection in tissue' if score else 'Marker intensity selection in tissue')
        self._extent = extent
        self._selection_changed()

    def select_range(self, lower, upper):
        self._interval = (lower, upper)
        with QSignalBlocker(self.population):
            self.population.setCurrentIndex(2)
        self.restore_gate.setVisible(self._histogram_selection)
        self._schedule()

    def _selection_changed(self):
        if self._gate is None or self._extent is None:
            return
        cutoff = self._gate['cutoff']
        low, high = self._extent
        mode = self.population.currentData()
        self.restore_gate.setVisible(self._histogram_selection and mode == 'range')
        if mode == 'positive':
            self._interval = (cutoff, max(cutoff, high))
        elif mode == 'negative':
            upper = nextafter(cutoff, -float('inf'))
            if not isfinite(upper):
                self.set_error('No finite measurement is below this gate.')
                return
            self._interval = (min(low, upper), upper)
        if self._interval is not None:
            self.range_changed.emit(*self._interval)
            self._schedule()

    def _image_changed(self):
        self._bundle = None
        self._viewport = {}
        self.cell_view.clear()
        self._schedule()

    def _field_changed(self, options):
        self._viewport = dict(options)
        self._schedule(disable=False)

    def _display_changed(self, options):
        self._display = dict(options)

    def _load_appearance(self, options):
        self._display = dict(options)
        self._schedule()

    def _schedule(self, *, disable=True):
        self.revision += 1
        self.export.setEnabled(False)
        self.retry.hide()
        if disable:
            self.cell_value.hide()
            self.cell_view.setEnabled(False)
            self.status.setText('Loading selected cells in tissue…')
            self.status.show()
        self._timer.start()

    def options(self):
        if self._gate is None or self._interval is None:
            return None
        viewport = self.cell_view.view.viewport()
        return {**self._display, **self._gate, **self._viewport,
                'image_identity': self.images.currentData(),
                'lower': self._interval[0], 'upper': self._interval[1],
                'viewport_size': [max(1,min(1024,viewport.width())),max(1,min(1024,viewport.height()))],
                'channel_display': self._display.get('channel_display', 'colour'),
                'merged_panel': self._display.get('merged_panel', True)}

    def set_bundle(self, bundle):
        self._bundle = bundle
        self._channels = bundle['metadata']['available_channels']
        self.cell_view.set_field_bundle(bundle)
        self._display = dict(self.cell_view.options)
        self.cell_view.setEnabled(True)
        self.export.setEnabled(True)
        self.retry.hide()
        details = []
        if self.population.currentData() == 'range':
            quantity = 'Score' if self._gate['source'] == 'nimbus' else 'Intensity'
            low, high = self._interval
            details.append(f'{quantity} range {low:.6g} to {high:.6g}')
        if self._gate['source'] == 'nimbus':
            metadata = bundle['metadata']
            details.append(f"{metadata['scored_cells']:,} scored cells")
            if metadata['unknown_scores']:
                details.append(f"{metadata['unknown_scores']:,} scores unavailable")
        self.status.setText(' · '.join(details))
        self.status.setVisible(bool(details))

    def set_error(self, message):
        self.status.setText(message)
        self.status.show()
        self.cell_view.setEnabled(False)
        self.export.setEnabled(False)
        self.retry.show()

    def _inspect_cell(self, row):
        value = row.get('value')
        quantity = 'Nimbus score' if self._gate['source'] == 'nimbus' else 'Intensity'
        if value is None or not isfinite(value):
            text = quantity + ' unavailable.'
        else:
            above = value >= self._gate['cutoff']
            call = 'Positive' if above else 'Negative'
            text = f'{call} · {quantity} {value:.6g}'
        self.cell_value.setText(text)
        self.cell_value.setToolTip(f'Cell {row["cell_id"]}\nExact saved value: {value!r}')
        self.cell_value.show()

    def _export(self, data_only):
        if self._bundle is None or not self.export.isEnabled():
            return
        from .figure_export import _choose_path, _save_bytes, _DATA_FORMATS, export_figure
        from mif_pipeline.cell_analysis.measurement_spatial_plots import field_figure, field_npz, field_cells_csv
        options = {**self.cell_view.options, 'displayed_channel':self.cell_view.channel.currentData(),
            'show_cell_outlines':self.cell_view.cells.isChecked(),
            'show_nuclear_outlines':self.cell_view.nuclei.isChecked(),
            'density_grid_factor':self.cell_view._density_factor,
            'selected_cell_id':self.cell_view.selected_cell_id(),
            'selected_cell_identity':self.cell_view.selected_cell_identity()}
        basename = 'nimbus-score-field' if self._gate['source'] == 'nimbus' else 'marker-tissue-field'
        if not data_only:
            export_figure(self,field_figure(self._bundle,options),basename + '.svg')
            return
        formats = _DATA_FORMATS if self._bundle['metadata']['outline_detail_available'] else {key:value for key,value in _DATA_FORMATS.items() if value == 'npz'}
        path = _choose_path(self,basename + '.npz',formats)
        if path is not None:
            try:
                if path.suffix.lower() == '.npz':
                    payload = field_npz(self._bundle,options)
                elif self._gate['source'] == 'nimbus':
                    import csv, io
                    stream = io.StringIO()
                    fields = ('sample_id','project_entry_id','slide_id','section_id','cell_id')
                    writer = csv.writer(stream)
                    writer.writerow((*fields,'nimbus_score','selected','x_um','y_um',
                        'field_selected','field_scored','all_visible_outlines_available'))
                    metadata = self._bundle['metadata']
                    for row in metadata['visible_cells']:
                        writer.writerow((*(row[key] for key in fields),row['value'],row['selected'],row['x_um'],row['y_um'],
                            metadata['field_selected'],metadata['field_measured'],metadata['outline_detail_available']))
                    payload = stream.getvalue().encode()
                else:
                    payload = field_cells_csv(self._bundle)
                _save_bytes(payload,path)
            except (OSError, ValueError, RuntimeError) as exc:
                QMessageBox.warning(self, "Data not exported", f"Could not save {path.name}.\n\n{exc}")
