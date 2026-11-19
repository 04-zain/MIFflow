"""Review saved libraries and enter numerical spectra in one fixed-axis table."""

# ==============================================================================
#                                    Imports
# ==============================================================================

import csv, io, math
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEvent, Signal, QSignalBlocker
from PySide6.QtGui import QColor, QBrush, QPalette, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QSizePolicy,
    QLabel,
    QLineEdit,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QMenu,
    QHeaderView,
    QMessageBox,
    QAbstractItemView,
    QToolButton,
    QToolTip,
)

from .workbench import style_scientific_table


def _spectral_cell_colour(fraction):
    """A light sequential scale that keeps the original numerical text legible."""
    low, high = (255, 255, 255), (134, 199, 190)
    return QColor(*(round(a + fraction * (b - a)) for a, b in zip(low, high)))

# ==============================================================================
#                                 Curated table
# ==============================================================================


class SpectralValueDelegate(QStyledItemDelegate):
    """Shorten displayed numbers while leaving EditRole and exported text exact."""

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setAlignment(Qt.AlignmentFlag.AlignRight)
        editor.setStyleSheet(
            "QLineEdit { border: 1px solid #006b70; border-radius: 0; "
            "padding: 0 6px; min-height: 0; }"
        )
        return editor

    def paint(self, painter, option, index):
        cell = QStyleOptionViewItem(option)
        focused = bool(cell.state & QStyle.StateFlag.State_HasFocus)
        cell.state &= ~QStyle.StateFlag.State_HasFocus
        background = index.data(Qt.ItemDataRole.BackgroundRole)
        if background is not None and not cell.state & QStyle.StateFlag.State_Selected:
            # The shared table stylesheet paints over BackgroundRole. Paint this
            # numerical matrix directly, retaining the shared selection treatment.
            self.initStyleOption(cell, index)
            painter.save()
            painter.fillRect(cell.rect, background)
            painter.setFont(cell.font)
            painter.setPen(cell.palette.color(QPalette.ColorRole.Text))
            rect = cell.rect.adjusted(9, 0, -9, 0)
            text = cell.fontMetrics.elidedText(cell.text, cell.textElideMode, rect.width())
            painter.drawText(rect, cell.displayAlignment, text)
            painter.restore()
        else:
            super().paint(painter, cell, index)
        if focused:
            painter.save()
            painter.setPen(QColor("#006b70"))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(option.rect.adjusted(0, 0, -1, -1))
            painter.restore()

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.ItemDataRole.EditRole) or "")
        editor.selectAll()

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress and event.matches(QKeySequence.StandardKey.Paste):
            text = QApplication.clipboard().text()
            if any(separator in text for separator in ("\t", "\n", "\r", ",")):
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
                self.parent().paste_requested.emit()
                return True
        return super().eventFilter(editor, event)

    def displayText(self, value, locale):
        try:
            return format(float(value), ".6g")
        except (TypeError, ValueError):
            return str(value)


class SpectralValuesTable(QTableWidget):
    """Open one cell on a plain click while retaining spreadsheet selection."""

    paste_requested = Signal()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.editTriggers() != QAbstractItemView.EditTrigger.NoEditTriggers
                and self.isEnabled() and index.isValid()
                and len(self.selectionModel().selectedIndexes()) == 1):
            self.edit(index)


