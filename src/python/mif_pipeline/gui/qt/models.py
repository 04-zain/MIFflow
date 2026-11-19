"""Qt item models for immutable presentation-neutral form tables.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt

from mif_pipeline.gui.interactions import FormTable
from mif_pipeline.gui.terminology import field_label, table_value_label

_DISPLAY_HEADERS = {
    "enabled": "Included",
    "batch_id": "Batch ID",
    "sample_id": "Sample name",
    "slide_id": "Slide ID",
    "subject_id": "Subject ID",
    "sample_ids": "Samples",
    "validation_domain_id": "Processing group",
    "source_mif_path": "mIF image",
    "source_image_index": "Image index",
    "source_series_index": "Series index",
    "current_or_final_stage": "Current step",
    "progress": "Progress",
    "pending_interaction_reason": "Review needed",
    "failure_reason": "Problem",
    "required_by_node_kinds": "Required by",
    "diagnostic": "Details",
    "final_non_spatial_export_id": "Export record",
    "processed_image_path": "Processed image",
    "qupath_project_entry": "QuPath project entry",
    "status": "Status",
    "kind": "Kind",
    "owner": "Owner",
    "requirement": "Requirement",
    "state": "State",
    "scope": "Scope",
    "path": "File",
    "sha256": "SHA-256",
}


# ==============================================================================
#                                     Models
# ==============================================================================


def _immutable_snapshot(value: object) -> object:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_snapshot(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_immutable_snapshot(item) for item in value)
    return value


class FormTableModel(QAbstractTableModel):
    """Expose one already-bounded immutable form table without editable cells."""

    def __init__(
        self,
        table: FormTable,
        parent: QObject | None = None,
    ) -> None:
        if not isinstance(table, FormTable):
            raise TypeError("Qt form table model requires a FormTable")
        super().__init__(parent)
        self._columns = tuple(table.columns)
        self._row_ids = tuple(row.row_id for row in table.rows)
        self._rows = tuple(
            tuple(_immutable_snapshot(value) for value in row.values) for row in table.rows
        )

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            not index.isValid()
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(self._columns)
        ):
            return None
        value = self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return table_value_label(self._columns[index.column()], value)
        if role == Qt.ItemDataRole.ToolTipRole:
            return "" if value is None else str(value)
        if role == Qt.ItemDataRole.UserRole:
            return value
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole or section < 0:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section >= len(self._columns):
                return None
            column = self._columns[section]
            return _DISPLAY_HEADERS.get(column, field_label(column))
        if orientation == Qt.Orientation.Vertical and section < len(self._row_ids):
            return section + 1
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        del index, value, role
        return False
