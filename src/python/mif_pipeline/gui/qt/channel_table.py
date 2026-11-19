"""Compact Stage-1 presentation using the existing field widgets and values.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import csv
import io
import re
from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal, QSignalBlocker, QItemSelectionModel, QPersistentModelIndex
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QIntValidator, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem, QWidget,
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QVBoxLayout,
    QDialogButtonBox, QGridLayout, QSizePolicy, QApplication, QStyledItemDelegate,
    QStyle, QMenu, QFrame,
)
from ..interactions import FormModel
from .researcher_widgets import ContentSizedButton

# ==============================================================================
#                                 Channel table
# ==============================================================================


def _style_cell_editor(editor):
    """Keep the form's own editor, but make its focus edge follow the cell."""
    editor.setStyleSheet(
        'QLineEdit, QComboBox { border: 1px solid transparent; border-radius: 0; '
        'padding: 0 9px; min-height: ' + str(max(36, editor.fontMetrics().height() + 16)) + 'px; background: transparent; }'
        'QLineEdit[activeTableRow="true"], QComboBox[activeTableRow="true"] { background: #d5ebe5; }'
        'QLineEdit:focus, QComboBox:focus { border-color: #006b70; background: #ffffff; }'
        'QComboBox { padding-right: 30px; }'
    )
    editor.setMinimumHeight(max(38, editor.fontMetrics().height() + 18))
    editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


def _highlight_field_row(table, current):
    """Show the active form row without changing table-selection semantics."""
    for row in range(table.rowCount()):
        active = row == current
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                item.setData(Qt.ItemDataRole.BackgroundRole, QBrush(QColor('#d5ebe5')) if active else None)
            field = table.cellWidget(row, column)
            if field is not None and field.property('activeTableRow') != active:
                field.setProperty('activeTableRow', active)
                field.style().unpolish(field)
                field.style().polish(field)
                field.update()


