"""Edit cluster inclusion and phenotype names beside their measured cell counts."""

from collections.abc import Mapping
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


class ClusterDecisionModel(QAbstractTableModel):
    """Keep original cluster IDs fixed while researchers review their interpretation."""

    def __init__(self, cluster_ids, counts, value=None, parent=None):
        super().__init__(parent)
        self._ids = tuple(cluster_ids)
        self._counts = dict(counts)
        self._decisions = {}
        self.restore_value(value or {})

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._ids)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("Cluster", "Cells", "Include", "Phenotype name")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        cluster_id = self._ids[index.row()]
        decision = self._decisions[cluster_id]
        column = index.column()
        if column == 2 and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if decision["include"] else Qt.CheckState.Unchecked
        if column == 1 and role == Qt.ItemDataRole.DisplayRole:
            return f"{self._counts[cluster_id]:,}"
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return (cluster_id, self._counts[cluster_id], "", decision["name"])[column]
        if column == 1 and role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 2:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        elif index.column() == 3:
            if self._decisions[self._ids[index.row()]]["include"]:
                flags |= Qt.ItemFlag.ItemIsEditable
            else:
                flags &= ~Qt.ItemFlag.ItemIsEnabled
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid():
            return False
        decision = self._decisions[self._ids[index.row()]]
        if index.column() == 2 and role == Qt.ItemDataRole.CheckStateRole:
            decision["include"] = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
        elif index.column() == 3 and role == Qt.ItemDataRole.EditRole and decision["include"]:
            decision["name"] = str(value).strip()
        else:
            return False
        self.dataChanged.emit(self.index(index.row(), 2), self.index(index.row(), 3))
        return True

    def restore_value(self, value):
        """Restore a page draft without changing the available cluster inventory."""
        if not isinstance(value, Mapping) or set(value) - set(self._ids):
            raise ValueError("Cluster choices refer to a different review")
        self.beginResetModel()
        self._decisions = {
            key: {"include": value.get(key, {}).get("include", True),
                  "name": value.get(key, {}).get("name", "")}
            for key in self._ids
        }
        self.endResetModel()

    def raw_value(self):
        return {key: dict(value) for key, value in self._decisions.items()}

    def name_clusters(self, cluster_ids, name):
        """Name a complete included selection without silently dropping members."""
        identities = tuple(cluster_ids)
        name = str(name).strip()
        if (not identities or not name or len(set(identities)) != len(identities)
                or any(key not in self._decisions or not self._decisions[key]['include']
                       for key in identities)):
            return False
        for key in identities:
            self._decisions[key]['name'] = name
        rows = [self._ids.index(key) for key in identities]
        self.dataChanged.emit(self.index(min(rows), 3), self.index(max(rows), 3))
        return True

    def reporting_groups(self):
        """Preview the same effective phenotype names used by label application."""
        groups = {}
        for key in self._ids:
            decision = self._decisions[key]
            if not decision['include']:
                continue
            name = decision['name'] or key
            group = groups.setdefault(name, {'phenotype_name': name, 'cell_count': 0, 'cluster_ids': []})
            group['cell_count'] += self._counts[key]
            group['cluster_ids'].append(key)
        return tuple({**group, 'cluster_ids': tuple(group['cluster_ids'])} for group in groups.values())

    def summary(self):
        excluded = sum(self._counts[key] for key in self._ids if not self._decisions[key]["include"])
        total = sum(self._counts.values())
        return f"{len(self._ids):,} clusters · {total - excluded:,} cells included · {excluded:,} cells excluded from phenotyping"
