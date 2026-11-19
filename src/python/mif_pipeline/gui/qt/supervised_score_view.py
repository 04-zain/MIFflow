"""Native score inspection and a bounded virtual table of exact predictions."""
from collections import OrderedDict
import math

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget, QToolTip, QToolButton


class PredictionPageModel(QAbstractTableModel):
    """Expose every matching row while retaining at most eight 100-row pages."""
    page_requested = Signal(int)
    page_size = 100
    cache_pages = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.total = 0
        self.pages = OrderedDict()
        self._requested = set()
        self._generation = 0

    def reset(self, total=0):
        self.beginResetModel()
        self.total = total
        self.pages.clear()
        self._requested.clear()
        self._generation += 1
        self.endResetModel()

    def put_page(self, offset, rows, total):
        if total != self.total:
            self.reset(total)
        self._requested.discard(offset)
        self.pages[offset] = tuple(rows)
        self.pages.move_to_end(offset)
        while len(self.pages) > self.cache_pages:
            self.pages.popitem(last=False)
        if rows:
            self.dataChanged.emit(self.index(offset, 0), self.index(offset+len(rows)-1, 3))

    def row(self, number):
        offset = number // self.page_size * self.page_size
        page = self.pages.get(offset)
        if page is not None and number-offset < len(page):
            self.pages.move_to_end(offset)
            return page[number-offset]
        return None

    def _need(self, offset):
        if offset in self._requested:
            return
        self._requested.add(offset)
        generation = self._generation
        QTimer.singleShot(0, lambda: self.page_requested.emit(offset)
            if generation == self._generation else None)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.total

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.ToolTipRole and orientation == Qt.Orientation.Horizontal and section == 2:
            return 'Scores descend within each predicted class; equal scores follow cell ID order.'
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ('Cell', 'Predicted class', 'Score ↓', 'At cutoff')[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.row(index.row())
        if row is None:
            self._need(index.row() // self.page_size * self.page_size)
            return 'Loading…' if role == Qt.ItemDataRole.DisplayRole and index.column() == 0 else None
        if role == Qt.ItemDataRole.UserRole:
            return row['cell_id']
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row['class_id']}\n{row['cell_id']}\nScore {row['confidence']:.17g}"
        if role == Qt.ItemDataRole.DisplayRole:
            return (f'{index.row()+1:,}', row['class_id'], f"{row['confidence']:.6g}",
                    'Unclassified' if row['outcome'] == 'Unclassified' else 'Assigned')[index.column()]
        return None


class _InspectionBound(QToolButton):
    """A plotted score bound that supports dragging and keyboard adjustment."""
    def __init__(self, which, parent):
        super().__init__(parent)
        self.which = which
        self._offset = None
        self.setAccessibleName('Lower inspection score' if which == 0 else 'Upper inspection score')
        self.setAccessibleDescription('Drag to adjust this score. Arrow keys change it by 0.01; Shift changes it by 0.001.')
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setStyleSheet('QToolButton {color:#006b70; background:transparent; border:1px solid transparent; padding:2px 4px;}'
                          'QToolButton:hover, QToolButton:focus {background:#e0eeeb; border-color:#88bdb7; border-radius:3px;}')

    def _value(self, event):
        parent = self.parentWidget()
        x = parent.mapFromGlobal(event.globalPosition().toPoint()).x()
        return parent.score_at(x+self._offset)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            parent = self.parentWidget()
            x = parent.mapFromGlobal(event.globalPosition().toPoint()).x()
            self._offset = parent.point(parent.inspection[self.which], 0).x()-x
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._offset is not None:
            self.parentWidget().set_bound(self.which, self._value(event), commit=False)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._offset is not None:
            value = self._value(event)
            self._offset = None
            self.parentWidget().set_bound(self.which, value, commit=True)

    def keyPressEvent(self, event):
        parent = self.parentWidget()
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = .001 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else .01
            value = parent.inspection[self.which]+(-step if key in (Qt.Key.Key_Left, Qt.Key.Key_Down) else step)
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            value = 0. if key == Qt.Key.Key_Home else 1.
        else:
            super().keyPressEvent(event)
            return
        parent.set_bound(self.which, value, commit=True)


class ScoreCoverageView(QWidget):
    """Inspect coverage with a separate cutoff handle and inclusive score range."""
    cutoff_changed = Signal(float)
    range_selected = Signal(float, float)

    def __init__(self, score_label, parent=None):
        super().__init__(parent)
        self.score_label = score_label
        self.inspection_only = False
        self.coverage = []
        self.total = 0
        self.cutoff = 0.
        self.inspection = (0., 1.)
        self.lower_handle = _InspectionBound(0, self)
        self.upper_handle = _InspectionBound(1, self)
        self._drag = None
        self._start = None
        self._cutoff_drag_offset = 0.
        self.hover_text = ''
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(240)
        self.setMaximumHeight(270)
        self.setAccessibleName('Assignment coverage and score-range inspection')
        self.setToolTip('Drag the amber cutoff handle to change assignments. Drag across the plot to inspect a score range.')

    def plot_rect(self):
        return QRectF(82., 66., max(1., self.width()-164.), max(1., self.height()-108.))

    @property
    def inspection(self):
        return self._inspection

    @inspection.setter
    def inspection(self, values):
        self._inspection = tuple(values)
        if hasattr(self, 'lower_handle'):
            self._position_bounds()

    def _position_bounds(self):
        for which, handle in enumerate((self.lower_handle, self.upper_handle)):
            value = self.inspection[which]
            handle.setText(('From ' if which == 0 else 'To ') + format(value, '.2f'))
            handle.setToolTip(f'{handle.accessibleName()}: {value:.12g}\nDrag or use arrow keys; hold Shift for a smaller step.')
            width = handle.fontMetrics().horizontalAdvance(handle.text())+14
            x = self.point(value, 0).x()
            handle.setGeometry(round(x-width if which == 0 else x), 33, width, 25)

    def set_bound(self, which, value, *, commit):
        """Move one inclusive inspection endpoint without changing acceptance."""
        low, high = self.inspection
        self.inspection = (max(0., min(high, value)), high) if which == 0 else (low, min(1., max(low, value)))
        self.update()
        if commit:
            self.range_selected.emit(*self.inspection)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_bounds()

    def point(self, score, fraction):
        rect = self.plot_rect()
        return QPointF(rect.left()+score*rect.width(), rect.bottom()-fraction*rect.height())

    def score_at(self, x):
        rect = self.plot_rect()
        return max(0., min(1., (x-rect.left())/rect.width()))

    def handle_rect(self):
        rect = self.plot_rect()
        x = min(rect.right()-51., max(rect.left()+51., self.point(self.cutoff, 1).x()))
        return QRectF(x-51., 3., 102., 25.)

    def set_summary(self, summary, inspection):
        self.coverage = summary['coverage']
        self.total = summary['scope_total']
        self.cutoff = summary['cutoff']
        self.inspection = inspection
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().window())
        rect = self.plot_rect()
        painter.setPen(QColor('#50686f'))
        for value in (0, .25, .5, .75, 1):
            y = self.point(0, value).y()
            painter.setPen(QPen(QColor('#dce4e4'), .8))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor('#50686f'))
            painter.drawText(QRectF(22, y-10, 39, 20), Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter, f'{100*value:g}%')
            x = self.point(value, 0).x()
            painter.drawText(QRectF(x-21, rect.bottom()+4, 42, 18), Qt.AlignmentFlag.AlignCenter, f'{value:g}')
        low, high = self.inspection
        painter.setPen(QPen(QColor('#83aaa6'), 1.))
        lower_x, upper_x = self.point(low, 0).x(), self.point(high, 0).x()
        painter.drawLine(QPointF(lower_x, 62.), QPointF(upper_x, 62.))
        for boundary in (lower_x, upper_x):
            painter.drawLine(QPointF(boundary, 59.), QPointF(boundary, rect.top()))
        if (low, high) != (0., 1.):
            left, right = self.point(low, 0).x(), self.point(high, 0).x()
            painter.fillRect(QRectF(left, rect.top(), max(1., right-left), rect.height()), QColor('#d5e9e6'))
        painter.setPen(QPen(QColor('#006b70'), 2.))
        if self.coverage:
            path = QPainterPath(self.point(*self.coverage[0]))
            previous = self.coverage[0]
            for current in self.coverage[1:]:
                path.lineTo(self.point(previous[0], current[1]))
                path.lineTo(self.point(*current))
                previous = current
            painter.drawPath(path)
        if not self.inspection_only:
            x = self.point(self.cutoff, 0).x()
            painter.setPen(QPen(QColor('#a36b20'), 1.8, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#a36b20'))
            painter.drawPolygon(QPolygonF([QPointF(x-5, rect.top()-4), QPointF(x+5, rect.top()-4), QPointF(x, rect.top()+3)]))
            painter.setPen(QPen(QColor('#a36b20'), 1))
            painter.setBrush(QColor('#fff3df'))
            painter.drawRoundedRect(self.handle_rect(), 4, 4)
            painter.drawText(self.handle_rect(), Qt.AlignmentFlag.AlignCenter, f'Cutoff {self.cutoff:.4g}')
        painter.setPen(QColor('#31575a'))
        painter.save()
        painter.translate(16., rect.center().y())
        painter.rotate(-90.)
        painter.drawText(QRectF(-rect.height()/2, -10., rect.height(), 20.),
                         Qt.AlignmentFlag.AlignCenter, 'Cells ≥ score' if self.inspection_only else 'Cells assigned')
        painter.restore()
        painter.drawText(QRectF(rect.left(), self.height()-21, rect.width(), 20),
                         Qt.AlignmentFlag.AlignCenter, self.score_label)
        painter.end()

    def _on_handle(self, position):
        tip = self.point(self.cutoff, 1.)
        return not self.inspection_only and (self.handle_rect().contains(position) or QRectF(tip.x()-7, tip.y()-6, 14, 12).contains(position))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position()
        if self._on_handle(position):
            self._drag = 'cutoff'
            self._cutoff_drag_offset = self.point(self.cutoff, 0).x()-position.x()
        elif self.plot_rect().contains(position):
            self._drag = 'range'
        else:
            return
        self._start = position
        self.setFocus()
        QToolTip.hideText()
        event.accept()

    def mouseMoveEvent(self, event):
        position = event.position()
        if self._drag == 'cutoff':
            self.cutoff = self.score_at(position.x()+self._cutoff_drag_offset)
            self.update()
            return
        if self._drag == 'range':
            self.inspection = tuple(sorted((self.score_at(self._start.x()), self.score_at(position.x()))))
            self.update()
            return
        self.setCursor(Qt.CursorShape.SizeHorCursor if self._on_handle(position) else Qt.CursorShape.CrossCursor)
        if self.coverage and self.plot_rect().contains(position):
            score, fraction = min(self.coverage, key=lambda row: abs(row[0]-self.score_at(position.x())))
            count = round(fraction*self.total)
            self.hover_text = (f'Score {score:.12g}\nAt or above: {count:,} / {self.total:,}' if self.inspection_only else\
                f'Score cutoff {score:.12g}\nAssigned {100*fraction:.12g}% ({count:,} / {self.total:,})')
            QToolTip.showText(event.globalPosition().toPoint(), self.hover_text, self)
        else:
            self.hover_text = ''
            QToolTip.hideText()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        kind, self._drag = self._drag, None
        if kind == 'cutoff':
            self.cutoff = self.score_at(event.position().x()+self._cutoff_drag_offset)
            self.cutoff_changed.emit(self.cutoff)
        else:
            if abs(event.position().x()-self._start.x()) < 4:
                # A click selects a score interval, including its two endpoints.
                slot = min(255, math.floor(self.score_at(event.position().x())*256))
                self.inspection = (slot/256, (slot+1)/256)
            else:
                self.inspection = tuple(sorted((self.score_at(self._start.x()), self.score_at(event.position().x()))))
            self.range_selected.emit(*self.inspection)
            self.lower_handle.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def leaveEvent(self, _event):
        self.hover_text = ''
        QToolTip.hideText()

    def keyPressEvent(self, event):
        if not self.inspection_only and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.cutoff = max(0., min(1., self.cutoff+(-.01 if event.key() == Qt.Key.Key_Left else .01)))
            self.cutoff_changed.emit(self.cutoff)
            self.update()
        else:
            super().keyPressEvent(event)
