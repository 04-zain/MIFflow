"""Image-state choices and the reviewed measurement-band assignments.

The form owns the editors and response values. These widgets expose those same
editors as visible choices and aligned source-to-component rows.
"""

from collections import Counter

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHeaderView, QLabel,
    QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .channel_table import _highlight_field_row, _style_cell_editor
from .selection_option import SelectionOption


class ImageStateChoices(QWidget):
    """Three visible, wrapped captions bound to the form's original combo."""

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self.setObjectName("image-state-choices")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._combo = combo
        self._group = QButtonGroup(self)
        self._rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for index in range(combo.count()):
            value = combo.itemData(index)
            if value is None:
                continue
            row = SelectionOption(combo.itemText(index), parent=self)
            button = row.button
            button.setObjectName("image-state-" + str(value))
            button.setAccessibleName(combo.itemText(index))
            self._group.addButton(button, index)
            button.clicked.connect(lambda _checked=False, i=index: combo.setCurrentIndex(i))
            self._rows[index] = row
            layout.addWidget(row)
        combo.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self, *_):
        selected = self._combo.currentIndex()
        self._group.setExclusive(False)
        for index, row in self._rows.items():
            self._group.button(index).setChecked(index == selected)
        self._group.setExclusive(True)
        self._combo.setFocusProxy(self._group.button(selected) or self._group.buttons()[0])

    def _fit_height(self):
        for row in self._rows.values():
            row.setFixedHeight(max(row.minimumSizeHint().height(), row.layout().heightForWidth(self.width())))
        self.setFixedHeight(sum(row.height() for row in self._rows.values())
                            + self.layout().spacing() * max(0, len(self._rows) - 1))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_height()


