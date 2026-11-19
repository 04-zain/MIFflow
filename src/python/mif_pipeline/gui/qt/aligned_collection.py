"""Choose cycle alignment before ordering files into one sample."""

from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFormLayout, QFrame, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QSpinBox, QStackedWidget, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .researcher_widgets import ContentSizedButton
from .workbench import style_scientific_table


class _CycleDelegate(QStyledItemDelegate):
    """Use the table cell itself until its cycle is being edited."""

    def __init__(self, parent):
        super().__init__(parent)
        self.editor = None
        self.closeEditor.connect(self._closed)

    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setRange(1, 1000000)
        editor.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        editor.setAlignment(Qt.AlignmentFlag.AlignLeft)
        editor.setAccessibleName(f"Imaging cycle for file {index.row() + 1}")
        editor.setStyleSheet(
            "QSpinBox { border: 1px solid #006b70; border-radius: 0; "
            "padding: 0 9px; min-height: 0; background: #ffffff; }"
            "QSpinBox QLineEdit { border: none; padding: 0; min-height: 0; background: transparent; }"
        )
        self.editor = editor
        return editor

    def setEditorData(self, editor, index):
        editor.setValue(int(index.data(Qt.ItemDataRole.EditRole)))
        editor.selectAll()

    def setModelData(self, editor, model, index):
        editor.interpretText()
        model.setData(index, editor.value(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def _closed(self, editor, *_):
        if self.editor is editor:
            self.editor = None

    def commit(self):
        if self.editor is not None:
            editor = self.editor
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)


class AlignedCollectionDialog(QDialog):
    def __init__(self, samples, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import cycle images")
        self.setObjectName("aligned-source-collection")
        self.resize(900, 600)
        self.setMinimumSize(680, 560)
        self._order = list(range(len(samples)))
        self._samples = samples
        self._cycles = list(range(len(samples)))
        self.alignment = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)
        self.pages = QStackedWidget(self)
        layout.addWidget(self.pages, 1)

        opening = QWidget(self)
        start = QVBoxLayout(opening)
        start.setContentsMargins(0, 0, 0, 0)
        start.setSpacing(14)
        heading = QLabel("Confirm cycle alignment", opening)
        heading.setProperty("dialogHeading", True)
        start.addWidget(heading)
        intro = QLabel(
            f"Combine {len(samples)} selected images from the same tissue into one sample. "
            "Each file must already be stitched.", opening,
        )
        intro.setProperty("choiceHelp", True)
        intro.setWordWrap(True)
        start.addWidget(intro)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for attribute, title, detail, action, route in (
            ("aligned", "Already aligned",
             "The tissue is in the same position on an identical pixel grid in every file.",
             "Use aligned files", "already_registered"),
            ("unaligned", "Needs alignment",
             "The tissue is in different positions across files. Align the cycles using a shared stain.",
             "Align cycle files", "needs_registration"),
        ):
            panel = QFrame(opening)
            panel.setObjectName("cycle-route-" + attribute)
            panel.setProperty("workbenchSection", True)
            description = QVBoxLayout(panel)
            description.setContentsMargins(18, 14, 18, 14)
            description.setSpacing(8)
            label = QLabel(title, panel)
            label.setProperty("panelHeading", True)
            description.addWidget(label)
            help_text = QLabel(detail, panel)
            help_text.setWordWrap(True)
            help_text.setProperty("choiceHelp", True)
            description.addWidget(help_text)
            button = ContentSizedButton(action, panel)
            button.setProperty("actionRole", "primary")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setObjectName("cycle-files-" + attribute)
            button.toggled.connect(lambda checked, selected=route: self._choose(selected) if checked else None)
            button.clicked.connect(lambda checked=False, selected=route: self._choose(selected))
            group.addButton(button)
            setattr(self, attribute, button)
            description.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
            start.addWidget(panel)
        start.addStretch()
        self.pages.addWidget(opening)

        details = QWidget(self)
        content = QVBoxLayout(details)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(12)
        heading = QLabel("Assign files to cycles", details)
        heading.setProperty("dialogHeading", True)
        content.addWidget(heading)
        self.help_text = QLabel(details)
        self.help_text.setWordWrap(True)
        self.help_text.setProperty("choiceHelp", True)
        content.addWidget(self.help_text)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self.name = QLineEdit(samples[0].sample_id, details)
        self.name.setObjectName("aligned-source-name")
        self.name.setAccessibleName("Grouped sample name")
        form.addRow("Sample name", self.name)
        content.addLayout(form)
        count = QLabel("Cycle files", details)
        count.setProperty("panelHeading", True)
        content.addWidget(count)
        instruction = QLabel("Click a cycle to edit, or move a file with its cycle.", details)
        instruction.setProperty("choiceHelp", True)
        instruction.setWordWrap(True)
        content.addWidget(instruction)

        self.table = QTableWidget(len(samples), 3, details)
        self.table.setObjectName("cycle-file-assignments")
        self.table.setAccessibleName("Cycle files in grouped input channel order")
        self.table.setAccessibleDescription(
            "Click an imaging cycle or press F2 to edit. Alt+Up and Alt+Down move the selected file "
            "with its assigned cycle. Control+C copies the full source path, image, series and cycle."
        )
        self.table.setHorizontalHeaderLabels(("Source file", "Image / series", "Imaging cycle"))
        style_scientific_table(self.table)
        self.table.verticalHeader().show()
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.verticalHeader().setFixedWidth(36)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.EditKeyPressed | QTableWidget.EditTrigger.AnyKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setStyleSheet("QHeaderView::section { font-weight: 700; }")
        self.table.horizontalHeader().setFixedHeight(self.table.fontMetrics().lineSpacing() + 18)
        for column, title in ((1, "Image / series"), (2, "Imaging cycle")):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, self.table.fontMetrics().horizontalAdvance(title) + 34)
        self._cycle_delegate = _CycleDelegate(self.table)
        self.table.setItemDelegateForColumn(2, self._cycle_delegate)
        self.table.currentCellChanged.connect(self._selection_changed)
        self.table.cellClicked.connect(self._edit_cycle)
        self.table.itemChanged.connect(self._cycle_changed)
        self.table.installEventFilter(self)
        for sequence, direction in (("Alt+Up", -1), ("Alt+Down", 1)):
            shortcut = QShortcut(QKeySequence(sequence), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda step=direction: self._move(step))
        content.addWidget(self.table, 1)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.selected_file = QLabel(details)
        self.selected_file.setObjectName("cycle-file-selection")
        self.selected_file.setProperty("choiceHelp", True)
        self.selected_file.setTextFormat(Qt.TextFormat.PlainText)
        tools.addWidget(self.selected_file, 1)
        self.copy = ContentSizedButton("Copy", details)
        self.copy.setObjectName("cycle-file-copy")
        self.copy.setToolTip("Copy the selected file’s full path, image, series and imaging cycle (Ctrl+C).")
        self.copy.clicked.connect(self._copy_selection)
        tools.addWidget(self.copy)
        for title, direction, shortcut in (("Move up", -1, "Alt+Up"), ("Move down", 1, "Alt+Down")):
            button = ContentSizedButton(title, details)
            button.setObjectName("cycle-file-move-up" if direction < 0 else "cycle-file-move-down")
            button.setToolTip(f"Move the selected file and its cycle together ({shortcut}).")
            button.clicked.connect(lambda checked=False, step=direction: self._move(step))
            setattr(self, "move_up" if direction < 0 else "move_down", button)
            tools.addWidget(button)
        content.addLayout(tools)
        note = QLabel(
            "The grouped sample’s input channels follow this file order, with each file’s "
            "channels kept together in their original order.", details,
        )
        note.setProperty("choiceHelp", True)
        note.setWordWrap(True)
        content.addWidget(note)
        content.addStretch()
        self.pages.addWidget(details)

        footer = QHBoxLayout()
        cancel = ContentSizedButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self.change = ContentSizedButton("Change alignment", self)
        self.change.setProperty("actionRole", "navigation")
        self.change.setVisible(False)
        self.change.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        footer.addWidget(self.change)
        footer.addStretch()
        self._accept_button = ContentSizedButton("Continue", self)
        self._accept_button.setProperty("actionRole", "primary")
        self._accept_button.setEnabled(False)
        self._accept_button.setVisible(False)
        self._accept_button.clicked.connect(self.accept)
        self.pages.currentChanged.connect(self._page_changed)
        footer.addWidget(self._accept_button)
        layout.addLayout(footer)
        self._render()

    def _page_changed(self, index):
        self._accept_button.setVisible(index == 1)
        self.change.setVisible(index == 1)

    def _choose(self, alignment):
        self.alignment = alignment
        aligned = alignment == "already_registered"
        self.help_text.setText(
            "Confirm the file order and imaging cycle for each file."
            if aligned else
            "Enter a different imaging cycle for each file."
        )
        self._accept_button.setText("Group as one sample" if aligned else "Continue to registration")
        self._accept_button.setEnabled(True)
        self.pages.setCurrentIndex(1)

    @staticmethod
    def _source_indices(sample):
        return tuple("—" if value is None else str(value + 1) for value in (
            getattr(sample, "source_image_index", None), getattr(sample, "source_series_index", None),
        ))

    def _render(self, selected_row=0):
        blocker = QSignalBlocker(self.table)
        height = max(40, self.table.fontMetrics().lineSpacing() + 20)
        for row, index in enumerate(self._order):
            sample = self._samples[index]
            image, series = self._source_indices(sample)
            detail = f"{sample.source_mif_path}\nImage {image} · Series {series}\nImage and series numbers start at 1."
            for column, text in enumerate((sample.source_mif_path.name, f"{image} / {series}")):
                item = QTableWidgetItem(text)
                item.setToolTip(detail)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
            cycle = QTableWidgetItem()
            cycle.setData(Qt.ItemDataRole.EditRole, self._cycles[index] + 1)
            cycle.setToolTip("Click to edit the imaging cycle, or press F2.")
            cycle.setData(Qt.ItemDataRole.AccessibleDescriptionRole, f"Editable imaging cycle for {sample.source_mif_path.name}")
            self.table.setItem(row, 2, cycle)
            self.table.setRowHeight(row, height)
        header = self.table.horizontalHeader().height() + 6
        self.table.setMinimumHeight(header + height * min(2, len(self._order)))
        self.table.setMaximumHeight(header + height * min(7, len(self._order)))
        self.table.setCurrentCell(selected_row, 0)
        self.table.selectRow(selected_row)
        del blocker
        self._selection_changed()

    def _edit_cycle(self, row, column):
        if column == 2:
            self.table.editItem(self.table.item(row, column))

    def _cycle_changed(self, item):
        if item.column() == 2:
            self._cycles[self._order[item.row()]] = int(item.data(Qt.ItemDataRole.EditRole)) - 1

    def _selection_changed(self, *_):
        row = self.table.currentRow()
        self.move_up.setEnabled(row > 0)
        self.move_down.setEnabled(0 <= row < len(self._order) - 1)
        self.copy.setEnabled(row >= 0)
        self.selected_file.setText(f"{len(self._order)} files · {1 if row >= 0 else 0} selected")
        if row >= 0:
            source = str(self._samples[self._order[row]].source_mif_path)
            self.selected_file.setToolTip(source)
            self.selected_file.setAccessibleDescription(source)

    def _copy_selection(self):
        self._cycle_delegate.commit()
        row = self.table.currentRow()
        if row >= 0:
            index = self._order[row]
            sample = self._samples[index]
            QApplication.clipboard().setText("\t".join((
                str(sample.source_mif_path), *self._source_indices(sample), str(self._cycles[index] + 1),
            )))

    def eventFilter(self, watched, event):
        if watched is self.table and event.type() == QEvent.Type.KeyPress:
            if event.matches(QKeySequence.StandardKey.Copy):
                self._copy_selection()
                return True
            if event.matches(QKeySequence.StandardKey.Paste) and self.table.currentColumn() == 2:
                text = QApplication.clipboard().text().strip()
                if text.isdecimal() and 1 <= int(text) <= 1000000:
                    self.table.currentItem().setData(Qt.ItemDataRole.EditRole, int(text))
                return True
        return super().eventFilter(watched, event)

    def _move(self, direction):
        row = self.table.currentRow()
        target = row + direction
        if row >= 0 and 0 <= target < len(self._order):
            editing_cycle = self._cycle_delegate.editor is not None
            self._cycle_delegate.commit()
            self._order[row], self._order[target] = self._order[target], self._order[row]
            self._render(target)
            if editing_cycle:
                self.table.setCurrentCell(target, 2)
                self.table.editItem(self.table.item(target, 2))

    def choices(self):
        self._cycle_delegate.commit()
        if self.alignment is None:
            raise ValueError("Choose whether the files already share a registered image grid")
        cycles = tuple(self._cycles[i] for i in self._order)
        if self.alignment == "needs_registration" and len(set(cycles)) != len(cycles):
            raise ValueError("Register one stitched image file per distinct imaging cycle")
        return self.name.text().strip(), tuple(self._order), cycles
