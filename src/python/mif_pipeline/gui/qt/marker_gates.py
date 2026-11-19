"""Qt-only drawing of frozen marker histograms and explicit threshold choices.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

import json
import re
from array import array
from bisect import bisect_left,bisect_right
from itertools import accumulate
from pathlib import Path
from math import expm1, isfinite, log1p, nextafter
from sys import float_info

from PySide6.QtCore import QSignalBlocker, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMenu,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .cohort_image_navigation import CohortImageNavigation
from .workbench import style_scientific_table, fit_scientific_columns
from ..terminology import field_label, cell_measurement_label
from ..presenters.marker_gates import marker_candidate_guidance, marker_gate_choices
from .method_guidance import MethodGuidanceCard
from .researcher_widgets import ContentSizedButton, configure_disclosure

# ==============================================================================
#                                  Marker gates
# ==============================================================================


def _intensity_gate_caption(value):
    """Name the open positive boundary while retaining its exact float64 value.

    The Gamma zero state uses the smallest positive float64 threshold to encode
    x > 0 under the common x >= threshold comparison. Rounding that boundary to
    zero would describe a different decision, so display the comparison itself.
    """
    return "Intensity > 0" if value == nextafter(0., float("inf")) else format(value, ".6g")


def _intensity_label(record):
    measurement = record["measurement"]
    statistic = measurement.rsplit(": ", 1)[-1]
    return cell_measurement_label(record["compartment"], statistic if statistic in {"Mean", "Median"} else measurement)


class MarkerChoiceCard(QAbstractButton):
    """A keyboard-operable choice with a title, short description and clear focus."""

    def __init__(self, title, description, parent=None):
        super().__init__(parent)
        self.title = title
        self.description = description
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(66)

    def sizeHint(self):
        return QSize(302, 68)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = self.isChecked()
        border = "#238a77" if selected else "#dbe5e4"
        background = "#edf8f4" if selected else "#f7faf9" if self.underMouse() else "#ffffff"
        painter.setPen(QPen(QColor(border), 1.8 if selected else 1))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)
        title_font = QFont(self.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor("#173c42" if self.isEnabled() else "#8b9c9f"))
        painter.drawText(QRectF(16, 12, self.width() - 50, 24), self.title)
        detail_font = QFont(self.font())
        detail_font.setPointSizeF(max(8, detail_font.pointSizeF() - 1))
        painter.setFont(detail_font)
        painter.setPen(QColor("#587177"))
        painter.drawText(QRectF(16, 39, self.width() - 32, 24), self.description)
        if selected:
            painter.setPen(QPen(QColor("#16836d"), 2))
            painter.setBrush(QColor("#16836d"))
            painter.drawEllipse(QRectF(self.width() - 30, 17, 12, 12))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#006b70"), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(5, 5, -5, -5), 7, 7)


class MarkerProgressRail(QListWidget):
    """Navigate marker identities while keeping configured and confirmed states distinct."""

    marker_selected = Signal(str)

    def __init__(self, markers, parent=None):
        super().__init__(parent)
        self.setObjectName("marker-progress-rail")
        self.setAccessibleName("Marker progress")
        self.setMinimumWidth(132)
        self.setMaximumWidth(178)
        self.setSpacing(5)
        self.setStyleSheet("""
            QListWidget { background: #f0f5f3; border: none; border-radius: 12px; padding: 7px; }
            QListWidget::item { padding: 12px 10px; border-radius: 7px; color: #587177; }
            QListWidget::item:selected { background: #ffffff; color: #075f58; border: 1px solid #afd7cb; }
            QListWidget::item:focus { border: 2px solid #006b70; }
        """)
        for marker in markers:
            item = QListWidgetItem(f"{marker}\nNot configured", self)
            item.setData(Qt.ItemDataRole.UserRole, marker)
            item.setSizeHint(QSize(135, 67))
        self.currentItemChanged.connect(
            lambda item, _previous: self.marker_selected.emit(item.data(Qt.ItemDataRole.UserRole))
            if item is not None else None
        )
        if self.count():
            self.setCurrentRow(0)

    def current_marker(self):
        item = self.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def set_current_marker(self, marker):
        for index in range(self.count()):
            if self.item(index).data(Qt.ItemDataRole.UserRole) == marker:
                self.setCurrentRow(index)
                return

    def set_marker_status(self, marker, status):
        for index in range(self.count()):
            item = self.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == marker:
                item.setText(f"{marker}\n{status}")
                item.setToolTip(f"{marker}: {status}")
                return


class MarkerHistogram(QWidget):
    """Draw supplied bins exactly; axis scaling is presentation, never refitting."""

    threshold_changed = Signal(float)
    range_selected = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(280)
        self.setMinimumWidth(340)
        self.histogram = {"counts": (), "bin_edges": ()}
        self.threshold_lines = ()
        self.show_legend_values = True
        self.log_counts = False
        self.display_range = (0.0, 1.0)
        self.axis_label = "Measured value"
        self.empty_message = "No valid measurements in this population"
        self._cdf_values, self._cdf_counts = (), (0,)
        self.manual_threshold = None
        self._dragging = False
        self.selected_range = None
        self._range_start = None
        self.range_selection_enabled = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Marker histogram; drag the final gate or use arrow keys")
        self.threshold_bounds = None
        self.show_fit = False
        self.density_x, self.component_densities, self.component_means = (), (), ()
        self.density_fraction = 1.
        self.setMouseTracking(True)

    def install_axis_controls(self, *controls):
        header = QHBoxLayout()
        header.addStretch(1)
        for control in controls:
            header.addWidget(control)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(round(self._plot_rect().left()), 0, 26, 0)
        layout.addLayout(header)
        layout.addStretch()

    def set_evidence(self, histogram, lines):
        """Validate saved bins and size the plot for its visible gate legend.

        We preserve every count and bin edge, derive a display range containing
        the supplied gates and reserve one line of space per legend entry. The
        widget performs no threshold fitting or measurement transformation.
        """
        counts, edges = histogram["counts"], histogram["bin_edges"]
        if (
            counts
            and len(edges) != len(counts) + 1
            or any(type(x) not in {int, float} or not isfinite(x) for x in edges)
            or any(type(x) is not int or x < 0 for x in counts)
            or any(right <= left for left, right in zip(edges, edges[1:]))
        ):
            raise ValueError("invalid owner histogram")
        self.histogram, self.threshold_lines = histogram, tuple(lines)
        positions = (*edges, *(line[1] for line in lines), *(self.component_means if self.show_fit else ()))
        low, high = (min(positions), max(positions)) if positions else (0, 1)
        padding = (high - low) * 0.05 or 1.0
        lower = max(0. if low >= 0. else -float_info.max, low - padding)
        upper = min(float_info.max, high + padding)
        if lower == upper:
            neighbor = nextafter(lower, -float("inf"))
            if isfinite(neighbor):
                lower = neighbor
            else:
                upper = nextafter(upper, float("inf"))
        if not self._dragging:
            self.display_range = self.threshold_bounds or (lower, upper)
        self._size_plot()
        self.update()

    def _size_plot(self):
        self.setFixedHeight(250 + 24 * len(self.threshold_lines) + (32 if self.show_fit else 0))

    def set_candidate(self, candidate):
        """Retain the saved weighted PDFs on their original measurement scale."""
        xs = tuple(candidate.get('density_x', ())) if candidate else ()
        curves = tuple(tuple(row) for row in candidate.get('component_densities', ())) if candidate else ()
        means = tuple(candidate.get('parameters', {}).get('means', ())) if candidate else ()
        valid = (len(xs) > 1 and len(curves) == len(means) == 2
            and all(len(row) == len(xs) for row in curves)
            and all(isfinite(value) for value in (*xs, *means))
            and all(isfinite(value) and value >= 0 for row in curves for value in row)
            and all(right > left for left, right in zip(xs, xs[1:])))
        self.density_x, self.component_densities, self.component_means = (xs, curves, means) if valid else ((), (), ())
        parameters = candidate.get('parameters', {}) if candidate else {}
        total = candidate.get('counts', {}).get('total', 0) if candidate else 0
        self.density_fraction = ((total - parameters.get('scope_zero_count', 0)) / total
            if total and parameters.get('conditional_positive_fit') else 1.)
        self.update()

    def displayed_component_densities(self):
        """Exact-zero Gamma cells add a point mass, not continuous PDF density."""
        return tuple(tuple(value * self.density_fraction for value in row) for row in self.component_densities)

    def set_fit_visible(self, visible):
        self.show_fit = bool(visible and self.density_x)
        self._size_plot()
        self.update()

    def bin_heights(self):
        """Use one density unit for the histogram and saved weighted PDFs."""
        counts, edges = self.histogram['counts'], self.histogram['bin_edges']
        if not self.show_fit:
            return tuple(counts)
        total = sum(counts)
        return tuple(count / (total * (right - left)) if total else 0.
                     for count, left, right in zip(counts, edges, edges[1:]))

    def set_log_counts(self, enabled):
        self.log_counts = enabled
        self.update()

    def count_axis_title(self):
        if self.show_fit:
            return "Density"
        return "Cells (log scale)" if self.log_counts else "Cells"

    def _y_maximum(self):
        heights = self.bin_heights()
        return max((*heights, *(value for row in self.displayed_component_densities() for value in row))
                   if self.show_fit else heights, default=0) or 1

    @staticmethod
    def display_line_label(label):
        names = {
            "triangle": "Triangle",
            "gmm": "Two-component Gaussian mixture",
            "gamma": "Two-component Gamma mixture",
            "nimbus": "Nimbus scores",
            "nimbus_distilled": "Nimbus intensity distillation",
            "manual": "Manual threshold",
        }
        for prefix, caption in (
            ("selected pooled ", "Selected pooled"),
            ("section override ", "Section override"),
            ("selected ", "Selected"),
        ):
            if label.startswith(prefix):
                method = label[len(prefix) :]
                return caption + " · " + names.get(method, method)
        return names.get(label, label)

    def set_empirical_cdf(self, cdf):
        """Index exact value multiplicities once for logarithmic threshold updates.

        Histogram bins cannot recover ties at arbitrary cutoffs. We therefore use
        the supplied finite support and multiplicities, retaining no cell identities.
        The complete finite population supplies the denominator for both fractions.
        """
        values, counts = tuple(cdf.get("values", ())), tuple(cdf.get("counts", ()))
        if (len(values) != len(counts)
                or any(not isinstance(value, (int, float)) or not isfinite(value) for value in values)
                or any(type(count) is not int or count < 1 for count in counts)
                or any(right <= left for left, right in zip(values, values[1:]))):
            raise ValueError("Exact threshold counts require ordered finite values and positive multiplicities")
        self._cdf_values, self._cdf_counts = values, array("Q", accumulate(counts, initial=0))

    def threshold_counts(self, threshold):
        """Return exact finite counts using x ≥ threshold and x < threshold."""
        if not isfinite(threshold):
            raise ValueError("Threshold must be finite")
        below = self._cdf_counts[bisect_left(self._cdf_values, threshold)]
        total = self._cdf_counts[-1]
        return {"above": total - below, "below": below, "total": total}

    def set_manual_threshold(self, threshold):
        """Enable dragging for a finite manual selection; None makes the chart read-only."""
        if threshold is not None and not isfinite(threshold):
            raise ValueError("Threshold must be finite")
        self.manual_threshold = threshold
        self.setCursor(Qt.CursorShape.SplitHCursor if threshold is not None else Qt.CursorShape.ArrowCursor)
        self.update()

    def _plot_rect(self):
        legend = 24 * len(self.threshold_lines)
        extra = 32 if self.show_fit else 0
        label_width = self.fontMetrics().horizontalAdvance("0.00e+00" if self.show_fit else f"{max(self.histogram['counts'], default=0):,}")
        left = max(68, self.fontMetrics().height() + label_width + 19)
        return QRectF(left, 32 + extra, max(1, self.width() - left - 26), max(130, self.height() - 112 - legend - extra))

    def _fraction(self, value):
        low, high = self.display_range
        scale = max(abs(low), abs(high), 1.)
        return (value / scale - low / scale) / (high / scale - low / scale)

    def _drag_to(self, x):
        plot = self._plot_rect()
        fraction = min(1., max(0., (x - plot.left()) / plot.width()))
        low, high = self.display_range
        threshold = low * (1. - fraction) + high * fraction
        if self.threshold_bounds is not None:
            threshold = min(self.threshold_bounds[1], max(self.threshold_bounds[0], threshold))
        self.manual_threshold = threshold
        self.threshold_changed.emit(threshold)
        self.update()

    def _value_at(self, x):
        plot = self._plot_rect()
        fraction = min(1., max(0., (x - plot.left()) / plot.width()))
        low, high = self.display_range
        return low * (1. - fraction) + high * fraction

    def set_selected_range(self, lower, upper):
        if not all(isfinite(v) for v in (lower, upper)) or lower > upper:
            raise ValueError('Choose a finite intensity range with its lower limit first.')
        self.selected_range = (lower, upper)
        self.update()

    def mousePressEvent(self, event):
        if self.manual_threshold is not None and event.button() == Qt.MouseButton.LeftButton and self._plot_rect().contains(event.position()):
            gate_x = self._plot_rect().left() + self._fraction(self.manual_threshold) * self._plot_rect().width()
            if self.range_selection_enabled and abs(event.position().x() - gate_x) > 9:
                self._range_start = self._value_at(event.position().x())
                self.set_selected_range(self._range_start, self._range_start)
                event.accept()
                return
            self._dragging = True
            self._drag_to(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._range_start is not None:
            self.set_selected_range(*sorted((self._range_start, self._value_at(event.position().x()))))
            event.accept()
            return
        if self._dragging:
            self._drag_to(event.position().x())
            event.accept()
            return
        plot=self._plot_rect()
        if plot.contains(event.position()):
            fraction=(event.position().x()-plot.left())/plot.width()
            low,high=self.display_range;value=low*(1-fraction)+high*fraction
            counts,edges=self.histogram['counts'],self.histogram['bin_edges']
            fraction_y = (plot.bottom() - event.position().y()) / plot.height()
            maximum = self._y_maximum()
            y = expm1(fraction_y * log1p(maximum)) if self.log_counts else fraction_y * maximum
            text=f'X {value:.12g} · Y {y:.12g} ' + ('density' if self.show_fit else 'cells')
            if counts and edges[0]<=value<=edges[-1]:
                index=min(len(counts)-1,bisect_right(edges,value)-1)
                text+=f'\nBin {edges[index]!r} to {edges[index+1]!r}\n{counts[index]:,} cells'
            QToolTip.showText(event.globalPosition().toPoint(),text,self)
        else:QToolTip.hideText()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._range_start is not None and event.button() == Qt.MouseButton.LeftButton:
            self.set_selected_range(*sorted((self._range_start, self._value_at(event.position().x()))))
            self._range_start = None
            self.range_selected.emit(*self.selected_range)
            event.accept()
            return
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._drag_to(event.position().x())
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if (self.range_selection_enabled and self.selected_range is not None
                and event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right}
                and event.modifiers() in {Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier}):
            low, high = self.selected_range
            step = (self.display_range[1] - self.display_range[0]) / 256
            step *= 1 if event.key() == Qt.Key.Key_Right else -1
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                low = min(high, max(self.display_range[0], low + step))
            else:
                high = max(low, min(self.display_range[1], high + step))
            self.set_selected_range(low, high)
            self.range_selected.emit(low, high)
            event.accept()
            return
        if self.manual_threshold is not None and event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right}:
            plot = self._plot_rect()
            offset = 1 if event.key() == Qt.Key.Key_Right else -1
            self._drag_to(plot.left() + self._fraction(self.manual_threshold) * plot.width() + offset)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event):
        """Paint saved bins and selected lines; fitting never runs on Qt callbacks."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()
        x_position = lambda value: plot.left() + self._fraction(value) * plot.width()
        counts, edges = self.histogram["counts"], self.histogram["bin_edges"]
        maximum = self._y_maximum()
        ticks = [0., maximum / 2, maximum]
        if self.log_counts:
            ticks = [0, 1]
            while ticks[-1] * 10 <= maximum:
                ticks.append(ticks[-1] * 10)
        def height_fraction(value):
            return log1p(value) / log1p(maximum) if self.log_counts else value / maximum
        for tick in ticks:
            y = plot.bottom() - height_fraction(tick) * plot.height()
            painter.setPen(QPen(QColor("#e6eded"), 1))
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(QColor("#61767c"))
            caption = f'{tick:.3g}' if self.show_fit else f'{tick:,.0f}'
            painter.drawText(QRectF(20, y - 10, plot.left() - 27, 20),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, caption)
        for index, count in enumerate(self.bin_heights()):
            left, right = x_position(edges[index]), x_position(edges[index + 1])
            height = height_fraction(count) * plot.height()
            painter.fillRect(QRectF(left, plot.bottom() - height, max(1., right - left - .5), height), QColor("#91bdb6"))
        if self.show_fit:
            painter.save()
            painter.setClipRect(plot)
            for curve, color, style in zip(self.displayed_component_densities(),
                    ('#357e9c', '#a25a35'), (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine)):
                path = QPainterPath()
                for index, (x, y) in enumerate(zip(self.density_x, curve)):
                    point = (x_position(x), plot.bottom() - y / maximum * plot.height())
                    path.moveTo(*point) if index == 0 else path.lineTo(*point)
                painter.setPen(QPen(QColor(color), 2.2, style))
                painter.drawPath(path)
            painter.restore()
            self._paint_component_means(painter, plot, x_position)
        if self.selected_range is not None and self.range_selection_enabled:
            left, right = (max(plot.left(), min(plot.right(), x_position(v))) for v in self.selected_range)
            painter.fillRect(QRectF(left, plot.top(), right-left, plot.height()), QColor(230, 162, 60, 48))
            painter.setPen(QPen(QColor('#bb7b1c'), 1.3))
            for x in (left, right):
                painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
        painter.setPen(QColor("#486169"))
        painter.save()
        painter.translate(self.fontMetrics().height() / 2 + 2, plot.center().y())
        painter.rotate(-90)
        height = self.fontMetrics().height()
        painter.drawText(QRectF(-plot.height() / 2, -height / 2, plot.height(), height),
                         Qt.AlignmentFlag.AlignCenter, self.count_axis_title())
        painter.restore()
        low, high = self.display_range
        divisions = min(5, max(2, int(plot.width() / 96)))
        for tick in range(1, divisions):
            part = tick / divisions
            x = plot.left() + part * plot.width()
            value = (1 - part) * low + part * high
            painter.drawLine(int(x), int(plot.bottom()), int(x), int(plot.bottom() + 4))
            painter.drawText(QRectF(x - 44, plot.bottom() + 8, 88, 22), Qt.AlignmentFlag.AlignCenter, f"{value:.3g}")
        painter.drawText(int(plot.left()), int(plot.bottom() + 25), f"{low:.5g}")
        painter.drawText(QRectF(plot.right() - 100, plot.bottom() + 8, 100, 22), Qt.AlignmentFlag.AlignRight, f"{high:.5g}")
        painter.drawText(QRectF(plot.left(), plot.bottom() + 30, plot.width(), 22), Qt.AlignmentFlag.AlignCenter, self.axis_label)
        if not counts:
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, self.empty_message)
        lines = list(self.threshold_lines)
        if self.manual_threshold is not None and not any(style == Qt.PenStyle.SolidLine for _, _, style in lines):
            lines.append(("selected manual", self.manual_threshold, Qt.PenStyle.SolidLine))
        for index, (label, value, style) in enumerate(lines):
            selected = style == Qt.PenStyle.SolidLine
            painter.setPen(QPen(QColor("#163a46" if selected else "#c08444"), 2.6 if selected else 1.5, style))
            x = x_position(value)
            painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
            if selected and self.manual_threshold is not None:
                painter.setBrush(QColor("#163a46"))
                painter.drawEllipse(QRectF(x - 5, plot.top() - 5, 10, 10))
            y = int(plot.bottom() + 77 + index * 24)
            painter.drawLine(12, y - 4, 38, y - 4)
            caption = format(value, ".6g") if self.axis_label == "Nimbus score" else _intensity_gate_caption(value)
            text = self.display_line_label(label)
            painter.drawText(48, y, text + (f"  ·  {caption}" if self.show_legend_values else ""))
        painter.end()

    def _paint_component_means(self, painter, plot, x_position):
        labels = []
        for name, value in zip(('Lower mean', 'Higher mean'), self.component_means):
            text = f'{name} {value:.2f}'
            width = painter.fontMetrics().horizontalAdvance(text) + 8
            left = max(plot.left(), min(plot.right() - width, x_position(value) - width / 2))
            labels.append((text, QRectF(left, plot.top() - 24, width, 22)))
        if labels[0][1].right() + 8 > labels[1][1].left():
            labels[0][1].translate(0, -22)
        for (text, rect), mean, color in zip(labels, self.component_means, ('#357e9c', '#a25a35')):
            painter.setPen(QPen(QColor(color), 1, Qt.PenStyle.DotLine))
            x = max(plot.left(), min(plot.right(), x_position(mean)))
            painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class PopulationSelectionEditor(QGroupBox):
    """Choose a dataset population and explicit replacements for individual images.

    Only authenticated, offered region classes are selectable. We retain stable
    image and class identities behind readable labels; a per-image choice replaces
    the dataset default, and disabling it restores inheritance without stale values.
    """

    changed = Signal()

    def __init__(self, options, settings, parent=None):
        super().__init__("Cells to phenotype", parent)
        self.options = options
        self._loading = False
        self._settings = {}
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self.target = QComboBox(self)
        self.target.setAccessibleName("Population setting scope")
        self.target.addItem("Shared across images", None)
        for slide in options.get("slides", ()):
            self.target.addItem(f"{slide['sample_id']} · {slide['slide_id']}", slide["image_identity"])
        layout.addWidget(self.target)
        self.override = QCheckBox("Use a different population for this image", self)
        layout.addWidget(self.override)
        self.restrict = QCheckBox("Restrict to selected regions", self)
        self.restrict.setObjectName("marker-restrict-cells")
        layout.addWidget(self.restrict)
        self.kind = QComboBox(self)
        self.kind.setAccessibleName("Cells to phenotype")
        layout.addWidget(self.kind)
        self.classes = QListWidget(self)
        self.classes.setAccessibleName("Included region classes")
        self.classes.setMaximumHeight(150)
        self.classes.setMinimumHeight(85)
        layout.addWidget(self.classes)
        self.help = QLabel(self)
        self.help.setWordWrap(True)
        self.help.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.help)
        self.target.currentIndexChanged.connect(self._show_target)
        self.override.toggled.connect(self._override_changed)
        self.restrict.toggled.connect(self._restriction_changed)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.classes.itemChanged.connect(self._save)
        self.set_value(settings)

    def value(self):
        return {"default": dict(self._settings["default"]),
                "slide_overrides": {key: dict(value) for key, value in self._settings["slide_overrides"].items()}}

    def matches(self, settings):
        """Compare canonical selections across mutable and frozen presentation values."""
        def selection(value):
            return value["kind"], tuple(value["class_ids"])
        return (selection(self._settings["default"]) == selection(settings["default"])
            and {key: selection(value) for key, value in self._settings["slide_overrides"].items()}
                == {key: selection(value) for key, value in settings["slide_overrides"].items()})

    def set_value(self, settings):
        self._settings = {"default": {"kind": settings["default"]["kind"], "class_ids": list(settings["default"]["class_ids"])},
            "slide_overrides": {key: {"kind": value["kind"], "class_ids": list(value["class_ids"])} for key, value in settings["slide_overrides"].items()}}
        slides = self.options.get("slides", ())
        self.target.setVisible(len(slides) > 1)
        if len(slides) == 1:
            key = slides[0]["image_identity"]
            self.target.setCurrentIndex(self.target.findData(key) if key in self._settings["slide_overrides"] else 0)
        self._show_target()

    def _offers(self):
        key = self.target.currentData()
        rows = tuple(self.options.get("slides", ()))
        if key is not None:
            return next(row for row in rows if row["image_identity"] == key)
        if not rows:
            return {"available_kinds": ("all_segmented",), "primary_classes": (), "secondary_classes": ()}
        kinds = set(rows[0]["available_kinds"])
        for row in rows[1:]:
            kinds.intersection_update(row["available_kinds"])
        result = {"available_kinds": kinds}
        for field in ("primary_classes", "secondary_classes"):
            common = {item["class_id"] for item in rows[0].get(field, ())}
            for row in rows[1:]:
                common.intersection_update(item["class_id"] for item in row.get(field, ()))
            result[field] = tuple(item for item in rows[0].get(field, ()) if item["class_id"] in common)
        return result

    def _show_target(self, *_args):
        self._loading = True
        key = self.target.currentData()
        overridden = key in self._settings["slide_overrides"]
        choice = self._settings["slide_overrides"].get(key, self._settings["default"])
        self.override.setVisible(key is not None and len(self.options.get("slides", ())) > 1)
        self.override.setChecked(overridden)
        self.kind.clear()
        names = (("detected_tissue", "Detected tissue"),
                 ("primary_structure", "Primary tissue classes"))
        offered = self._offers()
        for kind, label in names:
            if kind in offered["available_kinds"]:
                self.kind.addItem(label, kind)
        index = self.kind.findData(choice["kind"])
        if index < 0 and choice["kind"] != "all_segmented":
            self.kind.addItem("Unavailable population · refresh source data", choice["kind"])
            index = self.kind.count() - 1
            self.kind.model().item(index).setEnabled(False)
        self.kind.setCurrentIndex(max(0, index))
        enabled = key is None or overridden
        self.restrict.setChecked(choice["kind"] != "all_segmented")
        self.restrict.setVisible(self.kind.count() > 0)
        self.restrict.setEnabled(enabled)
        self.kind.setVisible(self.restrict.isChecked())
        self.kind.setEnabled(enabled)
        self._load_classes(choice["class_ids"])
        self._loading = False

    def _load_classes(self, selected):
        kind = self.kind.currentData() if self.restrict.isChecked() else "all_segmented"
        self.classes.clear()
        field = "primary_classes"
        region_kind = kind == "primary_structure"
        if region_kind:
            for row in self._offers().get(field, ()):
                item = QListWidgetItem(row.get("label", row["class_id"]), self.classes)
                item.setData(Qt.ItemDataRole.UserRole, row["class_id"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if row["class_id"] in selected else Qt.CheckState.Unchecked)
        self.classes.setVisible(region_kind)
        self.classes.setEnabled(self.kind.isEnabled())
        descriptions = {
            "all_segmented": "All cells from the accepted analysis regions.",
            "detected_tissue": "Include cells whose centres lie within the detected tissue.",
            "primary_structure": "Use cells assigned to the selected primary region classes.",
        }
        self.help.setText(descriptions.get(kind, "Choose a population supported by the current measurements."))

    def _restriction_changed(self, enabled):
        if self._loading:
            return
        self._loading = True
        self.kind.setVisible(enabled)
        self._load_classes(())
        self._loading = False
        self._save()

    def _override_changed(self, enabled):
        if self._loading:
            return
        key = self.target.currentData()
        if key is None:
            return
        if enabled:
            self._settings["slide_overrides"][key] = dict(self._settings["default"])
        else:
            self._settings["slide_overrides"].pop(key, None)
        self._show_target()
        self.changed.emit()

    def _kind_changed(self, *_args):
        if self._loading:
            return
        self._loading = True
        self._load_classes(())
        self._loading = False
        self._save()

    def _save(self, *_args):
        if self._loading:
            return
        choice = {"kind": self.kind.currentData() if self.restrict.isChecked() else "all_segmented", "class_ids": sorted(
            self.classes.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.classes.count()) if self.classes.item(index).checkState() == Qt.CheckState.Checked)}
        key = self.target.currentData()
        if key is None:
            self._settings["default"] = choice
        elif self.override.isChecked():
            self._settings["slide_overrides"][key] = choice
        self.changed.emit()


class MarkerSetupWorkbench(QWidget):
    """Inspect a frozen population before choosing the current marker's method.

    We move the renderer's semantic editors into a focused workspace; their values
    and validation remain owned by the form adapter. Preview navigation changes
    only the displayed histogram, never the configured fitting population.
    """

    def __init__(self, renderer, parent=None):
        super().__init__(parent)
        self.renderer = renderer
        self.context = renderer._form.context["stage_result"]["resume_context"]
        self.workflow = self.context.get("marker_workflow") or {}
        self._preview_identity = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(18)
        self.rail = MarkerProgressRail(self.context["canonical_marker_ids"], self)
        self.rail.setFlow(QListWidget.Flow.LeftToRight)
        self.rail.setMaximumWidth(16777215)
        self.rail.setMaximumHeight(88)
        self.rail.setVisible(self.rail.count() > 1 and not self.workflow)
        outer.addWidget(self.rail)
        body = QWidget(self)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        self.heading = QLabel(body)
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet("font-size: 21px; font-weight: 600; color: #173c42")
        body_layout.addWidget(self.heading)
        self.revisit_button = ContentSizedButton("Revisit a confirmed marker…", body)
        self.revisit_button.setObjectName("revisit-confirmed-marker")
        self.revisit_button.setProperty("actionRole", "secondary")
        self.revisit_button.setVisible(bool(self.workflow.get("completed")) and 'return_marker_id' not in self.workflow)
        self.revisit_button.clicked.connect(self._choose_revisit)
        body_layout.addWidget(self.revisit_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self._revisit_dialog = None
        self.measurement_fields = QVBoxLayout()
        self.measurement_fields.setSpacing(4)
        body_layout.addLayout(self.measurement_fields)
        self.columns = QVBoxLayout()
        self.columns.setSpacing(18)
        body_layout.addLayout(self.columns)
        self.distribution_panel = QWidget(body)
        distribution_layout = QVBoxLayout(self.distribution_panel)
        distribution_layout.setContentsMargins(0, 0, 0, 0)
        distribution_layout.setSpacing(10)
        self.settings_panel = QWidget(body)
        settings_layout = QVBoxLayout(self.settings_panel)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(10)
        self.columns.addWidget(self.distribution_panel, 5)
        self.columns.addWidget(self.settings_panel, 3)
        preview_row = QHBoxLayout()
        preview_label = QLabel("Slide", self)
        self.preview_label = preview_label
        self.population = QComboBox(self)
        self.population.setAccessibleName("Histogram population")
        self.population.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.population.setMinimumContentsLength(12)
        preview_label.setBuddy(self.population)
        preview_row.addWidget(preview_label)
        preview_row.addWidget(self.population, 1)
        distribution_layout.addLayout(preview_row)
        self.counts = QLabel(self)
        self.counts.setStyleSheet("color: #526971; padding: 2px 0")
        self.counts.setWordWrap(True)
        distribution_layout.addWidget(self.counts)
        self.chart = MarkerHistogram(self)
        self.log_counts = QCheckBox("Log count scale", self.chart)
        self.log_counts.setToolTip("Show sparse bins on a log(1 + count) scale. Hover over a bin for its cell count.")
        self.log_counts.toggled.connect(self.chart.set_log_counts)
        self.chart.install_axis_controls(self.log_counts)
        self.chart.setMinimumWidth(240)
        distribution_layout.addWidget(self.chart)
        distribution_layout.addStretch(1)
        self.method_fields = QVBoxLayout()
        settings_layout.addLayout(self.method_fields)
        self.advanced_area = QWidget(renderer)
        advanced_layout = QVBoxLayout(self.advanced_area)
        advanced_layout.setContentsMargins(0, 4, 0, 8)
        advanced_layout.setSpacing(12)
        self.advanced = QToolButton(self.advanced_area)
        self.advanced.setObjectName("marker-model-advanced")
        self.advanced.setText("Advanced settings")
        self.advanced.setCheckable(True)
        self.advanced.toggled.connect(renderer._apply_marker_visibility)
        from .researcher_widgets import configure_disclosure
        configure_disclosure(self.advanced)
        advanced_layout.addWidget(self.advanced, alignment=Qt.AlignmentFlag.AlignLeft)
        self.parameters = QVBoxLayout()
        self.parameters.setSpacing(6)
        advanced_layout.addLayout(self.parameters)
        settings_layout.addStretch(1)
        self.bounds_panel = QWidget(self.advanced_area)
        limits = QGridLayout(self.bounds_panel)
        limits.setContentsMargins(0, 2, 0, 0)
        limits.setHorizontalSpacing(14)
        limits.setVerticalSpacing(8)
        limits.setColumnStretch(1, 1)
        limits.setColumnStretch(2, 1)
        self.bounds_help = QLabel(self.bounds_panel)
        self.bounds_help.setProperty("fieldHelp", True)
        self.bounds_help.setWordWrap(True)
        limits.addWidget(self.bounds_help, 0, 0, 1, 3)
        limits.setRowMinimumHeight(1, 7)
        for column, title in enumerate(("Minimum", "Maximum"), start=1):
            heading = QLabel(title, self.bounds_panel)
            heading.setProperty("fieldHelp", True)
            limits.addWidget(heading, 2, column)
        self.limit_labels = []
        for row in (3, 4):
            label = QLabel(self.bounds_panel)
            self.limit_labels.append(label)
            limits.addWidget(label, row, 0)
        self.parameters.addWidget(self.bounds_panel)
        self._limit_drafts = {}
        for marker in self.context["canonical_marker_ids"]:
            prefix = f"marker_{marker}_"
            for suffix in ("feature", "method", "nimbus_source", "nimbus_cutoff",
                           "nimbus_score_path", "nimbus_checkpoint_path", "nimbus_magnification"):
                target = self.measurement_fields if suffix == "feature" else self.parameters if suffix == "nimbus_checkpoint_path" else self.method_fields
                for widget in renderer._marker_rows[prefix + suffix]:
                    target.addWidget(widget)
            for row, component in enumerate(("lower", "upper"), start=3):
                for column, endpoint in enumerate(("min", "max"), start=1):
                    label, field = renderer._marker_rows[prefix + component + "_" + endpoint]
                    label.hide()
                    limits.addWidget(field, row, column)
            picker = renderer._editors[prefix + "method"]
            picker.setProperty("limitFamily", self._limit_family(picker.currentData()))
            picker.currentIndexChanged.connect(lambda _index, key=prefix: self._switch_limit_family(key))
        outer.addWidget(body, 1)
        self.rail.marker_selected.connect(self._select_marker)
        self.population.currentIndexChanged.connect(self._show_preview)
        current = self.workflow.get("current_marker_id")
        if current:
            renderer._marker_selector.setCurrentIndex(renderer._marker_selector.findData(current))
            for index in range(self.rail.count()):
                item = self.rail.item(index)
                if item.data(Qt.ItemDataRole.UserRole) != current:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.refresh()

    @staticmethod
    def _limit_family(method):
        if method in ("gmm", "gmm_separate"):
            return "mean"
        return "peak" if method == "gamma" else ""

    def _switch_limit_family(self, prefix):
        picker = self.renderer._editors[prefix + "method"]
        previous = picker.property("limitFamily")
        current = self._limit_family(picker.currentData())
        if current == previous:
            return
        editors = [self.renderer._editors[prefix + suffix] for suffix in
                   ("lower_min", "lower_max", "upper_min", "upper_max")]
        if previous:
            self._limit_drafts[prefix, previous] = [editor.text() for editor in editors]
        for editor, value in zip(editors, self._limit_drafts.get((prefix, current), ("",) * 4)):
            editor.setText(value)
        picker.setProperty("limitFamily", current)

    def _select_marker(self, marker):
        self.renderer._marker_selector.setCurrentIndex(self.renderer._marker_selector.findData(marker))

    def _choose_revisit(self):
        """Open a saved method without changing the current sequence."""
        menu = QMenu(self.revisit_button)
        for item in self.workflow.get("completed", ()):
            marker = item["marker_rule"]["marker_id"]
            menu.addAction(marker, lambda checked=False, selected=marker:
                           self.renderer.revisit_marker(selected))
        menu.exec(self.revisit_button.mapToGlobal(self.revisit_button.rect().bottomLeft()))

    def refresh(self):
        marker = self.renderer._marker_selector.currentData()
        if marker is None:
            return
        prefix = f"marker_{marker}_"
        self.rail.set_current_marker(marker)
        index = self.context["canonical_marker_ids"].index(marker)
        total = len(self.context["canonical_marker_ids"])
        self.heading.setText(marker)
        completed = {entry["marker_rule"]["marker_id"] for entry in self.workflow.get("completed", ())}
        for key in self.context["canonical_marker_ids"]:
            chosen = self.renderer._editors[f"marker_{key}_method"].currentData()
            self.rail.set_marker_status(key, "Reviewed" if key in completed else "Current marker" if key == marker else "Configured" if chosen else "Upcoming")
        method = self.renderer._editors[prefix + "method"].currentData()
        mixture = method in {"gmm", "gmm_separate", "gamma"}
        self.advanced.setVisible(mixture or method == "nimbus")
        self.advanced.setText("Component limits" if mixture else "Advanced settings")
        self.bounds_panel.setVisible(mixture and self.advanced.isChecked())
        if mixture:
            quantity = "peak" if method == "gamma" else "mean"
            self.bounds_help.setText(f"Set limits for each component’s {quantity} using an intensity or a percentile such as Q95.")
            for label, rank in zip(self.limit_labels, ("Lower", "Higher")):
                label.setText(f"{rank} mean" if method != "gamma" else f"Peak of {rank.lower()}-mean component")
            for component, caption in (("lower", "Lower-mean"), ("upper", "Higher-mean")):
                for bound, bound_caption in (("min", "minimum"), ("max", "maximum")):
                    editor = self.renderer._editors[prefix + component + "_" + bound]
                    editor.setAccessibleName(f"{caption} component, {bound_caption} {quantity}")
        feature = self.renderer._editors[prefix + "feature"].currentData()
        self.renderer._editors[prefix + "method"].setEnabled(feature is not None)
        feature_help = self.renderer.findChild(QLabel, "form-help-" + prefix + "feature")
        if feature_help is not None:
            feature_help.hide()
        fit_scope = self.renderer._editors["marker_fit_scope"].currentData()
        identity = (marker, feature, fit_scope)
        if identity != self._preview_identity:
            self._preview_identity = identity
            self.population.blockSignals(True)
            self.population.clear()
            distribution = next((entry for entry in self.context.get("marker_distributions", ()) if entry["marker_id"] == marker and entry["feature_id"] == feature), None)
            self._pooled_distribution = None
            if distribution:
                self._pooled_distribution = distribution["pooled"]
                if fit_scope == "per_slide":
                    for slide in distribution.get("per_slide", ()):
                        self.population.addItem(f"{slide['sample_id']} · {slide['slide_id']}", slide)
                else:
                    self.population.addItem("All included slides", self._pooled_distribution)
                self.chart.axis_label = _intensity_label(distribution)
            else:
                self.population.addItem("Choose a cell measurement" if feature is None else "Preview unavailable", None)
            self.population.blockSignals(False)
        self.population.setVisible(self.population.count() > 1)
        self.preview_label.setVisible(self.population.count() > 1)
        self._show_preview()

    def has_empty_selected_population(self):
        """Block fitting only when the complete selected population has no cells.

        An empty individual image does not invalidate populated sibling images.
        Missing intensities still represent selected cells and may have Nimbus scores.
        """
        pooled = getattr(self, "_pooled_distribution", None)
        return pooled is not None and pooled.get("eligible_count", 0) + pooled.get("unknown_count", 0) == 0

    def _show_preview(self, *_args):
        self.population.setToolTip(self.population.currentText())
        population = self.population.currentData()
        self.chart.empty_message = "No valid measurements in this population"
        if population is None:
            self.chart.set_evidence({"counts": (), "bin_edges": ()}, ())
            self.counts.setText("Choose a cell measurement to view its intensity distribution." if self.renderer._editors[f"marker_{self.renderer._marker_selector.currentData()}_feature"].currentData() is None else "A preview was not supplied for this measurement.")
            return
        finite = population.get("eligible_count", 0)
        unknown = population.get("unknown_count", 0)
        selected = population.get("selected_count", finite + unknown)
        counts = f"{finite:,} cells have valid measurements."
        if unknown:
            counts += f" {unknown:,} measurements are unknown."
        if selected != finite + unknown:
            counts += f" The selected population contains {selected:,} cells."
        if finite + unknown == 0:
            self.chart.empty_message = "No cells in this population"
            counts += ("\nNo cells in the selected region. Choose another population and refresh its distribution before fitting."
                if self.has_empty_selected_population() else "\nNo cells in this image's selected regions. Other selected images remain available for fitting.")
        elif finite == 0:
            counts += "\nSelected cells have no finite intensities. Nimbus can use available image scores."
        self.counts.setText(counts)
        self.chart.set_evidence(population["histogram"], ())


class MarkerGatePanel(QWidget):
    """Review one marker/image with saved fits and responsive threshold decisions.

    We reuse the form's real editors, so a drag, numeric edit and submitted
    response share the same value. Saving records the displayed decision for
    that image; editing its cutoff clears only that saved decision.
    """

    image_requested = Signal()
    field_requested = Signal()

    def __init__(self, form, renderer, parent=None):
        """Build a distribution-led review using the renderer's retained editors.

        The optional fit overlay shares the histogram's scale. Existing editors
        retain exact decisions, with model parameters available in Fit details.
        """
        super().__init__(parent)
        self.setObjectName("marker-gate-review")
        self._form, self._renderer = form, renderer
        self._reviews = form.context["resume_context"]["marker_reviews"]
        self._rows = renderer._marker_review_rows
        self._editors = renderer._editors
        self._refreshing = False
        self._current_prefix = ""
        self._image_dialog = None
        self._image_gate = None
        self._field_review = None
        self._cdf_identity = None
        self._details_identity = None
        self._fit_identity = None
        renderer._heading.hide()
        renderer._message.hide()
        renderer.layout().setContentsMargins(0, 0, 0, 0)
        self.setStyleSheet("""
            QFrame#marker-review-card { background: transparent; border: none; }
            QFrame#marker-tissue-section, QFrame#marker-distillation-section { background: white; border: 1px solid #dbe4e5; border-radius: 3px; }
            QLabel#marker-review-heading { color: #173d47; font-size: 21px; font-weight: 600; }
            QLabel#marker-review-coverage, QLabel#marker-review-context { color: #526971; }
            QLabel#marker-above-count, QLabel#marker-below-count { color: #244f56; background: #edf5f3; border-radius: 7px; padding: 10px; }
            QLabel#marker-selection-status, QLabel#marker-component-summary { color: #48616a; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        if 'refit_current_marker' in self._editors:
            self.back_button = ContentSizedButton('Back: marker method', self)
            self.back_button.setProperty('actionRole', 'secondary')
            self.back_button.clicked.connect(self._request_refit)
            self.back_button.setToolTip('Return to this marker’s method and fitting choices. Earlier accepted markers are retained.')
            layout.addWidget(self.back_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.coverage = QLabel(self)
        self.coverage.setObjectName("marker-review-coverage")
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        self.main_card = QFrame(self)
        self.main_card.setObjectName("marker-review-card")
        self.main_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        main = QVBoxLayout(self.main_card)
        main.setContentsMargins(22, 20, 22, 20)
        main.setSpacing(10)
        layout.addWidget(self.main_card)
        self.heading = QLabel(self.main_card)
        self.heading.setObjectName("marker-review-heading")
        self.heading.setWordWrap(True)
        title_row = QHBoxLayout()
        title_row.addWidget(self.heading, 1)
        main.addLayout(title_row)
        self.semantic = QLabel(self.main_card)
        self.semantic.setObjectName("marker-review-context")
        self.semantic.setWordWrap(True)
        main.addWidget(self.semantic)
        navigation = QHBoxLayout()
        self.marker = QComboBox(self)
        self.image_navigation = CohortImageNavigation(self)
        self.scope = self.image_navigation.images
        self._navigation_fields = []
        for title, combo in (("Marker", self.marker), ("Image", self.scope)):
            field = QWidget(self.main_card)
            column = QVBoxLayout(field)
            column.setContentsMargins(0, 0, 0, 0)
            label = QLabel(title, field)
            label.setBuddy(combo)
            combo.setAccessibleName(title)
            column.addWidget(label)
            column.addWidget(self.image_navigation if combo is self.scope else combo)
            navigation.addWidget(field, 1)
            self._navigation_fields.append(field)
        main.addLayout(navigation)
        for marker in self._reviews:
            self.marker.addItem(marker["marker_id"], marker["marker_id"])
        self._navigation_fields[0].setVisible(self.marker.count() > 1)
        self.plot_header = QWidget(self.main_card)
        plot_header = QHBoxLayout(self.plot_header)
        plot_header.setContentsMargins(0, 2, 0, 0)
        self.gate_header = QWidget(self.plot_header)
        self.gate_fields = QGridLayout(self.gate_header)
        self.gate_fields.setContentsMargins(0, 0, 0, 0)
        self.gate_fields.setVerticalSpacing(4)
        plot_header.addWidget(self.gate_header)
        plot_header.addStretch(1)

        self.chart = MarkerHistogram(self.main_card)
        self.show_fit = QCheckBox("Show fitted components", self.chart)
        self.show_fit.toggled.connect(self._toggle_fit)
        self.score_log = QCheckBox("Log count scale", self.chart)
        self.score_log.setToolTip("Show sparsely populated score intervals on a logarithmic count scale.")
        self.score_log.toggled.connect(self.chart.set_log_counts)
        self.chart.install_axis_controls(self.show_fit, self.score_log)
        self.chart.show_legend_values = False
        self.chart.threshold_changed.connect(self._dragged)
        self.chart.range_selected.connect(self._inspect_range)
        main.addWidget(self.chart)
        self.chart_hint = QLabel(self.main_card)
        self.chart_hint.setWordWrap(True)
        self.chart_hint.setProperty('fieldHelp', True)
        main.addWidget(self.chart_hint)
        main.addWidget(self.plot_header)
        self.export_button = ContentSizedButton("Export figure…", self.main_card)
        self.export_button.setObjectName("marker-export-figure")
        from PySide6.QtWidgets import QMenu
        export_menu = QMenu(self.export_button)
        export_menu.addAction('Histogram…', self._export_figure)
        self.export_fit = export_menu.addAction('Fitted distribution…', self._export_fit)
        self.export_button.setMenu(export_menu)
        title_row.addWidget(self.export_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.distillation = QFrame(self)
        self.distillation.setObjectName("marker-distillation-section")
        distillation_layout = QGridLayout(self.distillation)
        distillation_layout.setContentsMargins(16, 10, 16, 12)
        distillation_layout.setHorizontalSpacing(24)
        distillation_layout.setColumnStretch(0, 1)
        distillation_layout.setVerticalSpacing(6)
        self.distillation_title = QLabel("Intensity distillation", self.distillation)
        self.distillation_title.setProperty("panelHeading", True)
        distillation_layout.addWidget(self.distillation_title, 0, 0)
        self.distillation_action = ContentSizedButton("Fit intensity threshold", self.distillation)
        self.distillation_action.setProperty("actionRole", "secondary")
        self.distillation_action.clicked.connect(self._request_distillation)
        distillation_layout.addWidget(self.distillation_action, 0, 1, 2, 1,
                                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.distillation_note = QLabel(self.distillation)
        self.distillation_note.setWordWrap(True)
        self.distillation_note.setProperty("fieldHelp", True)
        distillation_layout.addWidget(self.distillation_note, 1, 0)
        self.distillation_matrix = QWidget(self.distillation)
        matrix = QGridLayout(self.distillation_matrix)
        matrix.setContentsMargins(0, 6, 0, 0)
        matrix.setSpacing(3)
        self.distillation_counts = []
        for index, title in enumerate(("Intensity positive", "Intensity negative"), 1):
            column = QLabel(title, self.distillation_matrix)
            column.setAlignment(Qt.AlignmentFlag.AlignCenter)
            matrix.addWidget(column, 0, index)
            matrix.setColumnStretch(index, 1)
        for row, title in enumerate(("Nimbus positive", "Nimbus negative"), 1):
            matrix.addWidget(QLabel(title, self.distillation_matrix), row, 0)
            for col in range(1, 3):
                count = QLabel(self.distillation_matrix)
                count.setAlignment(Qt.AlignmentFlag.AlignCenter)
                count.setMargin(5)
                matrix.addWidget(count, row, col)
                self.distillation_counts.append(count)
        distillation_layout.addWidget(self.distillation_matrix, 2, 0, 1, 2)
        self.distillation_fields = QGridLayout()
        self.distillation_fields.setContentsMargins(0, 8, 0, 0)
        self.distillation_fields.setHorizontalSpacing(16)
        self.distillation_fields.setVerticalSpacing(4)
        self.distillation_fields.setColumnStretch(0, 3)
        self.distillation_fields.setColumnStretch(1, 2)
        distillation_layout.addLayout(self.distillation_fields, 3, 0, 1, 2)
        layout.addWidget(self.distillation)
        tissue_section = QFrame(self)
        tissue_section.setObjectName("marker-tissue-section")
        self.tissue_layout = QVBoxLayout(tissue_section)
        self.tissue_layout.setContentsMargins(16, 10, 16, 10)
        tissue_header = QHBoxLayout()
        tissue_title = QLabel("Cells in tissue", tissue_section)
        tissue_title.setProperty("panelHeading", True)
        tissue_header.addWidget(tissue_title, 1)
        self.inspect_button = ContentSizedButton("Open tissue view", tissue_section)
        self.inspect_button.setProperty("actionRole", "secondary")
        tissue_header.addWidget(self.inspect_button)
        self.tissue_layout.addLayout(tissue_header)
        layout.addWidget(tissue_section)
        self.inspect_button.setCheckable(True)
        self.inspect_button.toggled.connect(self._toggle_tissue)
        counts = QHBoxLayout()
        self.above_count, self.below_count = QLabel(self), QLabel(self)
        self.above_count.setObjectName("marker-above-count")
        self.below_count.setObjectName("marker-below-count")
        for label in (self.above_count, self.below_count):
            label.setWordWrap(True)
            counts.addWidget(label, 1)
        main.addLayout(counts)
        self.status = QLabel(self.main_card)
        self.status.setObjectName("marker-selection-status")
        self.status.setWordWrap(True)
        main.addWidget(self.status)
        self.decision_fields = QGridLayout()
        self.decision_fields.setColumnStretch(0, 1)
        self.decision_fields.setColumnStretch(1, 1)
        self.decision_fields.setHorizontalSpacing(22)
        self.decision_fields.setVerticalSpacing(5)
        main.addLayout(self.decision_fields)
        distilled_prefixes = set()
        for index, marker_review in enumerate(self._reviews):
            scopes = [(f"m{index}", marker_review["pooled_review"])]
            scopes.extend((f"m{index}_s{slide}", entry["review"])
                          for slide, entry in enumerate(marker_review.get("per_slide_reviews", ())))
            for prefix, review in scopes:
                if any(item["method"] == "nimbus_distilled" for item in review["candidates"]):
                    distilled_prefixes.add(prefix)
        self._gate_step_actions = {}
        for name, (label, field) in self._rows.items():
            scope = re.match(r"(m\d+(?:_s\d+)?)_", name)
            distilled = scope is not None and scope[1] in distilled_prefixes
            label.hide()
            field.hide()
            label.setParent(self.main_card)
            field.setParent(self.main_card)
            if name.endswith("_source") and not name.endswith("_adjustment_source"):
                label.setText("Classify cells by" if distilled else "Threshold method")
                if distilled:
                    field.layout().setContentsMargins(0, 0, 0, 0)
                    field.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
                row, column = 0, 0
            elif name.endswith(("_manual", "_adjusted_threshold", "_adjusted_score_cutoff")):
                label.setText("Nimbus score cutoff" if name.endswith("_score_cutoff") else "Final intensity gate")
                editor = self._editors[name]
                palette = editor.palette()
                palette.setColor(QPalette.ColorRole.PlaceholderText, palette.color(QPalette.ColorRole.Text))
                editor.setPalette(palette)
                editor.setToolTip("Enter an intensity cutoff." if name.endswith("_manual")
                    else "Type a value to adjust the gate. Clear the value to retain the saved proposal.")
                label.setParent(self.gate_header)
                label.setContentsMargins(0, 0, 0, 0)
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                field.setParent(self.gate_header)
                field.layout().setContentsMargins(0, 0, 0, 0)
                help_label = field.findChild(QLabel, 'form-help-' + name)
                if help_label is not None:
                    help_label.hide()
                if distilled:
                    field.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
                    self.distillation_fields.addWidget(label, 0, 1)
                    self.distillation_fields.addWidget(field, 1, 1)
                    for direction, icon, key in ((-1, "decrement", Qt.Key.Key_Down),
                                                 (1, "increment", Qt.Key.Key_Up)):
                        action = editor.addAction(QIcon(str(Path(__file__).with_name("assets") / f"{icon}.svg")),
                                                  QLineEdit.ActionPosition.TrailingPosition)
                        action.setText("Previous measured value" if direction < 0 else "Next measured value")
                        action.setToolTip(action.text())
                        action.setShortcut(QKeySequence(key))
                        action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
                        action.triggered.connect(lambda checked=False, e=editor, d=direction: self._step_cutoff(e, d))
                        self._gate_step_actions[(name, direction)] = action
                    continue
                field.setFixedWidth(132)
                self.gate_fields.addWidget(label, 0, 0, Qt.AlignmentFlag.AlignVCenter)
                self.gate_fields.addWidget(field, 0, 1, Qt.AlignmentFlag.AlignVCenter)
                continue
            elif name.endswith("_nimbus_distill"):
                choice = self._editors[name]
                if choice.itemData(0) is None:
                    choice.removeItem(0)
                continue
            else:
                continue
            grid = self.distillation_fields if distilled else self.decision_fields
            grid.addWidget(label, row, column)
            grid.addWidget(field, row + 1, column)
        self.component_summary = QLabel(self.main_card)
        self.component_summary.setObjectName("marker-component-summary")
        self.component_summary.setWordWrap(True)
        main.addWidget(self.component_summary)
        self.proposal = QLabel(self.main_card)
        self.proposal.setWordWrap(True)
        main.addWidget(self.proposal)
        self.reset_button = ContentSizedButton("Reset to proposal", self.main_card)
        self.reset_button.setProperty("actionRole", "secondary")
        self.reset_button.clicked.connect(self.reset_proposal)
        self.gate_fields.addWidget(self.reset_button, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        details = QToolButton(self)
        details.setText("Fit details")
        details.setCheckable(True)
        details.setObjectName("marker-review-details")
        configure_disclosure(details)
        layout.addWidget(details, alignment=Qt.AlignmentFlag.AlignLeft)
        self.details = QWidget(self)
        detail_layout = QVBoxLayout(self.details)
        detail_layout.setContentsMargins(14, 4, 14, 8)
        detail_layout.setSpacing(10)
        self.numeric = QTableWidget(self.details)
        self.numeric.setAccessibleName("Exact histogram values and fitted model details")
        self.numeric.setColumnCount(3)
        self.numeric.setHorizontalHeaderLabels(("Evidence", "Value", "Details"))
        self.numeric.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        style_scientific_table(self.numeric)
        detail_layout.addWidget(self.numeric)
        self.details.hide()
        details.toggled.connect(self.details.setVisible)
        layout.addWidget(self.details)
        for name, editor in self._editors.items():
            if not name.startswith("m"):
                continue
            changed = lambda *_args, field=name: self._edited(field)
            if isinstance(editor, QComboBox):
                editor.currentIndexChanged.connect(changed)
            elif isinstance(editor, QLineEdit):
                editor.textChanged.connect(changed)
            elif isinstance(editor, QCheckBox):
                editor.toggled.connect(changed)
        self.marker.currentIndexChanged.connect(self._change_marker)
        self.scope.currentIndexChanged.connect(self.refresh)
        action = renderer._marker_buttons.get("review_marker_thresholds")
        if action is not None:
            action.clicked.disconnect()
            action.clicked.connect(self._save_current_review)
        self._change_marker()

    def _toggle_fit(self, checked):
        self.chart.set_fit_visible(checked)
        self.chart.set_evidence(self.chart.histogram, self.chart.threshold_lines)

    def _plot_distribution(self, axis, *, density=False):
        """Render saved bins with either counts or the matching PDF density unit."""
        histogram = self.chart.histogram
        edges, counts = histogram['bin_edges'], histogram['counts']
        widths = [right - left for left, right in zip(edges, edges[1:])]
        total = sum(counts)
        heights = [count / (total * width) if total else 0. for count, width in zip(counts, widths)] if density else counts
        axis.bar(edges[:-1], heights, width=widths, align='edge', color='#91bdb6', alpha=.8)
        if density:
            for curve, color, style, label in zip(self.chart.displayed_component_densities(),
                    ('#357e9c', '#a25a35'), ('-', '--'),
                    ('Lower-mean component', 'Higher-mean component')):
                axis.plot(self.chart.density_x, curve, color=color, linestyle=style, linewidth=1.8, label=label)
        for label, value, style in self.chart.threshold_lines:
            solid = style == Qt.PenStyle.SolidLine
            caption = format(value, '.6g') if self.chart.axis_label == 'Nimbus score' else _intensity_gate_caption(value)
            axis.axvline(value, color='#163a46' if solid else '#c08444',
                linestyle='-' if solid else '--', linewidth=1.6,
                label=self.chart.display_line_label(label) + ': ' + caption)
        marker = self._reviews[self.marker.currentIndex()]
        axis.set(xlim=self.chart.display_range, ylim=(0, None),
            ylabel='Weighted density (1 / measurement unit)' if density else 'Cells',
            xlabel=marker['marker_id'] + ' · Nimbus score' if self.chart.axis_label == 'Nimbus score' else
                marker['measurement_semantic']['measurement'] + ' (measurement units)')
        if self.chart.log_counts and not density:
            import numpy as np
            axis.set_yscale('function', functions=(np.log1p, np.expm1))
            ticks = [0, 1]
            while ticks[-1] * 10 <= max(counts, default=0):
                ticks.append(ticks[-1] * 10)
            axis.set_yticks(ticks, labels=[f'{tick:,}' for tick in ticks])
            axis.set_ylabel('Cells (log scale)')
        axis.spines[['top', 'right']].set_visible(False)
        if self.chart.threshold_lines or density:
            axis.legend(frameon=False, loc='upper right')

    def _build_figure(self):
        from matplotlib.figure import Figure
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        figure = Figure(figsize=(7, 4.5), layout='constrained')
        self._plot_distribution(figure.subplots())
        apply_figure_font(figure)
        return figure

    def _build_fit_figure(self):
        """Keep a publication view of weighted densities and exact fitted parameters."""
        from matplotlib.figure import Figure
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        figure = Figure(figsize=(7.5, 5.4), layout='constrained')
        axis, table_axis = figure.subplots(2, 1, gridspec_kw={'height_ratios': (4, 1)})
        self._plot_distribution(axis, density=True)
        from mif_pipeline.cell_analysis.marker_fit_figure import add_component_parameters
        add_component_parameters(table_axis, self._fit_candidate['parameters'])
        apply_figure_font(figure)
        return figure

    def _export_figure(self):
        self._export_plot(self._build_figure(), 'marker-histogram.svg')

    def _export_fit(self):
        if self.chart.density_x:
            self._export_plot(self._build_fit_figure(), 'marker-fitted-distribution.svg')

    def _export_plot(self, figure, filename):
        from .figure_export import export_figure
        try:
            export_figure(self, figure, filename)
        finally:
            figure.clear()

    def _save_current_review(self):
        """Save the visibly selected decision, then visit any remaining image.

        Each click marks only the current marker/image decision as saved. Once
        all represented decisions are saved, the unchanged form validator binds
        their exact thresholds to the retained fitting evidence.
        Editing a saved decision clears its saved state and requires another save.
        """
        self._editors[self._current_prefix + "_confirm"].setChecked(True)
        for marker_index, marker in enumerate(self._reviews):
            scopes = range(len(marker["per_slide_reviews"])) if marker["fit_scope"] == "per_slide" else (None,)
            for scope_index in scopes:
                prefix = f"m{marker_index}" + (f"_s{scope_index}" if scope_index is not None else "")
                if not self._editors[prefix + "_confirm"].isChecked():
                    self.marker.setCurrentIndex(marker_index)
                    if scope_index is not None:
                        self.scope.setCurrentIndex(scope_index)
                    self._renderer.page_changed.emit()
                    return
        self._renderer._submit("review_marker_thresholds")

    def _edited(self, field):
        if self._refreshing:
            return
        match = re.match(r"(m\d+(?:_s\d+)?)_", field)
        if not match:
            return
        prefix = match.group(1)
        self._refreshing = True
        try:
            if field == prefix + "_source":
                for suffix in ("manual", "adjusted_threshold", "adjusted_score_cutoff", "adjustment_source"):
                    self._editors[prefix + "_" + suffix].clear()
                if self._editors[field].currentData() == "nimbus":
                    marker_key, _, slide_key = prefix.partition("_s")
                    marker = self._reviews[int(marker_key[1:])]
                    review = marker["per_slide_reviews"][int(slide_key)]["review"] if slide_key else marker["pooled_review"]
                    fitted = next((c for c in review["candidates"] if c["method"] == "nimbus_distilled"), {})
                    reviewed_cutoff = fitted.get("parameters", {}).get("nimbus_score_cutoff")
                    direct = next((c for c in review["candidates"] if c["method"] == "nimbus"), {})
                    if reviewed_cutoff is not None and reviewed_cutoff != direct.get("parameters", {}).get("score_cutoff"):
                        self._editors[prefix + "_adjusted_score_cutoff"].setText(str(reviewed_cutoff))
                        self._editors[prefix + "_adjustment_source"].setText("numeric")
            if field.endswith(("_adjusted_threshold", "_adjusted_score_cutoff")):
                self._editors[prefix + "_adjustment_source"].setText("numeric" if self._editors[field].text() else "")
            if not field.endswith(("_confirm", "_nimbus_distill")):
                confirm = self._editors.get(prefix + "_confirm", self._editors.get(prefix.split("_s")[0] + "_confirm"))
                if confirm is not None:
                    confirm.setChecked(False)
        finally:
            self._refreshing = False
        self.refresh()

    def _step_cutoff(self, editor, direction):
        """Move to the adjacent measured score or intensity without rounding the stored gate."""
        support = self.chart._cdf_values
        gate = self._image_gate["cutoff"]
        if gate is None or not len(support):
            return
        index = bisect_right(support, gate) if direction > 0 else bisect_left(support, gate) - 1
        if 0 <= index < len(support):
            editor.setText(repr(float(support[index])))

    def _request_distillation(self):
        choice = self._editors[f"m{self.marker.currentIndex()}_nimbus_distill"]
        if choice.currentData() == "intensity_review":
            choice.setCurrentIndex(choice.findData("score_cutoff"))
            return
        choice.setCurrentIndex(choice.findData("intensity_review"))
        self._save_current_review()

    def _display_distillation(self, marker_index, source, candidate, threshold, *, fitted_candidate=None):
        route = self._editors.get(f"m{marker_index}_nimbus_distill")
        if fitted_candidate is None and source == "nimbus_distilled":
            fitted_candidate = candidate
        requestable = fitted_candidate is None and source == "nimbus" and route is not None
        params = (fitted_candidate or {}).get("parameters", {})
        total = params.get("evaluated_count", 0)
        self.distillation.setVisible(requestable or fitted_candidate is not None)
        self.distillation_action.setVisible(requestable)
        has_counts = total > 0 and "score_positive_count" in params
        self.distillation_matrix.setVisible(has_counts)
        if requestable:
            pending = route.currentData() == "intensity_review"
            self.distillation_title.setText("Intensity distillation")
            self.distillation_action.setText("Use scores only" if pending else "Fit intensity threshold")
            self.distillation_action.setEnabled(pending or threshold is not None)
            self.distillation_note.setText(
                "Save the remaining score cutoffs to calculate the intensity threshold." if pending else
                "Find the intensity threshold that best reproduces the reviewed Nimbus calls.")
        elif fitted_candidate is not None:
            self.distillation_title.setText("Intensity distillation")
            if not total:
                self.distillation_note.setText("No cells have both a Nimbus score and an intensity for this comparison.")
                return
            fitted = fitted_candidate.get("threshold")
            if fitted is not None:
                gate = f"An intensity threshold of {fitted:.5g}"
            elif fitted_candidate.get("outcome") == "all_positive":
                gate = "Calling every cell positive"
            else:
                gate = "Calling every cell negative"
            agree = total - params["disagreements"]
            self.distillation_note.setText(f"{gate} reproduces the Nimbus call for {agree:,} of {total:,} cells.")
            if has_counts:
                positives = params["score_positive_count"]
                negatives = total - positives
                lost = params["score_positive_intensity_negative"]
                gained = params["score_negative_intensity_positive"]
                for label, value, size in zip(self.distillation_counts,
                        (positives - lost, lost, gained, negatives - gained),
                        (positives, positives, negatives, negatives)):
                    fraction = value / size if size else 0
                    label.setText(f"{value:,}   ({fraction:.1%})" if size else "0")
                    label.setAccessibleName(f"{label.text()} of {size:,} cells in this Nimbus class")
                    label.setStyleSheet(f"background-color: rgba(24, 130, 124, {int(14 + fraction * 52)});")

    def _request_refit(self):
        """Read the saved preparation while retaining this review's exact controls."""
        from .marker_preparation import open_marker_preparation
        open_marker_preparation(self._renderer)

    def _dragged(self, value):
        prefix = self._current_prefix
        source = self._editors[prefix + "_source"].currentData()
        suffix = "manual" if source == "manual" else "adjusted_score_cutoff" if source == "nimbus" else "adjusted_threshold"
        self._editors[prefix + "_" + suffix].setText(format(value, ".17g"))
        if source != "manual":
            self._refreshing = True
            self._editors[prefix + "_adjustment_source"].setText("drag")
            self._refreshing = False
        self.refresh()

    def reset_proposal(self):
        """Discard only the adjustment, keeping the saved fitted candidate selected."""
        self._refreshing = True
        prefix = self._current_prefix
        for suffix in ("adjusted_threshold", "adjusted_score_cutoff", "adjustment_source"):
            self._editors[prefix + "_" + suffix].clear()
        confirm = self._editors.get(prefix + "_confirm", self._editors.get(prefix.split("_s")[0] + "_confirm"))
        if confirm is not None:
            confirm.setChecked(False)
        self._refreshing = False
        self.refresh()

    def _open_cell_examples(self):
        if self._image_gate is not None:
            self._open_tissue()
            return
        from .marker_cell_examples import MarkerCellExamples
        if self._image_dialog is not None:
            self._image_dialog.close()
            self._image_dialog.deleteLater()
        marker = self._reviews[self.marker.currentIndex()]
        images = marker['per_image_distributions']
        if marker['fit_scope'] == 'per_slide':
            images = [row for row in images if row['image_identity'] == self.scope.currentData()]
        self._image_dialog = MarkerCellExamples(images, self)
        self._image_dialog.requested.connect(self._request_cell_examples)
        self._image_dialog.show()
        self._request_cell_examples()

    def _toggle_tissue(self, expanded):
        self.inspect_button.setText("Close tissue view" if expanded else "Open tissue view")
        if expanded:
            self._open_tissue()
        elif self._field_review is not None:
            self._field_review.hide()

    def _open_tissue(self):
        if self._image_gate is None or self._image_gate['cutoff'] is None:
            return
        if self._field_review is None:
            from .marker_gate_field import MarkerGateField
            self._field_review = MarkerGateField(self, histogram_selection=True)
            self._field_review.requested.connect(self.field_requested)
            self._field_review.range_changed.connect(self.chart.set_selected_range)
            self.tissue_layout.addWidget(self._field_review)
        with QSignalBlocker(self.inspect_button):
            self.inspect_button.setChecked(True)
        self.inspect_button.setText("Close tissue view")
        self._field_review.show()
        self._configure_field()

    def _configure_field(self):
        marker = self._reviews[self.marker.currentIndex()]
        images = marker['per_image_distributions']
        if marker['fit_scope'] == 'per_slide':
            images = [row for row in images if row['image_identity'] == self.scope.currentData()]
        values = self.chart._cdf_values or self.chart.histogram['bin_edges']
        if values:
            self._field_review.configure(marker['marker_id'], self._image_gate['source'],
                self._image_gate['cutoff'], (values[0], values[-1]), images)

    def _inspect_range(self, lower, upper):
        self._open_tissue()
        if self._field_review is not None:
            self.chart.set_selected_range(lower, upper)
            self._field_review.select_range(lower, upper)

    def _request_cell_examples(self):
        try:
            self.image_options()
        except ValueError as exc:
            self._image_dialog.set_error(str(exc))
            return
        self._image_dialog.set_busy()
        self.image_requested.emit()

    def image_options(self):
        if self._image_dialog is None or self._image_gate is None or self._image_gate['cutoff'] is None:
            return None
        return {'marker_gallery_settings': {**self._image_gate, **self._image_dialog.options()}}

    def set_image_plots(self, plots):
        if self._image_dialog is not None:
            self._image_dialog.set_plots(plots)

    def set_image_error(self, message):
        if self._image_dialog is not None:
            self._image_dialog.set_error(message)

    def _change_marker(self, *_args):
        if self._image_dialog is not None:
            self._image_dialog.close()
            self._image_dialog.deleteLater()
            self._image_dialog = None
        marker = self._reviews[self.marker.currentIndex()]
        if marker["fit_scope"] == "per_slide":
            images = [{"label": " · ".join(dict.fromkeys((slide["sample_id"], slide["slide_id"]))),
                       "key": slide["image_identity"]} for slide in marker["per_slide_reviews"]]
        else:
            images = [{"label": "Pooled", "key": None}]
        self.image_navigation.set_images(images)
        self._navigation_fields[1].setVisible(self.scope.count() > 1)
        self.refresh()

    def refresh(self, *_args):
        """Update presentation from saved evidence and exact counts without refitting."""
        if self._refreshing or self.marker.currentIndex() < 0:
            return
        self._refreshing = True
        try:
            marker_index = self.marker.currentIndex()
            marker = self._reviews[marker_index]
            per_slide = marker["fit_scope"] == "per_slide"
            scope_index = self.scope.currentIndex()
            prefix = f"m{marker_index}" + (f"_s{scope_index}" if per_slide else "")
            review = marker["per_slide_reviews"][scope_index]["review"] if per_slide else marker["pooled_review"]
            self._current_prefix = prefix
            values = self._renderer._raw_values()
            source = values.get(prefix + "_source")
            candidate = next((item for item in review["candidates"] if item["method"] == source), None)
            source_help = self.findChild(MethodGuidanceCard, "choice-help-" + prefix + "_source")
            if source_help is not None:
                source_help.hide()
            direct = source == "nimbus" and candidate is not None
            parameters = candidate.get("parameters", {}) if candidate else {}
            mixture = source in {"gmm", "gamma"}
            means = parameters.get("means", ())
            ordered_means = (len(means) == 2 and all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in means) and means[0] < means[1])
            self.chart.set_candidate(candidate if mixture else None)
            self._fit_candidate = candidate if mixture else None
            fit_identity = (id(review), source)
            if self._fit_identity != fit_identity:
                self.show_fit.blockSignals(True)
                self.show_fit.setChecked(False)
                self.show_fit.blockSignals(False)
                self._fit_identity = fit_identity
            available_fit = bool(self.chart.density_x)
            self.score_log.setVisible(direct)
            self.chart.set_log_counts(direct and self.score_log.isChecked())
            self.show_fit.setVisible(available_fit)
            self.export_fit.setVisible(available_fit)
            self.chart.set_fit_visible(self.show_fit.isChecked() and available_fit)
            self.show_fit.setToolTip(
                'Display bars and saved weighted component curves in density units. Counts and the gate stay unchanged.'
                + (f'\nExact means: {means[0]!r}, {means[1]!r}' if ordered_means else ''))
            self.component_summary.setVisible(mixture)
            self.component_summary.setText(
                "The higher-mean component represents marker-positive cells."
                if ordered_means else "The saved component means are unavailable. Use a manual gate or revise the fit.")
            if mixture and parameters.get('conditional_positive_fit') and parameters.get('scope_zero_count'):
                self.component_summary.setText(self.component_summary.text()
                    + f" The Gamma curves cover positive intensities; {parameters['scope_zero_count']:,} exact-zero cells remain in the histogram.")
            histogram = parameters.get("score_histogram", review["histogram"]) if direct else review["histogram"]
            cdf = parameters.get("score_empirical_cdf", {}) if direct else review.get("empirical_cdf", {})
            semantic = marker["measurement_semantic"]
            self.heading.setText("Review " + marker["marker_id"])
            quantity = "Nimbus score" if direct else _intensity_label(semantic)
            self.semantic.setText(quantity if per_slide else quantity + " · Pooled across slides")
            threshold = parameters.get("score_cutoff") if direct else (None if candidate is None else candidate["threshold"])
            proposal = threshold
            adjusted_suffix = "adjusted_score_cutoff" if direct else "adjusted_threshold"
            adjusted_raw = values.get(prefix + "_" + adjusted_suffix)
            adjusted = source != "manual" and adjusted_raw not in (None, "")
            if adjusted:
                try:
                    threshold = float(adjusted_raw)
                    if not isfinite(threshold) or direct and not 0 <= threshold <= 1:
                        threshold = None
                except (TypeError, ValueError):
                    threshold = None
            if source == "manual":
                raw = values.get(prefix + "_manual")
                try:
                    threshold = float(raw)
                    if not isfinite(threshold):
                        threshold = None
                except (TypeError, ValueError):
                    threshold = None
                if threshold is None and raw in (None, ""):
                    edges = histogram["bin_edges"]
                    threshold = (edges[0] + edges[-1]) / 2 if edges else 0.
                    self._editors[prefix + "_manual"].setText(format(threshold, ".17g"))
            lines = []
            if direct and adjusted and proposal is not None:
                lines.append(("Nimbus proposal", proposal, Qt.PenStyle.DashLine))
            if not direct and adjusted and proposal is not None:
                lines.append(("Saved proposal", proposal, Qt.PenStyle.DashLine))
            if threshold is not None:
                lines.append(("Final gate", threshold, Qt.PenStyle.SolidLine))
            self.chart.threshold_bounds = (0., 1.) if direct else None
            self.chart.axis_label = quantity
            self.chart.set_evidence(histogram, lines)
            cdf_identity = (id(review), direct)
            if self._cdf_identity != cdf_identity:
                self.chart.set_empirical_cdf(cdf)
                self.chart.selected_range = None
                self._cdf_identity = cdf_identity
            self.chart.set_manual_threshold(threshold)
            if self._image_dialog is not None and per_slide and self._image_dialog.image.currentData() != json.loads(self.scope.currentData()):
                self._image_dialog.close()
                self._image_dialog.deleteLater()
                self._image_dialog = None
            self._image_gate = {'marker_id': marker['marker_id'], 'source': source, 'cutoff': threshold}
            self.inspect_button.setEnabled(threshold is not None)
            if threshold is None:
                self.inspect_button.setChecked(False)
            self.chart.range_selection_enabled = True
            self.chart_hint.setText('Drag the gate line to adjust the cutoff. Drag across another part of the plot to inspect '
                + ('a score range' if direct else 'an intensity range') + ' in tissue.')
            if self._field_review is not None:
                if threshold is None:
                    self._field_review.hide()
                elif not self._field_review.isHidden():
                    self._configure_field()
            if self._image_dialog is not None:
                self._image_dialog.set_error('Select Refresh cells to inspect the current gate.')
            self.reset_button.setEnabled(adjusted)
            unit = "score" if direct else "measurement units"
            proposal_caption = (format(proposal, ".6g") if direct else _intensity_gate_caption(proposal)) if proposal is not None else None
            proposal_units = "" if proposal_caption == "Intensity > 0" else " " + unit
            self.proposal.setText(
                ("Enter a score from 0 to 1." if direct else "Enter a valid intensity cutoff.")
                if adjusted and threshold is None else
                f"Saved proposal: {proposal_caption}{proposal_units}" if proposal is not None else ""
            )
            numeric_editor = self._editors.get(prefix + "_" + adjusted_suffix)
            if numeric_editor is not None:
                numeric_editor.setPlaceholderText(proposal_caption if proposal is not None else "Enter a final gate")
            self.chart.setAccessibleDescription("Nimbus expression scores" if direct else semantic["measurement"])
            for name, (label, field) in self._rows.items():
                suffix = name.removeprefix(prefix + "_")
                meaningful_sources = [choice for choice in marker_gate_choices(review) if choice != 'manual']
                visible = (name.startswith(prefix + "_") and suffix == "source"
                    and ('refit_current_marker' not in self._editors or len(meaningful_sources) > 1))
                visible |= name == prefix + "_manual" and source == "manual"
                visible |= name == prefix + "_" + adjusted_suffix and source != "manual"
                label.setVisible(visible)
                field.setVisible(visible)
            if threshold is not None and cdf.get("values"):
                count = self.chart.threshold_counts(threshold)
                self._show_counts(count["above"], count["below"], count["total"], "At or above cutoff", "Below cutoff")
            elif candidate is not None and candidate.get("counts") and not adjusted:
                count = candidate["counts"]
                self._show_counts(count["positive"], count["negative"], count["positive"] + count["negative"], "Positive", "Negative")
            else:
                self.above_count.setText("At or above cutoff\nChoose a cutoff")
                self.below_count.setText("Below cutoff\nChoose a cutoff")
            outcome = candidate.get("outcome") if candidate else None
            if outcome in {"all_positive", "all_negative"} and not adjusted:
                text = "All cells with valid measurements are " + ("positive." if outcome == "all_positive" else "negative.")
            elif direct:
                text = ""
                unknown_scores = parameters.get("unknown_count", 0)
                if unknown_scores:
                    text += f" {unknown_scores:,} cells without scores remain Unknown."
            elif source == "manual" or adjusted:
                text = ""
            else:
                text = ""
            unknown = review.get("unknown_count", 0)
            if unknown and not direct:
                text += f" {unknown:,} cells have no valid measurement and remain Unknown for this marker."
            fitted = next((item for item in review["candidates"] if item["method"] == "nimbus_distilled"), None)
            self.plot_header.setVisible(fitted is None)
            if fitted is not None:
                if self.distillation_fields.indexOf(self.reset_button) < 0:
                    self.distillation_fields.addWidget(self.proposal, 2, 0)
                    self.distillation_fields.addWidget(self.reset_button, 2, 1, Qt.AlignmentFlag.AlignLeft)
                suffix = "manual" if source == "manual" else adjusted_suffix
                support = self.chart._cdf_values
                for direction in (-1, 1):
                    action = self._gate_step_actions.get((prefix + "_" + suffix, direction))
                    if action is not None:
                        limit = support[-1 if direction > 0 else 0] if len(support) else None
                        action.setEnabled(threshold is not None and limit is not None
                                          and (threshold < limit if direction > 0 else threshold > limit))
            elif self.gate_fields.indexOf(self.reset_button) < 0:
                self.main_card.layout().addWidget(self.proposal)
                self.gate_fields.addWidget(self.reset_button, 1, 1, Qt.AlignmentFlag.AlignLeft)
            self.proposal.setVisible(adjusted)
            self.reset_button.setVisible(adjusted)
            self._display_distillation(marker_index, source, candidate, threshold, fitted_candidate=fitted)
            failed = [item for item in review["candidates"] if item["status"] != "available"]
            for item in failed:
                text += "\n" + MarkerHistogram.display_line_label(item["method"]) + " unavailable: " + marker_candidate_guidance(
                    item, can_revise="refit_current_marker" in self._editors,
                    can_use_manual="manual" in marker_gate_choices(review),
                )
            self.status.setText(text)
            self.status.setVisible(bool(text.strip()))
            confirmation_names = [name for name in self._editors if name.endswith("_confirm")]
            confirmed = sum(self._editors[name].isChecked() for name in confirmation_names)
            total = len(confirmation_names)
            progress = self._form.context["resume_context"]
            position = progress["canonical_marker_ids"].index(marker["marker_id"]) + 1
            self.coverage.setText(f"Marker {position} of {len(progress['canonical_marker_ids'])}" +
                (f" · {confirmed} of {total} image decisions saved" if total > 1 else ""))
            action = self._renderer._marker_buttons.get("review_marker_thresholds")
            if action is not None:
                distilling = any(editor.currentData() == "intensity_review" for name, editor in self._editors.items() if name.endswith("_nimbus_distill"))
                remaining = sum(not self._editors[name].isChecked() for name in confirmation_names if name != prefix + "_confirm")
                action.setText("Save image and continue" if remaining else "Generate intensity review" if distilling else "Save marker and continue")
                valid = threshold is not None or bool(not adjusted and candidate and candidate.get("outcome") in {"all_positive", "all_negative"})
                action.setEnabled(valid)
            if self._details_identity != id(review):
                self._populate_details(review, marker)
                self._details_identity = id(review)
        finally:
            self._refreshing = False

    def _show_counts(self, above, below, total, above_label, below_label):
        for label, count, caption in ((self.above_count, above, above_label), (self.below_count, below, below_label)):
            percent = 100. * count / total if total else 0.
            label.setText(f"{caption}   {percent:.2f}%\n{count:,} / {total:,} cells")

    def _populate_details(self, review, marker):
        """Expose exact saved fit values separately from the live decision metrics."""
        edges = review["histogram"]["bin_edges"]
        rows = []
        parameter_names = {
            "means": "Component means", "variances": "Component variances",
            "modes": "Component modes", "shapes": "Gamma shapes", "scales": "Gamma scales",
            "weights": "Component weights", "resolved_location_bounds": "Resolved location bounds",
            "monotone_max_absolute_adjustment": "Largest score correction",
            "monotone_method": "Score correction",
            "component_ordering": "Component identity",
            "score_cutoff": "Marker score cutoff", "scope_zero_count": "Exact-zero cells",
            "score_positive_count": "Nimbus-positive cells compared",
            "score_positive_intensity_negative": "Positive only by Nimbus",
            "score_negative_intensity_positive": "Positive only by intensity",
            "disagreements": "Disagreements with Nimbus", "evaluated_count": "Cells compared with Nimbus",
            "unknown_score_count": "Unknown Nimbus scores",
        }
        for item in review["candidates"]:
            caption = MarkerHistogram.display_line_label(item["method"])
            if item["iterations"]:
                fit_status = (
                    f"Converged after {item['iterations']} iterations"
                    if item["converged"] else
                    f"Stopped after {item['iterations']} iterations without convergence"
                )
            else:
                fit_status = "Available" if item["status"] == "available" else "Unavailable"
            threshold = str(item["threshold"]) if item["threshold"] is not None else {
                "all_positive": "All positive", "all_negative": "All negative",
                "direct_scores": "Per-cell scores",
            }.get(item.get("outcome"), "Unavailable")
            rows.append((caption, threshold, fit_status))
            if item["status"] != "available":
                rows.append(("Fit unavailable", "", marker_candidate_guidance(
                    item, can_revise="refit_current_marker" in self._editors,
                    can_use_manual="manual" in marker_gate_choices(review),
                )))
            for key, title in parameter_names.items():
                if key in item.get("parameters", {}):
                    value = item["parameters"][key]
                    formatted = ", ".join(format(v, ".8g") if isinstance(v, (float, int)) else str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
                    if key == "monotone_method":
                        formatted = {"increasing_positive_score": "Increasing score correction"}.get(str(value), formatted)
                    elif key == "component_ordering" and value == "mean":
                        formatted = "Lower and higher component means"
                    rows.append((title, formatted, caption))
        if marker.get("component_location_constraints"):
            rows.append(("Shared location constraints", str(marker["component_location_constraints"]), "Applied before fitting; percentiles resolve in each fitted population"))
        rows.extend((f"Bin {index + 1}", str(count), f"{edges[index]:.8g} to {edges[index + 1]:.8g}")
                    for index, count in enumerate(review["histogram"]["counts"]))
        self.numeric.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.numeric.setItem(row, column, QTableWidgetItem(value))
        fit_scientific_columns(self.numeric)
