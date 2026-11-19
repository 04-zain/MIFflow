"""Responsive workflow actions with one shared baseline and deliberate spacing."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import QEvent, Qt, QTimer, QPoint
from PySide6.QtWidgets import QWidget, QBoxLayout, QHBoxLayout, QVBoxLayout, QAbstractScrollArea

# ==============================================================================
#                                   Action bar
# ==============================================================================


class WorkflowActionBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbench-action-bar")
        self._outer = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._outer.setContentsMargins(12, 10, 12, 10)
        self._outer.setSpacing(12)
        self._primary = QWidget(self)
        self._utility = QWidget(self)
        self._left = QHBoxLayout(self._primary)
        self._right = QHBoxLayout(self._utility)
        for layout in (self._left, self._right):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
        self._outer.addWidget(self._primary, 1)
        self._outer.addWidget(self._utility, 0, Qt.AlignmentFlag.AlignRight)
        self._left.addStretch(1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._adapt)
        self._main_buttons = []
        self._utility_buttons = []

    def add_primary(self, button):
        self._main_buttons.append(button)
        self._left.insertWidget(self._left.count() - 1, button)
        button.installEventFilter(self)
        self._timer.start(0)

    def add_utility(self, button):
        self._utility_buttons.append(button)
        self._right.addWidget(button)
        button.installEventFilter(self)
        self._timer.start(0)

    def _needed(self, buttons):
        visible = [button for button in buttons if not button.isHidden()]
        return sum(button.sizeHint().width() for button in visible) + max(0, len(visible) - 1) * 10

    def _adapt(self):
        width = self.width() - 24
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                inset = max(0, self.mapTo(viewport, QPoint()).x())
                width = min(width, viewport.width() - 2 * inset - 24)
                break
            parent = parent.parentWidget()
        needed = self._needed(self._main_buttons) + self._needed(self._utility_buttons) + 12
        direction = (
            QBoxLayout.Direction.LeftToRight
            if width >= needed
            else QBoxLayout.Direction.TopToBottom
        )
        if self._outer.direction() != direction:
            self._outer.setDirection(direction)
            self.updateGeometry()

    def eventFilter(self, watched, event):
        if event.type() in (
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ):
            self._timer.start(0)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._timer.start(0)
