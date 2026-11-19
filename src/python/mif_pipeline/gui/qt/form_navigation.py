"""Content-sized Back/Next controls shared by multi-page stage forms."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtWidgets import QWidget, QBoxLayout, QLayout, QSizePolicy, QAbstractScrollArea

# ==============================================================================
#                                Form navigation
# ==============================================================================


class FormNavigation(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self._row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(10)
        self._row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._row.addStretch(1)
        self._buttons = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._adapt)

    def addWidget(self, button):
        self._buttons.append(button)
        self._row.insertWidget(self._row.count() - 1, button, 0, Qt.AlignmentFlag.AlignLeft)
        button.installEventFilter(self)
        self._timer.start(0)

    def _adapt(self):
        visible = [button for button in self._buttons if not button.isHidden()]
        needed = sum(button.sizeHint().width() for button in visible) + 10 * max(
            0, len(visible) - 1
        )
        width = self.width()
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                inset = max(0, self.mapTo(parent.viewport(), QPoint()).x())
                width = min(width, parent.viewport().width() - 2 * inset)
                break
            parent = parent.parentWidget()
        direction = (
            QBoxLayout.Direction.LeftToRight
            if width >= needed
            else QBoxLayout.Direction.TopToBottom
        )
        if direction != self._row.direction():
            self._row.setDirection(direction)
            self.updateGeometry()

    def eventFilter(self, watched, event):
        if event.type() in (
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.LayoutRequest,
        ):
            self._timer.start(0)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._timer.start(0)
