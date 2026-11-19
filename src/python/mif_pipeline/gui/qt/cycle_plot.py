"""Metadata-only view of acquired bands assigned to independent imaging cycles."""
from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QSizePolicy, QToolTip
from matplotlib.figure import Figure
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import numpy as np

from .spectral_review import SpectralPlotToolbar


_CYCLE_COLORS = tuple(colormaps['tab20'].colors[index]
    for index in (*range(0, 20, 2), *range(1, 20, 2)))


class AcquiredCyclePlot(QWidget):
    channel_selected = Signal(int)

    def __init__(self, rows, source_cycles, source_series_index=0, parent=None):
        super().__init__(parent)
        self.rows, self.source_cycles = rows, source_cycles
        self.source_series_index = source_series_index
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        figure = Figure(figsize=(7, 4), layout="constrained")
        figure.set_facecolor(self.palette().window().color().name())
        self.axes = figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setMinimumHeight(340)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar = SpectralPlotToolbar(self.canvas, self, coordinates=False)
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toolbar._actions["home"].setIcon(QIcon())
        layout.addWidget(self.toolbar)
        self.hint = QLabel("Each row is one cycle. Hover for details; click a channel to edit.", self)
        self.hint.setProperty("supportingText", True)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        layout.addWidget(self.canvas)
        self.canvas.mpl_connect("motion_notify_event", self._hover)
        self.canvas.mpl_connect("button_press_event", self._select)
        self.canvas.mpl_connect("figure_leave_event", lambda _event: QToolTip.hideText())
        self._cycles, self._points = (), np.empty((0, 2))
        self._selected_channels = ()
        self._selection_artist = None

    def set_cycles(self, cycles):
        self._cycles = tuple(cycles)
        known = sorted({cycle for cycle in cycles if cycle is not None})
        groups = ([None] if None in cycles else []) + known
        positions = {cycle: index for index, cycle in enumerate(groups)}
        self._points = np.array([(i + 1, positions[cycle]) for i, cycle in enumerate(cycles)])
        ax = self.axes
        ax.clear()
        ax.set_facecolor("#f4f7f9")
        for cycle in groups:
            indices = [i for i, value in enumerate(cycles) if value == cycle]
            ax.scatter([i + 1 for i in indices], [positions[cycle]] * len(indices),
                s=55 if cycle is None else 60, marker="x" if cycle is None else "o",
                color="#ad623b" if cycle is None else _CYCLE_COLORS[cycle % len(_CYCLE_COLORS)],
                linewidths=1.4 if cycle is None else 0,
                zorder=3)
        # Colour follows the cycle number; channel/marker names never define a group.
        self._selection_artist = ax.scatter([], [], s=132, facecolors="none",
            edgecolors="#005f66", linewidths=1.8, zorder=5, label="Selected channels")
        self.set_selected_channels(self._selected_channels)
        ax.set_yticks(range(len(groups)), ["Unknown" if cycle is None else f"Cycle {cycle + 1}" for cycle in groups])
        if len(self.rows) <= 10:
            ax.set_xticks(range(1, len(self.rows) + 1),
                [row["channel_name"] or row["channel_id"] for row in self.rows], rotation=30, ha="right")
        else:
            ticks = list(range(1, len(self.rows) + 1, max(1, len(self.rows) // 10)))
            if ticks[-1] != len(self.rows):
                ticks.append(len(self.rows))
            ax.set_xticks(ticks)
        ax.set_xlabel("Acquired planes in source order" if any(row.get('source_t', 0) > 0 for row in self.rows)
                      else "Acquired channels in source order")
        ax.set_xlim(.5, len(self.rows) + .5)
        ax.set_ylim(-.6, max(.6, len(groups) - .4))
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#a7b8c3")
        ax.tick_params(color="#a7b8c3", labelcolor="#344957")
        ax.grid(axis="y", color="#d5dfe5", linewidth=.7, linestyle=(0, (3, 4)))
        ax.set_axisbelow(True)
        self.toolbar.update()
        self.toolbar.push_current()
        self.canvas.draw_idle()

    def set_selected_channels(self, indices):
        """Outline the exact source rows without changing grouping or plot limits."""
        self._selected_channels = tuple(sorted(set(indices)))
        if self._selection_artist is not None:
            points = [self._points[index] for index in self._selected_channels
                if 0 <= index < len(self._points)]
            self._selection_artist.set_offsets(np.asarray(points).reshape(-1, 2))
            self.canvas.draw_idle()

    def _nearest(self, event):
        if event.inaxes is not self.axes or self.toolbar.mode or not len(self._points):
            return None
        distances = np.sum((self.axes.transData.transform(self._points) - [event.x, event.y]) ** 2, axis=1)
        index = int(np.argmin(distances))
        return index if distances[index] <= 144 else None

    def _hover(self, event):
        index = self._nearest(event)
        if index is None:
            QToolTip.hideText()
            return
        row, cycle = self.rows[index], self._cycles[index]
        source = self.source_cycles[index] if self.source_cycles is not None else None
        text = (f"{row['channel_name'] or row['channel_id']} · Channel {index + 1}\n"
            f"Cycle: {'Unknown' if cycle is None else cycle + 1}\n"
            f"Source cycle: {'Unknown' if source is None else source + 1}\n"
            f"{row.get('source_channel_id', row['channel_id'])} · Series {self.source_series_index} · C{row.get('source_c', row.get('channel_index', index))} · T{row.get('source_t', 0)} · Z{row.get('source_z', 0)}")
        position = self.canvas.mapToGlobal(QPoint(int(event.x), self.canvas.height() - int(event.y)))
        QToolTip.showText(position, text, self.canvas)

    def _select(self, event):
        index = self._nearest(event)
        if index is not None and event.button == 1:
            self.channel_selected.emit(index)
