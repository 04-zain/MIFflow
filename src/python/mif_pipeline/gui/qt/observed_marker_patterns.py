"""Interactive joint marker states with exact counts and linked cell examples."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QStyle, QStyledItemDelegate,
    QTableView, QVBoxLayout, QHBoxLayout, QWidget,
)
from .researcher_widgets import ContentSizedButton


class _PatternModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.markers = []
        self.rows = []
        self.total = 0
        self.assigned_phenotype = None

    def assigned_here(self, row):
        return self.assigned_phenotype is not None and any(
            item['phenotype'] == self.assigned_phenotype and item['count'] > 0
            for item in row.get('assignments', ()))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.markers) + 1

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return (self.markers + ['Cells'])[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignCenter if col < len(self.markers)
                       else Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if col < len(self.markers):
            state = row['states'][col]
            return f'{self.markers[col]}: {state}' if role == Qt.ItemDataRole.ToolTipRole else state.title()
        if col == len(self.markers):
            percentage = 100 * row['count'] / self.total if self.total else 0
            percent = f'{percentage:.1f}'.rstrip('0').rstrip('.')
            if 0 < percentage < .1:
                percent = '<0.1'
            return f"{row['count']:,} ({percent}%)"
        return None


class _PatternDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        model = index.model()
        row, col = model.rows[index.row()], index.column()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect)
        assigned = model.assigned_here(row)
        painter.fillRect(rect, QColor('#cce7e1' if selected else '#eaf5f1' if assigned else '#f6f8f7'))
        if assigned and col == 0:
            painter.fillRect(QRectF(rect.left(), rect.top(), 3, rect.height()), QColor('#087b7d'))
        painter.setPen(QPen(QColor('#e2e9e6'), .5))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if col < len(model.markers):
            state = row['states'][col]
            cx, cy = rect.center().x(), rect.center().y()
            # A compact state matrix leaves the abundance plot as the row's focus.
            ink = QColor('#087b7d' if state == 'positive' else '#768984' if state == 'negative' else '#956b22')
            painter.setPen(QPen(ink, 1.4))
            painter.setBrush(ink if state == 'positive' else Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(cx - 8, cy - 8, 16, 16), 3, 3)
            painter.setPen(QColor('#ffffff') if state == 'positive' else ink)
            font = option.font
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, {'positive': '+', 'negative': '−', 'unknown': '?'}[state])
        else:
            text_width = max(110, option.fontMetrics.horizontalAdvance(model.data(index)) + 18)
            track = QRectF(rect.left() + 14, rect.center().y() - 6,
                           max(0, rect.width() - text_width - 26), 12)
            fraction = row['count'] / model.total if model.total else 0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#dce6e2'))
            painter.drawRoundedRect(track, 3, 3)
            bar = QRectF(track)
            bar.setWidth(track.width() * fraction)
            painter.setBrush(QColor('#087b7d'))
            painter.drawRoundedRect(bar, 3, 3)
            painter.setPen(QColor('#244a48'))
            painter.setFont(option.font)
            painter.drawText(rect.adjusted(rect.width() - text_width, 0, -12, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, model.data(index))
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor('#14777a'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        painter.restore()


class ObservedMarkerPatterns(QWidget):
    """Show observed marker patterns, their abundance and linked cell examples."""

    pattern_selected = Signal(object)
    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection_hint = 'Select a row to inspect cell examples.'
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.population = QLabel(self)
        self.population.setTextFormat(Qt.TextFormat.PlainText)
        self.population.setWordWrap(True)
        layout.addWidget(self.population)
        self.legend = QLabel(self)
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet('color:#526e64;')
        layout.addWidget(self.legend)
        self.table = QTableView(self)
        self.table.setAccessibleName('Observed marker combinations and cell abundance')
        self._model = _PatternModel(self)
        self.table.setModel(self._model)
        self.table.setItemDelegate(_PatternDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.horizontalHeader().setMinimumSectionSize(56)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setStyleSheet('QTableView { background:#f6f8f7; border:0; } '
                                 'QHeaderView::section { background:#f6f8f7; color:#254c48; padding:10px 8px; border:0; border-bottom:1px solid #b8cfca; font-weight:600; }')
        self.table.selectionModel().currentRowChanged.connect(self._selected)
        layout.addWidget(self.table)
        self.pager = QWidget(self)
        paging = QHBoxLayout(self.pager)
        paging.setContentsMargins(0, 0, 0, 0)
        self.page_label = QLabel(self.pager)
        paging.addWidget(self.page_label, 1)
        self.previous = ContentSizedButton('Previous', self.pager)
        self.next = ContentSizedButton('Next', self.pager)
        self.previous.setAccessibleName('Previous marker-pattern page')
        self.next.setAccessibleName('Next marker-pattern page')
        self.previous.clicked.connect(lambda: self._request_page(self._offset - 128))
        self.next.clicked.connect(lambda: self._request_page(self._offset + 128))
        paging.addWidget(self.previous)
        paging.addWidget(self.next)
        layout.addWidget(self.pager)
        self.pager.hide()
        self._offset = self._total_patterns = 0
        self.coverage = QLabel(self)
        self.coverage.setWordWrap(True)
        self.coverage.setStyleSheet('color:#526e64;')
        layout.addWidget(self.coverage)

    def set_patterns(self, markers, projection, *, population, assigned_phenotype=None):
        model = self._model
        model.beginResetModel()
        model.markers = list(markers)
        model.rows = list(projection.get('rows', ()))
        model.total = projection.get('selected_cells', 0)
        model.assigned_phenotype = assigned_phenotype
        model.endResetModel()
        self.legend.setText('+ Positive     − Negative     ? Unavailable'
                            + ('     Teal edge: assigned to this phenotype' if assigned_phenotype else ''))
        self.population.setText(f'{model.total:,} {population}.')
        metrics = self.table.fontMetrics()
        for column, marker in enumerate(markers):
            self.table.setColumnWidth(column, min(160, max(56, metrics.horizontalAdvance(marker) + 16)))
        self.table.setColumnWidth(len(markers), 300)
        self.table.setFixedHeight(40 * min(8, max(2, len(model.rows))) + 64)
        self._offset = projection.get('offset', 0)
        self._total_patterns = projection.get('observed_pattern_count', len(model.rows))
        self.page_label.setText(f'Patterns {self._offset + 1:,}–{self._offset + len(model.rows):,} of {self._total_patterns:,}')
        self.pager.setVisible(self._total_patterns > 128)
        self.set_page_busy(False)
        self.coverage.setText(self.selection_hint if model.rows else 'No cells match this rule.')

    def set_page_busy(self, busy):
        self.previous.setEnabled(not busy and self._offset > 0)
        self.next.setEnabled(not busy and self._offset + len(self._model.rows) < self._total_patterns)
        self.table.setEnabled(not busy)

    def _request_page(self, offset):
        self.set_page_busy(True)
        self.page_requested.emit(offset)

    def page_failed(self, message):
        self.set_page_busy(False)
        self.coverage.setText(message)

    def _selected(self, current, previous):
        if current.isValid():
            self.pattern_selected.emit(self._model.rows[current.row()])
