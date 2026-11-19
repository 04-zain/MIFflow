"""Review retained source spectra and assign their stable sets to stained regions."""
from copy import deepcopy
from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHeaderView, QHBoxLayout, QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget

from .reference_workspace import decision_combo, reference_table, text_label


class _PreparationWorkspace(QWidget):
    native_action_requested = Signal(str)

    def set_option_editor(self, editor):
        self.option_editor = editor
        editor.hide()

    def show_page(self, _page):
        """These single-page checkpoints have no annotation subpage to switch."""


class ReferenceControlSourceWorkspace(_PreparationWorkspace):
    """The accepted proposal remains owned by the service; review submits no curves."""
    revision_requested = Signal(str)
    changed = Signal()

    def __init__(self, review, values, parent=None):
        super().__init__(parent)
        from .af_control_review import AfControlSpectrumPanel
        self.setObjectName('shared-af-source-review-workspace')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.validation_editors, self.field_errors = {}, {}
        self._editable = bool(review.get('composition_enabled'))
        self._invalid_draft = None
        self._draft_invalid = False
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.spectrum = AfControlSpectrumPanel(review['review'], self, source=True, editable=self._editable)
        self.spectrum.set_revision_option(review.get('revision_option'))
        self.spectrum.revision_requested.connect(self.revision_requested)
        layout.addWidget(self.spectrum)
        if review.get('composition_note'):
            layout.addWidget(text_label(review['composition_note'], self, supporting=True))
        if self._editable:
            self.validation_editors['control_sets'] = self.spectrum.table
            self.field_errors['control_sets'] = self.spectrum.status
            self.spectrum.changed.connect(self._composition_changed)

    def owns_control(self, name):
        return self._editable and name == 'control_sets'

    def values(self):
        return {'control_sets': deepcopy(self._invalid_draft) if self._draft_invalid
                else self.spectrum.control_sets()} if self._editable else {}

    def set_values(self, values):
        if self._editable and 'control_sets' in values:
            try:
                self.spectrum.set_control_sets(values['control_sets'])
                self._invalid_draft = None
                self._draft_invalid = False
            except (ValueError, TypeError) as exc:
                self._invalid_draft = deepcopy(values['control_sets'])
                self._draft_invalid = True
                self.spectrum.status.setText(str(exc))
                self.spectrum.status.show()
            self.changed.emit()

    @property
    def can_continue(self):
        return not self._draft_invalid and self.spectrum.can_continue

    @property
    def action_label(self):
        return 'Apply set changes' if self._editable and self.spectrum.composition_changed else 'Use AF sets'

    def _composition_changed(self):
        self._invalid_draft = None
        self._draft_invalid = False
        self.changed.emit()

    def validation_errors(self):
        return ({'control_sets': self.spectrum.status.text() or 'Choose at least one usable AF set.'}
            if self._editable and not self.can_continue else {})

    def reveal(self, _name):
        self.spectrum.setFocus(Qt.FocusReason.OtherFocusReason)


