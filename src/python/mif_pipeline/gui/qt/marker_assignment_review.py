"""Final ordered phenotype assignments in the continuous native tissue view."""
from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QMenu

from .cell_image_field import CellImageFieldReview
from .observed_marker_patterns import ObservedMarkerPatterns
from .researcher_widgets import ContentSizedButton


class AssignmentImageField(CellImageFieldReview):
    def _cell_key(self, row):
        keys = ('sample_id', 'project_entry_id', 'slide_id', 'section_id', 'cell_id')
        return tuple(row[key] for key in keys) if all(key in row for key in keys) else row['cell_id']

    def set_field_bundle(self, bundle):
        super().set_field_bundle(bundle)
        for item in self.view.items_by_id.values():
            row = self._by_id[item.data(0)]
            item.setToolTip(f'{row["phenotype"]}\nCell {row["cell_id"]}')
        self.view.setAccessibleName('Phenotype assignments in tissue')
        self.view.setAccessibleDescription('Click the spatial count overlay to zoom in. Drag to pan; Control and scroll zooms. Select a cell outline to inspect its assigned phenotype and marker states.')


class MarkerAssignmentReview(QWidget):
    requested = Signal()
    preview_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.revision = 0
        self.definition = self.result = self._bundle = None
        self._pattern = None
        self._page_request = None
        self._pattern_context = self._projection = None
        self._display, self._viewport, self._channels = {}, {}, []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.requested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.summary = QLabel('Counting phenotype assignments…', self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        choices = QHBoxLayout()
        self.phenotypes = QComboBox(self)
        self.phenotypes.setAccessibleName('Assigned phenotype to inspect')
        self.phenotypes.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.phenotypes.setMinimumContentsLength(15)
        choices.addWidget(self.phenotypes, 1)
        self.scope = QComboBox(self)
        self.scope.addItem('All images', 'all')
        self.scope.addItem('Selected image', 'image')
        self.scope.setAccessibleName('Images included in the marker pattern plot')
        self.scope.currentIndexChanged.connect(self._phenotype_changed)
        self.scope.hide()
        choices.addWidget(self.scope)
        self.all_patterns = ContentSizedButton('All patterns', self)
        self.all_patterns.clicked.connect(self._phenotype_changed)
        self.all_patterns.hide()
        choices.addWidget(self.all_patterns)
        layout.addLayout(choices)
        self.patterns = ObservedMarkerPatterns(self)
        self.patterns.table.horizontalHeader().setMinimumSectionSize(36)
        self.patterns.pattern_selected.connect(self._pattern_selected)
        self.patterns.page_requested.connect(self._request_pattern_page)
        layout.addWidget(self.patterns)
        from .cohort_image_navigation import CohortImageNavigation
        self.image_navigation = CohortImageNavigation(self)
        self.images = self.image_navigation.images
        self.image_navigation.image_changed.connect(self._image_changed)
        layout.addWidget(self.image_navigation)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.cell_view = AssignmentImageField(self)
        layout.addWidget(self.cell_view)
        self.cell_value = QLabel(self)
        self.cell_value.setWordWrap(True)
        self.cell_value.hide()
        layout.addWidget(self.cell_value)
        actions = QHBoxLayout()
        self.retry = ContentSizedButton('Retry tissue view', self)
        self.retry.clicked.connect(lambda: self.preview_requested.emit() if self.result is None else self._schedule())
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
        self.phenotypes.currentIndexChanged.connect(self._phenotype_changed)
        self.cell_view.field_requested.connect(self._field_changed)
        self.cell_view.field_cell_selected.connect(self._inspect_cell)
        self.cell_view.appearance_requested.connect(lambda: self.cell_view.open_appearance(available_channels=self._channels))
        self.cell_view.display_changed.connect(self._display_changed)
        self.cell_view.load_requested.connect(self._load_appearance)

    def configure(self, definition):
        self._timer.stop()
        self.definition = definition
        self.result = self._bundle = None
        self._pattern = None
        self._page_request = None
        self._display, self._viewport = {}, {}
        self.cell_view.clear()
        self.cell_view.setEnabled(False)
        self.patterns.hide()
        self.image_navigation.hide()
        self.scope.hide()
        self.phenotypes.setEnabled(False)
        self.summary.setText('Counting phenotype assignments…')
        self.status.clear()
        self.retry.hide()
        self.export.setEnabled(False)
        self.revision += 1

    def set_result(self, result):
        if self.definition is None or self.isHidden():
            return
        from mif_pipeline.orchestration.batch_phenotyping._common import digest
        if result['definition_sha256'] != digest(self.definition):
            return
        self.result = result
        total = result['selected']
        images = len(result['images'])
        scope = f' across {images} images' if images > 1 else ''
        self.summary.setText(f'{total:,} included cells{scope} · {len(result["rows"])} phenotype rules · {result["unmatched"]:,} unmatched')
        with QSignalBlocker(self.phenotypes):
            previous_phenotype = self.phenotypes.currentData()
            self.phenotypes.clear()
            for row in result['rows']:
                n = row['newly_assigned']
                self.phenotypes.addItem(f'{row["phenotype"]} · {n:,}' + (f' ({100*n/total:.3g}%)' if total else ''), row['phenotype'])
            n = result['unmatched']
            self.phenotypes.addItem(f'{result["residual_phenotype"]} · {n:,}' + (f' ({100*n/total:.3g}%)' if total else ''), result['residual_phenotype'])
            self.phenotypes.addItem(f'All included cells · {total:,}', None)
            if self._pattern_context is not None and self._pattern_context[0] == result['definition_sha256']:
                index = self.phenotypes.findData(previous_phenotype)
                if index >= 0:
                    self.phenotypes.setCurrentIndex(index)
        self.phenotypes.setEnabled(True)
        self._set_images(result['images'])
        self.scope.setVisible(self.images.count() > 1)
        self._phenotype_changed()

    def _set_images(self, images):
        labels = [' · '.join(dict.fromkeys((image['sample_id'], image['slide_id']))) for image in images]
        self.image_navigation.set_images([dict(image,
            key=[image[k] for k in ('sample_id','project_entry_id','slide_id')],
            label=label + (' · ' + image['project_entry_id'] if labels.count(label) > 1 else ''))
            for image,label in zip(images, labels, strict=True)])

    def _pattern_title(self):
        return ('Observed marker patterns in the selected image' if self.scope.currentData() == 'image'
                else 'Observed marker patterns' + (' across included images' if self.images.count() > 1 else ''))

    def _phenotype_changed(self):
        if self.result is None:
            return
        self._pattern = None
        self._page_request = None
        self.all_patterns.hide()
        phenotype = self.phenotypes.currentData()
        observed = self.result['observed_patterns']
        projection = observed if phenotype is None else observed['assigned_by_phenotype'][phenotype]
        context = (self.result['definition_sha256'], phenotype,
                   tuple(self.images.currentData()) if self.scope.currentData() == 'image' else None)
        if context == self._pattern_context:
            projection = self._projection
        self._pattern_context, self._projection = context, projection
        population = 'included cells' if phenotype is None else f'cells assigned to {phenotype}'
        if len(self.result['images']) > 1:
            population += ' across included images'
        self.patterns.set_patterns(observed['markers'], projection, population=population)
        self.patterns.table.setEnabled(self.scope.currentData() != 'image')
        self.patterns.population.setText(self._pattern_title())
        self.patterns.table.setFixedHeight(40 * min(4, max(1, len(projection['rows']))) + 50)
        self.patterns.coverage.setText('Select a marker pattern to locate its cells.' if projection['rows'] else 'No cells receive this phenotype.')
        self.patterns.show()
        self._fit_pattern_columns()
        if self.scope.currentData() == 'image':
            self.patterns.coverage.setText('Loading patterns for the selected image…')
            self._request_pattern_page(0)
        self._schedule()

    def _request_pattern_page(self, offset):
        phenotype = self.phenotypes.currentData()
        self._page_request = dict(population='all' if phenotype is None else 'assigned',
                                 phenotype=phenotype, offset=offset)
        if self.scope.currentData() == 'image':
            self._page_request['image_identity'] = self.images.currentData()
        self.preview_requested.emit()

    def set_pattern_page(self, result):
        self._projection = result['projection']
        self.patterns.set_patterns(result['markers'], result['projection'], population='included cells')
        self.patterns.table.setEnabled(True)
        self.patterns.population.setText(self._pattern_title())
        self.patterns.table.setFixedHeight(40 * min(4, max(1, len(result['projection']['rows']))) + 50)
        self.patterns.coverage.setText('Select a marker pattern to locate its cells.')
        self._page_request = None
        self._fit_pattern_columns()

    def _fit_pattern_columns(self):
        table = self.patterns.table
        metrics = table.fontMetrics()
        widths = [max(42, min(100, metrics.horizontalAdvance(marker) + 14)) for marker in self.patterns._model.markers]
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)
        table.setColumnWidth(len(widths), max(150, table.viewport().width() - sum(widths) - 2))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_pattern_columns)
        QTimer.singleShot(0, self._fit_text_heights)

    def _fit_text_heights(self):
        for label in (self.summary, self.status, self.cell_value, self.patterns.coverage):
            if not label.isHidden():
                label.setMinimumHeight(max(0, label.heightForWidth(label.width())))
        self.updateGeometry()

    def _pattern_selected(self, pattern):
        self._pattern = pattern
        self.all_patterns.show()
        self._schedule()

    def _image_changed(self, *_args):
        self._viewport = {}
        self._display = {}
        self._bundle = None
        self.cell_view.clear()
        if self.scope.currentData() == 'image':
            self._phenotype_changed()
        else:
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
        self.cell_value.hide()
        self.retry.hide()
        self.export.setEnabled(False)
        if disable:
            self.cell_view.setEnabled(False)
        if self.result is not None:
            self.status.setText('Loading assigned cells in tissue…')
            self._timer.start()

    def options(self):
        if self.result is None or self.images.currentData() is None:
            return None
        phenotype = self.phenotypes.currentData()
        viewport = self.cell_view.view.viewport()
        return {**self._display, **self._viewport, 'phenotype_definition': self.definition,
            'selection_kind': 'pattern' if self._pattern is not None else 'all' if phenotype is None else 'phenotype',
            'selection_value': self._pattern['pattern_id'] if self._pattern is not None else phenotype,
            'image_identity': self.images.currentData(),
            'viewport_size': [max(1,min(1024,viewport.width())),max(1,min(1024,viewport.height()))],
            'channel_display': self._display.get('channel_display', 'colour'),
            'merged_panel': self._display.get('merged_panel', True)}

    def set_bundle(self, bundle):
        metadata = bundle['metadata']
        if 'cohort_images' in metadata:
            self._set_images(metadata['cohort_images'])
        self._channels = metadata['available_channels']
        self._bundle = bundle
        self.cell_view.set_field_bundle(bundle)
        self._display = dict(self.cell_view.options)
        selected = self.cell_view._by_id.get(self.cell_view._selected_id)
        if selected is not None:
            self._inspect_cell(selected)
        self.cell_view.setEnabled(True)
        self.export.setEnabled(True)
        self.retry.hide()
        n, total = metadata['total_selected'], metadata['total_measured']
        label = 'Selected pattern' if self._pattern is not None else self.phenotypes.currentData() or 'All included cells'
        self.status.setText(f'{label}: {n:,} of {total:,} included cells in this image' + (f' ({100*n/total:.3g}%).' if total else '.'))
        QTimer.singleShot(0, self._fit_text_heights)

    def set_error(self, message):
        self.status.setText(message)
        self.cell_view.setEnabled(False)
        self.export.setEnabled(False)
        self.retry.show()

    def _inspect_cell(self, row):
        symbols = {'1': '+', '0': '−', 'u': '?'}
        states = tuple(marker + symbols[state] for marker,state in zip(self.result['observed_patterns']['markers'], row['pattern_id'], strict=True))
        label = row['phenotype']
        if label != ' '.join(states):
            label += ' · ' + ' · '.join(states)
        self.cell_value.setText(label)
        self.cell_value.setToolTip('Cell ' + row['cell_id'])
        self.cell_value.show()

    def _export(self, data_only):
        if self._bundle is None or not self.export.isEnabled():
            return
        from .figure_export import _choose_path, _save_bytes, _DATA_FORMATS, export_figure
        from mif_pipeline.cell_analysis.measurement_spatial_plots import field_npz, field_figure
        options = {**self.cell_view.options, 'displayed_channel': self.cell_view.channel.currentData(),
            'show_cell_outlines': self.cell_view.cells.isChecked(), 'show_nuclear_outlines': self.cell_view.nuclei.isChecked(),
            'density_grid_factor': self.cell_view._density_factor, 'selected_cell_id': self.cell_view.selected_cell_id()}
        if not data_only:
            figure = field_figure(self._bundle, options)
            export_figure(self, figure, 'phenotype-assignment-field.svg')
            return
        formats = _DATA_FORMATS if self._bundle['metadata']['outline_detail_available'] else {k:v for k,v in _DATA_FORMATS.items() if v == 'npz'}
        path = _choose_path(self, 'phenotype-assignment-field.npz', formats)
        if path is not None:
            if path.suffix.lower() == '.npz':
                payload = field_npz(self._bundle, options)
            else:
                import csv, io
                stream = io.StringIO()
                markers = self._bundle['metadata']['marker_ids']
                fields = ['cell_id','phenotype','selected','x_um','y_um']
                writer = csv.DictWriter(stream, fieldnames=fields + [f'marker:{marker}' for marker in markers])
                writer.writeheader()
                for row in self._bundle['metadata']['visible_cells']:
                    states = {f'marker:{marker}': {'1':'positive','0':'negative','u':'unknown'}[state]
                              for marker,state in zip(markers,row['pattern_id'],strict=True)}
                    writer.writerow({**{key:row[key] for key in fields}, **states})
                payload = stream.getvalue().encode()
            _save_bytes(payload, path)
