"""View an already-rendered scientific PNG without a system image viewer."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout,
)


class _FigureView(QGraphicsView):
    zoom_changed = Signal(float)

    def __init__(self, pixmap, parent):
        super().__init__(parent)
        self.setObjectName("figure-preview-view")
        self.setAccessibleName("Saved figure")
        self.setAccessibleDescription(
            "Drag to pan. Use arrow keys to pan, or Ctrl and scroll to zoom. "
            "Reset view fits the whole figure in the window."
        )
        scene = QGraphicsScene(self)
        scene.addPixmap(pixmap)
        self.setScene(scene)
        self.setSceneRect(scene.itemsBoundingRect())
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#eaf0ef"))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # Fixed scrollbar visibility avoids fit/resize feedback when fitting.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit = True
        self._fit_width = False

    def reset_view(self):
        self._fit = True
        self._fit_width = False
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.centerOn(self.sceneRect().center())
        self.zoom_changed.emit(self.transform().m11())

    def fit_width(self):
        """Keep portrait cell galleries readable; scroll down through their rows."""
        if self.sceneRect().width() <= 0:
            return
        self._fit = False
        self._fit_width = True
        scale = max(1, self.viewport().width() - 4) / self.sceneRect().width()
        self.resetTransform()
        self.scale(scale, scale)
        self.centerOn(self.sceneRect().center())
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        self.zoom_changed.emit(scale)

    def zoom_by(self, factor):
        current = self.transform().m11()
        target = max(.02, min(8., current * factor))
        self._fit = False
        self._fit_width = False
        self.scale(target / current, target / current)
        self.zoom_changed.emit(target)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.reset_view()
        elif self._fit_width:
            self.fit_width()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y():
                self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
                self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
                self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            event.accept()
        else:
            super().wheelEvent(event)


class FigurePreview(QDialog):
    """A parent-owned, disposable preview of existing figure bytes."""

    def __init__(self, path: Path, parent=None):
        if not isinstance(path, Path) or not path.is_absolute() or not path.is_file():
            raise ValueError("The saved figure is no longer available. Open it again from Results.")
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            raise ValueError("The saved figure could not be read. Export it again from its result.")
        super().__init__(parent)
        self.setObjectName("figure-preview")
        self.setWindowTitle("Figure preview")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumSize(420, 300)
        area = self.screen().availableGeometry()
        self.resize(min(920, int(area.width() * .88)), min(700, int(area.height() * .85)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self.view = _FigureView(pixmap, self)
        tools = QHBoxLayout()
        for text, name, description, action in (
            ("Zoom out", "zoom-out", "Reduce figure magnification", lambda: self.view.zoom_by(1 / 1.2)),
            ("Zoom in", "zoom-in", "Increase figure magnification", lambda: self.view.zoom_by(1.2)),
            ("Reset view", "reset", "Fit the whole figure in the window and center it", self.view.reset_view),
        ):
            button = QPushButton(text, self)
            button.setObjectName("figure-preview-" + name)
            button.setAccessibleName(text)
            button.setToolTip(description)
            button.setAccessibleDescription(description)
            button.setAutoDefault(False)
            button.clicked.connect(action)
            tools.addWidget(button)
        self._zoom = QLabel(self)
        self._zoom.setAccessibleName("Figure magnification")
        self._zoom.setMinimumWidth(48)
        self.view.zoom_changed.connect(lambda scale: self._zoom.setText(f"{scale:.0%}"))
        tools.addWidget(self._zoom)
        tools.addStretch()
        layout.addLayout(tools)
        layout.addWidget(self.view, 1)
        hint = QLabel("Drag to pan · Ctrl + scroll to zoom · Reset view to fit", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
