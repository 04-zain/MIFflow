"""Select recorded reference/prediction pairs in a native, keyboard-accessible matrix."""

from textwrap import fill

from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QSizePolicy,
    QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class _ConfusionCellDelegate(QStyledItemDelegate):
    """Draw the saved count and within-reference fraction in each selectable cell."""

    def paint(self, painter, option, index):
        count, fraction = index.data(Qt.ItemDataRole.UserRole)
        pale, dark = QColor('#f0f8f7'), QColor('#0b7777')
        color = QColor('#e6ebed') if fraction is None else QColor(*(
            round(a + (b-a)*fraction) for a, b in zip(pale.getRgb()[:3], dark.getRgb()[:3])))
        ink = QColor('#ffffff') if fraction is not None and fraction > .62 else QColor('#172d35')
        painter.save()
        rectangle = option.rect.adjusted(2, 2, -2, -2)
        painter.fillRect(rectangle, color)
        painter.setPen(ink)
        font = option.font
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF()+2)
        painter.setFont(font)
        upper = rectangle.adjusted(4, 6, -4, -rectangle.height()//2+4)
        painter.drawText(upper, Qt.AlignmentFlag.AlignCenter, f'{count:,}')
        font.setBold(False)
        font.setPointSizeF(option.font.pointSizeF())
        painter.setFont(font)
        lower = rectangle.adjusted(4, rectangle.height()//2-3, -4, -5)
        painter.drawText(lower, Qt.AlignmentFlag.AlignCenter,
                         '—' if fraction is None else f'{fraction:.1%}')
        if option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus):
            painter.setPen(QPen(QColor('#ffffff'), 5))
            painter.drawRect(rectangle.adjusted(2, 2, -2, -2))
            painter.setPen(QPen(QColor('#172d35'), 2))
            painter.drawRect(rectangle.adjusted(2, 2, -2, -2))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(QPen(QColor('#486169'), 1))
            painter.drawRect(rectangle.adjusted(1, 1, -1, -1))
        painter.restore()


class ClassifierConfusionMatrix(QWidget):
    """Show saved counts and row fractions; emit the selected class-ID pair.

    Rows are reference classes and columns are recorded predictions. Fractions
    come from the saved matrix table, so an absent reference class stays absent.
    Selection changes the review filter only; it never changes the evidence.
    """

    pair_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('classifier-confusion-matrix')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._class_ids = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        axes = QHBoxLayout()
        self.reference_axis = QLabel('Reference class', self)
        axes.addWidget(self.reference_axis)
        predicted = QLabel('Predicted class', self)
        predicted.setAlignment(Qt.AlignmentFlag.AlignCenter)
        axes.addWidget(predicted, 1)
        layout.addLayout(axes)
        self.table = QTableWidget(self)
        self.table.setObjectName('classifier-confusion-cells')
        self.table.setAccessibleName('Recorded confusion matrix')
        self.table.setAccessibleDescription(
            'Reference classes are rows; recorded predictions are columns. '
            'Select a cell with the mouse or arrow keys to review that class pair.')
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setItemDelegate(_ConfusionCellDelegate(self.table))
        self.table.setMouseTracking(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(128)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.currentCellChanged.connect(self._selected)
        layout.addWidget(self.table)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setProperty('choiceHelp', True)
        layout.addWidget(self.note)

    def set_counts(self, class_ids, names, rows, unit, selected_pair=None):
        """Display one classifier/split/subject table without recalculating its values."""
        self._class_ids = tuple(class_ids)
        values = {(row['reference_class'], row['predicted_class']): row for row in rows}
        with QSignalBlocker(self.table):
            self.table.clear()
            self.table.setRowCount(len(class_ids))
            self.table.setColumnCount(len(class_ids))
            wrapped = [fill(name, width=17, max_lines=3, placeholder='…') for name in names]
            self.table.setHorizontalHeaderLabels(wrapped)
            self.table.setVerticalHeaderLabels(wrapped)
            for index, name in enumerate(names):
                self.table.horizontalHeaderItem(index).setToolTip(name)
                self.table.verticalHeaderItem(index).setToolTip(name)
            for i, reference in enumerate(class_ids):
                for j, predicted in enumerate(class_ids):
                    row = values[reference, predicted]
                    count = int(row['count'])
                    raw_fraction = row['fraction_of_reference_class']
                    fraction = None if raw_fraction is None or raw_fraction == '' else float(raw_fraction)
                    percentage = 'unavailable' if fraction is None else f'{fraction:.1%}'
                    item = QTableWidgetItem(f'{count:,} {unit}; {percentage} within reference class')
                    item.setData(Qt.ItemDataRole.UserRole, (count, fraction))
                    item.setToolTip(f'{names[i]} → {names[j]}\n{count:,} {unit}\n'
                                    + ('No reference observations for this class.' if fraction is None
                                       else f'{percentage} of the {int(row["reference_total"]):,} reference {unit} in this row.'))
                    self.table.setItem(i, j, item)
            row_height = max(72, min(82, 280//max(1, len(class_ids))))
            self.table.verticalHeader().setDefaultSectionSize(row_height)
            self.table.verticalHeader().setFixedWidth(154)
            self.reference_axis.setFixedWidth(154)
            header_height = max(48, self.table.fontMetrics().lineSpacing()*max((label.count('\n')+1 for label in wrapped), default=1)+20)
            self.table.horizontalHeader().setFixedHeight(header_height)
            self.table.setFixedHeight(min(480, header_height + row_height*len(class_ids) + 24))
            self.set_selected_pair(selected_pair)
        self.note.setText(f'Percentages use the total number of reference {unit} in each row.')

    def set_selected_pair(self, pair):
        """Synchronize the highlighted cell without emitting another filter change."""
        with QSignalBlocker(self.table):
            if pair is None:
                self.table.clearSelection()
                self.table.setCurrentIndex(QModelIndex())
            else:
                self.table.setCurrentCell(self._class_ids.index(pair[0]), self._class_ids.index(pair[1]))

    def _selected(self, row, column, _previous_row, _previous_column):
        if row >= 0 and column >= 0:
            self.pair_selected.emit((self._class_ids[row], self._class_ids[column]))
