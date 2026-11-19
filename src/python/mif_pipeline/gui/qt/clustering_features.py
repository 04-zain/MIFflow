"""Choose exact measured inputs through a marker-by-statistic matrix."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMenu, QSizePolicy, QStyle,
    QStyledItemDelegate, QStyleOptionButton, QTableView,
    QVBoxLayout, QWidget,
)

from .researcher_widgets import ContentSizedButton
from .selected_measurements import SelectedMeasurements
from .workbench import style_scientific_table


_REGIONS = {"cell": "Whole cell", "nucleus": "Nucleus", "cytoplasm": "Cytoplasm", "membrane": "Membrane"}
_STATISTICS = {"Mean": "Mean", "Median": "Median", "Min": "Minimum", "Max": "Maximum",
               "Minimum": "Minimum", "Maximum": "Maximum", "Std.Dev.": "SD", "StdDev": "SD",
               "Standard deviation": "SD", "Variance": "Variance", "Sum": "Sum"}


def feature_parts(feature):
    """Use catalog metadata and known statistic suffixes; retain unknown names."""
    name = str(feature["measurement_name"])
    suffix = name.rsplit(":", 1)[-1].strip()
    statistic = _STATISTICS.get(suffix, name)
    measured_parts = name.split(":")
    measured_channel = (measured_parts[1].strip()
                        if len(measured_parts) == 3
                        and measured_parts[0].strip().casefold() in _REGIONS
                        and suffix in _STATISTICS else "")
    marker = str(feature.get("biomarker") or measured_channel or feature.get("channel_id") or name)
    region = str(feature.get("compartment") or "")
    return marker, _REGIONS.get(region, region or "Unspecified region"), statistic


def feature_caption(feature):
    marker, region, statistic = feature_parts(feature)
    return f"{marker} · {region} · {statistic}"


class _FeatureMatrix(QAbstractTableModel):
    selection_changed = Signal()

    def __init__(self, features, parent=None):
        super().__init__(parent)
        self.features = tuple(features)
        self.by_id = {row["feature_id"]: row for row in self.features}
        self.selected = []
        self.filtered_ids = set(self.by_id)
        self.statistics = tuple(dict.fromkeys(feature_parts(row)[2] for row in self.features))
        self.rows = []
        groups = {}
        marker_channels = {}
        for feature in self.features:
            marker, region, statistic = feature_parts(feature)
            channel = str(feature.get("channel_id") or "")
            marker_channels.setdefault(marker, set()).add(channel)
            key = (marker, channel, region)
            row = groups.get(key)
            if row is None or statistic in row["features"]:
                # Never overwrite two offered IDs with the same displayed label.
                row = dict(marker=marker, channel=channel, region=region, features={})
                self.rows.append(row)
                groups[key] = row
            row["features"][statistic] = feature["feature_id"]
        self.marker_channels = marker_channels
        for row in self.rows:
            row["label"] = row["marker"] + (
                "\n" + row["channel"] if len(marker_channels[row["marker"]]) > 1 else ""
            )

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.statistics) + 2

    def feature_id(self, index):
        if not index.isValid() or index.column() < 2:
            return None
        return self.rows[index.row()]["features"].get(self.statistics[index.column() - 2])

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        title = ("Marker / channel", "Cell region", *self.statistics)[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return title + ("  ▾" if section >= 2 else "")
        if role == Qt.ItemDataRole.ToolTipRole and section >= 2:
            return f"Select or clear {title} measurements in the shown rows."
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, column = self.rows[index.row()], index.column()
        feature_id = self.feature_id(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return row["label"] if column == 0 else row["region"] if column == 1 else ("—" if feature_id is None else "")
        if role == Qt.ItemDataRole.CheckStateRole and feature_id is not None:
            return Qt.CheckState.Checked if feature_id in self.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter if column >= 2 else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            if column < 2:
                return f"{row['marker']}\n{row['channel']}\n{row['region']}"
            if feature_id is None:
                return "This measurement was not saved for this marker and cell region."
            feature = self.by_id[feature_id]
            parts = [feature["measurement_name"], str(feature.get("channel_id") or "")]
            if feature.get("units"):
                parts.append(str(feature["units"]))
            parts.append(feature_id)
            return "\n".join(part for part in parts if part)
        return None

    def flags(self, index):
        if index.column() >= 2:
            return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                    if self.feature_id(index) in self.filtered_ids else Qt.ItemFlag.NoItemFlags)
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        feature_id = self.feature_id(index)
        if role != Qt.ItemDataRole.CheckStateRole or feature_id not in self.filtered_ids:
            return False
        chosen = value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value
        selected = [item for item in self.selected if item != feature_id]
        if chosen:
            selected.append(feature_id)
        self.set_selected(selected)
        return True

    def set_selected(self, selected):
        selected = list(selected or ())
        if len(set(selected)) != len(selected) or not set(selected) <= self.by_id.keys():
            raise ValueError("Selected clustering measurements must be distinct IDs from this catalog")
        self.selected = selected
        if self.rows:
            self.dataChanged.emit(self.index(0, 2), self.index(len(self.rows) - 1, self.columnCount() - 1),
                                  [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()


class _MeasurementCheck(QStyledItemDelegate):
    """Use the whole available matrix cell as a native check target."""

    def paint(self, painter, option, index):
        state = index.data(Qt.ItemDataRole.CheckStateRole)
        if state is None:
            super().paint(painter, option, index)
            return
        painter.save()
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        if enabled and state == Qt.CheckState.Checked:
            painter.fillRect(option.rect.adjusted(3, 3, -3, -3), QColor("#e6f3ed"))
        style = option.widget.style()
        check = QStyleOptionButton()
        size = max(16, style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth))
        check.rect = option.rect.adjusted(0, 0, 0, 0)
        check.rect.setSize(QSize(size, size))
        check.rect.moveCenter(option.rect.center())
        check.state = QStyle.StateFlag.State_On if state == Qt.CheckState.Checked else QStyle.StateFlag.State_Off
        if enabled:
            check.state |= QStyle.StateFlag.State_Enabled
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox, check, painter, option.widget)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor("#006b70"), 1, Qt.PenStyle.DotLine))
            painter.drawRect(option.rect.adjusted(4, 4, -4, -4))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        state = index.data(Qt.ItemDataRole.CheckStateRole)
        if state is None or not index.flags() & Qt.ItemFlag.ItemIsEnabled:
            return False
        clicked = event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
        pressed = event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select)
        if clicked or pressed:
            return model.setData(index, Qt.CheckState.Unchecked if state == Qt.CheckState.Checked else Qt.CheckState.Checked,
                                 Qt.ItemDataRole.CheckStateRole)
        return False


class ClusteringFeatures(QWidget):
    changed = Signal()

    def __init__(self, features, selected=(), parent=None, *, show_inputs=True):
        super().__init__(parent)
        self.setObjectName("clustering-measurement-selector")
        self.model = _FeatureMatrix(features, self)
        self._layout_pending = False
        self._side_by_side = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        filters = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setObjectName("clustering-measurement-search")
        self.search.setPlaceholderText("Find a marker, channel or measurement")
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.region = QComboBox(self)
        self.region.setObjectName("clustering-measurement-region")
        self.region.setAccessibleName("Filter cell region")
        self.region.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.region.setMinimumContentsLength(12)
        self.region.setMaximumWidth(180)
        self.region.addItem("All cell regions", None)
        regions = dict.fromkeys(str(item.get("compartment") or "") for item in features)
        for region in regions:
            self.region.addItem(_REGIONS.get(region, region or "Unspecified region"), region)
        self.region.setVisible(len(regions) > 1)
        filters.addWidget(self.region)
        self.statistic = QComboBox(self)
        self.statistic.setObjectName("clustering-measurement-statistic")
        self.statistic.setAccessibleName("Filter measurement statistic")
        self.statistic.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.statistic.setMinimumContentsLength(10)
        self.statistic.setMaximumWidth(180)
        self.statistic.addItem("All statistics", None)
        for statistic in self.model.statistics:
            self.statistic.addItem(statistic, statistic)
        self.statistic.setVisible(len(self.model.statistics) > 1)
        filters.addWidget(self.statistic)
        layout.addLayout(filters)
        toolbar = QHBoxLayout()
        self.catalog_summary = QLabel(self)
        self.catalog_summary.setWordWrap(True)
        self.catalog_summary.setProperty("fieldHelp", True)
        toolbar.addWidget(self.catalog_summary, 1)
        self.select_shown = ContentSizedButton("Select shown", self)
        self.clear_shown = ContentSizedButton("Clear shown", self)
        for name, button, selected_value in (("select", self.select_shown, True), ("clear", self.clear_shown, False)):
            button.setObjectName("clustering-measurements-" + name)
            button.clicked.connect(lambda _checked=False, value=selected_value: self.select_visible(value))
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.matrix = QTableView(self)
        self.matrix.setObjectName("clustering-measurement-matrix")
        self.matrix.setAccessibleName("Measured marker and statistic inputs")
        self.matrix.setModel(self.model)
        self.matrix.setItemDelegate(_MeasurementCheck(self.matrix))
        style_scientific_table(self.matrix)
        self.matrix.setAlternatingRowColors(False)
        self.matrix.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.matrix.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.matrix.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.matrix.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.matrix.horizontalHeader().setMinimumSectionSize(80)
        self.matrix.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.matrix.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(2, self.model.columnCount()):
            self.matrix.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.matrix.horizontalHeader().setSectionsClickable(True)
        self.matrix.horizontalHeader().sectionClicked.connect(self._statistic_menu)
        self.matrix.verticalHeader().setDefaultSectionSize(42)
        self.selection_layout = QGridLayout()
        self.selection_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_layout.setHorizontalSpacing(16)
        self.selection_layout.setVerticalSpacing(12)
        self.selection_layout.addWidget(self.matrix, 0, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(self.selection_layout)

        self.input_captions = {}
        for feature in self.model.features:
            marker, region, statistic = feature_parts(feature)
            if len(self.model.marker_channels[marker]) > 1:
                marker += " · " + str(feature.get("channel_id") or "")
            self.input_captions[feature["feature_id"]] = (
                f"{marker}\n{region} · {statistic}",
                str(feature["measurement_name"]) + "\n" + str(feature.get("channel_id") or ""))
        self.inputs = SelectedMeasurements(self.input_captions, self) if show_inputs else None
        if self.inputs is not None:
            self.inputs.empty.setText("Choose the measurements that should determine similarity between cells.")
            self.inputs.remove_requested.connect(
                lambda key: self.set_value([item for item in self.value() if item != key]))
            self.selection_layout.addWidget(self.inputs, 1, 0, Qt.AlignmentFlag.AlignTop)
        self.search.textChanged.connect(self._filter)
        self.region.currentIndexChanged.connect(self._filter)
        self.statistic.currentIndexChanged.connect(self._filter)
        self.model.selection_changed.connect(self._selection_changed)
        self.setFocusProxy(self.search)
        self.set_value(selected)

    def value(self):
        return list(self.model.selected)

    def set_value(self, selected):
        self.model.set_selected(selected)

    def visible_ids(self, statistic=None):
        visible = set()
        for row_index, row in enumerate(self.model.rows):
            if self.matrix.isRowHidden(row_index):
                continue
            for column, name in enumerate(self.model.statistics, 2):
                if not self.matrix.isColumnHidden(column) and (statistic is None or name == statistic):
                    feature_id = row["features"].get(name)
                    if feature_id in self.model.filtered_ids:
                        visible.add(feature_id)
        return [item["feature_id"] for item in self.model.features if item["feature_id"] in visible]

    def select_visible(self, selected, statistic=None):
        visible = self.visible_ids(statistic)
        current = self.value()
        self.set_value(current + [item for item in visible if item not in current] if selected
                       else [item for item in current if item not in visible])

    def _statistic_menu(self, column):
        if column < 2:
            return
        statistic = self.model.statistics[column - 2]
        menu = QMenu(self)
        for text, selected in (("Select", True), ("Clear", False)):
            action = menu.addAction(f"{text} shown {statistic}")
            action.triggered.connect(lambda _checked=False, value=selected: self.select_visible(value, statistic))
        header = self.matrix.horizontalHeader()
        from PySide6.QtCore import QPoint
        menu.exec(header.mapToGlobal(QPoint(header.sectionViewportPosition(column), header.height())))

    def _filter(self, *_args):
        terms = self.search.text().casefold().split()
        region, statistic = self.region.currentData(), self.statistic.currentData()
        matches = set()
        for feature in self.model.features:
            text = " ".join(str(feature.get(key) or "") for key in
                            ("biomarker", "channel_id", "measurement_name", "compartment")).casefold()
            if (all(term in text for term in terms)
                    and (region is None or str(feature.get("compartment") or "") == region)
                    and (statistic is None or feature_parts(feature)[2] == statistic)):
                matches.add(feature["feature_id"])
        self.model.filtered_ids = matches
        for index, row in enumerate(self.model.rows):
            self.matrix.setRowHidden(index, not matches.intersection(row["features"].values()))
        for column, name in enumerate(self.model.statistics, 2):
            self.matrix.setColumnHidden(column, not any(row["features"].get(name) in matches for row in self.model.rows))
        visible = self.visible_ids()
        self.catalog_summary.setText(f"{len(visible):,} of {len(self.model.features):,} saved measurements shown")
        self.select_shown.setEnabled(any(item not in self.model.selected for item in visible))
        self.clear_shown.setEnabled(any(item in self.model.selected for item in visible))
        rows = sum(not self.matrix.isRowHidden(row) for row in range(self.model.rowCount()))
        self.matrix.setFixedHeight(self.matrix.horizontalHeader().sizeHint().height() + 42 * min(7, max(1, rows)) + 2 * self.matrix.frameWidth() + 10
                                   + (self.matrix.horizontalScrollBar().sizeHint().height() if self.matrix.horizontalScrollBar().isVisible() else 0))
        self.matrix.viewport().update()
        self._queue_layout()

    def _selection_changed(self):
        if self.inputs is not None:
            self.inputs.set_value(self.model.selected)
        self._filter()
        self.changed.emit()

    def _queue_layout(self):
        if not self._layout_pending:
            self._layout_pending = True
            QTimer.singleShot(0, self, self._fit_selection)

    def _fit_selection(self):
        self._layout_pending = False
        if self.inputs is None:
            return
        matrix_width = 260 + sum(self.matrix.columnWidth(column)
                                 for column in range(2, self.model.columnCount())
                                 if not self.matrix.isColumnHidden(column))
        side_by_side = self.width() >= max(720, matrix_width + 280)
        if side_by_side != self._side_by_side:
            self._side_by_side = side_by_side
            self.selection_layout.removeWidget(self.inputs)
            self.selection_layout.addWidget(self.inputs, 0 if side_by_side else 1,
                                            1 if side_by_side else 0, Qt.AlignmentFlag.AlignTop)
            self.selection_layout.setColumnStretch(0, 3 if side_by_side else 1)
            self.selection_layout.setColumnStretch(1, 2 if side_by_side else 0)
        self.inputs.maximum_list_height = 340 if side_by_side else 180
        self.inputs.fit_cards()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._queue_layout()


__all__ = ["ClusteringFeatures", "feature_caption", "feature_parts"]
