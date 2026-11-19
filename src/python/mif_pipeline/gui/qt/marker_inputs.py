"""Choose marker identities and their existing saved measurement controls."""

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QComboBox, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
    QSizePolicy, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .workbench import style_scientific_table


class _MeasurementCellDelegate(QStyledItemDelegate):
    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class MarkerInputs(QWidget):
    changed = Signal()

    def __init__(self, context, editors, selected, parent=None):
        super().__init__(parent)
        self.setObjectName('marker-input-selector')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._markers = tuple(context['canonical_marker_ids'])
        self._sources = {marker: editors[f'marker_{marker}_feature'] for marker in self._markers}
        self._search_text = {marker: ' '.join([marker, *(str(row.get(key) or '')
            for row in context['offered_marker_features'] if row['marker_id'] == marker
            for key in ('measurement', 'compartment', 'channel_id'))]).casefold()
            for marker in self._markers}
        self.measurements = {}
        self.measurement_labels = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.search = QLineEdit(self)
        self.search.setObjectName('marker-input-search')
        self.search.setAccessibleName('Find a marker or saved measurement')
        self.search.setPlaceholderText('Find a marker or saved measurement')
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        toolbar = QHBoxLayout()
        self.summary = QLabel(self)
        self.summary.setProperty('fieldHelp', True)
        self.summary.setWordWrap(True)
        toolbar.addWidget(self.summary, 1)
        layout.addLayout(toolbar)
        self.table = QTableWidget(len(self._markers), 2, self)
        self.table.setObjectName('marker-input-measurements')
        self.table.setAccessibleName('Markers and their saved cell measurements')
        self.table.setHorizontalHeaderLabels(('Marker', 'Cell measurement'))
        style_scientific_table(self.table)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setItemDelegateForColumn(1, _MeasurementCellDelegate(self.table))
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setMinimumWidth(0)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, min(200, max(125,
            max((self.table.fontMetrics().horizontalAdvance(marker) for marker in self._markers), default=0) + 48)))
        self.table.verticalHeader().setDefaultSectionSize(46)
        for row, marker in enumerate(self._markers):
            item = QTableWidgetItem(marker)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, marker)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(marker + '\nSelect this marker for phenotype rules. Space toggles selection.')
            self.table.setItem(row, 0, item)
            source = self._sources[marker]
            cell = QWidget(self.table)
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            combo = QComboBox(cell)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            combo.setStyleSheet(
                'QComboBox { border: 1px solid transparent; border-radius: 0; '
                'padding: 0 30px 0 10px; min-height: 0; background: transparent; }'
                'QComboBox:focus { border-color: #006b70; background: #ffffff; }'
            )
            combo.setObjectName('marker-input-measurement-' + marker)
            combo.setAccessibleName('Saved measurement for ' + marker)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
            combo.setMinimumWidth(0)
            combo.setModel(source.model())
            combo.setCurrentIndex(source.currentIndex())
            combo.currentIndexChanged.connect(source.setCurrentIndex)
            source.currentIndexChanged.connect(lambda _index, identity=marker: self._source_changed(identity))
            cell_layout.addWidget(combo)
            label = QLabel(cell)
            label.setContentsMargins(10, 0, 10, 0)
            label.setAccessibleName('Saved measurement for ' + marker)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            cell_layout.addWidget(label)
            self.measurements[marker] = combo
            self.measurement_labels[marker] = label
            self.table.setCellWidget(row, 1, cell)
        layout.addWidget(self.table)
        self.empty = QLabel('No markers match this search.', self)
        self.empty.setProperty('fieldHelp', True)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.table.itemChanged.connect(self._selection_changed)
        self.search.textChanged.connect(self._filter)
        self.setFocusProxy(self.search)
        self.set_value(selected)
        self._filter()

    def value(self):
        return [marker for row, marker in enumerate(self._markers)
                if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def set_value(self, selected):
        selected = tuple(selected or ())
        if len(set(selected)) != len(selected) or not set(selected) <= set(self._markers):
            raise ValueError('Select distinct markers available in this analysis.')
        with QSignalBlocker(self.table):
            for row, marker in enumerate(self._markers):
                self.table.item(row, 0).setCheckState(
                    Qt.CheckState.Checked if marker in selected else Qt.CheckState.Unchecked)
        self.refresh()
        self.changed.emit()

    def refresh(self):
        """Resync after restoring the original form editors with signals blocked."""
        selected = set(self.value())
        for marker, combo in self.measurements.items():
            source = self._sources[marker]
            with QSignalBlocker(combo):
                combo.setCurrentIndex(source.currentIndex())
            combo.setEnabled(marker in selected)
            self._measurement_presentation(marker)
        self._refresh_summary()

    def _source_changed(self, marker):
        source, combo = self._sources[marker], self.measurements[marker]
        with QSignalBlocker(combo):
            combo.setCurrentIndex(source.currentIndex())
        self._measurement_presentation(marker)
        self.changed.emit()

    def _measurement_presentation(self, marker):
        source, combo = self._sources[marker], self.measurements[marker]
        label = self.measurement_labels[marker]
        single = source.currentData() is not None and sum(
            source.itemData(index) is not None for index in range(source.count())) == 1
        combo.setVisible(not single)
        combo.setToolTip(source.currentText())
        label.setVisible(single)
        label.setText(source.currentText())
        label.setToolTip(source.currentText())
        label.setEnabled(combo.isEnabled())

    def _selection_changed(self, item):
        if item.column() == 0:
            self.refresh()
            self.changed.emit()

    def _filter(self, *_args):
        terms = self.search.text().casefold().split()
        shown = 0
        for row, marker in enumerate(self._markers):
            visible = all(term in self._search_text[marker] for term in terms)
            self.table.setRowHidden(row, not visible)
            shown += visible
        self.table.setVisible(bool(shown))
        self.empty.setVisible(not shown)
        self.table.setFixedHeight(self.table.horizontalHeader().sizeHint().height()
            + 46 * min(9, shown) + 2 * self.table.frameWidth())
        self._refresh_summary()
        self.updateGeometry()

    def _refresh_summary(self):
        selected = set(self.value())
        self.summary.setText(f'{len(selected):,} of {len(self._markers):,} markers selected')


__all__ = ['MarkerInputs']
