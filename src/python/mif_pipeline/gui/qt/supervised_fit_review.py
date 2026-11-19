"""One multiclass matrix for saved cell evaluations and their available references."""

from collections import Counter

from PySide6.QtCore import QSignalBlocker, Qt, Signal, QTimer
from PySide6.QtWidgets import QBoxLayout, QComboBox, QHeaderView, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .classifier_confusion_matrix import ClassifierConfusionMatrix
from .classifier_review_data import read_artifact_rows, review_table, set_table
from .workbench import _fit_bounded_table_height


class SupervisedCellEvaluation(QWidget):
    subject_selected = Signal(str, str)
    image_requested = Signal()
    inspect_cell_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._rows = self._cells = self._visible_cells = ()
        self._selected_pair = None
        self._inspection_available = False
        self._image_dialog = self._image_selection = self._pending_options = None
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        toolbar = QHBoxLayout()
        self.choice, self.fold, self.subject = (QComboBox(self) for _ in range(3))
        for widget, name in ((self.choice, 'Evaluation split'), (self.fold, 'Held-out fold'),
                             (self.subject, 'Subject')):
            widget.setAccessibleName(name)
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        self.overview_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.overview_layout.setSpacing(18)
        self.matrix = ClassifierConfusionMatrix(self)
        self.overview_layout.addWidget(self.matrix, 6, Qt.AlignmentFlag.AlignTop)
        subjects_panel = QWidget(self)
        subjects_layout = QVBoxLayout(subjects_panel)
        subjects_layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel('Subjects', self)
        heading.setProperty('panelHeading', True)
        subjects_layout.addWidget(heading)
        self.subjects = review_table('supervised-evaluation-subjects', self)
        subjects_layout.addWidget(self.subjects)
        self.metrics = QLabel(self)
        self.metrics.setWordWrap(True)
        subjects_layout.addWidget(self.metrics)
        subjects_layout.addStretch()
        self.overview_layout.addWidget(subjects_panel, 5, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(self.overview_layout)
        layout.addSpacing(6)
        self.selection = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.selection_summary = QLabel('Reference cells', self)
        self.selection_summary.setProperty('panelHeading', True)
        self.selection_summary.setWordWrap(True)
        self.selection.addWidget(self.selection_summary, 1)
        self.clear = QPushButton('Clear matrix selection', self)
        self.clear.setToolTip('Show all reference classes and predictions within the selected evaluation scope.')
        self.clear.clicked.connect(lambda: self._select_pair(None))
        self.selection.addWidget(self.clear)
        self.inspect = QPushButton('Inspect cell in QuPath', self)
        self.inspect.setEnabled(False)
        self.inspect.clicked.connect(self._inspect_selected)
        self.selection.addWidget(self.inspect)
        layout.addLayout(self.selection)
        self.cells = review_table('supervised-evaluated-reference-cells', self)
        self.cells.setWordWrap(True)
        self._cell_fit_timer = QTimer(self)
        self._cell_fit_timer.setSingleShot(True)
        self._cell_fit_timer.timeout.connect(self._fit_cell_rows)
        self.cells.horizontalHeader().sectionResized.connect(self._schedule_cell_fit)
        self.cells.setMouseTracking(True)
        self.cells.entered.connect(lambda index:self.cells.setToolTip(
            'Cell '+self._visible_cells[index.row()]['cell_id'] if index.isValid() and self._visible_cells else ''))
        self.cells.doubleClicked.connect(lambda _index:self._inspect_selected())
        layout.addWidget(self.cells)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setProperty('choiceHelp', True)
        self.note.hide()
        layout.addWidget(self.note)
        self.choice.currentIndexChanged.connect(self._scope_changed)
        self.fold.currentIndexChanged.connect(self._subjects_changed)
        self.subject.currentIndexChanged.connect(self._show_counts)
        self.matrix.pair_selected.connect(self._select_pair)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.LeftToRight if event.size().width() >= 940 else QBoxLayout.Direction.TopToBottom
        self.overview_layout.setDirection(direction)
        self.selection.setDirection(direction)
        self._schedule_cell_fit()

    def _schedule_cell_fit(self, *_args):
        self._cell_fit_timer.start(0)

    def _fit_cell_rows(self):
        if self.cells.model() is not None:
            _fit_bounded_table_height(self.cells)

    def _subject_row_selected(self, current, _previous):
        if current.isValid():
            self.subject.setCurrentIndex(self.subject.findData(self._subject_ids[current.row()]))

    def set_inspection_available(self, available):
        self._inspection_available = bool(available)
        self._cell_selected()

    def _cell_selected(self, *_args):
        self.inspect.setEnabled(self._inspection_available and bool(self._visible_cells)
                                and self.cells.currentIndex().isValid())

    def _inspect_selected(self):
        index = self.cells.currentIndex()
        if not self._inspection_available or not index.isValid():
            return
        row = self._visible_cells[index.row()]
        self._image_selection = {key: row[key] for key in ('classifier','split','project_entry_id','cell_id')}
        self._pending_options = {**self._image_selection, 'channel_display': 'colour', 'merged_panel': True}
        self.inspect.setEnabled(False)
        self.note.setText('Opening the evaluated cell in QuPath…')
        self.note.show()
        self.image_requested.emit()

    def image_options(self):
        if self._pending_options is None:
            return None
        return {'supervised_gallery_settings': dict(self._pending_options)}

    def set_image_plots(self, plots):
        if plots and plots[0].display_options.get('supervised_request') == self._pending_options:
            request = plots[0].display_options.get('qupath_review')
            if request:
                self.note.clear()
                self.note.hide()
                self._cell_selected()
                self.inspect_cell_requested.emit(request)

    def set_image_error(self, message):
        self.note.setText(message)
        self.note.setVisible(bool(message))
        self._cell_selected()

    def set_artifacts(self, artifacts):
        self._rows = (*read_artifact_rows(artifacts, 'classifier-cv-confusion-data'),
                      *read_artifact_rows(artifacts, 'classifier-confusion-data'))
        self._cells = read_artifact_rows(artifacts, 'classifier-evaluation-cells')
        names = {'cross_validation': 'Cross-validation', 'validation': 'Validation', 'test': 'Independent test'}
        with QSignalBlocker(self.choice):
            previous = self.choice.currentData()
            self.choice.clear()
            for classifier, split in dict.fromkeys((r['classifier'], r['split']) for r in self._rows):
                self.choice.addItem(names[split], (classifier, split))
            self.choice.setCurrentIndex(max(0, self.choice.findData(previous)))
        self.setVisible(bool(self._rows))
        self._scope_changed()
        return bool(self._rows)

    def _scope_changed(self, *_args):
        self._selected_pair = None
        self._group = tuple(r for r in self._rows
                            if (r['classifier'], r['split']) == self.choice.currentData())
        with QSignalBlocker(self.fold):
            self.fold.clear()
            self.fold.addItem('All folds', '')
            for fold in dict.fromkeys(r['fold'] for r in self._group if r.get('fold')):
                self.fold.addItem('Fold ' + fold, fold)
        self.fold.setVisible(self.fold.count() > 1)
        self._subjects_changed()

    def _subjects_changed(self, *_args):
        self._selected_pair = None
        self._scoped = tuple(r for r in self._group
                             if not self.fold.currentData() or r.get('fold') == self.fold.currentData())
        with QSignalBlocker(self.subject):
            prior = self.subject.currentData()
            self.subject.clear()
            self.subject.addItem('All subjects', '')
            for subject in dict.fromkeys(r['subject_id'] for r in self._scoped if r['subject_id']):
                self.subject.addItem(subject, subject)
            self.subject.setCurrentIndex(max(0, self.subject.findData(prior)))
        self._subject_ids = tuple(dict.fromkeys(r['subject_id'] for r in self._scoped if r['subject_id']))
        self._show_counts()

    def _show_subjects(self):
        from mif_pipeline.compartment_analysis.classifier_qc import classifier_metrics_from_confusion
        classes = tuple(dict.fromkeys(r['reference_class'] for r in self._group))
        summaries = []
        for identity in self._subject_ids:
            counts = Counter()
            for row in self._scoped:
                if row['subject_id'] == identity:
                    counts[row['reference_class'], row['predicted_class']] += int(row['count'])
            metrics = classifier_metrics_from_confusion(tuple(tuple(counts[a,b] for b in classes) for a in classes))
            summaries.append((identity, *((f'{counts[self._selected_pair]:,}',) if self._selected_pair else ()),
                              f'{sum(counts.values()):,}', f"{100*metrics['accuracy']:.1f}%"))
        set_table(self.subjects, ('Subject', *(('Cells in\nselected pair',) if self._selected_pair else ()),
                                  'Evaluated\ncells', 'Cell\nagreement'), summaries, identities=self._subject_ids)
        self.subjects.setToolTip('Cell agreement is the fraction of this subject’s evaluated reference cells whose predicted phenotype matches the reference phenotype.')
        self.subjects.selectionModel().currentRowChanged.connect(self._subject_row_selected)

    def select_subject(self, classifier, subject):
        if self.choice.currentData() and self.choice.currentData()[0] == classifier:
            index = self.subject.findData(subject)
            if index >= 0:
                self.subject.setCurrentIndex(index)

    def _select_pair(self, pair):
        self._selected_pair = pair
        self._show_counts()

    def _show_counts(self, *_args):
        if self._image_dialog is not None:
            self._image_dialog.close()
            self._image_dialog.deleteLater()
            self._image_dialog = self._image_selection = self._pending_options = None
        self._pending_options = self._image_selection = None
        self._visible_cells = ()
        self.inspect.setEnabled(False)
        if not self._scoped:
            return
        self._show_subjects()
        subject = self.subject.currentData()
        rows = tuple(r for r in self._scoped if r['subject_id'] == subject) if subject else (
            tuple(r for r in self._scoped if r['subject_id']) or self._scoped)
        classes = tuple(dict.fromkeys(r['reference_class'] for r in self._group))
        counts = Counter()
        for row in rows:
            counts[row['reference_class'], row['predicted_class']] += int(row['count'])
        self._display_rows = []
        for reference in classes:
            total = sum(counts[reference, predicted] for predicted in classes)
            for predicted in classes:
                count = counts[reference, predicted]
                self._display_rows.append(dict(reference_class=reference, predicted_class=predicted,
                    count=count, reference_total=total, fraction_of_reference_class=count/total if total else None))
        self.matrix.set_counts(classes, classes, self._display_rows, 'cells', self._selected_pair)
        from mif_pipeline.compartment_analysis.classifier_qc import classifier_metrics_from_confusion
        metrics = classifier_metrics_from_confusion(tuple(tuple(counts[a, b] for b in classes) for a in classes))
        self.metrics.setText(f"{sum(counts.values()):,} reference cells · " + ' · '.join(
            f'{label} {metrics[key]:.3f}' for label, key in (('Accuracy', 'accuracy'), ('Macro F1', 'macro_f1'))))
        classifier, split = self.choice.currentData()
        self.choice.setToolTip('Out-of-fold predictions: each subject was evaluated by the model trained without its fold.'
                               if split == 'cross_validation' else '')
        cells = tuple(r for r in self._cells
            if r['classifier'] == classifier and r['split'] == split
            and (not subject or r['subject_id'] == subject)
            and (self._selected_pair is None or (r['reference_class'], r['predicted_class']) == self._selected_pair))
        def score(row):
            import math
            value=row.get('score',row.get('confidence'))
            try:value=float(value)
            except (TypeError,ValueError):return None
            return value if math.isfinite(value) else None
        self._visible_cells=tuple(sorted(cells,key=lambda row:(row['predicted_class'],
            -(score(row) if score(row) is not None else -1),row['sample_id'],row['subject_id'],row['cell_id'])))
        self.selection_summary.setText('Reference cells' if self._selected_pair is None else f'Reference: {self._selected_pair[0]} · Predicted: {self._selected_pair[1]}')
        self.clear.setVisible(self._selected_pair is not None)
        self.cells.setVisible(bool(self._visible_cells))
        has_scores=any(score(row) is not None for row in self._visible_cells)
        if self._visible_cells:
            set_table(self.cells, ('Cell', *(('Reference\nphenotype','Predicted\nphenotype') if self._selected_pair is None else ()),
                                  *(('Score',) if has_scores else ()), 'Image','Subject'),
                ((str(number+1), *((r['reference_class'],r['predicted_class']) if self._selected_pair is None else ()),
                  *((format(score(r),'.6g') if score(r) is not None else 'Not retained',) if has_scores else ()),
                  r['sample_id'],r['subject_id'])
                 for number,r in enumerate(self._visible_cells)), identities=(f"{r['project_entry_id']}:{r['cell_id']}" for r in self._visible_cells))
            header = self.cells.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            if has_scores:
                header.setSectionResizeMode(3 if self._selected_pair is None else 1, QHeaderView.ResizeMode.ResizeToContents)
            self._schedule_cell_fit()
            self.cells.selectionModel().currentRowChanged.connect(self._cell_selected)
        self.cells.setAccessibleDescription('Double-click a cell to inspect its saved outline and evaluated prediction in QuPath.'
            + (' Per-cell scores were not retained for this evaluation.' if not has_scores else ''))
        self.note.setText('Cell-level fold predictions were not retained with this evaluation.'
            if split == 'cross_validation' and not any(r['classifier'] == classifier and r['split'] == split for r in self._cells) else
            'No reference cells match the selected class pair.' if not self._visible_cells and self._selected_pair else
            'Reference cells were not saved with this evaluation.' if not self._visible_cells else '')
        self.note.setVisible(bool(self.note.text()))
        self.subject_selected.emit(classifier, subject or '')