class MeasurementBandMapping(QFrame):
    """Present real acquired bands beside the unchanged reviewed-component editors."""

    def __init__(self, mapping, parent=None):
        super().__init__(parent)
        self.setObjectName("measurement-band-component-mapping")
        self.setProperty("workbenchSection", True)
        self._bands = mapping.get("bands", [])
        self._components = {str(row["index"]): row for row in mapping.get("components", [])}
        self.cyclic = len({band["cycle"] for band in self._bands if band["cycle"] is not None}) > 1
        self.band_rows = {}
        self.feedback = {}
        self._editors = {}
        self._cycle_rows = []
        self._resizing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QLabel("Match measurement bands to components", self)
        heading.setProperty("panelHeading", True)
        layout.addWidget(heading)
        help_text = QLabel("Match each measurement band to its fluorophore and marker using your imaging notes.", self)
        help_text.setWordWrap(True)
        help_text.setProperty("choiceHelp", True)
        layout.addWidget(help_text)
        self.table = QTableWidget(0, 3, self)
        self.table.setObjectName("measurement-band-mapping-table")
        self.table.setProperty("scientificTable", True)
        self.table.setHorizontalHeaderLabels(("Measurement band", "", "Component and marker"))
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 34)
        self.table.setStyleSheet(
            'QTableWidget { border-radius: 4px; padding: 0; }'
            'QTableWidget::item { padding: 0 10px; }'
            'QWidget[bandMappingCell="true"][activeTableRow="true"] { background: #d5ebe5; }'
        )
        groups = {}
        for index, band in enumerate(self._bands):
            groups.setdefault(band["cycle"] if self.cyclic else None, []).append(index)
        for cycle, indices in groups.items():
            if self.cyclic:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._cycle_rows.append(row)
                title = "Cycle not recorded" if cycle is None else f"Cycle {cycle + 1}"
                group = QTableWidgetItem(f"{title} · {len(indices)} measurement bands")
                group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                group.setBackground(QBrush(QColor("#e7eeeb")))
                font = QFont(self.font())
                font.setWeight(QFont.Weight.Medium)
                group.setFont(font)
                self.table.setItem(row, 0, group)
                self.table.setSpan(row, 0, 1, 3)
            for index in indices:
                band = self._bands[index]
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.band_rows[index] = row
                source = QTableWidgetItem(f"{index + 1}   {band['name']}")
                source.setFlags(Qt.ItemFlag.ItemIsEnabled)
                source.setData(Qt.ItemDataRole.UserRole, band["channel_id"])
                source.setToolTip(f"{band['name']}\nSource channel: {band['channel_id']}")
                self.table.setItem(row, 0, source)
                arrow = QTableWidgetItem("→")
                arrow.setFlags(Qt.ItemFlag.ItemIsEnabled)
                arrow.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                arrow.setForeground(QBrush(QColor("#58736b")))
                self.table.setItem(row, 1, arrow)
        layout.addWidget(self.table)
        self.status = QLabel(self)
        self.status.setObjectName("measurement-band-mapping-status")
        self.status.setWordWrap(True)
        self.status.setProperty("choiceHelp", True)
        layout.addWidget(self.status)
        for widget in (self, heading, help_text, self.status):
            policy = widget.sizePolicy()
            policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
            widget.setSizePolicy(policy)
        if not self._bands:
            self.table.hide()
            help_text.setText("Review the component and marker list in Channels & markers before matching measurement bands.")
        self._refresh()

    def add_control(self, index, field, editor):
        self._editors[index] = editor
        field.layout().setContentsMargins(0, 0, 0, 0)
        field.layout().setSpacing(0)
        field.setProperty("bandMappingCell", True)
        _style_cell_editor(editor)
        editor.setMinimumContentsLength(8)
        editor.setItemText(0, "Choose a component…")
        editor.setAccessibleName(f"{self._bands[index]['name']}: component and marker")
        note = QLabel(field)
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setProperty("validationMessage", True)
        note.setContentsMargins(9, 1, 9, 5)
        note.hide()
        self.feedback[index] = note
        field.layout().addWidget(note)
        self.table.setCellWidget(self.band_rows[index], 2, field)
        field.installEventFilter(self)
        editor.installEventFilter(self)
        editor.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_):
        selected = {index: editor.currentData() for index, editor in self._editors.items()}
        counts = Counter(value for value in selected.values() if value is not None)
        issues = 0
        for index, value in selected.items():
            component = self._components.get(value)
            messages = []
            if component is not None:
                if counts[value] > 1:
                    messages.append("Also assigned to another band.")
                cycle = component.get("cycle")
                if cycle != self._bands[index]["cycle"] and (cycle is not None or self.cyclic):
                    messages.append("Component cycle is not recorded." if cycle is None else f"Component belongs to cycle {cycle + 1}.")
            elif value is not None:
                messages.append("Component is no longer in Channels & markers.")
            note = self.feedback[index]
            note.setText(" ".join(messages))
            note.setVisible(bool(messages))
            issues += bool(messages)
            editor = self._editors[index]
            editor.setToolTip(editor.currentText() + ("\n" + note.text() if messages else ""))
        assigned = sum(value in self._components for value in selected.values())
        text = f"{assigned} of {len(self._bands)} bands assigned"
        if len(self._bands) != len(self._components):
            text += f" · {len(self._components)} components listed; the counts must match."
        elif issues:
            text += f" · Check {issues} assignment" + ("s." if issues != 1 else ".")
        elif assigned == len(self._bands) and self._bands:
            text += " · Each component used once."
        self.status.setText(text if self._bands else "")
        self.status.setVisible(bool(self._bands))
        self._resize_rows()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn:
            index = next((index for index, editor in self._editors.items() if editor is watched), None)
            if index is not None:
                _highlight_field_row(self.table, self.band_rows[index])
                for row in self._cycle_rows:
                    self.table.item(row, 0).setBackground(QBrush(QColor("#e7eeeb")))
        elif event.type() == QEvent.Type.LayoutRequest:
            self._resize_rows()
        return super().eventFilter(watched, event)

    def _resize_rows(self):
        if self._resizing:
            return
        self._resizing = True
        try:
            for row in range(self.table.rowCount()):
                field = self.table.cellWidget(row, 2)
                height = self.fontMetrics().lineSpacing() + 16
                if field is not None:
                    height = max(44, field.sizeHint().height(), field.minimumSizeHint().height())
                self.table.setRowHeight(row, height)
            total = self.table.horizontalHeader().sizeHint().height() + sum(
                self.table.rowHeight(row) for row in range(self.table.rowCount())) + 4
            self.table.setFixedHeight(min(500, total))
        finally:
            self._resizing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_rows()

    def showEvent(self, event):
        super().showEvent(event)
        self._resize_rows()
