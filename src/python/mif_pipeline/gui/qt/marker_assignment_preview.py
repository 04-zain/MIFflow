"""Review ordered phenotype assignments and their cell images."""
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QComboBox
from .researcher_widgets import ContentSizedButton
from .observed_marker_patterns import ObservedMarkerPatterns


class MarkerAssignmentPreview(QWidget):
    result_available = Signal(object)
    failed = Signal(str)
    """Display returned counts; request exact examples through the review host."""

    update_requested = Signal()
    examples_requested = Signal(str, object)
    image_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        self._phenotype = None
        self.examples_enabled = False
        self._image_dialog = None
        self._gallery_selection = None
        self._selected_pattern = None
        self._page_request = None
        self._pattern_context = self._projection = None
        self._live = False
        self._busy = False
        self._dirty = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(450)
        self._update_timer.timeout.connect(self._request_live)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(8)
        header = QHBoxLayout()
        label = QLabel('Matching cells', self)
        label.setProperty('panelHeading', True)
        header.addWidget(label)
        header.addStretch()
        self.population = QComboBox(self)
        self.population.setAccessibleName('Cells in the marker combination review')
        for title, key in (('Selected phenotype', 'rule'), ('Unmatched cells', 'unmatched'), ('All included cells', 'all')):
            self.population.addItem(title, key)
        self.population.currentIndexChanged.connect(self._population_changed)
        self.population.hide()
        header.addWidget(self.population)
        self.refresh = ContentSizedButton('Preview assignments', self)
        self.refresh.clicked.connect(self.update_requested)
        header.addWidget(self.refresh)
        layout.addLayout(header)
        self.status = QLabel('Add marker conditions to see matching cells.', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.figure = QFrame(self)
        self.figure.setObjectName('phenotype-assignment-flow')
        self.figure.setStyleSheet('QFrame#phenotype-assignment-flow { background:transparent; border:0; }')
        figure = QVBoxLayout(self.figure)
        figure.setContentsMargins(0, 4, 0, 4)
        self.matching = QLabel(self.figure)
        self.matching.setTextFormat(Qt.TextFormat.PlainText)
        self.matching.setWordWrap(True)
        self.matching.setStyleSheet('color:#174f53; font-weight:600;')
        self.matching.hide()
        self.patterns = ObservedMarkerPatterns(self.figure)
        self.population_total = QLabel(self.patterns)
        self.population_total.setWordWrap(True)
        self.population_total.setProperty("fieldHelp", True)
        self.patterns.layout().insertWidget(1, self.population_total)
        self.patterns.pattern_selected.connect(self._inspect_pattern)
        self.patterns.page_requested.connect(self._request_pattern_page)
        figure.addWidget(self.patterns)
        outcomes = QHBoxLayout()
        outcomes.setSpacing(8)
        self.buttons = {}
        for key, colour in (('assigned', '#087f80'), ('overlap', '#adc2bc')):
            button = ContentSizedButton('', self.figure)
            button.setStyleSheet('QPushButton { background:transparent; color:#245d59; border:0; padding:5px 0; text-align:left; } QPushButton:hover { text-decoration:underline; } QPushButton:disabled { color:#506964; }')
            button.clicked.connect(lambda _checked=False, selected=key: self._inspect(selected))
            outcomes.addWidget(button)
            self.buttons[key] = button
        figure.addLayout(outcomes)
        self.unknown = ContentSizedButton('', self.figure)
        self.unknown.clicked.connect(lambda:self._inspect('unknown'))
        figure.addWidget(self.unknown, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.figure)
        self.figure.hide()

    def enable_live_updates(self):
        self._live = True
        self.refresh.hide()
        self.invalidate()

    def _request_live(self):
        if self._live and self._dirty and not self._busy and self.isVisible():
            self._dirty = False
            self.update_requested.emit()

    def request_finished(self):
        self._busy = False
        if self._live and self._dirty:
            self._update_timer.start()

    def invalidate(self):
        self._close_examples()
        self._result = None
        self.population.setEnabled(False)
        self.status.show()
        self._selected_pattern = None
        self._page_request = None
        self._pattern_context = self._projection = None
        self._dirty = True
        self.figure.setEnabled(False)
        self.status.setText('Updating matching cells…' if self._live else 'Rules changed. Update the preview to see their assignments.')
        if self._live:
            self._update_timer.start()
        else:
            self.figure.hide()
            self.refresh.setEnabled(True)

    def set_busy(self):
        self._busy = True
        self.status.show()
        self._update_timer.stop()
        self.refresh.setEnabled(False)
        self.status.setText('Updating matching cells…')

    def set_incomplete(self):
        self._busy = False
        self._dirty = False
        self._result = None
        self.population.hide()
        self.status.show()
        self.figure.hide()
        self.status.setText('Add a marker condition to each phenotype to see matching cells.')

    def set_error(self, message):
        self.failed.emit(message)
        self._busy = False
        self._dirty = False
        self._close_examples()
        self._result = None
        self.population.hide()
        self.status.show()
        self.figure.hide()
        self.refresh.setEnabled(True)
        self.status.setText(message)
        self.refresh.setText('Retry preview')
        self.refresh.show()

    def set_result(self, result):
        self.result_available.emit(result)
        self._busy = False
        self._dirty = False
        self.figure.setEnabled(True)
        self.refresh.setVisible(not self._live)
        self._close_examples()
        self._result = result
        self.population.setEnabled(True)
        self.population.show()
        self.refresh.setEnabled(True)
        self.refresh.setText('Update assignment preview')
        totals = f"Of {result['selected']:,} included cells, {result['unmatched']:,} match none of the rules."
        self.population_total.setText(totals)
        self.status.setText(totals)
        self.select_rule(self._phenotype)

    def select_rule(self, phenotype):
        if phenotype != self._phenotype:
            self._close_examples()
            self._page_request = None
        self._phenotype = phenotype
        row = next((row for row in self._result['rows'] if row['phenotype'] == phenotype), None) if self._result else None
        mode = self.population.currentData()
        self.figure.setVisible(self._result is not None and (row is not None or mode != 'rule'))
        if self._result is None or (row is None and mode == 'rule'):
            return
        self.matching.setText('Observed marker combinations')
        observed = self._result.get('observed_patterns', {})
        projection = (observed if mode == 'all' else observed.get('unmatched') if mode == 'unmatched'
                      else observed.get('by_phenotype', {}).get(phenotype))
        context = (self._result['definition_sha256'], mode, phenotype if mode == 'rule' else None)
        if context == self._pattern_context:
            projection = self._projection
        self._pattern_context, self._projection = context, projection
        self.patterns.setVisible(projection is not None)
        if projection is not None:
            self.patterns.set_patterns(observed['markers'], projection,
                population=('included cells' if mode == 'all' else 'cells matching no rule' if mode == 'unmatched'
                            else f'cells matching {phenotype}'),
                assigned_phenotype=phenotype if mode == 'rule' else None)
        for button in self.buttons.values():
            button.setVisible(mode == 'rule')
        self.unknown.setVisible(mode == 'rule' and row is not None and row['unknown_required'] > 0)
        self.population_total.setVisible(mode == 'rule')
        self.status.setVisible(mode == 'rule' and projection is None)
        if mode != 'rule':
            return
        self.buttons['assigned'].setText(f"Assigned here: {row['newly_assigned']:,}")
        self.buttons['overlap'].setText(f"Assigned by earlier rules: {row['overlap_with_prior']:,}")
        for key, button in self.buttons.items():
            button.setEnabled(self.examples_enabled and bool(row['examples'][key]))
        self.unknown.setText(f"Required marker unavailable: {row['unknown_required']:,} cells")
        self.unknown.setVisible(row['unknown_required'] > 0)
        self.unknown.setEnabled(self.examples_enabled and bool(row['examples']['unknown']))

    def _population_changed(self):
        self._close_examples()
        self._selected_pattern = None
        self._page_request = None
        self.select_rule(self._phenotype)

    def _request_pattern_page(self, offset):
        mode = self.population.currentData()
        self._page_request = dict(population='matching' if mode == 'rule' else mode,
                                 phenotype=self._phenotype if mode == 'rule' else None, offset=offset)
        self.update_requested.emit()

    def set_pattern_page(self, result):
        mode = self.population.currentData()
        self._projection = result['projection']
        self.patterns.set_patterns(result['markers'], result['projection'],
            population=('included cells' if mode == 'all' else 'cells matching no rule' if mode == 'unmatched'
                        else f'cells matching {self._phenotype}'),
            assigned_phenotype=self._phenotype if mode == 'rule' else None)
        self._page_request = None

    def _inspect(self, category):
        if not self.examples_enabled or self._result is None:
            return
        row = next((row for row in self._result['rows'] if row['phenotype'] == self._phenotype), None)
        examples = self._result['unmatched_examples'] if category == 'unmatched' else row['examples'][category] if row else []
        if examples:
            self._selected_pattern = None
            self.examples_requested.emit(category, examples)

    def _inspect_pattern(self, pattern):
        if self.examples_enabled and self._result is not None and pattern['examples']:
            self._selected_pattern = pattern
            self.examples_requested.emit('pattern', pattern['examples'])

    def _close_examples(self):
        if self._image_dialog is not None:
            self.layout().removeWidget(self._image_dialog)
            self._image_dialog.close()
            self._image_dialog.deleteLater()
            self._image_dialog = None
        self._gallery_selection = None

    def open_examples(self, definition, category, examples):
        from .marker_cell_examples import MarkerCellExamples
        self._close_examples()
        row = next((row for row in self._result['rows'] if row['phenotype'] == self._phenotype), None)
        self._gallery_selection = {
            'phenotype_definition': definition,
            'precedence': None if category in ('unmatched', 'pattern') else row['precedence'],
            'category': category,
        }
        if category == 'pattern':
            self._gallery_selection['pattern_id'] = self._selected_pattern['pattern_id']
        identities = list(dict.fromkeys(tuple(identity[:3]) for identity in examples))
        images = [dict(zip(('sample_id', 'project_entry_id', 'slide_id'), identity)) for identity in identities]
        self._image_dialog = MarkerCellExamples(images, self, title='Inspect phenotype assignments', embedded=True)
        self._image_dialog.requested.connect(self._request_images)
        self.layout().addWidget(self._image_dialog)
        self._image_dialog.show()
        self._request_images()

    def _request_images(self):
        try:
            self.image_options()
        except ValueError as exc:
            self._image_dialog.set_error(str(exc))
            return
        self._image_dialog.set_busy()
        self.image_requested.emit()

    def image_options(self):
        if (
            self._image_dialog is None
            or not self._image_dialog.isVisible()
            or self._gallery_selection is None
        ):
            return None
        return {'combination_gallery_settings': {**self._gallery_selection, **self._image_dialog.options()}}

    def set_image_plots(self, plots):
        if self._image_dialog is not None:
            self._image_dialog.set_plots(plots)

    def set_image_error(self, message):
        if self._image_dialog is not None:
            self._image_dialog.set_error(message)
