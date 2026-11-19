"""Native vector workflow overview with keyboard-accessible stage controls.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from math import ceil
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QGridLayout,
)
from ..presenters.workflow import WorkflowGraphView, WorkflowStageView
from .theme import WORKFLOW_PALETTE as COLORS

# ==============================================================================
#                                 Workflow graph
# ==============================================================================


def milestone_connection(
    a: QRectF, b: QRectF, scale: float, outer_gutter: float, track: int
) -> tuple[QPainterPath, QPainterPath]:
    """Connect milestone ports through the space reserved around each row."""
    start = QPointF(a.center().x() + 18 * scale, a.top() + 20 * scale)
    end = QPointF(b.center().x() - 19 * scale, b.top() + 20 * scale)
    if abs(a.top() - b.top()) < 1:
        if 0 < b.left() - a.right() < 40 * scale:
            points = [start, end]
        else:
            gutter = a.top() - 6 * scale
            points = [start, QPointF(start.x() + 12 * scale, start.y()),
                      QPointF(start.x() + 12 * scale, gutter),
                      QPointF(end.x() - 12 * scale, gutter),
                      QPointF(end.x() - 12 * scale, end.y()), end]
    else:
        right = a.right() + 7 * scale
        gutter = a.bottom() + (6 + 4 * (track % 3)) * scale
        approach = b.top() - (8 + 4 * (track % 3)) * scale
        left = end.x() - 12 * scale
        points = [start, QPointF(right, start.y()), QPointF(right, gutter),
                  QPointF(outer_gutter, gutter), QPointF(outer_gutter, approach),
                  QPointF(left, approach), QPointF(left, end.y()), end]
    path = QPainterPath(points[0])
    for i in range(1, len(points) - 1):
        previous, corner, following = points[i - 1:i + 2]
        incoming, outgoing = previous - corner, following - corner
        length_in = (incoming.x() ** 2 + incoming.y() ** 2) ** 0.5
        length_out = (outgoing.x() ** 2 + outgoing.y() ** 2) ** 0.5
        radius = min(7 * scale, length_in / 2, length_out / 2)
        if not length_in or not length_out:
            continue
        path.lineTo(corner + incoming * (radius / length_in))
        path.quadTo(corner, corner + outgoing * (radius / length_out))
    path.lineTo(end)
    arrow = QPainterPath(end + QPointF(-5 * scale, -3 * scale))
    arrow.lineTo(end)
    arrow.lineTo(end + QPointF(-5 * scale, 3 * scale))
    return path, arrow


class WorkflowNodeButton(QPushButton):
    """Keyboard-accessible stage with a milestone, title and explicit state."""

    def __init__(self, stage: WorkflowStageView, number: int):
        super().__init__()
        self.stage, self.number = stage, number
        self.setObjectName("workflow-node-" + stage.key)
        self.setProperty("workflowPresentation", "milestone")
        self._status_summary = stage.status_summary
        self.setText(stage.definition.title + "\n" + self._status_summary)
        self.setAccessibleName(self.text().replace("\n", ". "))
        self.setAccessibleDescription(stage.definition.description)
        self.setToolTip(stage.definition.description)
        self._scale = max(1.0, self.font().pointSizeF() / 11.0)
        self.setFixedSize(round(200 * self._scale), round(116 * self._scale))
        self.setFlat(True)
        self.setStyleSheet("background: transparent; border: none; padding: 0px;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def connection_anchor(self) -> QPointF:
        return QPointF(self.width() / 2, 20 * self._scale)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(self._scale, self._scale)
        width = self.width() / self._scale
        center = width / 2
        status = self.stage.status
        active = status in {"ready", "needs_interaction", "running"}
        complete = status == "completed"
        warning = status == "failed"
        accent = QColor(COLORS["warning"] if warning else COLORS["primary"])
        muted = QColor(COLORS["muted"])
        focused = self.hasFocus() or self.underMouse()
        if warning:
            painter.setBrush(QColor(COLORS["warning_surface"]))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawRoundedRect(QRectF(2, 1, width - 4, 114), 10, 10)
        filled = (complete or active) and not focused
        painter.setBrush(accent if filled else QColor(COLORS["surface"]))
        painter.setPen(QPen(accent if active or complete or warning else QColor(COLORS["edge"]), 1.5))
        painter.drawEllipse(QPointF(center, 20), 15, 15)
        font = QFont(self.font())
        font.setPointSizeF(9)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["surface"]) if filled else accent if focused or warning else muted)
        if complete:
            pen = QPen(QColor(COLORS["surface"]) if filled else accent, 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            check = QPainterPath(QPointF(center - 6, 20))
            check.lineTo(center - 1, 25)
            check.lineTo(center + 7, 14)
            painter.drawPath(check)
        else:
            painter.drawText(QRectF(center - 15, 5, 30, 30), Qt.AlignmentFlag.AlignCenter, f"{self.number:02d}")
        font.setPointSizeF(12.5)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["ink"]))
        painter.drawText(QRectF(6, 40, width - 12, 40),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                         self.stage.definition.title)
        font.setPointSizeF(10.5)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(accent if active or warning else muted)
        painter.drawText(QRectF(6, 80, width - 12, 35),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                         self._status_summary)


class WorkflowCanvas(QGraphicsView):
    viewport_resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("run-workflow-graph")
        self.setAccessibleName("Analysis workflow")
        self.setAccessibleDescription(
            "Select a step to inspect it. Use the step selector for keyboard navigation."
        )
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(COLORS["canvas"]))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setMinimumHeight(240)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewport_resized.emit()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            if 0.5 <= self.transform().m11() * factor <= 2.0:
                self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)


class WorkflowGraph(QWidget):
    stage_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = WorkflowCanvas(self)
        self._scene = QGraphicsScene(self)
        self._view.setScene(self._scene)
        self._buttons: dict[str, WorkflowNodeButton] = {}
        self._graph: WorkflowGraphView | None = None
        self._layout_width = 0
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._layout_graph)
        self._view.viewport_resized.connect(lambda: self._reflow_timer.start(0))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tools = QGridLayout()
        tools.setColumnStretch(0, 1)
        title = QLabel("Analysis stages", self)
        title.setProperty("panelHeading", True)
        title.setWordWrap(True)
        instructions = "Select a stage to inspect it. Ctrl + scroll to zoom."
        title.setToolTip(instructions)
        self._view.setToolTip(instructions)
        self._view.setAccessibleDescription(
            instructions + " Use the step selector for keyboard navigation."
        )
        tools.addWidget(title, 0, 0)
        self._jump = QComboBox(self)
        self._jump.setObjectName("workflow-step-selector")
        self._jump.setAccessibleName("Open workflow step")
        self._jump.setMinimumContentsLength(14)
        self._jump.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._jump.activated.connect(self._jump_to_step)
        self._jump.setMaximumWidth(360)
        tools.addWidget(self._jump, 0, 1)
        fit = QPushButton("Fit", self)
        fit.setObjectName("workflow-fit")
        fit.setAccessibleName("Arrange workflow for window width")
        fit.clicked.connect(self.fit_graph)
        tools.addWidget(fit, 0, 2)
        actual = QPushButton("100%", self)
        actual.setObjectName("workflow-actual-size")
        actual.setAccessibleName("Show workflow at actual size")
        actual.clicked.connect(self._view.resetTransform)
        tools.addWidget(actual, 0, 3)
        layout.addLayout(tools)
        layout.addWidget(self._view, 1)
        self.legend = QLabel(
            "Solid lines show the current workflow; dashed lines show planned steps.",
            self,
        )
        self.legend.setObjectName("workflow-caption")
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)

    def button_for_stage(self, key: str) -> QPushButton | None:
        return self._buttons.get(key)

    def _jump_to_step(self, index: int) -> None:
        key = self._jump.itemData(index)
        if key is not None:
            self.stage_requested.emit(key)
        self._jump.setCurrentIndex(0)

    def set_graph(self, graph: WorkflowGraphView) -> None:
        """Display the current processing stages and their connections."""
        if type(graph) is not WorkflowGraphView:
            raise TypeError("workflow canvas requires a typed graph")
        if graph == self._graph:
            return
        self._graph = graph
        self._jump.clear()
        self._jump.addItem("Open a step…", None)
        for stage in graph.stages:
            self._jump.addItem(stage.definition.title, stage.key)
        self._layout_graph(force=True)

    def _layout_graph(self, *, force=False):
        if self._graph is None:
            return
        scale = max(1.0, self.font().pointSizeF() / 11.0)
        # Reserve the vertical scrollbar width even before it appears, keeping
        # wrapping stable when a taller phase first requires scrolling.
        width = max(460 * scale, self._view.width() - 24)
        if not force and abs(width - self._layout_width) < 2:
            return
        self._layout_width = width
        focused = next((key for key, button in self._buttons.items() if button.hasFocus()), None)
        previous_scene, previous_buttons = self._scene, self._buttons
        self._scene = QGraphicsScene(self)
        self._view.setScene(self._scene)
        self._buttons = {}
        previous_scene.destroyed.connect(lambda _object=None, retained=previous_buttons: None)
        previous_scene.deleteLater()
        graph = self._graph
        positions = {}
        phase_names = ("Image preparation", "Tissue organisation", "Cell analysis")
        numbers = {stage.key: number for number, stage in enumerate(graph.stages, 1)}
        y = 8 * scale
        for lane, name in enumerate(phase_names):
            stages = [stage for stage in graph.stages if stage.definition.lane == lane]
            if not stages:
                continue
            available = width - 80 * scale
            max_columns = max(1, int(available / (208 * scale)))
            rows = ceil(len(stages) / max_columns)
            columns = ceil(len(stages) / rows)
            row_height = 140
            phase_height = (54 + rows * row_height) * scale
            band = QPainterPath()
            band.addRoundedRect(QRectF(4 * scale, y, width - 8 * scale, phase_height), 12 * scale, 12 * scale)
            self._scene.addPath(band, QPen(Qt.PenStyle.NoPen), QColor(COLORS["surface"])).setZValue(-3)
            title = self._scene.addText(f"{lane + 1:02d}   {name}")
            font = QFont(self.font())
            font.setPointSizeF(12)
            font.setWeight(QFont.Weight.DemiBold)
            title.setFont(font)
            title.setDefaultTextColor(QColor(COLORS["primary"]))
            title.setPos(20 * scale, y + 9 * scale)
            completed = sum(stage.status == "completed" for stage in stages)
            summary = self._scene.addText(f"{completed} / {len(stages)} completed")
            font.setPointSizeF(10)
            font.setWeight(QFont.Weight.Normal)
            summary.setFont(font)
            summary.setDefaultTextColor(QColor(COLORS["muted"]))
            summary.setPos(width - summary.boundingRect().width() - 58 * scale, y + 11 * scale)
            for row in range(rows):
                row_stages = stages[row * columns:(row + 1) * columns]
                stride = available / len(row_stages)
                for column, stage in enumerate(row_stages):
                    point = QPointF(40 * scale + column * stride, y + (54 + row * row_height) * scale)
                    button = WorkflowNodeButton(stage, numbers[stage.key])
                    button.setFixedWidth(round(stride - 16 * scale))
                    button.clicked.connect(lambda _checked=False, key=stage.key: self.stage_requested.emit(key))
                    proxy = self._scene.addWidget(button)
                    proxy.setPos(point)
                    proxy.setZValue(2)
                    self._buttons[stage.key] = button
                    positions[stage.key] = QRectF(point, button.size())
            y += phase_height + 18 * scale
        edges = (*graph.edges, *graph.planned_edges)
        cross_row_edges = [(source, target) for source, target in edges
                           if positions[source].top() != positions[target].top()]
        for source, target in edges:
            # Long dependencies get distinct outer tracks, never columns of text.
            track = cross_row_edges.index((source, target)) if (source, target) in cross_row_edges else 0
            gutter = width - (8 + 5 * track) * scale
            path, arrow = milestone_connection(positions[source], positions[target], scale, gutter, track)
            planned = (source, target) in graph.planned_edges
            pen = QPen(QColor(COLORS["edge"] if planned else COLORS["primary"]), 1.3)
            if planned:
                pen.setDashPattern([3, 4])
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            item = self._scene.addPath(path, pen)
            item.setZValue(3)
            item.setData(0, source)
            item.setData(1, target)
            item.setData(2, "planned" if planned else "dependency")
            arrow_pen = QPen(pen)
            arrow_pen.setStyle(Qt.PenStyle.SolidLine)
            self._scene.addPath(arrow, arrow_pen).setZValue(3)
        self._scene.setSceneRect(0, 0, width, y)
        if focused in self._buttons:
            self._buttons[focused].setFocus(Qt.FocusReason.OtherFocusReason)

    def fit_graph(self):
        """Reflow to the viewport, retaining readable type and vertical scrolling."""
        self._view.resetTransform()
        self._layout_graph(force=True)
        self._view.horizontalScrollBar().setValue(0)
        self._view.verticalScrollBar().setValue(0)
