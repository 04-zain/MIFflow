"""Interactive display of the current checkpoint's recorded reference spectra."""

from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QSize
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QGridLayout, QLabel, QSizePolicy, QToolTip, QToolButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QPushButton, QHBoxLayout,
)
from matplotlib.figure import Figure
from matplotlib.colors import to_hex
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
import numpy as np

from .figure_export import export_figure
from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis, plot_spectral_profile, label_spectral_axis


class SpectralPlotToolbar(NavigationToolbar2QT):
    """Shared reset, pan and rectangle-zoom controls for reference spectra."""
    toolitems = (
        ("Reset view", "Restore the full spectra view", "home", "home"),
        ("Pan", "Move the visible plot", "move", "pan"),
        ("Zoom", "Drag a rectangle to zoom", "zoom_to_rect", "zoom"),
    )


class SpectralLegend(QWidget):
    """Wrapping curve controls in their own space below a spectral canvas.

    Buttons affect line visibility only. Their colours/styles follow the actual
    plotted lines, and hidden entries remain available with dimmed text/swatches.
    Reflow uses the available Qt width; it does not resize or obscure data axes.
    """
    def __init__(self, canvas, parent=None, *, changed=None):
        super().__init__(parent)
        self.setObjectName("spectral-curve-legend")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.canvas, self.changed = canvas, changed
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(5)
        self._entries = []
        self._columns = 0

    def set_lines(self, lines):
        for button, *_ in self._entries:
            self._grid.removeWidget(button)
            button.hide()
            button.deleteLater()
        self._entries = []
        for line in lines:
            button = QPushButton(self)
            button.setCheckable(True)
            button.setChecked(line.get_visible())
            button.setAccessibleName(line.get_label())
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.installEventFilter(self)
            button.setStyleSheet("QPushButton { text-align: left; border: none; border-radius: 0; background: transparent; padding: 0; } QPushButton:hover { background: transparent; }")
            contents = QHBoxLayout(button)
            contents.setContentsMargins(0, 3, 0, 3)
            swatch = QLabel(button)
            swatch.setFixedSize(52, 18)
            label = QLabel(line.get_label(), button)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setMinimumHeight(label.fontMetrics().height())
            for child in (swatch, label):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            contents.addWidget(swatch); contents.addWidget(label, 1)
            self._entries.append((button, line, swatch, label))
            button.toggled.connect(lambda visible, entry=self._entries[-1]: self._toggle(entry, visible))
            self._style(self._entries[-1], line.get_visible())
        self._reflow(force=True)

    def _style(self, entry, visible):
        button, line, swatch, label = entry
        pixmap = QPixmap(52, 18); pixmap.fill(Qt.GlobalColor.transparent)
        colour = QColor(to_hex(line.get_color()))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = line.get_alpha()
        painter.setOpacity((1 if alpha is None else alpha) * (1 if visible else .3))
        pen = QPen(colour, line.get_linewidth())
        pen.setStyle({"--": Qt.PenStyle.DashLine, ":": Qt.PenStyle.DotLine,
                      "-.": Qt.PenStyle.DashDotLine}.get(line.get_linestyle(), Qt.PenStyle.SolidLine))
        painter.setPen(pen)
        painter.drawLine(1, 9, 51, 9)
        if line.get_marker() in {"o", "."}:
            radius = max(2, line.get_markersize() / 2)
            painter.setPen(QPen(colour, 1))
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(26, 9), radius, radius)
        painter.end()
        swatch.setPixmap(pixmap)
        label.setStyleSheet("color: #344957;" if visible else "color: #89969f;")
        button.setToolTip(("Hide " if visible else "Show ") + line.get_label() + " (display only)")

    def _toggle(self, entry, visible):
        line = entry[1]
        line.set_visible(visible)
        self._style(entry, visible)
        if self.changed is not None:
            self.changed(line.get_label(), visible)
        self.canvas.draw_idle()

    def _reflow(self, *, force=False):
        columns = max(1, min(4, len(self._entries), self.width() // 220))
        if columns != self._columns or force:
            for column in range(max(self._columns, columns)):
                self._grid.setColumnStretch(column, 1 if column < columns else 0)
            self._columns = columns
            for index, (button, *_) in enumerate(self._entries):
                self._grid.addWidget(button, index // columns, index % columns)
        width = max(1, (self.width() - (columns - 1) * self._grid.horizontalSpacing()) // columns)
        for button, _line, swatch, label in self._entries:
            available = max(1, width - swatch.width() - button.layout().spacing())
            caption_height = max(label.minimumHeight(), label.heightForWidth(available))
            button.setMinimumHeight(max(swatch.height(), caption_height) + 6)
        self.updateGeometry()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            keyboard_focus = (event.type() == QEvent.Type.FocusIn and event.reason() in {
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason})
            for button, _line, _swatch, label in self._entries:
                if button is watched:
                    font = label.font()
                    font.setUnderline(keyboard_focus)
                    label.setFont(font)
                    break
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()


def show_spectral_hover(canvas, axes, event, names, x, profiles, *, toolbar=None):
    """Identify original measurements near the pointer, including coincident bands.

    Profiles supply line visibility, label, original values and displayed values.
    Distances use displayed points; indices always refer to the unchanged source
    arrays rather than the NaN-separated drawing line. Exact overlaps retain all
    identities in the tooltip instead of choosing an arbitrary wavelength match.
    """
    if event.inaxes is not axes or (toolbar is not None and toolbar.mode):
        QToolTip.hideText()
        return
    hits = []
    for line, label, values, displayed in profiles:
        if not line.get_visible():
            continue
        positions = axes.transData.transform(np.column_stack((x, displayed)))
        distances = np.sum((positions - [event.x, event.y]) ** 2, axis=1)
        for index in np.flatnonzero(distances <= 81):
            hits.append((float(distances[index]), label, values, displayed, int(index)))
    if not hits:
        QToolTip.hideText()
        return
    nearest = min(hit[0] for hit in hits)
    descriptions = []
    for distance, label, values, displayed, index in hits:
        if distance > nearest + .01:
            continue
        text = f"{label} · Band {index + 1}: {names[index]}\nValue: {values[index]:.6g}"
        if displayed[index] != values[index]:
            text += f"\nDisplay fraction: {displayed[index]:.6g}"
        descriptions.append(text)
    position = canvas.mapToGlobal(QPoint(int(event.x), canvas.height() - int(event.y)))
    QToolTip.showText(position, "\n\n".join(descriptions), canvas)


class SpectralReviewPanel(QWidget):
    def __init__(self, review, parent=None):
        super().__init__(parent)
        self.setObjectName("spectral-review-panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        messages = {
            "pass": "The selected references pass the spectral-separation checks.",
            "review_required": "Inspect the spectral overlap before starting unmixing.",
            "failed": "The reference matrix failed its separation checks. Review the selected spectra and number of components.",
        }
        notice = QLabel(messages[review["quality"]], self)
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setProperty("statusBanner", True)
        layout.addWidget(notice)
        pairs = sorted((
            (pair["similarity"], pair["first"], pair["second"])
            for pair in review["pairwise_spectra"]
        ), reverse=True)
        condition = review.get("condition_number")
        condition_text = "unavailable" if condition is None else f"{condition:.6g}"
        limits = review.get("review_limits")
        rejection = review.get("rejection_limits")
        metric_panel = QFrame(self)
        metric_panel.setProperty("workbenchSection", True)
        metrics = QGridLayout(metric_panel)
        metrics.setContentsMargins(16, 14, 16, 14)
        metrics.setHorizontalSpacing(24)
        metrics.setVerticalSpacing(8)
        layout.addWidget(metric_panel)

        def add_metric(column, title, value, status_text, status_name):
            # Shared rows keep all three checks aligned, including wrapped text.
            for row, (text, role) in enumerate((
                (title, "fieldCaption"), (value, None), (status_text, None),
            )):
                label = QLabel(text, metric_panel)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                if role:
                    label.setProperty(role, True)
                if row == 2:
                    label.setObjectName(status_name)
                metrics.addWidget(label, row, column, Qt.AlignmentFlag.AlignTop)
            metrics.setColumnStretch(column, 1)

        def threshold_status(value, key):
            subject = "This library’s condition number" if key == "condition_number" else "Their cosine similarity"
            if value is None:
                return f"{subject} could not be calculated for this matrix."
            if rejection and value >= rejection[key]:
                return f"{subject} is at or above the stop threshold of {rejection[key]:g}. Change the references before proceeding."
            if limits and value >= limits[key]:
                return f"{subject} is at or above the warning threshold of {limits[key]:g}. Inspect the reference spectra before proceeding."
            if limits:
                return f"{subject} is below the warning threshold of {limits[key]:g}."
            return f"No threshold was recorded for {"condition number" if key == "condition_number" else "cosine similarity"}."

        add_metric(
            0, "Condition number", condition_text,
            threshold_status(condition, "condition_number"), "spectral-condition-status",
        )
        add_metric(
            1, "Highest cosine similarity", f"{pairs[0][0]:.6g}" if pairs else "Not applicable",
            (f"This library’s most similar pair is {pairs[0][1]} and {pairs[0][2]}. " + threshold_status(pairs[0][0], "cosine_similarity")
             if pairs else "There is only one component, so no pairwise check is needed."),
            "spectral-cosine-status",
        )
        full_rank = review["rank"] == review["component_count"]
        add_metric(
            2, "Matrix rank", f"{review['rank']} / {review['component_count']}",
            (f"All {review['component_count']} component spectra are linearly independent." if full_rank else
             "This library has fewer independent spectra than components. Change the references before proceeding."),
            "spectral-rank-status",
        )
        if review["quality"] == "review_required":
            reference_check = QLabel(
                "Check that the reference library matches the markers and imaging "
                "settings used for these images.", self
            )
            reference_check.setWordWrap(True)
            layout.addWidget(reference_check)
        for pair in review.get("flagged_pairs", ()):
            rejected = bool(rejection and pair["similarity"] >= rejection["cosine_similarity"])
            label = QLabel(
                f"{pair['first']} and {pair['second']} have a cosine similarity of "
                f"{pair['similarity']:.4g}. Their similar spectral shapes make them difficult to separate. "
                + ("This reaches the rejection threshold; change the references before proceeding."
                   if rejected else "Inspect this pair before accepting the references."), self)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)

        figure = Figure(figsize=(9, 3.5), layout="constrained")
        figure.set_facecolor(self.palette().window().color().name())
        axes = figure.add_subplot(111)
        axes.set_facecolor(self.palette().window().color().name())
        self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setObjectName("spectral-canvas")
        self.canvas.setMinimumHeight(410)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar = SpectralPlotToolbar(self.canvas, self, coordinates=False)
        self.toolbar.setObjectName("spectral-plot-controls")
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        reset = self.toolbar._actions["home"]
        reset.setIcon(QIcon())
        self.toolbar.widgetForAction(reset).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toolbar.addSeparator()
        export = self.toolbar.addAction("Export figure…", self._save_figure)
        export.setIconText("Export figure…")
        export.setToolTip("Save the visible curves and current plot limits as SVG, PDF or PNG")
        layout.addWidget(self.toolbar)
        interaction = QLabel("Hover over a point for values. Click a legend label to show or hide a curve.", self)
        interaction.setWordWrap(True)
        interaction.setProperty("supportingText", True)
        layout.addWidget(interaction)
        layout.addWidget(self.canvas, 1)
        self._axes = axes
        self._names = tuple(review["measurement_names"])
        self._optical_settings = review.get("measurement_optical_settings")
        self._profiles = []
        x, self._groups, self._wavelength_unit = spectral_axis(self._names,
            wavelengths=review.get("measurement_wavelengths"), wavelength_unit=review.get("wavelength_unit"), optical_settings=review.get("measurement_optical_settings"))
        self._x = x
        wavelengths = review.get("measurement_wavelengths")
        self._recorded_wavelengths = (wavelengths if wavelengths is not None
                                     and len(wavelengths) == len(self._names) else None)
        self._recorded_wavelength_unit = review.get("wavelength_unit")
        self._wavelength_axis = self._wavelength_unit is not None
        for spectrum in review["spectra"]:
            values = np.asarray(spectrum["values"], dtype=float)
            total = values.sum()
            if (values.shape != x.shape or total <= 0
                    or not np.all(np.isfinite(values))):
                raise ValueError("The reference spectrum is invalid.")
            line = plot_spectral_profile(axes, x, values / total, self._groups,
                marker="o", markersize=4, linewidth=1.7, label=spectrum["component"])
            self._profiles.append((line, spectrum["component"], values))
        label_spectral_axis(axes, self._names, x, self._groups, self._wavelength_unit)
        axes.set_ylabel("Fraction of total reference signal")
        axes.spines[["top", "right"]].set_visible(False)
        axes.set_axisbelow(True)
        axes.grid(axis="y", color="#d5dfe5", linewidth=0.7, linestyle=(0, (3, 4)))
        axes.spines[["left", "bottom"]].set_color("#a7b8c3")
        axes.tick_params(color="#a7b8c3", labelcolor="#344957")
        axes.set_ylim(bottom=0)
        self.toolbar.push_current()
        self.legend = SpectralLegend(self.canvas, self)
        self.legend.set_lines([profile[0] for profile in self._profiles])
        layout.addWidget(self.legend)
        self.canvas.mpl_connect("motion_notify_event", self._hover_profile)
        self.canvas.mpl_connect("figure_leave_event", lambda _event: QToolTip.hideText())
        caption = QLabel("Curves are normalized to sum to one for display.", self)
        caption.setWordWrap(True)
        caption.setProperty("supportingText", True)
        layout.addWidget(caption)
        self._export_status = QLabel(self)
        self._export_status.setTextFormat(Qt.TextFormat.PlainText)
        self._export_status.setWordWrap(True)
        self._export_status.hide()
        layout.addWidget(self._export_status)

        toggle = QToolButton(self)
        toggle.setText("Pairwise cosine similarities")
        toggle.setCheckable(True)
        from .researcher_widgets import configure_disclosure
        configure_disclosure(toggle)
        table = QTableWidget(len(pairs), 3, self)
        table.setObjectName("spectral-cosine-table")
        table.setHorizontalHeaderLabels(("Component", "Compared with", "Cosine similarity"))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, (cosine, first, second) in enumerate(pairs):
            for column, value in enumerate((first, second, f"{cosine:.6g}")):
                table.setItem(row, column, QTableWidgetItem(value))
        table.setMinimumHeight(180)
        table.hide()
        toggle.toggled.connect(table.setVisible)
        layout.addWidget(toggle)
        layout.addWidget(table)

    def _figure_for_export(self):
        visible = [line for line, _component, _values in self._profiles if line.get_visible()]
        if not visible:
            raise ValueError("Show at least one spectrum by clicking its legend label, then export again.")
        from mif_pipeline.preprocessing.unmixing.spectral_plots import build_spectral_figure

        return build_spectral_figure(
            ((self._x, values / values.sum(), line.get_label(), line.get_color())
             for line, _component, values in self._profiles if line.get_visible()), measurement_names=self._names,
            wavelength_unit=self._wavelength_unit if self._wavelength_axis else None,
            optical_settings=self._optical_settings,
            xlim=self._axes.get_xlim(), ylim=self._axes.get_ylim(),
        )

    def _save_figure(self):
        try:
            figure = self._figure_for_export()
        except ValueError as exc:
            QMessageBox.warning(self, "Figure not exported", str(exc))
            return
        path = export_figure(self, figure, "reference-spectra")
        if path is not None:
            self._export_status.setText(f"Figure saved to {path}")
            self._export_status.show()

    def _hover_profile(self, event):
        names = self._names
        if self._recorded_wavelengths is not None and self._recorded_wavelength_unit:
            names = tuple(
                name if wavelength is None else f"{name} ({wavelength:g} {self._recorded_wavelength_unit})"
                for name, wavelength in zip(names, self._recorded_wavelengths, strict=True)
            )
        show_spectral_hover(self.canvas, self._axes, event, names, self._x,
            ((line, component, values, values / values.sum())
             for line, component, values in self._profiles), toolbar=self.toolbar)