class ReferenceControlAssignmentWorkspace(_PreparationWorkspace):
    """Assign reviewed AF sets without repeating their spectral review."""
    revision_requested = Signal(str)
    def __init__(self, review, values, parent=None):
        super().__init__(parent)
        self.setObjectName('reference-control-assignment-workspace')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._review = review
        self._fields = dict(review['assignment_fields'])
        self._sets = {row['set_id']: row for row in review['sets']}
        self._regions = {row['roi_id']: row for row in review['positive_regions']}
        self._values = {name: values[name] for name in self._fields if name in values}
        self.validation_editors, self.field_errors = {}, {}
        self._indices = {name: index for index, name in enumerate(self._fields)}
        self._choice_count = len(self._sets) + 1
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)

        self.surface = QFrame(self)
        self.surface.setObjectName('reference-assignment-surface')
        self.surface.setStyleSheet('QFrame#reference-assignment-surface {'
            'background: #ffffff; border: 1px solid #d5e0df; border-radius: 8px; }')
        content = QVBoxLayout(self.surface)
        content.setContentsMargins(20, 20, 20, 20)
        content.setSpacing(20)
        self._source_context = QGridLayout()
        self._source_context.setHorizontalSpacing(28)
        self._source_context.setVerticalSpacing(3)
        self._source_context.setColumnStretch(0, 1)
        self._source_context.setColumnStretch(1, 1)
        self._control_filenames = tuple(dict.fromkeys(row.get('control_filename', 'Filename unavailable')
            for row in self._sets.values()))
        stained_filename = review.get('stained_filename') or next((row.get('positive_filename')
            for row in self._regions.values() if row.get('positive_filename')), 'Stained image')
        self.stained_source = self._add_source_context(0, 'Stained image', stained_filename,
            'Edit regions…', review.get('edit_regions_option'))
        self.control_source = self._add_source_context(1, 'AF control image' if len(self._control_filenames) == 1 else 'AF control images',
            self._control_filenames[0] if len(self._control_filenames) == 1 else
            f'{len(self._control_filenames)} control images' if self._control_filenames else 'No reviewed AF sets',
            'Review AF sets…', review.get('review_sets_option'))
        content.addLayout(self._source_context)
        layout.addWidget(self.surface)
        self.table = QTableWidget(len(self._fields), 2, self.surface)
        self.table.setObjectName('reference-control-assignment-table')
        self.table.setAccessibleName('AF control set for each stained region')
        self.table.setHorizontalHeaderLabels(('Stained region', 'AF control set'))
        reference_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in range(2):
            self.table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for row, (name, identity) in enumerate(self._fields.items()):
            region = self._regions[identity]
            item = QTableWidgetItem(region['label'])
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(region['label'])
            self.table.setItem(row, 0, item)
            cell = QWidget(self.table)
            cell_layout = QVBoxLayout(cell); cell_layout.setContentsMargins(0, 0, 0, 0); cell_layout.setSpacing(0)
            editor = decision_combo(cell, [('Choose AF control set', None),
                *((self._set_label(control), control['set_id']) for control in review['sets'])])
            editor.setObjectName(name)
            editor.setAccessibleName(f"AF control set for {region['label']}")
            editor.setMinimumHeight(44)
            editor.setMinimumWidth(0)
            editor.setSizeAdjustPolicy(editor.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            editor.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            cell_layout.addWidget(editor)
            error = text_label('', cell); error.setObjectName('error-' + name)
            error.setProperty('validationMessage', True); error.setContentsMargins(8, 0, 8, 6); error.hide()
            error.installEventFilter(self)
            cell_layout.addWidget(error)
            self.table.setCellWidget(row, 1, cell)
            self.validation_editors[name], self.field_errors[name] = editor, error
            self._restore_editor(name)
            editor.currentIndexChanged.connect(lambda _index, field=name: self._assignment_changed(field))
        content.addWidget(self.table)
        if self._fields:
            self.table.setCurrentCell(0, 0)
        self._fit_table()

    def _add_source_context(self, column, role, filename, action_label, option):
        self._source_context.addWidget(text_label(role, self.surface, heading=True), 0, column)
        row = QHBoxLayout()
        row.setSpacing(8)
        name = text_label(filename, self.surface)
        name.setMinimumWidth(0)
        row.addWidget(name, 1)
        if option:
            button = QToolButton(self.surface)
            button.setText(action_label)
            button.setObjectName('reference-revision-' + option)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setStyleSheet('QToolButton { color: #007b84; border: none; background: transparent; padding: 6px 0px; }')
            button.clicked.connect(lambda: self.revision_requested.emit(option))
            row.addWidget(button)
        self._source_context.addLayout(row, 1, column)
        return name

    def _set_label(self, control):
        return control['label'] + (' · ' + control.get('control_filename', 'Filename unavailable')
            if len(self._control_filenames) > 1 else '')

    def _set_tooltip(self, identity):
        control = self._sets.get(identity) if isinstance(identity, str) else None
        return (' · '.join(filter(None, (control.get('control_filename'),
            ', '.join(member['label'] for member in control.get('regions', ())))))
            if control else str(identity or ''))

    def owns_control(self, name):
        return name in self._fields

    def values(self):
        return dict(self._values)

    def set_values(self, values):
        # Restoration replaces the mapping snapshot. Omitted or explicit empty
        # draft fields must not inherit a fresh form's singleton default.
        self._values = {name: values[name] for name in self._fields if name in values}
        for name in self._fields:
            self._restore_editor(name)
        self._present_errors()

    def _restore_editor(self, name):
        editor = self.validation_editors[name]
        blocker = QSignalBlocker(editor)
        while editor.count() > self._choice_count:
            editor.removeItem(editor.count() - 1)
        value = self._values.get(name)
        index = editor.findData(value)
        if index < 0:
            label = 'Choose AF control set' if value == '' else 'Unavailable AF set'
            editor.addItem(label, value); index = editor.count() - 1
        for choice in range(editor.count()):
            editor.setItemData(choice, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                               Qt.ItemDataRole.TextAlignmentRole)
        editor.setCurrentIndex(index)
        for choice in range(editor.count()):
            editor.setItemData(choice, self._set_tooltip(editor.itemData(choice)), Qt.ItemDataRole.ToolTipRole)
        editor.setToolTip(self._set_tooltip(value))
        del blocker

    def validation_errors(self):
        errors = {}
        for name in self._fields:
            value = self._values.get(name)
            if value is None or value == '':
                errors[name] = 'Choose an AF control set.'
            elif not isinstance(value, str) or value not in self._sets:
                errors[name] = 'This saved AF set is unavailable. Choose a reviewed set.'
        return errors

    def _present_errors(self):
        errors = self.validation_errors()
        for name, label in self.field_errors.items():
            label.setText(errors.get(name, '')); label.setVisible(name in errors)
        self._fit_table()

    def _assignment_changed(self, name):
        editor = self.validation_editors[name]
        self._values[name] = editor.currentData()
        editor.setToolTip(self._set_tooltip(editor.currentData()))
        self._present_errors()
        self.table.setCurrentCell(self._indices[name], 1)

    def reveal(self, name):
        row = self._indices[name]
        self.table.setCurrentCell(row, 1)
        self.table.scrollToItem(self.table.item(row, 0))
        self.validation_editors[name].setFocus(Qt.FocusReason.OtherFocusReason)

    def eventFilter(self, watched, event):
        if watched in self.field_errors.values() and event.type() in {QEvent.Type.Show, QEvent.Type.Hide}:
            QTimer.singleShot(0, self, self._fit_table)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_table()

    def _fit_table(self):
        width = max(100, self.table.columnWidth(1))
        for name, row in self._indices.items():
            error = self.field_errors[name]
            extra = 0 if error.isHidden() else max(error.fontMetrics().height() + 6, error.heightForWidth(width))
            self.table.setRowHeight(row, 46 + extra)
        self.table.setFixedHeight(min(390, self.table.horizontalHeader().height() +
            sum(self.table.rowHeight(row) for row in range(self.table.rowCount())) +
            2 * self.table.frameWidth() + 2))
