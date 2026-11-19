"""Shared spacing and responsive table/plot layout for reference-image reviews."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QComboBox, QGridLayout, QLabel, QMenu, QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget
from .workbench import style_scientific_table


class ReferenceWorkspace(QWidget):
    """Keep band choices beside the evidence when both fit comfortably."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._narrow = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(16)
        self.side = QWidget(self)
        self.plot = QWidget(self)
        for widget in (self.side, self.plot):
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.side_layout = QVBoxLayout(self.side)
        self.plot_layout = QVBoxLayout(self.plot)
        for layout in (self.side_layout, self.plot_layout):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._arrange(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(self.width() < 980)

    def _arrange(self, narrow):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        for widget in (self.side, self.plot):
            self.grid.removeWidget(widget)
        self.side.setMinimumWidth(0 if narrow else 340)
        self.side.setMaximumWidth(16777215 if narrow else 340)
        self.grid.addWidget(self.side, 1 if narrow else 0, 0, Qt.AlignmentFlag.AlignTop)
        self.grid.addWidget(self.plot, 0, 0 if narrow else 1, Qt.AlignmentFlag.AlignTop)
        self.grid.setColumnStretch(0, 1 if narrow else 0)
        self.grid.setColumnStretch(1, 0 if narrow else 1)


def text_label(text, parent, *, heading=False, supporting=False):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    label.setProperty('panelHeading' if heading else 'supportingText', heading or supporting)
    return label


def af_preparation_phases(parent, current):
    """Show the two real source phases without implying an uncalculated plot."""
    from PySide6.QtWidgets import QHBoxLayout
    phases = QWidget(parent)
    phases.setObjectName('af-preparation-phases')
    layout = QHBoxLayout(phases)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    for key, title in (('regions', '1  Regions'), ('spectra', '2  Review spectra')):
        label = text_label(title, phases)
        active = key == current
        label.setStyleSheet('padding: 9px 12px; border-bottom: 2px solid '
            + ('#006b70; color: #006b70; background: #eaf4f2; font-weight: 600;'
               if active else '#dce5e3; color: #63787d;'))
        layout.addWidget(label)
    return phases


class _ReferenceCellDelegate(QStyledItemDelegate):
    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


def reference_table(table, *, clipboard=True):
    style_scientific_table(table)
    table.setStyleSheet('QTableView[scientificTable="true"] { border: none; '
        'border-top: 1px solid #c7d4d3; border-bottom: 1px solid #c7d4d3; '
        'border-radius: 0; padding: 0; }')
    table.setItemDelegate(_ReferenceCellDelegate(table))
    table.setEditTriggers(table.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(table.SelectionBehavior.SelectRows)
    table.setSelectionMode(table.SelectionMode.SingleSelection)
    table.setMinimumWidth(0)
    table.verticalHeader().setDefaultSectionSize(44)
    if clipboard:
        def copy_selection():
            indexes = [index for index in table.selectedIndexes()
                       if not table.isRowHidden(index.row()) and not table.isColumnHidden(index.column())]
            if not indexes:
                return
            values = {}
            for index in indexes:
                editor = table.cellWidget(index.row(), index.column())
                value = editor.currentText() if isinstance(editor, QComboBox) else index.data()
                values[index.row(), index.column()] = '' if value is None else str(value)
            rows = sorted({index.row() for index in indexes})
            columns = sorted({index.column() for index in indexes})
            QApplication.clipboard().setText('\n'.join('\t'.join(
                values.get((row, column), '') for column in columns) for row in rows))

        shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), table)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(copy_selection)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def context_menu(position):
            menu = QMenu(table)
            menu.addAction('Copy', copy_selection).setEnabled(bool(table.selectedIndexes()))
            menu.exec(table.viewport().mapToGlobal(position))
            menu.deleteLater()

        table.customContextMenuRequested.connect(context_menu)


def decision_combo(parent, choices, *, combo_type=QComboBox):
    combo = combo_type(parent)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    combo.setStyleSheet('QComboBox { border: 1px solid transparent; border-radius: 0; '
        'padding: 0 28px 0 8px; min-height: 0; background: transparent; }'
        'QComboBox:focus { border-color: #006b70; background: #ffffff; }')
    for label, value in choices:
        combo.addItem(label, value)
    return combo


INTENSITY_UNITS = {'full':'Fully corrected intensity',
                   'exposure_only':'Exposure-normalized intensity', 'as_stored':'Stored calibrated intensity'}
