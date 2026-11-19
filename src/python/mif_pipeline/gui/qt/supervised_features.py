"""Select saved channel and shape measurements for a cell classifier."""

from __future__ import annotations

from PySide6.QtCore import QRect, QSignalBlocker, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHeaderView, QLabel, QLineEdit, QSizePolicy,
    QStyle, QStyleOptionViewItem,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .clustering_features import ClusteringFeatures
from .selected_measurements import SelectedMeasurements
from .workbench import style_scientific_table


class SupervisedFeatures(QWidget):
    changed = Signal()

    def __init__(self, features, selected=(), parent=None):
        super().__init__(parent)
        self.setObjectName("supervised-measurement-selector")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._features = tuple(features)
        self._ids = {row["feature_id"] for row in features}
        self._selected = []
        self._layout_pending = False
        self._side_by_side = None
        channel_features = [row for row in features if row.get("channel_id")]
        other_features = [row for row in features if not row.get("channel_id")]
        self._marker_ids = {row["feature_id"] for row in channel_features}
        self._other_ids = self._ids - self._marker_ids
        self.selection_layout = QGridLayout(self)
        self.selection_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_layout.setHorizontalSpacing(18)
        self.selection_layout.setVerticalSpacing(14)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("supervised-input-groups")
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet("QTabWidget#supervised-input-groups::pane { border: none; background: transparent; } QStackedWidget { background: transparent; }")
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.selection_layout.addWidget(self.tabs, 0, 0, Qt.AlignmentFlag.AlignTop)
        marker_page = QWidget(self.tabs)
        marker_layout = QVBoxLayout(marker_page)
        marker_layout.setContentsMargins(0, 14, 0, 0)
        marker_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.markers = ClusteringFeatures(channel_features, parent=marker_page, show_inputs=False)
        self.markers.search.setVisible(len({row["channel_id"] for row in channel_features}) > 20)
        marker_layout.addWidget(self.markers)
        self.tabs.addTab(marker_page, "Marker intensities")

        shape_page = QWidget(self.tabs)
        shape_layout = QVBoxLayout(shape_page)
        shape_layout.setContentsMargins(0, 14, 0, 0)
        shape_layout.setSpacing(10)
        shape_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.shape_search = QLineEdit(shape_page)
        self.shape_search.setObjectName("supervised-shape-search")
        self.shape_search.setPlaceholderText("Find a shape measurement")
        self.shape_search.setClearButtonEnabled(True)
        self.shape_search.setVisible(len(other_features) > 20)
        shape_layout.addWidget(self.shape_search)
        self.shapes = QTableWidget(len(other_features), 2, shape_page)
        self.shapes.setObjectName("supervised-shape-measurements")
        self.shapes.setAccessibleName("Saved cell and nuclear shape measurements")
        self.shapes.setHorizontalHeaderLabels(("Measurement", "Units"))
        style_scientific_table(self.shapes)
        self.shapes.setAlternatingRowColors(False)
        self.shapes.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.shapes.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.shapes.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.shapes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shapes.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shapes.setWordWrap(True)
        self.shapes.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.shapes.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.shapes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.shapes.horizontalHeader().sectionResized.connect(self._queue_layout)
        self.shapes.verticalHeader().setDefaultSectionSize(38)
        from mif_pipeline.cell_analysis.measurement_plan import shape_measurement_choices
        labels = {key: (title, row.get("units")) for key, title, row in shape_measurement_choices()}
        captions = dict(self.markers.input_captions)
        for index, feature in enumerate(other_features):
            key = feature["feature_id"]
            title, units = labels.get(key, (feature["measurement_name"], feature.get("units")))
            if units and title.endswith(f" ({units})"):
                title = title[:-(len(units) + 3)]
            captions[key] = (title + ("\n" + units if units else ""), feature["measurement_name"])
            item = QTableWidgetItem(title)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(feature["measurement_name"])
            self.shapes.setItem(index, 0, item)
            unit_item = QTableWidgetItem(units or "—")
            unit_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.shapes.setItem(index, 1, unit_item)
        shape_layout.addWidget(self.shapes)
        self.shape_empty = QLabel("No shape measurements were selected in Cell measurements.", shape_page)
        self.shape_empty.setWordWrap(True)
        self.shape_empty.setProperty("fieldHelp", True)
        self.shape_empty.setVisible(not other_features)
        shape_layout.addWidget(self.shape_empty)
        self.tabs.addTab(shape_page, "Cell and nuclear shape")
        self.tabs.setTabVisible(0, bool(channel_features))
        self.tabs.setTabVisible(1, bool(other_features))
        self.tabs.tabBar().setVisible(bool(channel_features and other_features))
        self.inputs = SelectedMeasurements(captions, self)
        self.inputs.empty.setText("Select marker intensities or shape measurements to use for classification.")
        self.inputs.remove_requested.connect(
            lambda key: self.set_value([item for item in self.value() if item != key]))
        self.selection_layout.addWidget(self.inputs, 1, 0, Qt.AlignmentFlag.AlignTop)
        self.markers.changed.connect(self._markers_changed)
        self.shapes.itemChanged.connect(self._shape_changed)
        self.shape_search.textChanged.connect(self._filter_shapes)
        self.tabs.currentChanged.connect(self._queue_layout)
        self.setFocusProxy(self.markers if channel_features else self.shapes)
        self.set_value(selected)
        self._filter_shapes()

    def value(self):
        return list(self._selected)

    def set_value(self, values):
        values = list(values or ())
        if len(set(values)) != len(values) or not set(values) <= self._ids:
            raise ValueError("Choose distinct measurements from the saved measurement list.")
        self._selected = values
        with QSignalBlocker(self.markers), QSignalBlocker(self.shapes):
            self.markers.set_value([key for key in values if key in self._marker_ids])
            for row in range(self.shapes.rowCount()):
                item = self.shapes.item(row, 0)
                item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in values
                                   else Qt.CheckState.Unchecked)
        self._refresh_summary()
        self.changed.emit()

    def _markers_changed(self):
        chosen = self.markers.value()
        self._selected = [key for key in self._selected if key in self._other_ids or key in chosen]
        self._selected.extend(key for key in chosen if key not in self._selected)
        self._refresh_summary()
        self.changed.emit()

    def _shape_changed(self, item):
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked and key not in self._selected:
            self._selected.append(key)
        elif item.checkState() != Qt.CheckState.Checked:
            self._selected = [value for value in self._selected if value != key]
        self._refresh_summary()
        self.changed.emit()

    def _refresh_summary(self):
        channel_count = sum(key in self._marker_ids for key in self._selected)
        shape_count = len(self._selected) - channel_count
        self.tabs.setTabText(0, f"Marker intensities · {channel_count}")
        self.tabs.setTabText(1, f"Cell and nuclear shape · {shape_count}")
        self.inputs.set_value(self._selected)
        self._queue_layout()

    def _filter_shapes(self, *_args):
        terms = self.shape_search.text().casefold().split()
        for row in range(self.shapes.rowCount()):
            item = self.shapes.item(row, 0)
            visible = all(term in (item.text() + " " + item.toolTip()).casefold() for term in terms)
            self.shapes.setRowHidden(row, not visible)
        self._queue_layout()

    def _queue_layout(self, *_args):
        if not self._layout_pending:
            self._layout_pending = True
            QTimer.singleShot(0, self, self._fit_page)

    def _fit_page(self, *_args):
        self._layout_pending = False
        side_by_side = self.width() >= 860
        if side_by_side != self._side_by_side:
            self._side_by_side = side_by_side
            self.selection_layout.removeWidget(self.inputs)
            self.selection_layout.addWidget(self.inputs, 0 if side_by_side else 1,
                                            1 if side_by_side else 0, Qt.AlignmentFlag.AlignTop)
            self.selection_layout.setColumnStretch(0, 3 if side_by_side else 1)
            self.selection_layout.setColumnStretch(1, 2 if side_by_side else 0)
            self.selection_layout.activate()
        # Use the real row/header geometry after styling, including wrapped names.
        # The enclosing form scrolls the catalogue as one part of the page.
        self.shapes.resizeRowsToContents()
        for row in range(self.shapes.rowCount()):
            if self.shapes.isRowHidden(row):
                continue
            index = self.shapes.model().index(row, 0)
            option = QStyleOptionViewItem()
            self.shapes.itemDelegate().initStyleOption(option, index)
            option.rect = self.shapes.visualRect(index)
            text_rect = self.shapes.style().subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, self.shapes)
            text_height = option.fontMetrics.boundingRect(
                QRect(0, 0, max(1, text_rect.width()), 10000), Qt.TextFlag.TextWordWrap, option.text).height()
            self.shapes.setRowHeight(row, max(self.shapes.rowHeight(row),
                                             text_height + option.rect.height() - text_rect.height()))
        height = self.shapes.horizontalHeader().height() + 2 * self.shapes.frameWidth() + 2
        height += sum(self.shapes.rowHeight(row) for row in range(self.shapes.rowCount())
                      if not self.shapes.isRowHidden(row))
        self.shapes.setFixedHeight(height)
        # QTabWidget normally reserves space for its largest hidden page.
        for index in range(self.tabs.count()):
            self.tabs.widget(index).setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred if index == self.tabs.currentIndex() else QSizePolicy.Policy.Ignored)
        page_layout = self.tabs.currentWidget().layout()
        page_height = (page_layout.totalHeightForWidth(self.tabs.width())
                       if page_layout.hasHeightForWidth() else page_layout.totalSizeHint().height())
        tab_height = self.tabs.tabBar().sizeHint().height() if self.tabs.tabBar().isVisible() else 0
        self.tabs.setFixedHeight(page_height + tab_height + 2)
        self.tabs.updateGeometry()
        self.inputs.fit_cards()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._queue_layout()


__all__ = ["SupervisedFeatures"]
