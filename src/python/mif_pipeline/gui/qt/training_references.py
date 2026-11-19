"""Compare fitted class balance and inspect the recorded subject/image allocation."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QTimer, QSize
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QApplication, QAbstractItemView, QBoxLayout, QHeaderView,
    QHBoxLayout, QGridLayout, QLabel, QMenu, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from mif_pipeline.gui.interactions import FormModel


def _label(text, parent, *, heading=False):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if heading:
        label.setProperty("panelHeading", True)
    return label


class _ShareBar(QWidget):
    """A common 0–100% scale for each class's share of actual fitted observations."""
    def __init__(self, fraction, parent):
        super().__init__(parent)
        self.fraction = fraction
        self.setFixedHeight(9)
        self.setAccessibleName(f'{fraction:.1%} of the fitted observations')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#e2eaea'))
        painter.fillRect(0, 0, round(self.width() * self.fraction), self.height(), QColor('#3b8790'))


class ClassBalance(QWidget):
    """Show class imbalance once, with exact numbers available for selection/copy."""
    def __init__(self, observations, unit, parent=None):
        super().__init__(parent)
        self.setObjectName('training-class-balance')
        self.observations = observations
        self.unit = unit
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        heading.addWidget(_label('Class balance', self, heading=True))
        heading.addStretch()
        layout.addLayout(heading)
        if observations is None:
            self.copy_text = f'Training {unit} counts were not recorded for this model.'
            layout.addWidget(_label(self.copy_text, self))
            return
        total = observations['total']
        caption = _label(f'{total:,} training {unit} · all fitting images', self)
        caption.setWordWrap(False)
        caption.setProperty('fieldHelp', True)
        heading.addWidget(caption)
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(28)
        self._grid.setVerticalSpacing(12)
        self._class_rows = []
        self._columns = 0
        layout.addLayout(self._grid)
        rows = []
        for item in observations['classes']:
            fraction = item['count'] / total if total else 0.
            row = QWidget(self)
            content = QVBoxLayout(row)
            content.setContentsMargins(0, 0, 0, 0)
            content.setSpacing(7)
            heading = QHBoxLayout()
            heading.setSpacing(14)
            heading.addWidget(_label(item['name'], row), 1)
            value = _label(f"{item['count']:,} · {fraction:.1%}", row)
            value.setWordWrap(False)
            heading.addWidget(value)
            content.addLayout(heading)
            content.addWidget(_ShareBar(fraction, row))
            self._class_rows.append(row)
            rows.append(f"{item['name']}\t{item['count']}\t{fraction:.1%}")
        self._arrange()
        self.copy_text = 'Class\tTraining ' + unit + '\tShare\n' + '\n'.join(rows)
        self.setAccessibleName('Class balance across all fitting images')
        self.setAccessibleDescription(self.copy_text)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(self._copy)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def _arrange(self):
        if not hasattr(self, '_class_rows'):
            return
        columns = min(len(self._class_rows), 2 if self.width() >= 560 else 1)
        if columns == self._columns or not columns:
            return
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for index, row in enumerate(self._class_rows):
            self._grid.addWidget(row, index // columns, index % columns)
        for column in range(2):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()

    def _copy(self):
        QApplication.clipboard().setText(self.copy_text)

    def _menu(self, position):
        menu = QMenu(self)
        menu.addAction('Copy data', self._copy)
        menu.exec(self.mapToGlobal(position))


class SubjectImageTree(QTreeWidget):
    """One native hierarchy for subject roles and the images they contain."""
    def __init__(self, name, unit, parent):
        super().__init__(parent)
        self.setObjectName(name)
        self.setAccessibleName('Subjects, their roles and reference images')
        self.setColumnCount(3)
        self.setHeaderLabels(('Subject / image', 'Role', 'Training ' + unit))
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setIndentation(18)
        self.header().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.setStyleSheet(
            f'QTreeView#{name} {{ background: transparent; border: none; border-radius: 0; padding: 0; }}'
            f'QTreeView#{name}::item {{ padding: 5px 7px; border-bottom: 1px solid #dce5e5; }}'
            f'QTreeView#{name}::item:selected {{ background: #d4e9e7; color: #173d47; }}'
            f'QTreeView#{name} QHeaderView::section {{ background: #e8eeee; '
            'border: none; border-bottom: 1px solid #bdced0; padding: 8px 7px; }')
        shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(self.copy_selection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def copy_selection(self):
        selected = set(self.selectedItems())
        rows = []
        for index in range(self.topLevelItemCount()):
            subject = self.topLevelItem(index)
            for item in (subject, *(subject.child(i) for i in range(subject.childCount()))):
                if item in selected:
                    rows.append('\t'.join(item.text(column) for column in range(3)))
        if rows:
            QApplication.clipboard().setText('\n'.join(rows))

    def _menu(self, position):
        menu = QMenu(self)
        menu.addAction('Copy', self.copy_selection).setEnabled(bool(self.selectedItems()))
        menu.exec(self.viewport().mapToGlobal(position))


class TrainingReferenceSummary(QWidget):
    """Class balance stays global; subject selection keeps the whole allocation visible."""
    subject_selected = Signal(str, str)
    image_selected = Signal(object)

    def __init__(self, classifiers, labels, parent=None, *, supervised=False):
        super().__init__(parent)
        self.setObjectName('training-reference-summary')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._classifiers = {row['classifier_id']: row for row in classifiers}
        self._supervised = supervised
        self._allocations = ()
        self._trees = {}
        self._subjects = {}
        self._active_classifier = None
        self._notes = {}
        self.class_balance = {}
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_trees)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 0)
        layout.setSpacing(12)
        for index, row in enumerate(classifiers):
            key = row['classifier_id']
            if len(classifiers) > 1:
                layout.addWidget(_label(labels[key], self, heading=True))
            balance = ClassBalance(row.get('training_observations'), self._unit(key), self)
            self.class_balance[key] = balance
            layout.addWidget(balance)
            layout.addSpacing(8)
            allocation = QWidget(self)
            body = QVBoxLayout(allocation)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(8)
            heading = QHBoxLayout()
            heading.addWidget(_label('Subjects and images', allocation, heading=True), 1)
            body.addLayout(heading)
            if index == 0:
                self.inspection_actions = heading
            tree = SubjectImageTree(f'training-reference-subjects-{index}', self._unit(key), allocation)
            self._trees[key] = tree
            tree.currentItemChanged.connect(lambda current, _previous, key=key: self._selected(key, current))
            tree.itemExpanded.connect(self._schedule_fit)
            tree.itemCollapsed.connect(self._schedule_fit)
            body.addWidget(tree)
            note = _label('', allocation)
            note.setProperty('fieldHelp', True)
            body.addWidget(note)
            self._notes[key] = note
            layout.addWidget(allocation)
        self.set_allocation(())

    def _unit(self, key):
        return ('cells' if self._supervised else 'patches' if 'kronos' in
                self._classifiers[key]['estimator_settings']['configuration_schema'] else 'pixels')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_fit()

    def _schedule_fit(self, *_args):
        self._fit_timer.start(0)

    def _fit_trees(self):
        for tree in self._trees.values():
            visible = []
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                visible.append(item)
                if item.isExpanded():
                    visible.extend(item.child(index) for index in range(item.childCount()))
            height = tree.header().height() + sum(tree.rowHeight(tree.indexFromItem(item)) for item in visible) + 6
            tree.setFixedHeight(height if len(visible) <= 24 else min(500, height))

    def _image_rows(self, key):
        observations = self._classifiers[key].get('training_observations') or {'images': ()}
        counts = {item['entry_id']: item['count'] for item in observations['images']}
        rows = [dict(row, fitted_count=counts.get(row['project_entry_id']))
                for row in self._allocations if row['classifier'] == key]
        return rows or [dict(classifier=key, subject_id=item['subject_id'], sample_id=item['sample_id'], split='training',
            project_entry_id=item['entry_id'], fitted_count=item['count']) for item in observations['images']]

    def set_allocation(self, rows):
        self._allocations = tuple(rows)
        for key, tree in self._trees.items():
            current = tree.currentItem()
            selected = tuple(current.data(0, Qt.ItemDataRole.UserRole)) if current is not None else None
            roles = {'training': 'Training and validation' if self._classifiers[key].get('model_selection') else 'Training',
                     'validation': 'Validation', 'test': 'Independent test'}
            records = self._image_rows(key)
            with QSignalBlocker(tree):
                tree.clear()
                self._subjects[key] = {}
                for subject in dict.fromkeys(row['subject_id'] for row in records):
                    images = [row for row in records if row['subject_id'] == subject]
                    fitting = [row for row in images if row['split'] == 'training']
                    count = (sum(row['fitted_count'] for row in fitting)
                             if fitting and all(row['fitted_count'] is not None for row in fitting) else None)
                    item = QTreeWidgetItem(tree, (subject, ', '.join(dict.fromkeys(roles[row['split']] for row in images)),
                        f'{count:,}' if count is not None else 'Not recorded' if fitting else '—'))
                    item.setData(0, Qt.ItemDataRole.UserRole, (subject, None))
                    item.setToolTip(0, subject + f' · {len(images)} images')
                    font = item.font(0); font.setBold(True); item.setFont(0, font)
                    item.setSizeHint(0, QSize(0, 30))
                    self._subjects[key][subject] = item
                    for row in images:
                        child = QTreeWidgetItem(item, (row['sample_id'], '',
                            f"{row['fitted_count']:,}" if row['fitted_count'] is not None else
                            'Not recorded' if row['split'] == 'training' else '—'))
                        child.setData(0, Qt.ItemDataRole.UserRole, (subject, row['project_entry_id']))
                        child.setSizeHint(0, QSize(0, 30))
                        child.setToolTip(0, row['sample_id'])
                        child.setToolTip(1, roles[row['split']])
                        child.setToolTip(2, 'Not used for fitting.' if row['split'] != 'training' else 'Recorded fitted observations.')
                        if selected == (subject, row['project_entry_id']):
                            tree.setCurrentItem(child)
                    item.setExpanded(True)
                    if selected == (subject, None):
                        tree.setCurrentItem(item)
                if self._active_classifier in (None, key) and tree.currentItem() is None and tree.topLevelItemCount():
                    tree.setCurrentItem(tree.topLevelItem(0).child(0))
                    self._active_classifier = key
            note = ('Only fitting-image identities were recorded for this model.'
                    if not self._allocations and (self._classifiers[key]['test_patient_count'] or self._classifiers[key]['validation_patient_count']) else '')
            self._notes[key].setText(note)
            self._notes[key].setVisible(bool(note))
        self._schedule_fit()
        key = self._active_classifier
        if key is not None:
            self._selected(key, self._trees[key].currentItem())

    def set_folds(self, rows):
        """Fold membership is presented by the cross-validation page."""
        pass

    def _selected(self, key, item):
        self._active_classifier = key
        for other, tree in self._trees.items():
            if other != key:
                with QSignalBlocker(tree):
                    tree.clearSelection()
        if item is None:
            self.image_selected.emit(None)
            return
        subject, entry = item.data(0, Qt.ItemDataRole.UserRole)
        row = next((row for row in self._image_rows(key)
                    if row['project_entry_id'] == entry and row['subject_id'] == subject), None)
        self.subject_selected.emit(key, subject)
        self.image_selected.emit(dict(row, classifier=key) if row is not None else None)

    def select_image(self, classifier, entry):
        tree = self._trees.get(classifier)
        if tree is None:
            return
        for index in range(tree.topLevelItemCount()):
            subject = tree.topLevelItem(index)
            for child in (subject.child(i) for i in range(subject.childCount())):
                if child.data(0, Qt.ItemDataRole.UserRole)[1] == entry:
                    subject.setExpanded(True)
                    tree.setCurrentItem(child)
                    tree.scrollToItem(child)
                    return

    def select_subject(self, classifier, subject):
        tree = self._trees.get(classifier)
        if tree is None:
            return
        current = tree.currentItem()
        if (self._active_classifier == classifier and current is not None
                and current.data(0, Qt.ItemDataRole.UserRole)[0] == subject):
            return
        self._active_classifier = classifier
        with QSignalBlocker(tree):
            tree.clearSelection()
            item = self._subjects[classifier].get(subject)
            tree.setCurrentItem(item)
            if item is not None:
                item.setExpanded(True)
                tree.scrollToItem(item)
        self.image_selected.emit(None)
        self._schedule_fit()


def build_training_reference_summary(form: FormModel, parent=None):
    if form.form_id != 'training_fit_review':
        return None
    stage = form.context['stage_result']
    if stage['stage'] not in {'primary_classifier_training_review', 'secondary_classifier_training_review',
                             'supervised_phenotype_training_review'}:
        return None
    summary = next(table for table in form.tables if table.table_id == 'training_fit_classifiers')
    return TrainingReferenceSummary(stage['resume_context']['classifiers'],
        {row.row_id: row.values[0] for row in summary.rows}, parent,
        supervised=stage['stage'] == 'supervised_phenotype_training_review')


def is_training_reference_table(table_id):
    return table_id == 'training_fit_classifiers' or table_id.startswith(('training_classes_', 'training_images_'))
