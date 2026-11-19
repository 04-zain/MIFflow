"""Edit one cellular region and the selected intensity measurements per channel."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHeaderView, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from mif_pipeline.orchestration.batch_measurement_settings import MeasurementSettings
from mif_pipeline.gui.measurement_setup import (
    available_shape_choices, compartment_choices, segmentation_measurement_note, _initial_channel_role,
)
from .measurement_widgets import IntensityMeasurements
from .measurement_channel_grid import _MeasurementHeader


class MeasurementOptionsEditor(QWidget):
    """Group saved intensities by channel while retaining their exact feature IDs.

    Each channel has one region and several optional intensity measurements.
    Required measurements stay selected, and their region and role stay fixed.
    Shape measurements retain their separate inclusion controls.
    """

    def __init__(self, settings: MeasurementSettings, required_feature_ids=(), parent=None, *, cell_boundary_source=None):
        super().__init__(parent)
        self._channels = settings.channels
        self._saved_requests = settings.feature_requests
        self._rows = []
        self._channel_editors = {}
        self._shape_rows = []
        self._required = frozenset(required_feature_ids)
        self._boundary_source = cell_boundary_source
        shapes = available_shape_choices(cell_boundary_source)
        self._shape_labels = {row["measurement_name"]: label for _, label, row in shapes}
        layout = QVBoxLayout(self)
        note = QLabel(segmentation_measurement_note(cell_boundary_source), self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.table = QTableWidget(0, 10, self)
        self.table.setObjectName("measurement-options-table")
        self.table.setProperty("scientificTable", True)
        self.table.setHorizontalHeader(_MeasurementHeader(self.table))
        self.table.horizontalHeader().setMinimumSectionSize(64)
        self.table.setHorizontalHeaderLabels(("Include", "Channel", "Role", "Cell region", "Mean", "Median", "Min", "Max", "Std. dev.", "Variance"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column, width in ((0, 66), (1, 130), (2, 200), (3, 180)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        for column in range(4, 10):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.table, "Channel intensities")
        intensity_kinds = {"expression", "dapi", "autofluorescence"}
        for channel in self._channels:
            originals = [dict(row) for row in settings.feature_requests
                         if row["kind"] in intensity_kinds and row["channel_id"] == channel.channel_id]
            self._add_channel(channel, originals)
        self.shapes = QTableWidget(0, 2, self)
        self.shapes.setObjectName("measurement-options-shapes")
        self.shapes.setProperty("scientificTable", True)
        self.shapes.setHorizontalHeaderLabels(("Include", "Shape measurement"))
        self.shapes.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.shapes.verticalHeader().hide()
        self.shapes.setShowGrid(False)
        self.shapes.setAlternatingRowColors(True)
        self.shapes.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.shapes.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.shapes.setColumnWidth(0, 66)
        self.shapes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for request in settings.feature_requests:
            if request["kind"] not in intensity_kinds:
                self._add_shape(dict(request))
        for key, label, request in shapes:
            if not any(row["measurement_name"] == request["measurement_name"] for row, _ in self._shape_rows):
                self._add_shape(dict(request), selected=False)
        self.tabs.addTab(self.shapes, "Cell and nuclear shape")
        self.tabs.currentChanged.connect(self._fit_tables)
        self._fit_tables()
        if self._required:
            required = QLabel("Measurements used by the current phenotyping settings remain selected.", self)
            required.setWordWrap(True)
            layout.addWidget(required)
        layout.addStretch(1)

    def _fit_tables(self, *_args):
        table = self.table if self.tabs.currentIndex() == 0 else self.shapes
        height = table.horizontalHeader().sizeHint().height() + sum(table.rowHeight(row) for row in range(table.rowCount()))
        self.tabs.setFixedHeight(min(440, max(220, height + self.tabs.tabBar().sizeHint().height() + 24)))

    def _append_row(self, record, *, selected: bool, required: bool, height: int) -> int:
        index = self.table.rowCount()
        self.table.insertRow(index)
        self.table.setRowHeight(index, height)
        self._rows.append(record)
        include = QCheckBox(self.table)
        include.setChecked(selected)
        include.setEnabled(not required)
        if required:
            include.setToolTip("Used by the current phenotyping settings.")
        self._center_check(self.table, index, 0, include)
        return index

    @staticmethod
    def _center_check(table, row, column, check):
        cell = QWidget(table)
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(check, alignment=Qt.AlignmentFlag.AlignCenter)
        table.setCellWidget(row, column, cell)

    def _included(self, row):
        return self.table.cellWidget(row, 0).findChild(QCheckBox)

    def _choice(self, choices, selected=None):
        combo = QComboBox(self.table)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.addItem("Choose…", None)
        for value, label in choices:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.setFixedHeight(combo.sizeHint().height())
        return combo

    def _add_channel(self, channel, originals) -> None:
        """Display one row for this channel and every saved statistic in its region."""
        required = [row for row in originals if row["feature_id"] in self._required]
        first = originals[0] if originals else None
        selected = [row["measurement_name"].rsplit(": ", 1)[-1] for row in originals] or ["Mean"]
        required_statistics = [row["measurement_name"].rsplit(": ", 1)[-1] for row in required]
        index = self._append_row((channel, originals), selected=bool(originals), required=bool(required), height=48)
        name = channel.name if not channel.biomarker or channel.name == channel.biomarker else f"{channel.name} · {channel.biomarker}"
        self.table.setItem(index, 1, QTableWidgetItem(name))
        role = self._choice((("expression", "Protein marker"), ("dapi", "DNA"), ("autofluorescence", "Autofluorescence")), _initial_channel_role(channel, None) if first is None else first["kind"])
        choices = compartment_choices(self._boundary_source)
        region = self._choice(choices, ("cell" if self._boundary_source is None else None) if first is None else first["compartment"])
        statistics = IntensityMeasurements(selected, required=required_statistics, compact=True, parent=self.table)
        self._channel_editors[index] = role, region, statistics
        for column, widget in ((2, role), (3, region)):
            self.table.setCellWidget(index, column, widget)
        while statistics.layout().count():
            statistics.layout().takeAt(0)
        for column, (statistic, check) in enumerate(statistics._checks.items(), 4):
            check.setText("")
            check.setAccessibleName(f"{name}: {statistic} intensity")
            check.setToolTip(check.accessibleName())
            self._center_check(self.table, index, column, check)
        statistics.hide()
        def update_enabled(included):
            role.setEnabled(included and not required)
            region.setEnabled(included and not required)
            for statistic, check in statistics._checks.items():
                check.setEnabled(included and statistic not in required_statistics)
        self._included(index).toggled.connect(update_enabled)
        update_enabled(bool(originals))

    def _add_shape(self, request, selected=True) -> None:
        required = request["feature_id"] in self._required
        index = self.shapes.rowCount()
        self.shapes.insertRow(index)
        self.shapes.setRowHeight(index, 36)
        check = QCheckBox(self.shapes)
        check.setChecked(selected)
        check.setEnabled(not required)
        self._center_check(self.shapes, index, 0, check)
        self._shape_rows.append((request, check))
        name = self._shape_labels.get(request["measurement_name"], request["measurement_name"])
        check.setAccessibleName(name)
        self.shapes.setItem(index, 1, QTableWidgetItem(name))

    def selected_settings(self) -> MeasurementSettings:
        """Expand checked statistics into the existing flat measurement requests.

        Unchanged requests retain their IDs, units, optional status and ordering.
        New statistics use the same names and IDs as the initial setup form.
        """
        rows = []
        for index, record in enumerate(self._rows):
            if not self._included(index).isChecked():
                continue
            channel, originals = record
            role_editor, region_editor, statistics_editor = self._channel_editors[index]
            role = role_editor.currentData()
            region = region_editor.currentData()
            statistics = statistics_editor.value()
            if role is None or region is None or not statistics:
                raise ValueError(f"{channel.name}: choose a role, one cell region and at least one intensity measurement.")
            for statistic in statistics:
                name = f"{region.capitalize()}: {channel.name}: {statistic}"
                original = next((row for row in originals
                                 if row["kind"] == role and row["compartment"] == region
                                 and row["measurement_name"] == name), None)
                if original is not None:
                    rows.append(original)
                else:
                    rows.append({"feature_id": f"intensity-{channel.channel_id.encode('utf-8').hex()}-{region}-{statistic}",
                        "measurement_name": name, "kind": role, "channel_id": channel.channel_id,
                        "biomarker": channel.biomarker if role == "expression" else None,
                        "compartment": region, "units": None, "opt_in": False})
        rows.extend(request for request, check in self._shape_rows if check.isChecked())
        selected = {row["feature_id"]: row for row in rows}
        ordered = [selected.pop(row["feature_id"]) for row in self._saved_requests
                   if row["feature_id"] in selected]
        return MeasurementSettings(self._channels, tuple(ordered + list(selected.values())))