class SpectralTableWidget(QWidget):
    """One fixed-axis table for imported values, manual entry and bounded paste."""
    def __init__(self, band_names, component_names, parent=None, *, values=None, library=None):
        super().__init__(parent)
        self.bands = tuple(band_names)
        self.components = tuple(component_names)
        self.library = library
        self._columns_sized = False
        self._columns_user_sized = False
        self._sizing_columns = False
        self._undo = []
        self._redo = []
        self._validation_requested = False
        self._preview_canvas = None
        self.setWindowTitle("Review spectral library")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(16)
        description = QVBoxLayout()
        description.setSpacing(4)
        title = QLabel("Reference spectra", self)
        title.setObjectName("spectral-table-title")
        title.setProperty("panelHeading", True)
        description.addWidget(title)
        mode = " · Saved library" if library is not None else ""
        component_unit = "component" if len(self.components) == 1 else "components"
        summary = QLabel(f"{len(self.bands)} acquired bands × {len(self.components)} {component_unit}{mode}", self)
        summary.setObjectName("spectral-table-summary")
        summary.setProperty("choiceHelp", True)
        summary.setWordWrap(True)
        description.addWidget(summary)
        colour_key = QLabel(self)
        colour_key.setObjectName("spectral-colour-key")
        colour_key.setProperty("choiceHelp", True)
        colour_key.setTextFormat(Qt.TextFormat.RichText)
        colour_key.setWordWrap(True)
        swatches = "".join(
            f'<span style="color:{_spectral_cell_colour(value).name()};">&#9632;</span>'
            for value in (0, .2, .4, .6, .8, 1)
        )
        colour_key.setText(f"Colour per column: 0 {swatches} peak")
        colour_key.setToolTip("Each column is coloured relative to its largest reference value. Colours show spectral shape; the entered values are unchanged.")
        description.addWidget(colour_key)
        toolbar.addLayout(description, 1)
        layout.addLayout(toolbar)
        self.table = SpectralValuesTable(len(self.bands), len(self.components), self)
        self.table.setObjectName("curated-spectra-table")
        style_scientific_table(self.table)
        self.table.verticalHeader().show()
        self.table.setStyleSheet(
            "QTableWidget#curated-spectra-table { border: none; border-radius: 0; padding: 0; }"
            "QTableWidget#curated-spectra-table::item { padding: 4px 9px; }"
            "QTableWidget#curated-spectra-table::item:selected { background: #d5ebe5; color: #173642; }"
        )
        self.table.setItemDelegate(SpectralValueDelegate(self.table))
        self.table.setHorizontalHeaderLabels(self.components)
        self.table.setVerticalHeaderLabels(self.bands)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed
                                  | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setStyleSheet("QHeaderView::section { font-weight: 700; }")
        self.table.horizontalHeader().setFixedHeight(self.table.fontMetrics().lineSpacing() + 18)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(116)
        for i in range(len(self.bands)):
            self.table.verticalHeaderItem(i).setToolTip(self.bands[i])
            for j in range(len(self.components)):
                value = "" if values is None or values[i][j] is None else format(values[i][j], ".17g")
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, j, item)
        for j, component in enumerate(self.components):
            self.table.horizontalHeaderItem(j).setToolTip(component)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().sectionResized.connect(self._remember_column_resize)
        self.table.setCurrentCell(0, 0)
        self.table.setAccessibleName("Reference spectra: acquired bands by component")
        self.table.setAccessibleDescription(
            "Rows are acquired bands; columns are spectral components. Click a value to edit, or press F2. "
            "Copy and paste spreadsheet cells with Ctrl+C and Ctrl+V. Use Shift to select a rectangle."
        )
        self.table.paste_requested.connect(self.paste_values)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.table)
        self._selection_summary = QLabel(self)
        self._selection_summary.setObjectName("spectral-table-selection")
        self._selection_summary.setTextFormat(Qt.TextFormat.PlainText)
        self._selection_summary.setProperty("choiceHelp", True)
        self._selection_summary.setWordWrap(True)
        self._selection_summary.setMinimumWidth(0)
        layout.addWidget(self._selection_summary)
        shortcuts = [(QKeySequence.StandardKey.Copy, self.copy_values)]
        if library is None:
            shortcuts.extend(((QKeySequence.StandardKey.Paste, self.paste_values),
                              (QKeySequence.StandardKey.Undo, self.undo_values),
                              (QKeySequence.StandardKey.Redo, self.redo_values)))
        else:
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for key, handler in shortcuts:
            shortcut = QShortcut(key, self.table)
            # Selected text inside an active editor keeps its ordinary shortcuts.
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(handler)
        self._validation = QLabel(self)
        self._validation.setObjectName("spectral-table-validation")
        self._validation.setProperty("validationMessage", True)
        self._validation.setTextFormat(Qt.TextFormat.PlainText)
        self._validation.setWordWrap(True)
        layout.addWidget(self._validation)
        self._preview_toggle = QToolButton(self)
        self._preview_toggle.setObjectName("spectral-preview-toggle")
        self._preview_toggle.setText("Preview spectra")
        self._preview_toggle.setCheckable(True)
        from .researcher_widgets import configure_disclosure
        configure_disclosure(self._preview_toggle)
        self._preview_toggle.setToolTip("Preview the components selected in the table, or choose a component in the preview.")
        self._preview_toggle.toggled.connect(self._toggle_preview)
        layout.addWidget(self._preview_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self._preview = QWidget(self)
        self._preview.setObjectName("spectral-value-preview")
        self._preview_layout = QVBoxLayout(self._preview)
        self._preview_layout.setContentsMargins(0, 0, 0, 0)
        self._preview_layout.setSpacing(8)
        self._preview.hide()
        layout.addWidget(self._preview)
        self._current_values = self._cell_values()
        self.table.itemChanged.connect(self._values_changed)
        self.table.itemSelectionChanged.connect(self._update_selection)
        self._update_selection()
        self._refresh_validation()
        if library is not None:
            self._add_saved_metadata(layout, library)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._size_initial_columns)
        QTimer.singleShot(0, self, self._refresh_preview)

    def resizeEvent(self, event):
        """Redistribute untouched columns as the available table width changes."""
        super().resizeEvent(event)
        QTimer.singleShot(0, self, self._size_initial_columns)

    def _remember_column_resize(self, *_args):
        """Preserve researcher-adjusted widths across resizing and local Back."""
        if self._columns_sized and not self._sizing_columns:
            self._columns_user_sized = True
            QTimer.singleShot(0, self, self._fit_table_height)

    def _size_initial_columns(self):
        """Fill available width until a researcher resizes a column themselves."""
        if not self.isVisible():
            return
        if self._columns_user_sized:
            self._fit_table_height()
            return
        self._sizing_columns = True
        share = self.table.viewport().width() // max(1, len(self.components))
        metrics = self.table.horizontalHeader().fontMetrics()
        for index, name in enumerate(self.components):
            self.table.setColumnWidth(index, max(116, share, metrics.horizontalAdvance(name) + 30))
        self._sizing_columns = False
        self._columns_sized = True
        self._fit_table_height()

    def _fit_table_height(self):
        # Reserve the scroll-bar lane during reflow so the last acquired-band
        # row stays complete when the page gains its own vertical scroll bar.
        scrollbar = self.table.horizontalScrollBar().sizeHint().height()
        height = (self.table.horizontalHeader().height()
                  + sum(self.table.rowHeight(row) for row in range(min(8, len(self.bands))))
                  + scrollbar
                  + 2 * self.table.frameWidth() + 6)
        self.table.setFixedHeight(height)

    def _cell_values(self):
        return tuple(tuple(self.table.item(i, j).text() for j in range(len(self.components)))
                     for i in range(len(self.bands)))

    def _values_changed(self):
        values = self._cell_values()
        if values != self._current_values:
            self._undo.append(self._current_values)
            del self._undo[:-50]
            self._redo.clear()
            self._current_values = values
        self._refresh_validation()
        self._refresh_preview()

    def _restore_cells(self, values):
        with QSignalBlocker(self.table):
            for i, row in enumerate(values):
                for j, value in enumerate(row):
                    self.table.item(i, j).setText(value)
        self._current_values = values
        self._refresh_validation()
        self._refresh_preview()

    def undo_values(self):
        if self.library is None and self._undo:
            self._redo.append(self._current_values)
            self._restore_cells(self._undo.pop())

    def redo_values(self):
        if self.library is None and self._redo:
            self._undo.append(self._current_values)
            self._restore_cells(self._redo.pop())

    def _show_context_menu(self, position):
        menu = QMenu(self.table)
        actions = [("Copy", QKeySequence.StandardKey.Copy, self.copy_values,
                    len(self.table.selectedRanges()) == 1)]
        if self.library is None:
            actions.extend((
                ("Paste", QKeySequence.StandardKey.Paste, self.paste_values, True),
                ("Undo", QKeySequence.StandardKey.Undo, self.undo_values, bool(self._undo)),
                ("Redo", QKeySequence.StandardKey.Redo, self.redo_values, bool(self._redo)),
            ))
        actions.append(("Select all", QKeySequence.StandardKey.SelectAll, self.table.selectAll, True))
        for label, key, callback, enabled in actions:
            action = menu.addAction(label)
            action.setShortcut(QKeySequence(key))
            action.setEnabled(enabled)
            action.triggered.connect(callback)
        menu.exec(self.table.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _update_selection(self):
        ranges = self.table.selectedRanges()
        if len(ranges) == 1:
            selection = ranges[0]
            count = selection.rowCount() * selection.columnCount()
            rows, columns = selection.rowCount(), selection.columnCount()
            text = (f"Band: {self.bands[selection.topRow()]} · Component: {self.components[selection.leftColumn()]}"
                    if count == 1 else f"{rows} {'band' if rows == 1 else 'bands'} × "
                    f"{columns} {'component' if columns == 1 else 'components'} selected")
        else:
            text = "Select cells to copy" if self.library is not None else "Select cells to edit or paste"
        self._selection_summary.setText(text)
        self._refresh_preview()

    def _toggle_preview(self, checked):
        if not checked:
            QToolTip.hideText()
        if checked and self._preview_canvas is None:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from .spectral_review import SpectralLegend

            controls = QHBoxLayout()
            caption = QLabel("Component", self._preview)
            controls.addWidget(caption)
            self._preview_component = QComboBox(self._preview)
            self._preview_component.setObjectName("spectral-preview-component")
            self._preview_component.setAccessibleName("Component to preview")
            self._preview_component.addItems(self.components)
            self._preview_component.setMinimumWidth(0)
            self._preview_component.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            caption.setBuddy(self._preview_component)
            self._preview_component.currentIndexChanged.connect(self._select_preview_component)
            controls.addWidget(self._preview_component, 1)
            self._preview_layout.addLayout(controls)
            self._preview_notice = QLabel(self._preview)
            self._preview_notice.setObjectName("spectral-preview-notice")
            self._preview_notice.setTextFormat(Qt.TextFormat.PlainText)
            self._preview_notice.setProperty("choiceHelp", True)
            self._preview_notice.setWordWrap(True)
            self._preview_layout.addWidget(self._preview_notice)
            figure = Figure(figsize=(8, 3), layout="constrained")
            figure.set_facecolor(self.palette().window().color().name())
            figure.add_subplot(111)
            self._preview_canvas = FigureCanvasQTAgg(figure)
            self._preview_canvas.setObjectName("spectral-preview-plot")
            self._preview_canvas.setAccessibleName("Supplied reference values in acquired-band row order")
            self._preview_canvas.setFixedHeight(280)
            self._preview_canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._preview_layout.addWidget(self._preview_canvas)
            self._preview_canvas.mpl_connect("motion_notify_event", self._hover_preview)
            self._preview_canvas.mpl_connect("figure_leave_event", lambda _event: QToolTip.hideText())
            self._preview_legend = SpectralLegend(self._preview_canvas, self._preview)
            self._preview_layout.addWidget(self._preview_legend)
        self._preview.setVisible(checked)
        self._refresh_preview()

    def _select_preview_component(self, column):
        if column >= 0:
            self.table.setCurrentCell(0, column)
            self.table.selectColumn(column)

    def _refresh_preview(self):
        """Inspect committed values without normalization, fitting, or axis reordering."""
        if (self._preview_canvas is None or not self._preview_toggle.isChecked()
                or not self.isVisible()):
            return
        columns = sorted({index.column() for index in self.table.selectedIndexes()})
        with QSignalBlocker(self._preview_component):
            self._preview_component.setPlaceholderText(
                f"{len(columns)} selected components" if columns else "Select a component")
            self._preview_component.setCurrentIndex(columns[0] if len(columns) == 1 else -1)
        axes = self._preview_canvas.figure.axes[0]
        axes.clear()
        axes.set_facecolor("#f4f7f9")
        self._preview_profiles = []
        profiles, invalid = [], []
        for column in columns:
            try:
                values = [float(self.table.item(row, column).text()) for row in range(len(self.bands))]
                if any(not math.isfinite(value) or value < 0 for value in values):
                    raise ValueError
            except ValueError:
                invalid.append(self.components[column])
            else:
                profiles.append((column, values))
        if invalid or not columns:
            QToolTip.hideText()
            self._preview_notice.setText(
                "Complete every value for " + ", ".join(invalid) + " to preview the selected spectra."
                if invalid else "Choose a component to preview its spectrum.")
            self._preview_notice.show()
            self._preview_canvas.hide()
            self._preview_legend.hide()
            return
        from mif_pipeline.preprocessing.unmixing.spectral_plots import plot_spectral_profile
        import numpy as np

        lines = []
        self._preview_x = np.arange(1, len(self.bands) + 1)
        for column, values in profiles:
            line = plot_spectral_profile(axes, self._preview_x, values, (),
                ordinal=True, color=f"C{column % 10}", marker="o", markersize=4,
                linewidth=1.7, label=self.components[column])
            lines.append(line)
            original = np.asarray(values)
            self._preview_profiles.append((line, self.components[column], original, original))
        stride = max(1, math.ceil(len(self.bands) / 8))
        ticks = list(range(1, len(self.bands) + 1, stride))
        axes.set_xticks(ticks, [self.bands[index - 1] for index in ticks], rotation=20, ha="right")
        axes.set_xlabel("Acquired band · table row order")
        axes.set_ylabel("Supplied reference value")
        axes.set_xlim(.5, len(self.bands) + .5)
        axes.set_ylim(bottom=0)
        axes.spines[["top", "right"]].set_visible(False)
        axes.spines[["left", "bottom"]].set_color("#a7b8c3")
        axes.set_axisbelow(True)
        axes.grid(axis="y", color="#d5dfe5", linewidth=.7, linestyle=(0, (3, 4)))
        axes.tick_params(color="#a7b8c3", labelcolor="#344957")
        self._preview_notice.hide()
        self._preview_canvas.show()
        self._preview_legend.set_lines(lines)
        self._preview_legend.setVisible(len(lines) > 1)
        self._preview_canvas.draw_idle()

    def _hover_preview(self, event):
        if self._preview_canvas.isVisible() and self._preview_profiles:
            from .spectral_review import show_spectral_hover
            show_spectral_hover(self._preview_canvas, self._preview_canvas.figure.axes[0], event,
                self.bands, self._preview_x, self._preview_profiles)

    def _refresh_validation(self):
        errors = []
        with QSignalBlocker(self.table):
            columns = [[] for _ in self.components]
            for i, band in enumerate(self.bands):
                for j, component in enumerate(self.components):
                    item = self.table.item(i, j)
                    value = item.text().strip()
                    try:
                        number = float(value)
                        valid = math.isfinite(number) and number >= 0
                    except ValueError:
                        valid = False
                    columns[j].append(number if valid else None)
                    message = (f"Band: {band} · Component: {component}: enter a finite number of zero or greater."
                               if not valid and (value or self._validation_requested) else "")
                    item.setToolTip(message or f"Band: {band}\nComponent: {component}\nValue: {value or 'Not entered'}")
                    if message:
                        errors.append((i, j, message))
            for column, values in enumerate(columns):
                complete = bool(values) and all(value is not None for value in values)
                peak = max(values) if complete else 0
                for row, value in enumerate(values):
                    item = self.table.item(row, column)
                    item.setData(Qt.ItemDataRole.BackgroundRole,
                                 QBrush(_spectral_cell_colour(value / peak)) if peak > 0 else None)
        self._validation.setText(errors[0][2] + (f" {len(errors)} cells need attention." if len(errors) > 1 else "")
                                 if errors else "")
        self._validation.setVisible(bool(errors))
        return errors

    @classmethod
    def from_file(cls, path, inventory, parent=None):
        """Populate the shared review from the selected bounded file, not an image.

        Packages/JSON retain their typed library and recorded values read-only.
        CSV values are matched by their band and component labels; empty cells remain editable.
        Normalization and acquisition declarations are checked on submission.
        """
        from mif_pipeline.preprocessing.unmixing.library_exchange import (
            read_library_payload, load_library_exchange,
        )
        path = Path(path)
        payload = read_library_payload(path)
        if path.suffix.casefold() != ".csv":
            if not (path.name.casefold().endswith(".miflib.zip") or path.suffix.casefold() == ".json"):
                raise ValueError("Choose a .miflib.zip package, reviewed library JSON or CSV.")
            library = load_library_exchange(payload)
            from mif_pipeline.preprocessing.unmixing.curated_library import _spectral_label_order
            from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
            bands = tuple(inventory["band_names"])
            requested = tuple(name for name in inventory["component_names"]
                              if not is_autofluorescence_component(name))
            row_order = _spectral_label_order(library.axis.parent_band_ids, bands)
            column_order = _spectral_label_order(
                tuple(item.fluorophore_identity for item in library.spectra),
                requested or None, components=True)
            spectra = tuple(library.spectra[i] for i in column_order)
            values = tuple(tuple(item.values[i] for item in spectra) for i in row_order)
            return cls(bands, tuple(item.component_identity for item in spectra), parent,
                       values=values, library=library)
        return cls.from_text(payload.decode("utf-8-sig"), inventory, parent)

    @classmethod
    def from_text(cls, text, inventory, parent=None, *, draft=False):
        """Align CSV labels to the table, allowing unfinished cells in a local draft."""
        from mif_pipeline.preprocessing.unmixing.curated_library import read_spectral_csv_table
        bands = tuple(inventory["band_names"])
        components, cells = read_spectral_csv_table(
            text, band_names=bands, component_names=tuple(inventory["component_names"]) or None)
        try:
            values = cells if draft else tuple(
                tuple(float(value) if value.strip() else None for value in row) for row in cells)
        except ValueError as error:
            raise ValueError("Complete every spectral value with a number.") from error
        result = cls(bands, components, parent, values=None if draft else values)
        # Keep the supplied decimal text exact; display rounding belongs to the delegate.
        with QSignalBlocker(result.table):
            for i, row in enumerate(cells):
                for j, value in enumerate(row):
                    result.table.item(i, j).setText(value)
        result._current_values = result._cell_values()
        result._refresh_validation()
        return result

    def _add_saved_metadata(self, layout, library):
        """Show the saved normalization and per-band acquisition facts without editing them."""
        normalization = {"unit_l1": "Unit sum (L1)", "unit_l2_spectral_shape_v1": "Unit length (L2)"}[library.normalization_convention]
        modes = {"full": "Full intensity correction", "exposure_only": "Exposure-only correction",
                 "as_stored": "Recorded calibrated intensities"}
        correction = ", ".join(dict.fromkeys(modes[item.correction_mode]
                              for item in library.quantitative_signature.measurements))
        summary = QLabel("Recorded intensity corrections", self)
        summary.setObjectName("spectral-library-metadata")
        summary.setProperty("panelHeading", True)
        layout.addWidget(summary)
        facts = QGridLayout()
        facts.setHorizontalSpacing(24)
        facts.setVerticalSpacing(6)
        for row, (name, value) in enumerate((("Normalization", normalization),
                                           ("Intensity correction", correction))):
            caption = QLabel(name, self)
            caption.setProperty("fieldCaption", True)
            facts.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
            recorded = QLabel(value, self)
            recorded.setWordWrap(True)
            recorded.setTextFormat(Qt.TextFormat.PlainText)
            facts.addWidget(recorded, row, 1)
        facts.setColumnStretch(1, 1)
        layout.addLayout(facts)
        toggle = QToolButton(self)
        toggle.setText("Recorded imaging settings")
        toggle.setObjectName("spectral-acquisition-toggle")
        toggle.setCheckable(True)
        from .researcher_widgets import configure_disclosure
        configure_disclosure(toggle)
        layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        from mif_pipeline.preprocessing.unmixing.curated_library import _spectral_label_order
        order = _spectral_label_order(library.axis.parent_band_ids, self.bands)
        rows = tuple(library.quantitative_signature.measurements[i] for i in order)
        details = QTableWidget(len(rows), 6, self)
        details.setObjectName("spectral-library-acquisition")
        style_scientific_table(details)
        details.setHorizontalHeaderLabels(("Acquired band", "Exposure (s)", "Gain", "Binning area", "Offset", "Response factor"))
        details.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        details.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        details.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        details.horizontalHeader().setStyleSheet("QHeaderView::section { font-weight: 700; }")
        details.setFixedHeight(min(320, details.horizontalHeader().sizeHint().height()
            + len(rows) * details.verticalHeader().defaultSectionSize()
            + details.horizontalScrollBar().sizeHint().height() + 2 * details.frameWidth() + 6))
        for index, (band, item) in enumerate(zip(self.bands, rows, strict=True)):
            for column, value in enumerate((band, item.exposure_seconds, item.gain,
                                           item.binning_area, item.offset, item.response_correction_multiplier)):
                cell = QTableWidgetItem("Not recorded" if value is None else str(value))
                cell.setTextAlignment((Qt.AlignmentFlag.AlignLeft if column == 0 else Qt.AlignmentFlag.AlignRight)
                                      | Qt.AlignmentFlag.AlignVCenter)
                cell.setToolTip(cell.text())
                details.setItem(index, column, cell)
        details.hide()
        toggle.toggled.connect(details.setVisible)
        toggle.toggled.connect(lambda checked: toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
        layout.addWidget(details)

    def copy_values(self):
        """Copy one exact cell or a labelled rectangle that can be pasted back."""
        selections = self.table.selectedRanges()
        if len(selections) != 1:
            return
        selection = selections[0]
        if selection.rowCount() == selection.columnCount() == 1:
            QApplication.clipboard().setText(
                self.table.item(selection.topRow(), selection.leftColumn()).text()
            )
            return
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        columns = range(selection.leftColumn(), selection.rightColumn() + 1)
        writer.writerow(("measurement", *(self.components[column] for column in columns)))
        for row in range(selection.topRow(), selection.bottomRow() + 1):
            writer.writerow((self.bands[row], *(self.table.item(row, column).text() for column in columns)))
        QApplication.clipboard().setText(stream.getvalue())

    def paste_values(self):
        """Apply a bounded CSV/TSV rectangle only after all cells and labels pass.

        The selected top-left cell anchors the paste. Optional component headings
        and acquired-band labels must match that exact table slice. The clipboard
        cannot resize/reorder the scientific axes, and rejected input changes no cells.
        Saved package/JSON reviews remain read-only.
        """
        if self.library is not None:
            return
        try:
            text = QApplication.clipboard().text()
            if not text.strip() or len(text.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("Copy a table of reference values smaller than 2 MiB.")
            rows = list(csv.reader(io.StringIO(text.strip("\r\n")),
                                   delimiter="\t" if "\t" in text else ",", strict=True))
            if not rows or any(not row for row in rows) or len(rows) > len(self.bands) + 1:
                raise ValueError("Paste one nonempty row per selected acquired band.")
            selections = self.table.selectedRanges()
            if len(selections) > 1:
                raise ValueError("Select one cell or one rectangular area before pasting.")
            row = selections[0].topRow() if selections else max(0, self.table.currentRow())
            column = selections[0].leftColumn() if selections else max(0, self.table.currentColumn())
            rows = [[cell.strip() for cell in values] for values in rows]
            headings = None
            row_labels = rows[0][0] in ("measurement", "")
            if row_labels:
                headings = rows.pop(0)[1:]
            elif rows[0][0] in self.bands:
                row_labels = True
            else:
                try:
                    for cell in rows[0]:
                        float(cell)
                except ValueError:
                    headings = rows.pop(0)
                    row_labels = bool(rows and len(rows[0]) == len(headings) + 1)
            if not rows:
                raise ValueError("Paste spectral values beneath the headings.")
            width = len(rows[0]) - int(row_labels)
            if width < 1 or row + len(rows) > len(self.bands) or column + width > len(self.components):
                raise ValueError("The copied values exceed the table's rows or columns.")
            if headings is not None and tuple(headings) != self.components[column:column + width]:
                raise ValueError("The pasted component headings do not match the selected columns or their order.")
            if selections and (selections[0].rowCount(), selections[0].columnCount()) != (1, 1):
                if (selections[0].rowCount(), selections[0].columnCount()) != (len(rows), width):
                    raise ValueError("Select one cell to paste from, or an area matching the copied rows and columns.")
            values = []
            for offset, cells in enumerate(rows):
                if len(cells) != width + int(row_labels):
                    raise ValueError("Every pasted row must contain the same number of values.")
                if row_labels:
                    if cells[0] != self.bands[row + offset]:
                        raise ValueError("The pasted acquired-band labels do not match the selected rows or their order.")
                    cells = cells[1:]
                for cell in cells:
                    try:
                        number = float(cell)
                    except ValueError:
                        raise ValueError("Paste a number of zero or greater in every cell.") from None
                    if not math.isfinite(number) or number < 0:
                        raise ValueError("Paste a number of zero or greater in every cell.")
                values.append(cells)
            with QSignalBlocker(self.table):
                for i, cells in enumerate(values):
                    for j, value in enumerate(cells):
                        self.table.item(row + i, column + j).setText(value)
            self._values_changed()
        except (ValueError, csv.Error) as error:
            QMessageBox.warning(self, "Spectral values not pasted", str(error))

    def csv_text(self, *, validate=True):
        """Return the displayed numerical rows without writing a file.

        Submission requires finite nonnegative values. Draft capture retains empty
        or unfinished cells so returning to the page loses no researcher edits.
        """
        output = io.StringIO()
        if validate:
            self._validation_requested = True
            errors = self._refresh_validation()
            if errors:
                row, column, message = errors[0]
                self.table.setCurrentCell(row, column)
                self.table.scrollToItem(self.table.item(row, column))
                raise ValueError(message)
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("measurement", *self.components))
        for i, name in enumerate(self.bands):
            values = [self.table.item(i, j).text().strip() for j in range(len(self.components))]
            writer.writerow((name, *values))
        text = output.getvalue()
        if len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Enter a spectral table smaller than 2 MiB.")
        return text

