"""Cycle-only source organization using the shared spectral-review plot controls."""
from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout,
    QLabel, QSpinBox, QVBoxLayout, QWidget)
from .channel_table import ChannelCycleReview
from .researcher_widgets import ContentSizedButton


class AcquisitionOrganizationEditor(QWidget):
    changed = Signal()

    def __init__(self, owner, value, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.temporal = owner['source_size_t'] > 1
        self._synchronizing = False
        self._cycle_history = []
        self._cycle_snapshot = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.layout_choice = QComboBox(self)
        self.layout_choice.addItem('Choose what the source T positions represent', None)
        self.layout_choice.addItem('Imaging cycles', 't_cycles')
        self.layout_choice.addItem('A time series or mixed time/cycle dimensions', 'time')
        self.layout_choice.setVisible(self.temporal)
        layout.addWidget(self.layout_choice)
        self.registered = QCheckBox('The cycles are already registered to the same spatial grid', self)
        self.registered.setVisible(self.temporal)
        layout.addWidget(self.registered)
        self.explanation = QLabel(self)
        self.explanation.setWordWrap(True)
        self.explanation.setProperty('choiceHelp', True)
        layout.addWidget(self.explanation)
        self.panel = ChannelCycleReview(owner['plane_rows'], value['channel_cycle_values'],
            owner.get('source_channel_cycles'), self, source_series_index=owner['source_series_index'],
            embedded=True)
        self.panel.cyclic.setChecked(True)
        self.panel.cyclic.hide()
        self.selection_bar = QWidget(self)
        actions = QHBoxLayout(self.selection_bar)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.selection_summary = QLabel(self)
        self.selection_summary.setProperty('choiceHelp', True)
        self.selection_summary.setToolTip('Use Ctrl-click or Shift-click to select several channel rows.')
        actions.addWidget(self.selection_summary, 1)
        self.bulk_label = QLabel('Cycle', self)
        self.bulk_cycle = QSpinBox(self)
        self.bulk_cycle.setRange(1, 2147483647)
        self.bulk_cycle.setFixedWidth(88)
        self.bulk_cycle.setAccessibleName('Cycle for selected acquired channels')
        self.assign_selected = ContentSizedButton('Assign', self)
        self.assign_selected.setObjectName('assign-selected-channel-cycles')
        self.assign_selected.setToolTip('Select channel rows in the table, then assign the chosen cycle to those rows.')
        self.assign_selected.clicked.connect(self._assign_selected)
        for widget in (self.bulk_label, self.bulk_cycle, self.assign_selected):
            actions.addWidget(widget)
            widget.ensurePolished()
        self.selection_bar.setMinimumHeight(max(max(widget.minimumHeight(), widget.sizeHint().height())
            for widget in (self.bulk_cycle, self.assign_selected)))
        self.selection_bar.setVisible(not self.temporal)
        self.panel.table_footer.addWidget(self.selection_bar)
        navigation = QHBoxLayout()
        self.undo_assignment = ContentSizedButton('Undo', self)
        self.undo_assignment.setObjectName('undo-channel-cycle-assignment')
        self.undo_assignment.setToolTip('Undo the last cycle edit or selected-channel assignment (Ctrl+Z).')
        self.undo_assignment.clicked.connect(self._undo_cycle_edit)
        navigation.addWidget(self.undo_assignment)
        navigation.addStretch()
        self.next_unassigned = ContentSizedButton('Next unassigned', self)
        self.next_unassigned.setObjectName('next-unassigned-cycle')
        self.next_unassigned.clicked.connect(self._next_unassigned)
        navigation.addWidget(self.next_unassigned)
        self.panel.table_footer.addLayout(navigation)
        self.panel.table.itemSelectionChanged.connect(self._refresh_actions)
        layout.addWidget(self.panel)
        self.layout_choice.currentIndexChanged.connect(self._layout_changed)
        self.registered.toggled.connect(self.changed)
        for index, editor in enumerate(self.panel._editors):
            editor.installEventFilter(self)
            editor.textChanged.connect(lambda text, index=index: self._cycle_changed(index, text))
        undo = QShortcut(QKeySequence.StandardKey.Undo, self)
        undo.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo.activated.connect(self._undo_cycle_edit)
        self.changed.connect(self._refresh_actions)
        self.set_value(value)

    def _refresh_actions(self, *_):
        self.undo_assignment.setEnabled(bool(self._cycle_history) and self.panel.isEnabled())
        selected = len(self.panel.table.selectionModel().selectedRows())
        multiple = selected > 1 and not self.temporal
        for widget in (self.bulk_label, self.bulk_cycle, self.assign_selected):
            widget.setVisible(multiple)
        self.assign_selected.setEnabled(multiple)
        self.selection_summary.setText(f'{selected} selected' if multiple else
            '1 channel selected' if selected else 'Ctrl/Shift-click rows to select several')
        self.next_unassigned.setEnabled(self.panel.isEnabled()
            and any(not editor.text().strip() for editor in self.panel._editors))

    def _focus_row(self, row):
        self.panel._focus_channel(row)

    def _assign_selected(self):
        if self.temporal:
            return
        self._synchronizing = True
        try:
            for selected in self.panel.table.selectionModel().selectedRows():
                self.panel._editors[selected.row()].setText(str(self.bulk_cycle.value()))
        finally:
            self._synchronizing = False
        self._record_cycle_edit()
        self.changed.emit()

    def _record_cycle_edit(self):
        values = tuple(editor.text() for editor in self.panel._editors)
        if values != self._cycle_snapshot:
            self._cycle_history.append(self._cycle_snapshot)
            self._cycle_snapshot = values

    def _undo_cycle_edit(self):
        if not self._cycle_history or not self.panel.isEnabled():
            return
        values = self._cycle_history.pop()
        self._synchronizing = True
        try:
            for editor, text in zip(self.panel._editors, values, strict=True):
                editor.setText(text)
        finally:
            self._synchronizing = False
        self._cycle_snapshot = values
        self.panel._refresh_groups()
        self.changed.emit()

    def eventFilter(self, watched, event):
        # A cell's native text undo must not split a multi-channel assignment.
        if watched in self.panel._editors and event.type() in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress}:
            if event.matches(QKeySequence.StandardKey.Undo):
                if event.type() == QEvent.Type.KeyPress:
                    self._undo_cycle_edit()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _next_unassigned(self):
        missing = [row for row, editor in enumerate(self.panel._editors) if not editor.text().strip()]
        if missing:
            current = self.panel.table.currentRow()
            self._focus_row(next((row for row in missing if row > current), missing[0]))

    def _layout_changed(self, *_, fill_missing=True):
        selected = self.layout_choice.currentData()
        self.panel.setEnabled(not self.temporal or selected == 't_cycles')
        if self.temporal and selected == 't_cycles':
            self.explanation.setText('Each original T position supplies one cycle and all of its acquired bands. The source order is preserved.')
            if fill_missing and all(not editor.text().strip() for editor in self.panel._editors):
                self._synchronizing = True
                for row, editor in zip(self.owner['plane_rows'], self.panel._editors, strict=True):
                    editor.setText(str(row['source_t'] + 1))
                self._synchronizing = False
                self.panel._refresh_groups()
        elif self.temporal and selected == 'time':
            self.explanation.setText('This analysis path does not process time series or mixed time/cycle dimensions. Export a registered imaging-cycle image before continuing.')
        elif self.temporal:
            self.explanation.setText('T identifies a source position. Confirm what it represents before assigning cycles.')
        elif self.owner.get('previous_authority_sha256'):
            self.explanation.setText('Your previous cycle review is filled in. Confirm the grouping for this inspection revision.')
        elif self.owner.get('source_channel_cycles') is None and any(row['cycle'] is not None for row in self.owner['channel_cycle_values']):
            self.explanation.setText('Cycle assignments from the stored review are filled in. Confirm the grouping for this inspection.')
        elif self.owner.get('source_channel_cycles') is None:
            self.explanation.setText('Assign the acquired channels to cycles. Unknown cycles may remain blank for a prepared image.')
        else:
            self.explanation.setText('Review the recorded cycle assignments. Unknown cycles may remain blank for a prepared image.')
        self._cycle_history.clear()
        self._cycle_snapshot = tuple(editor.text() for editor in self.panel._editors)
        self.changed.emit()

    def _cycle_changed(self, index, text):
        if self._synchronizing:
            return
        if self.temporal:
            self._synchronizing = True
            source_t = self.owner['plane_rows'][index]['source_t']
            for row, editor in zip(self.owner['plane_rows'], self.panel._editors, strict=True):
                if row['source_t'] == source_t and editor.text() != text:
                    editor.setText(text)
            self._synchronizing = False
            self.panel._refresh_groups()
        self._record_cycle_edit()
        self.changed.emit()

    def value(self):
        return dict(layout=self.layout_choice.currentData() if self.temporal else 'planar_channels',
            registered=self.registered.isChecked() if self.temporal else False,
            channel_cycle_values=self.panel.value())

    def set_value(self, value):
        self._synchronizing = True
        values = {row['channel_id']: row['cycle'] for row in value['channel_cycle_values']}
        for row, editor in zip(self.owner['plane_rows'], self.panel._editors, strict=True):
            number = values.get(row['channel_id'])
            editor.setText('' if number is None else str(number + 1))
        blocker = QSignalBlocker(self.layout_choice)
        self.layout_choice.setCurrentIndex(self.layout_choice.findData(value.get('layout')))
        del blocker
        self.registered.setChecked(value.get('registered', False))
        self._synchronizing = False
        self._layout_changed(fill_missing=False)
        self.panel._refresh_groups()