class WrappedTableHeader(QHeaderView):
    """The Samples table's clear headings, with wrapping when space requires it."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumSectionSize(64)
        self.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        font = self.font()
        font.setBold(True)
        self.setFont(font)
        self.discovery = None

    def sizeHint(self):
        size = super().sizeHint()
        height = self.fontMetrics().lineSpacing()
        if self.model() is not None:
            for column in range(self.count()):
                caption = str(self.model().headerData(column, self.orientation()) or "")
                bounds = self.fontMetrics().boundingRect(
                    QRect(0, 0, max(1, self.sectionSize(column) - 24), 1000),
                    Qt.TextFlag.TextWordWrap, caption)
                height = max(height, bounds.height())
        size.setHeight(height + 18)
        return size

    def paintSection(self, painter, rect, logical_index):
        if not rect.isValid():
            return
        painter.save()
        painter.setClipRect(rect)
        if self.discovery is None or not self.discovery.isVisible():
            background = QPainterPath()
            background.addRoundedRect(0, 0, self.viewport().width(), rect.height() + 8, 7, 7)
            painter.fillPath(background, QColor("#f0f4f3"))
        else:
            painter.fillRect(rect, QColor("#f0f4f3"))
        painter.setPen(QColor("#d5ded9"))
        painter.drawLine(rect.topRight(), rect.bottomRight())
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.setFont(self.font())
        painter.setPen(QColor("#294148"))
        text = self.model().headerData(
            logical_index, self.orientation(), Qt.ItemDataRole.DisplayRole
        )
        alignment = self.model().headerData(
            logical_index, self.orientation(), Qt.ItemDataRole.TextAlignmentRole
        )
        painter.drawText(
            rect.adjusted(12, 8, -12, -8),
            (Qt.AlignmentFlag(alignment) if alignment is not None else self.defaultAlignment())
            | Qt.TextFlag.TextWordWrap,
            str(text or ""),
        )
        painter.restore()


class AssignmentTableSurface(QFrame):
    """A bounded table surface; its action row stays outside, as on Samples."""

    def __init__(self, table, discovery, parent=None):
        super().__init__(parent)
        self.setProperty("workbenchSection", True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        discovery.layout().setContentsMargins(12, 10, 12, 10)
        table.horizontalHeader().discovery = discovery
        layout.addWidget(discovery)
        layout.addWidget(table)


class _AssignmentDelegate(QStyledItemDelegate):
    """Only the active cell has a visible editor; typed form controls own values."""

    def createEditor(self, parent, option, index):
        table = self.parent()
        source = table.editor_for_cell(index.row(), index.column())
        if source is None or not source.isEnabled():
            return None
        if isinstance(source, QComboBox):
            editor = QComboBox(parent)
            editor.setObjectName("assignment-cell-editor")
            for i in range(source.count()):
                editor.addItem(source.itemText(i), source.itemData(i))
            editor.setStyleSheet(
                "QComboBox#assignment-cell-editor, QComboBox#assignment-cell-editor:focus { "
                "border: 1px solid #006b70; border-radius: 0; "
                "padding: 0 30px 0 11px; min-height: 0; background: #ffffff; }")
        else:
            editor = QLineEdit(parent)
            editor.setObjectName("assignment-cell-editor")
            editor.setPlaceholderText(source.placeholderText())
            editor.setStyleSheet(
                "QLineEdit#assignment-cell-editor, QLineEdit#assignment-cell-editor:focus { "
                "border: 1px solid #006b70; border-radius: 0; "
                "padding: 0 11px; min-height: 0; background: #ffffff; }")
        editor.setAccessibleName(source.accessibleName())
        editor.setAccessibleDescription(source.accessibleDescription())
        table._active_editor = (editor, QPersistentModelIndex(index))
        return editor

    def setEditorData(self, editor, index):
        source = self.parent().editor_for_cell(index.row(), index.column())
        if isinstance(editor, QComboBox):
            editor.setCurrentIndex(editor.findData(source.currentData()))
        else:
            editor.setText(source.text())
            editor.selectAll()

    def setModelData(self, editor, model, index):
        value = editor.currentData() if isinstance(editor, QComboBox) else editor.text()
        self.parent()._apply_changes([(index.row(), index.column(), value)])

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def destroyEditor(self, editor, index):
        table = self.parent()
        if table._active_editor is not None and table._active_editor[0] is editor:
            table._active_editor = None
        super().destroyEditor(editor, index)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress and event.matches(QKeySequence.StandardKey.Paste):
            text = QApplication.clipboard().text()
            if any(separator in text for separator in ("\t", "\n", "\r")):
                table = self.parent()
                table.commit_pending()
                table.paste_values()
                return True
        return super().eventFilter(editor, event)

    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        source = self.parent().editor_for_cell(index.row(), index.column())
        color = QColor("#172d35")
        if not option.text and source is not None:
            option.text = (source.placeholderText() if isinstance(source, QLineEdit)
                           else "Choose…") if source.isEnabled() else "—"
            color = QColor("#77888c")
        elif source is not None and not source.isEnabled():
            color = QColor("#60747b")
        background = index.data(Qt.ItemDataRole.BackgroundRole)
        flagged = background is not None and background.style() != Qt.BrushStyle.NoBrush
        if option.state & QStyle.StateFlag.State_Selected:
            background = QColor("#d5ebe5")
        elif not flagged:
            background = QColor("#e5f0ec" if index.row() == self.parent().currentRow()
                                else "#f3f5f5" if index.row() % 2 else "#ffffff")
        painter.save()
        painter.fillRect(option.rect, background)
        painter.setPen(QColor("#edf3f1"))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        choice = isinstance(source, QComboBox) and source.isEnabled()
        rect = option.rect.adjusted(12, 0, -32 if choice else -12, 0)
        painter.setFont(option.font)
        painter.setPen(color)
        text = option.fontMetrics.elidedText(option.text, option.textElideMode, rect.width())
        painter.drawText(rect, option.displayAlignment, text)
        if choice:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#486169"), 1.4, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            x, y = option.rect.right() - 15, option.rect.center().y()
            painter.drawLine(x - 3, y - 1, x, y + 2)
            painter.drawLine(x, y + 2, x + 3, y - 1)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QPen(QColor("#006b70"), 1))
            painter.drawRect(option.rect.adjusted(0, 0, -1, -1))
        painter.restore()


class _AssignmentGrid(QTableWidget):
    """Flat channel/component cells, with atomic clipboard edits and local undo."""
    data_edited = Signal()
    feedback = Signal(str)
    history_changed = Signal()

    def __init__(self, rows, columns, parent=None):
        super().__init__(rows, columns, parent)
        self._bound = {}
        self._active_editor = None
        self._applying = False
        self._undo = []
        self._redo = []
        self._visible_rows = None
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self.fit_height)
        self._control_store = QWidget(self)
        self._control_store.hide()
        self.setProperty("scientificTable", True)
        self.setStyleSheet("QTableWidget { border: none; border-radius: 0; padding: 0; } QTableWidget::item { padding: 0; }")
        self.setHorizontalHeader(WrappedTableHeader(self))
        # Section resize signals can precede Qt's final stretch widths. Fit once
        # those widths settle, so a transient narrow column does not leave a tall header.
        self.horizontalHeader().sectionResized.connect(lambda *_: self._height_timer.start(0))
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed
                             | QTableWidget.EditTrigger.AnyKeyPressed)
        self.setTabKeyNavigation(True)
        self.setWordWrap(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(_AssignmentDelegate(self))
        self.currentCellChanged.connect(lambda *_: self.viewport().update())
        for key, callback in ((QKeySequence.StandardKey.Copy, self.copy_values),
                              (QKeySequence.StandardKey.Paste, self.paste_values),
                              (QKeySequence.StandardKey.Undo, self.undo_edit),
                              (QKeySequence.StandardKey.Redo, self.redo_edit)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)

    def fit_height(self, visible_rows=None):
        if visible_rows is not None:
            self._visible_rows = max(1, visible_rows)
        if self._visible_rows is not None:
            height = self.horizontalHeader().sizeHint().height()
            self.horizontalHeader().setFixedHeight(height)
            self.setFixedHeight(height + self._visible_rows * self.verticalHeader().defaultSectionSize())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def bind_editor(self, row, column, editor):
        item = QTableWidgetItem()
        self.setItem(row, column, item)
        self._bound[editor] = item
        editor.setParent(self._control_store)
        editor.hide()
        editor.installEventFilter(self)
        signal = editor.currentIndexChanged if isinstance(editor, QComboBox) else editor.textChanged
        signal.connect(lambda *_: self._source_changed(editor))
        self._sync_cell(editor)

    def editor_for_cell(self, row, column):
        item = self.item(row, column)
        return next((editor for editor, bound in self._bound.items() if bound is item), None)

    def _source_changed(self, editor):
        self._sync_cell(editor)
        if not self._applying:
            self.data_edited.emit()

    def _sync_cell(self, editor):
        item = self._bound[editor]
        text = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        item.setText(str(editor.property("cellDisplay") or text))
        item.setToolTip(editor.toolTip() or text)
        editable = editor.isEnabled() and not (isinstance(editor, QLineEdit) and editor.isReadOnly())
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                      | (Qt.ItemFlag.ItemIsEditable if editable else Qt.ItemFlag.NoItemFlags))
        item.setForeground(QBrush(QColor("#667777")) if not text else QBrush())

    def refresh_cells(self):
        for editor in self._bound:
            self._sync_cell(editor)
        self.viewport().update()

    def eventFilter(self, watched, event):
        if watched in self._bound and event.type() == QEvent.Type.EnabledChange:
            self._sync_cell(watched)
        return super().eventFilter(watched, event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and index.isValid() and index.flags() & Qt.ItemFlag.ItemIsEditable
                and len(self.selectedIndexes()) == 1):
            self.edit(index)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        for label, callback in (("Copy", self.copy_values), ("Paste", self.paste_values),
                                ("Undo", self.undo_edit), ("Redo", self.redo_edit)):
            action = menu.addAction(label)
            action.triggered.connect(callback)
            if label in {"Undo", "Redo"}:
                action.setEnabled(bool(self._undo if label == "Undo" else self._redo))
        menu.exec(event.globalPos())

    def commit_pending(self):
        if self._active_editor is not None:
            editor, index = self._active_editor
            self._active_editor = None
            if index.isValid():
                self.itemDelegate().setModelData(editor, self.model(), index)
            self.itemDelegate().closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)

    @staticmethod
    def _value(editor):
        return editor.currentData() if isinstance(editor, QComboBox) else editor.text()

    def _snapshot(self):
        return [(editor, self._value(editor)) for editor in self._bound]

    def _restore(self, values):
        self._applying = True
        changed = []
        try:
            for editor, value in values:
                if editor not in self._bound or self._value(editor) == value:
                    continue
                blocker = QSignalBlocker(editor)
                if isinstance(editor, QComboBox):
                    editor.setCurrentIndex(editor.findData(value))
                else:
                    editor.setText(value)
                del blocker
                changed.append(editor)
            # All values are present before existing role/identity observers run.
            for editor in changed:
                if isinstance(editor, QComboBox):
                    editor.currentIndexChanged.emit(editor.currentIndex())
                else:
                    editor.textChanged.emit(editor.text())
            self.refresh_cells()
        finally:
            self._applying = False
        self.data_edited.emit()

    def _apply_changes(self, changes):
        before = self._snapshot()
        self._restore([(self.editor_for_cell(row, column), value) for row, column, value in changes])
        if before != self._snapshot():
            self._undo.append(before)
            self._redo.clear()
            self.history_changed.emit()
        self.feedback.emit("")

    def undo_edit(self):
        self.commit_pending()
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())
            self.history_changed.emit()

    def redo_edit(self):
        self.commit_pending()
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())
            self.history_changed.emit()

    def clear_history(self):
        self._undo.clear()
        self._redo.clear()
        self.history_changed.emit()

    def _rectangle(self):
        indexes = [index for index in self.selectedIndexes() if not self.isRowHidden(index.row())]
        if not indexes:
            raise ValueError("Select a cell or a rectangular range first.")
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        visible = [row for row in range(rows[0], rows[-1] + 1) if not self.isRowHidden(row)]
        if (rows != visible or columns != list(range(columns[0], columns[-1] + 1))
                or len(indexes) != len(rows) * len(columns)):
            raise ValueError("Select one rectangular range to copy or paste.")
        return rows, columns

    def copy_values(self):
        self.commit_pending()
        try:
            rows, columns = self._rectangle()
        except ValueError as error:
            self.feedback.emit(str(error))
            return False
        if len(rows) * len(columns) == 1:
            text = self.item(rows[0], columns[0]).text()
        else:
            stream = io.StringIO()
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow([self.horizontalHeaderItem(column).text() for column in columns])
            writer.writerows([[self.item(row, column).text() for column in columns] for row in rows])
            text = stream.getvalue()
        QApplication.clipboard().setText(text)
        self.feedback.emit("")
        return True

    def paste_values(self):
        self.commit_pending()
        try:
            rows, columns = self._rectangle()
            text = QApplication.clipboard().text()
            block = list(csv.reader(io.StringIO(text), delimiter="\t"))
            if not block or not block[0] or any(len(row) != len(block[0]) for row in block):
                raise ValueError("Paste a rectangle with the same number of columns in every row.")
            headers = [self.horizontalHeaderItem(i).text() for i in range(self.columnCount())]
            if all(cell in headers for cell in block[0]) and len(set(block[0])) == len(block[0]):
                columns = [headers.index(cell) for cell in block.pop(0)]
                if not block:
                    raise ValueError("The clipboard contains headings but no values.")
            elif len(block) == len(block[0]) == 1:
                block = [[block[0][0]] * len(columns) for _ in rows]
            else:
                columns = list(range(columns[0], columns[0] + len(block[0])))
            visible = [row for row in range(rows[0], self.rowCount()) if not self.isRowHidden(row)]
            if len(block) > len(visible) or max(columns) >= self.columnCount():
                raise ValueError("Paste into the existing table rows and columns.")
            planned = []
            for row, values in zip(visible, block):
                for column, text in zip(columns, values):
                    item = self.item(row, column)
                    editor = self.editor_for_cell(row, column)
                    if not item.flags() & Qt.ItemFlag.ItemIsEditable and text.strip() == item.text():
                        value = self._value(editor) if editor is not None else text.strip()
                    elif isinstance(editor, QComboBox):
                        matches = [i for i in range(editor.count())
                                   if text.strip().casefold() in {editor.itemText(i).casefold(),
                                       str(editor.itemData(i)).casefold()}]
                        if not matches:
                            raise ValueError(f"Choose an offered {self.horizontalHeaderItem(column).text().lower()} in row {row + 1}.")
                        value = editor.itemData(matches[0])
                    else:
                        value = text.strip()
                    planned.append((row, column, item, editor, text.strip(), value))
            prospective = {editor: value for _row, _column, _item, editor, _text, value in planned
                           if editor is not None}
            changes = []
            for row, column, item, editor, text, value in planned:
                editable = bool(item.flags() & Qt.ItemFlag.ItemIsEditable)
                assignment = getattr(editor, "_marker_assignment", None)
                if assignment is not None:
                    assigned = prospective.get(assignment, assignment.currentData()) == "assigned"
                    if value and not assigned:
                        raise ValueError(f"A marker name requires Marker assigned in row {row + 1}.")
                    editable = assignment.isEnabled() and (assigned or not value)
                if not editable:
                    if text != item.text():
                        raise ValueError(f"{self.horizontalHeaderItem(column).text()} is read-only in row {row + 1}.")
                    continue
                changes.append((row, column, value))
            self._apply_changes(changes)
            return True
        except (ValueError, csv.Error) as error:
            self.feedback.emit(str(error))
            return False

    def forget_row(self, row):
        for editor, item in list(self._bound.items()):
            if item.row() == row:
                del self._bound[editor]
                editor.deleteLater()
        self.removeRow(row)
        self.clear_history()

    def reset_rows(self):
        if self._active_editor is not None:
            self.commit_pending()
        for editor in self._bound:
            editor.deleteLater()
        self._bound.clear()
        self.setRowCount(0)
        self.clear_history()


class AssignmentTableTools(QWidget):
    """Compact actions; discovery controls only appear for long inventories."""

    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.table = table
        table.tools = self
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.discovery = QWidget(self)
        discovery = QHBoxLayout(self.discovery)
        discovery.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(self.discovery)
        self.search.setPlaceholderText("Find a channel, component or marker")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Find a channel, component or marker")
        discovery.addWidget(self.search, 1)
        self.needs_review = QCheckBox("Incomplete rows", self.discovery)
        self.needs_review.setToolTip("Show rows with missing editable labels, assignments, marker names or component cycles.")
        discovery.addWidget(self.needs_review)
        self.match_count = QLabel(self.discovery)
        self.match_count.setProperty("choiceHelp", True)
        discovery.addWidget(self.match_count)
        layout.addWidget(self.discovery)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addStretch()
        layout.addLayout(self.actions)
        self.message = QLabel(self)
        self.message.setProperty("validationMessage", True)
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        table.feedback.connect(self._feedback)
        self.search.textChanged.connect(self.filter_rows)
        self.needs_review.toggled.connect(self.filter_rows)
        table.data_edited.connect(self.filter_rows)
        self.filter_rows()

    def _feedback(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def filter_rows(self, *_):
        long = self.table.rowCount() > 10
        self.discovery.setVisible(long)
        text = self.search.text().strip().casefold() if long else ""
        shown = 0
        for row in range(self.table.rowCount()):
            match = any(text in self.table.item(row, column).text().casefold()
                        for column in range(self.table.columnCount()) if self.table.item(row, column))
            review = any((isinstance(editor, QLineEdit) and editor.isEnabled() and not editor.text().strip())
                         or (isinstance(editor, QComboBox) and editor.currentData() in {None, "missing"})
                         for column in range(self.table.columnCount())
                         if (editor := self.table.editor_for_cell(row, column)) is not None)
            show = match and (not long or not self.needs_review.isChecked() or review)
            self.table.setRowHidden(row, not show)
            shown += int(show)
        self.match_count.setText(f"{shown} of {self.table.rowCount()}")
        self.match_count.setVisible(bool(text) or self.needs_review.isChecked())


class ComponentAssignmentTable(QWidget):
    """Editable component inventory; rows never add acquired measurement bands."""
    changed = Signal()

    def __init__(self, values=(), parent=None, *, af_retained=True, cycles=()):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.af_retained = af_retained
        self.cycles = tuple(sorted(set(cycles or ())))
        self._nuclear_identity = None
        self._assignment_column = 2 if self.cycles else 1
        self._marker_column = self._assignment_column + 1
        self.setObjectName("component-assignment-table")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.table = _AssignmentGrid(0, 4 if self.cycles else 3, self)
        self.table.setAccessibleName("Output components and marker assignments")
        self.table.setAccessibleDescription("Click a cell or press F2 to edit. Tab moves between cells. "
            "Use Shift to select a range and Ctrl+C or Ctrl+V to copy or paste.")
        self._configure_columns()
        self.tools = AssignmentTableTools(self.table, self)
        self.surface = AssignmentTableSurface(self.table, self.tools.discovery, self)
        layout.addWidget(self.surface)
        layout.addWidget(self.tools)
        add = ContentSizedButton("Add component", self)
        add.setObjectName("add-output-component")
        add.clicked.connect(self._add_component)
        self.remove = ContentSizedButton("Remove", self)
        self.remove.setObjectName("remove-output-components")
        self.remove.setToolTip("Remove selected components. Retained autofluorescence cannot be removed.")
        self.remove.clicked.connect(self._remove_selected)
        self.tools.actions.insertWidget(0, add)
        self.tools.actions.insertWidget(1, self.remove)
        self.table.itemSelectionChanged.connect(self._update_remove)
        self.table.data_edited.connect(self.changed)
        self.set_value(values)

    def _configure_columns(self):
        self.table.setHorizontalHeaderLabels(["Fluorophore / component"] + (["Cycle"] if self.cycles else [])
            + ["Marker assignment", "Marker name"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        if self.cycles:
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(1, 88)
            self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def add_row(self, value):
        from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
        from mif_pipeline.io.metadata.cycles import split_cycle_component
        is_af = is_autofluorescence_component(value.get("component_name") or "")
        row = self.table.rowCount()
        if self.af_retained and not is_af:
            row = next((i for i in range(row) if self.table.editor_for_cell(i, 0).property("retainedAF")), row)
        self.table.insertRow(row)
        raw_name = str(value.get("component_name") or "")
        base_name, recorded_cycle = split_cycle_component(raw_name)
        name = QLineEdit(base_name if self.cycles else raw_name)
        name.setPlaceholderText("Fluorophore or component")
        name.setProperty("retainedAF", self.af_retained and is_af)
        editors = [name]
        if self.cycles:
            cycle = QComboBox()
            cycle.addItem("Choose…", None)
            for number in self.cycles:
                cycle.addItem(str(number + 1), number)
            cycle.setCurrentIndex(max(0, cycle.findData(recorded_cycle)))
            editors.append(cycle)
        assignment = QComboBox()
        for label, data in (("Choose…", "missing"), ("Marker assigned", "assigned"), ("No marker", "none")):
            assignment.addItem(label, data)
        assignment.setCurrentIndex(max(0, assignment.findData(value.get("assignment", "missing"))))
        marker = QLineEdit(str(value.get("biomarker") or ""))
        marker.setPlaceholderText("Marker name")
        marker._marker_assignment = assignment
        editors.extend((assignment, marker))
        for column, editor in enumerate(editors):
            editor.setAccessibleName(f"Component {row + 1}: {self.table.horizontalHeaderItem(column).text()}")
            self.table.bind_editor(row, column, editor)
            if self.cycles and column == 1:
                self.table.item(row, column).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        def update_assignment():
            marker.setEnabled(assignment.currentData() == "assigned")
            if assignment.currentData() == "none":
                marker.clear()
            self.table.refresh_cells()
        assignment.currentIndexChanged.connect(update_assignment)
        if self.af_retained and is_af:
            assignment.setCurrentIndex(assignment.findData("none"))
            name.setReadOnly(True)
            assignment.setEnabled(False)
            name.setToolTip("Autofluorescence is retained because the metadata does not record prior AF subtraction.")
            if self.cycles:
                editors[1].setEnabled(False)
        update_assignment()
        self.table.clear_history()
        self.tools.filter_rows()
        self._resize()
        self._update_remove()
        self.changed.emit()
        return row

    def _add_component(self):
        self.tools.search.clear()
        self.tools.needs_review.setChecked(False)
        row = self.add_row({})
        self.table.setCurrentCell(row, 0)
        self.table.scrollToItem(self.table.item(row, 0))
        self.table.editItem(self.table.item(row, 0))

    def _update_remove(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        self.remove.setEnabled(any(not self.table.editor_for_cell(row, 0).property("retainedAF") for row in rows))

    def _remove_selected(self):
        self.table.commit_pending()
        rows = {index.row() for index in self.table.selectedIndexes()}
        for row in sorted(rows, reverse=True):
            if not self.table.editor_for_cell(row, 0).property("retainedAF"):
                self.table.forget_row(row)
        self.tools.filter_rows()
        self._resize()
        self._update_remove()
        self.changed.emit()

    def set_nuclear_stain(self, identity):
        self._nuclear_identity = identity
        for index, row in enumerate(self.value()):
            assignment = self.table.editor_for_cell(index, self._assignment_column)
            nuclear = bool(identity) and row["component_name"] == identity
            if nuclear:
                assignment.setCurrentIndex(assignment.findData("none"))
            retained = self.table.editor_for_cell(index, 0).property("retainedAF")
            assignment.setEnabled(not nuclear and not retained)
            assignment.setProperty("cellDisplay", "Nuclear stain" if nuclear else None)
        self.table.refresh_cells()

    def showEvent(self, event):
        super().showEvent(event)
        self._resize()

    def _resize(self):
        height = max(38, self.fontMetrics().lineSpacing() + 18)
        self.table.verticalHeader().setDefaultSectionSize(height)
        shown = min(self.table.rowCount(), 7) if self.table.rowCount() > 10 else self.table.rowCount()
        self.table.fit_height(shown)

    def value(self):
        from mif_pipeline.io.metadata.cycles import cycle_component_name, split_cycle_component
        self.table.commit_pending()
        values = []
        for i in range(self.table.rowCount()):
            name = self.table.editor_for_cell(i, 0).text().strip()
            if self.cycles and name:
                cycle = self.table.editor_for_cell(i, 1).currentData()
                if cycle is not None:
                    name = cycle_component_name(split_cycle_component(name)[0], cycle)
            values.append({"component_name": name,
                           "assignment": self.table.editor_for_cell(i, self._assignment_column).currentData(),
                           "biomarker": self.table.editor_for_cell(i, self._marker_column).text().strip() or None})
        return values

    def set_value(self, values):
        from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
        from mif_pipeline.io.metadata.cycles import cycle_component_name, split_cycle_component
        blocker = QSignalBlocker(self)
        self.table.reset_rows()
        values = list(values or ())
        if self.af_retained:
            if self.cycles:
                af_cycles = {split_cycle_component(v.get("component_name") or "")[1]
                             for v in values if is_autofluorescence_component(v.get("component_name") or "")}
                for cycle in self.cycles:
                    if cycle not in af_cycles:
                        values.append({"component_name": cycle_component_name("AF", cycle), "assignment": "none", "biomarker": None})
            elif not any(is_autofluorescence_component(v.get("component_name") or "") for v in values):
                values.append({"component_name": "AF", "assignment": "none", "biomarker": None})
        for value in values:
            self.add_row(value)
        self.tools.filter_rows()
        self._resize()
        del blocker
        self.changed.emit()

    def set_cycles(self, cycles):
        """Update offered cycles without inventing a cycle for an unassigned dye."""
        cycles = tuple(sorted(set(cycles or ())))
        if cycles == self.cycles:
            return
        from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
        values = self.value()
        blocker = QSignalBlocker(self)
        if self.af_retained:
            values = [v for v in values if not is_autofluorescence_component(v["component_name"])]
        self.cycles = cycles
        self._assignment_column = 2 if cycles else 1
        self._marker_column = self._assignment_column + 1
        self.table.reset_rows()
        self.table.setColumnCount(4 if cycles else 3)
        self._configure_columns()
        self.set_value(values)
        del blocker
        self.changed.emit()


class ChannelCycleReview(QWidget):
    """Reusable cycle-only review with linked source-order plot and table."""
    applied = Signal()
    cancelled = Signal()

    def __init__(self, rows, values, source_cycles, parent=None, *, channel_content=None, source_series_index=0, embedded=False):
        super().__init__(parent)
        self.channel_content = channel_content
        self._embedded = embedded
        self.setObjectName("channel-cycle-review")
        self._rows = rows
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.status = QLabel(self)
        self.status.setProperty("statusBanner", True)
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.status)
        self.cyclic = QCheckBox("Assign channels to cycles", self)
        self.cyclic.setChecked(bool(values) or source_cycles is not None)
        self.cyclic.setEnabled(source_cycles is None and not values)
        self.cyclic.setVisible(source_cycles is None and not values)
        layout.addWidget(self.cyclic)
        cycle_column = 1
        self.table = QTableWidget(len(rows), cycle_column + 1, self)
        self.table.setProperty("scientificTable", True)
        self.table.setAccessibleName("Acquired channels and cycle assignments")
        self.table.setStyleSheet("QTableWidget::item { padding: 0px 6px; }")
        self.table.setHorizontalHeaderLabels(["Acquired channel", "Cycle"])
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(cycle_column, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(cycle_column, 150)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerItem)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        current = {value["channel_id"]: value["cycle"] for value in values}
        self._editors = []
        self._cycle_fields = []
        for index, row in enumerate(rows):
            source_cycle = source_cycles[index] if source_cycles is not None else None
            label = row["channel_name"] or row["channel_id"]
            if any(item.get('source_t', 0) > 0 for item in rows):
                label = f"T{row['source_t']} · {label}"
            labels = [label]
            for column, text in enumerate(labels):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{row.get('source_channel_id', row['channel_id'])} · Series {source_series_index} · C{row.get('source_c', row.get('channel_index', index))} · T{row.get('source_t', 0)} · Z{row.get('source_z', 0)}")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(index, column, item)
            cycle = current.get(row["channel_id"], source_cycle)
            field = QWidget(self.table)
            field.setObjectName('cycle-assignment-field')
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(0)
            editor = QLineEdit("" if cycle is None else str(cycle + 1), field)
            _style_cell_editor(editor)
            editor.setPlaceholderText("Unknown")
            editor.setValidator(QIntValidator(1, 2147483647, editor))
            editor.setAccessibleName(f"{row['channel_name'] or row['channel_id']}: cycle")
            source_text = "Unknown" if source_cycle is None else str(source_cycle + 1)
            editor.setToolTip(f"{row['channel_id']}\nSource cycle: {source_text}")
            editor.installEventFilter(self)
            field_layout.addWidget(editor)
            source_label = QLabel(f"Source: {source_text}", field)
            source_label.setProperty("choiceHelp", True)
            source_label.setContentsMargins(9, 0, 9, 0)
            field_layout.addWidget(source_label)
            def show_source(text, label=source_label, original=source_cycle):
                text = text.strip()
                label.setVisible(text != ("" if original is None else str(original + 1)))
                self._resize_cycle_rows()
            editor.textChanged.connect(show_source)
            source_label.setVisible(cycle != source_cycle)
            self.table.setCellWidget(index, cycle_column, field)
            self._editors.append(editor)
            self._cycle_fields.append(field)
        self.table.setEnabled(self.cyclic.isChecked())
        self.cyclic.toggled.connect(self.table.setEnabled)
        from .cycle_plot import AcquiredCyclePlot
        self.plot = AcquiredCyclePlot(rows, source_cycles, source_series_index, self)
        self.plot.channel_selected.connect(self._focus_channel)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.plot.layout().removeWidget(self.plot.toolbar)
        self.plot.layout().removeWidget(self.plot.hint)
        self.plot.canvas.setMinimumHeight(0)
        self.table_hint = QLabel('Edit cycles directly, or select rows to assign together.', self)
        self.table_hint.setProperty('supportingText', True)
        self.table_hint.setWordWrap(True)
        self.table_panel = QWidget(self)
        table_layout = QVBoxLayout(self.table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)
        table_layout.addWidget(self.table)
        self.table_footer = QVBoxLayout()
        self.table_footer.setSpacing(8)
        table_layout.addLayout(self.table_footer)
        self._group_layout = QGridLayout()
        self._group_layout.setHorizontalSpacing(16)
        self._group_layout.setVerticalSpacing(8)
        self._stacked = None
        self._fitting_table = False
        self._arrange_review()
        layout.addLayout(self._group_layout)
        note = QLabel("Raw spectral unmixing requires a cycle for every acquired band." if channel_content == "raw" else
            "Leave an unknown cycle blank, including an unrecorded nuclear reference.", self)
        note.setWordWrap(True)
        note.setProperty("choiceHelp", True)
        note.setVisible(not embedded)
        layout.addWidget(note)
        self._error = QLabel("", self)
        self._error.setWordWrap(True)
        self._error.hide()
        layout.addWidget(self._error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use cycle groups")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setToolTip("Save the reviewed mapping while preserving the source metadata.")
        buttons.accepted.connect(self._accept_values)
        buttons.rejected.connect(self.cancelled)
        layout.addWidget(buttons)
        buttons.setVisible(not embedded)
        self._resize_cycle_rows()
        self.table.verticalScrollBar().valueChanged.connect(self._fit_table_height)
        for editor in self._editors:
            editor.textChanged.connect(self._refresh_groups)
        self.cyclic.toggled.connect(self._refresh_groups)
        self._refresh_groups()

    def _refresh_groups(self, *_):
        try:
            values = self.value()
        except ValueError:
            return
        numbers = [value["cycle"] for value in values] if values else [None] * len(self._rows)
        self.plot.set_cycles(numbers)
        missing = numbers.count(None)
        known = len({value for value in numbers if value is not None})
        if self._embedded:
            self.status.setText(f"There {'is' if len(numbers) == 1 else 'are'} {len(numbers)} "
                f"{'channel' if len(numbers) == 1 else 'channels'} across {known} "
                f"{'cycle' if known == 1 else 'cycles'}"
                + (f", with {missing} {'channel' if missing == 1 else 'channels'} unassigned."
                   if missing else '. All channels are assigned.'))
        elif missing:
            self.status.setText(f"{missing} of {len(numbers)} acquired channels have no cycle. "
                + ("Assign each band before raw unmixing." if self.channel_content == "raw" else
                   "Unknown reference-channel cycles can remain blank for a prepared image."))
        else:
            self.status.setText(f"All {len(numbers)} acquired channels are assigned across {known} {'cycle' if known == 1 else 'cycles'}.")
        self._error.hide()

    def _focus_channel(self, index):
        self.table.setCurrentCell(index, 0)
        self.table.selectRow(index)
        self.table.scrollToItem(self.table.item(index, 0))
        self._editors[index].setFocus()
        self._editors[index].selectAll()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn and watched in self._editors:
            row = self._editors.index(watched)
            selected = {index.row() for index in self.table.selectionModel().selectedRows()}
            self.table.setCurrentCell(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            if row not in selected:
                self.table.selectRow(row)
        return super().eventFilter(watched, event)

    def _selection_changed(self):
        selected = {index.row() for index in self.table.selectionModel().selectedRows()}
        self.plot.set_selected_channels(selected)
        for row, field in enumerate(self._cycle_fields):
            field.setStyleSheet('QWidget#cycle-assignment-field { background: #d5ebe5; }'
                if row in selected else '')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_review()
        self._fit_table_height()

    def _arrange_review(self):
        stacked = self.width() < 850
        if stacked == self._stacked:
            return
        self._stacked = stacked
        for widget in (self.plot.toolbar, self.plot.hint, self.plot, self.table_hint, self.table_panel):
            self._group_layout.removeWidget(widget)
        self._group_layout.addWidget(self.plot.toolbar, 0, 0, 1, 1 if stacked else 2)
        self._group_layout.addWidget(self.plot.hint, 1, 0)
        self._group_layout.addWidget(self.plot, 2, 0, alignment=Qt.AlignmentFlag.AlignTop)
        self._group_layout.addWidget(self.table_hint, 3 if stacked else 1, 0 if stacked else 1)
        self._group_layout.addWidget(self.table_panel, 4 if stacked else 2, 0 if stacked else 1,
            alignment=Qt.AlignmentFlag.AlignTop)
        self._group_layout.setColumnStretch(0, 1 if stacked else 3)
        self._group_layout.setColumnStretch(1, 0 if stacked else 2)

    def _resize_cycle_rows(self):
        for row, field in enumerate(self._cycle_fields):
            field.ensurePolished()
            self.table.setRowHeight(row, max(42, field.sizeHint().height() + 2))
        self._fit_table_height()

    def _fit_table_height(self, *_):
        if self._fitting_table:
            return
        self._fitting_table = True
        try:
            self.table.ensurePolished()
            overhead = self.table.horizontalHeader().sizeHint().height() + 2 * self.table.frameWidth()
            limit = (350 if self.width() < 1100 else 440) if self._embedded else 520
            first = self.table.verticalScrollBar().value()
            height = overhead
            for row in range(first, self.table.rowCount()):
                row_height = self.table.rowHeight(row)
                if height > overhead and height + row_height > limit:
                    break
                height += row_height
            self.table.setFixedHeight(height)
            self.plot.setFixedHeight(max(340, height))
        finally:
            self._fitting_table = False

    def value(self):
        if not self.cyclic.isChecked():
            return []
        result = []
        for row, editor in zip(self._rows, self._editors, strict=True):
            text = editor.text().strip()
            if text and (not text.isascii() or not text.isdecimal() or int(text) < 1):
                raise ValueError("Use a whole cycle number starting at 1, or leave an unknown cycle blank.")
            result.append(dict(channel_id=row["channel_id"], cycle=int(text) - 1 if text else None))
        return result

    def _accept_values(self):
        try:
            values = self.value()
            if self.channel_content == "raw" and values and any(value["cycle"] is None for value in values):
                raise ValueError("Assign a cycle to every acquired band before raw unmixing.")
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.show()
            return
        self.applied.emit()


class ChannelCycleEditor(QWidget):
    """Keep cycle mapping compact until the researcher opens its review dialog."""
    changed = Signal()
    review_state_changed = Signal()

    def __init__(self, rows, values=(), source_cycles=None, parent=None):
        super().__init__(parent)
        self.rows, self.source_cycles = rows, source_cycles
        self.channel_content = None
        self.source_series_index = 0
        self.reviewing = False
        self._panel = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._compact = QWidget(self)
        compact_layout = QHBoxLayout(self._compact)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        self.button = ContentSizedButton("Assign cycles…", self)
        self.button.setObjectName("assign-channel-cycles")
        self.button.clicked.connect(self._edit)
        compact_layout.addWidget(self.button)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        compact_layout.addWidget(self.summary, 1)
        layout.addWidget(self._compact)
        self.set_value(values)

    def value(self):
        if self.reviewing:
            return self._panel.value()
        return [dict(value) for value in self._values]

    def set_value(self, values):
        from mif_pipeline.io.metadata.cycles import validate_channel_cycle_values
        values = list(values or ())
        if values:
            validate_channel_cycle_values(tuple(row["channel_id"] for row in self.rows), values)
        self._values = [dict(value) for value in values]
        known = {value["cycle"] for value in values if value["cycle"] is not None}
        missing = sum(value["cycle"] is None for value in values)
        self.summary.setText((f"{len(known)} {'cycle' if len(known) == 1 else 'cycles'}"
            + (f" · {missing} {'channel' if missing == 1 else 'channels'} unknown" if missing else "")) if values else "")
        self.changed.emit()

    def _edit(self):
        self.open_review()

    def open_review(self):
        if self.reviewing:
            return
        if self._panel is not None:
            self.layout().removeWidget(self._panel)
            self._panel.deleteLater()
        self._panel = ChannelCycleReview(self.rows, self.value(), self.source_cycles, self,
            channel_content=self.channel_content, source_series_index=self.source_series_index)
        if not self._values and self.source_cycles is None:
            self._panel.cyclic.setChecked(True)
        self._panel.applied.connect(self._commit_review)
        self._panel.cancelled.connect(self._close_review)
        self.layout().addWidget(self._panel)
        self.reviewing = True
        self._compact.hide()
        self._panel.show()
        self.review_state_changed.emit()

    def continue_review(self):
        self._panel._accept_values()

    def _commit_review(self):
        values = self._panel.value()
        self._close_review()
        self.set_value(values)

    def _close_review(self):
        self.reviewing = False
        self._panel.hide()
        self._compact.show()
        self.review_state_changed.emit()


class ChannelAssignmentTable(_AssignmentGrid):
    """One row per separated image channel; source labels and cycles are facts."""

    HEADERS = ("Image channel", "Channel label", "Marker assignment", "Marker name")
    _COLUMN = {"component_name": 1, "assignment": 2, "biomarker": 3}

    def __init__(self, form: FormModel, parent: QWidget | None = None):
        rows = next(table.rows for table in form.tables if table.table_id == "stage1_channel_rows")
        accepted_cycles = {row["channel_id"]: row["cycle"]
            for row in form.context.get("accepted_channel_cycles", ())}
        show_cycles = any(value is not None for value in accepted_cycles.values())
        super().__init__(len(rows), 5 if show_cycles else 4, parent)
        self._headers = (self.HEADERS[:1] + ("Cycle",) + self.HEADERS[1:]
            if show_cycles else self.HEADERS)
        self._columns = {name: index + int(show_cycles) for name, index in self._COLUMN.items()}
        self.setObjectName("channel-assignment-table")
        self.setAccessibleName("Channels and markers")
        self.setAccessibleDescription("Click a label or press F2 to edit. Tab moves between cells. "
            "Use Shift to select a range and Ctrl+C or Ctrl+V to copy or paste. "
            "Image channel identities and cycles are read-only.")
        self.setHorizontalHeaderLabels(self._headers)
        self.horizontalHeader().setSectionResizeMode(self._columns["assignment"], QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self._columns["assignment"], 174)
        if show_cycles:
            self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(1, 88)
            self.horizontalHeaderItem(1).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._fields = {}
        self._focus_editors = {}
        self._rows = rows
        for index, row in enumerate(rows):
            channel_id, source_name, fluor, component, assignment, marker = row.values
            name = str(source_name or f"Channel {index + 1}")
            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, row.row_id)
            item.setToolTip(f"Image channel: {name}\nRecorded identifier: {channel_id}\nRecorded dye: {fluor or 'Unknown'}")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.setItem(index, 0, item)
            if show_cycles:
                cycle = accepted_cycles.get(channel_id)
                cycle_item = QTableWidgetItem("Unknown" if cycle is None else str(cycle + 1))
                cycle_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                cycle_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                cycle_item.setToolTip("No cycle assigned for this channel." if cycle is None
                    else f"Cycle {cycle + 1}, accepted in Image inspection.")
                self.setItem(index, 1, cycle_item)
            for semantic, value in (("component_name", component or fluor or ""),
                                    ("assignment", assignment or ""), ("biomarker", marker or "")):
                self.setItem(index, self._columns[semantic], QTableWidgetItem(str(value)))
        self._resize_rows()

    def owns_control(self, name: str) -> bool:
        return re.fullmatch(r"stage1_\d+_(assignment|biomarker|component_name)", name) is not None

    def add_control(self, name: str, field: QWidget, editor: QWidget) -> None:
        match = re.fullmatch(r"stage1_(\d+)_(assignment|biomarker|component_name)", name)
        if match is None or int(match[1]) >= self.rowCount():
            raise ValueError("Channel field does not match the inspected channel inventory")
        row, semantic = int(match[1]), match[2]
        column = self._columns[semantic]
        field.setParent(self._control_store)
        field.hide()
        self._fields[name] = (row, field)
        self._focus_editors[(row, column)] = editor
        editor.setAccessibleName(f"{self.item(row, 0).text()}: {self._headers[column]}")
        if isinstance(editor, QLineEdit):
            editor.setPlaceholderText("Marker name" if semantic == "biomarker" else "Channel label")
        self.bind_editor(row, column, editor)

    def finish_tab_order(self):
        for row in range(self.rowCount()):
            assignment = self.editor_for_cell(row, self._columns["assignment"])
            marker = self.editor_for_cell(row, self._columns["biomarker"])
            if assignment is None or marker is None:
                continue
            marker._marker_assignment = assignment
            def update_assignment(*_, assignment=assignment, marker=marker):
                marker.setEnabled(assignment.currentData() == "assigned")
                if assignment.currentData() == "none":
                    marker.clear()
                self.refresh_cells()
            assignment.currentIndexChanged.connect(update_assignment)
            update_assignment()

    def set_nuclear_stain(self, identity):
        for index, row in enumerate(self._rows):
            assignment = self.editor_for_cell(index, self._columns["assignment"])
            if assignment is None:
                continue
            nuclear = identity is not None and row.values[0] == identity
            if nuclear:
                assignment.setCurrentIndex(assignment.findData("none"))
            marker = self.editor_for_cell(index, self._columns["biomarker"])
            # Draft restoration blocks control signals, so derive this state
            # from the restored values as well as ordinary choice events.
            marker.setEnabled(assignment.currentData() == "assigned" and not nuclear)
            if assignment.currentData() == "none":
                marker.clear()
            assignment.setEnabled(not nuclear)
            assignment.setProperty("cellDisplay", "Nuclear stain" if nuclear else None)
        self.refresh_cells()

    def reveal(self, name):
        if name not in self._fields:
            return None
        tools = getattr(self, "tools", None)
        if tools is not None:
            tools.search.clear()
            tools.needs_review.setChecked(False)
        row, _field = self._fields[name]
        semantic = name.rsplit("_", 1)[-1]
        if name.endswith("_component_name"):
            semantic = "component_name"
        column = self._columns[semantic]
        self.setRowHidden(row, False)
        self.setCurrentCell(row, column)
        self.scrollToItem(self.item(row, column))
        self.setFocus()
        self.editItem(self.item(row, column))
        return self._active_editor[0] if self._active_editor is not None else self

    def set_field_errors(self, errors):
        for name, (row, _field) in self._fields.items():
            source = self._focus_editors[(row, self._columns["component_name" if name.endswith("_component_name") else name.rsplit("_", 1)[-1]])]
            item = self._bound[source]
            item.setToolTip(errors.get(name) or source.toolTip() or item.text())
            item.setBackground(QBrush(QColor("#fff1e9")) if name in errors else QBrush())

    def _resize_rows(self):
        height = max(38, self.fontMetrics().lineSpacing() + 18)
        self.verticalHeader().setDefaultSectionSize(height)
        shown = min(self.rowCount(), 8) if self.rowCount() > 10 else self.rowCount()
        self.fit_height(shown)


class _MeasurementRoleDelegate(QStyledItemDelegate):
    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class MeasurementChannelTable(QWidget):
    """Keep channel selection and roles together using the form's original editors.

    Filtering changes only the displayed rows. The two bulk actions change the
    shown inclusion checkboxes, which emit their ordinary draft-change signals.
    Roles and selections in hidden rows remain available to form validation.
    """

    def __init__(self, form: FormModel, parent=None):
        super().__init__(parent)
        self._channels = form.tables[0].rows
        self._fields = {}
        self._editors = {}
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.search = QLineEdit(self)
        self.search.setObjectName("measurement-channel-filter")
        self.search.setPlaceholderText("Find a channel or marker…")
        self.search.setAccessibleName("Filter measurement channels")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        tools.addWidget(self.search, 1)
        for title, name, checked in (("Select shown", "select", True), ("Clear shown", "clear", False)):
            button = ContentSizedButton(title, self)
            button.setObjectName(f"measurement-{name}-shown")
            button.setToolTip("Change the included channels in the current filter.")
            button.clicked.connect(lambda _checked=False, selected=checked: self._select_shown(selected))
            tools.addWidget(button)
        body.addLayout(tools)
        self.table = QTableWidget(len(self._channels), 2, self)
        self.table.setObjectName("measurement-channel-table")
        self.table.setProperty("scientificTable", True)
        self.table.setAccessibleName("Channels to measure and measurement roles")
        self.table.setAccessibleDescription("Tab between channel inclusion and its role. Filtering keeps all selected channels.")
        self.table.setHorizontalHeaderLabels(("Include channel", "Measurement role"))
        self.table.setItemDelegateForColumn(1, _MeasurementRoleDelegate(self.table))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setTabKeyNavigation(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.addWidget(self.table)
        self.summary = QLabel(self)
        self.summary.setObjectName("measurement-channel-count")
        self.summary.setProperty("choiceHelp", True)
        self.summary.setWordWrap(True)
        body.addWidget(self.summary)

    def add_control(self, name, field, editor):
        match = re.fullmatch(r"channel_(\d+)_(selected|role)", name)
        if match is None:
            raise ValueError("Measurement table requires a channel inclusion or role control")
        row, semantic = int(match[1]), match[2]
        column = 0 if semantic == "selected" else 1
        channel = self._channels[row]
        source_name = str(channel.values[0])
        field.layout().setContentsMargins(10, 3, 10, 3)
        field.layout().setSpacing(0)
        self.table.setCellWidget(row, column, field)
        self._fields[name] = (row, field)
        self._editors[(row, column)] = editor
        editor.setAccessibleName(f"{source_name}: {'include channel' if column == 0 else 'measurement role'}")
        if isinstance(editor, QCheckBox):
            editor.setText(source_name)
            editor.setToolTip(f"Channel: {source_name}\nComponent: {channel.values[1]}\nMarker: {channel.values[3]}")
            editor.toggled.connect(self._update_summary)
        elif isinstance(editor, QComboBox):
            editor.setItemText(0, "Choose a role…")
            field.layout().setContentsMargins(0, 0, 0, 0)
            _style_cell_editor(editor)
        field.installEventFilter(self)
        self._fit_rows()
        self._update_summary()
        editors = [self._editors[key] for key in sorted(self._editors)]
        for first, second in zip(editors, editors[1:]):
            QWidget.setTabOrder(first, second)

    def _filter(self, text):
        needle = text.casefold().strip()
        for row, channel in enumerate(self._channels):
            self.table.setRowHidden(row, needle not in " ".join(map(str, channel.values)).casefold())
        self._fit_rows()
        self._update_summary()

    def _select_shown(self, selected):
        for (row, column), editor in self._editors.items():
            if column == 0 and not self.table.isRowHidden(row):
                editor.setChecked(selected)

    def reveal(self, name):
        """Show the selected row whose existing form validation needs attention."""
        if name not in self._fields:
            return
        row, _field = self._fields[name]
        if self.table.isRowHidden(row):
            self.search.clear()
        self.table.scrollTo(self.table.model().index(row, 0))

    def _update_summary(self, *_args):
        selected = sum(editor.isChecked() for (row, column), editor in self._editors.items() if column == 0)
        shown = sum(not self.table.isRowHidden(row) for row in range(self.table.rowCount()))
        count = len(self._channels)
        text = f"{selected} of {count} channels selected"
        if shown != count:
            text += f"  ·  {shown} shown by this filter"
        self.summary.setText(text)

    def _fit_rows(self):
        heights = {}
        for row, field in self._fields.values():
            heights[row] = max(heights.get(row, 0), field.sizeHint().height() + 4)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(self.fontMetrics().lineSpacing() + 24, heights.get(row, 0)))
        height = self.table.horizontalHeader().sizeHint().height() + sum(
            self.table.rowHeight(row) for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)
        ) + 8
        self.table.setFixedHeight(min(520, max(80, height)))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit_rows()
        return super().eventFilter(watched, event)
