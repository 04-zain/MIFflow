"""Select reusable AF control sets from the current immutable region export."""
from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QHeaderView, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from .reference_regions import _CenteredUseDelegate, _ImageNameDelegate
from .reference_workspace import decision_combo, reference_table, text_label
from .researcher_widgets import ContentSizedButton


class AfControlRegionsWorkspace(QWidget):
    native_action_requested = Signal(str)

    def __init__(self, review, values, parent=None):
        super().__init__(parent)
        self.setObjectName('reference-regions-workspace')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._review = review
        self._fields = dict(review['pairing_fields'])
        self._selectable = tuple(review['selectable_ids'])
        by_id = {row['roi_id']: row for row in review['rows']}
        self._controls = tuple(by_id[roi_id] for roi_id in self._selectable)
        self._positive_fields = tuple((field, by_id[roi_id]) for field, roi_id in self._fields.items()
                                      if roi_id in by_id)
        self._positive = tuple(row for _, row in self._positive_fields)
        self._sets = []
        self.validation_editors, self.field_errors, self.pairing_editors = {}, {}, {}
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)
        actions = QHBoxLayout(); actions.setSpacing(8); self.buttons = {}
        for action, label in (('edit', 'Edit regions in QuPath'), ('load', 'Load saved regions'),
                              ('accept', 'Accept displayed regions')):
            button = ContentSizedButton(label, self)
            button.setObjectName('reference-regions-' + action)
            button.clicked.connect(lambda _checked=False, key=action: self.native_action_requested.emit(key))
            button.setVisible(action == 'load' or (action == 'accept' and bool(review['rows']) and not review['accepted']))
            if action == 'accept': button.setProperty('actionRole', 'primary')
            actions.addWidget(button); self.buttons[action] = button
        actions.addStretch(1); layout.addLayout(actions)
        self.status = text_label('', self, supporting=True); layout.addWidget(self.status)
        self._content = QWidget(self); content = QVBoxLayout(self._content)
        content.setContentsMargins(0, 0, 0, 0); content.setSpacing(10)
        header = QHBoxLayout(); header.setSpacing(10)
        title = text_label('Control regions', self, heading=True)
        title.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        header.addWidget(title)
        self.set_selector = QComboBox(self); self.set_selector.setObjectName('af-control-set-selector')
        self.set_selector.setAccessibleName('AF control set whose regions are shown')
        self.set_selector.setMinimumWidth(180); header.addWidget(self.set_selector)
        self.new_set_button = ContentSizedButton('New control set', self)
        self.new_set_button.setToolTip('Use a separate set when regions have a different AF shape or tissue context.')
        self.new_set_button.clicked.connect(self._new_set)
        self.new_set_button.setEnabled(review['accepted'])
        self.new_set_button.setVisible(len(self._controls) > 1)
        header.addWidget(self.new_set_button); header.addStretch(1); content.addLayout(header)
        self.table = QTableWidget(len(self._controls), 4, self)
        self.table.setObjectName('reference-region-selection-table')
        self.table.setHorizontalHeaderLabels(('Use', 'Image', 'AF region', 'Area (µm²)'))
        reference_table(self.table)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setItemDelegateForColumn(0, _CenteredUseDelegate(self.table))
        self.table.setItemDelegateForColumn(1, _ImageNameDelegate(self.table))
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 48), (2, 145), (3, 115)):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        for index, row in enumerate(self._controls):
            use = QTableWidgetItem(); use.setData(Qt.ItemDataRole.UserRole, row['roi_id'])
            use.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                         (Qt.ItemFlag.ItemIsUserCheckable if review['accepted'] else Qt.ItemFlag.NoItemFlags))
            use.setCheckState(Qt.CheckState.Unchecked); self.table.setItem(index, 0, use)
            for col, value in ((1, row['image_name']), (2, f"AF region {row['number']}"), (3, f"{row['area_um2']:,.1f}")):
                item = QTableWidgetItem(value); item.setToolTip(value)
                if col == 3: item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(index, col, item)
        self.table.setFixedHeight(min(310, 48 + 52 * len(self._controls)))
        content.addWidget(self.table)
        self.membership_status = text_label('', self, supporting=True); content.addWidget(self.membership_status)
        content.addWidget(text_label('Controls for each stained region', self, heading=True))
        self.positive_table = QTableWidget(len(self._positive), 3, self)
        self.positive_table.setObjectName('af-control-set-assignments')
        self.positive_table.setHorizontalHeaderLabels(('Stained image', 'Region', 'AF control set'))
        reference_table(self.positive_table)
        self.positive_table.verticalHeader().setDefaultSectionSize(52)
        self.positive_table.setItemDelegateForColumn(0, _ImageNameDelegate(self.positive_table))
        self.positive_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 120), (2, 220)):
            self.positive_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.positive_table.setColumnWidth(col, width)
        for index, (field, row) in enumerate(self._positive_fields):
            self.positive_table.setItem(index, 0, QTableWidgetItem(row['image_name']))
            self.positive_table.setItem(index, 1, QTableWidgetItem(f"Stained {row['number']}"))
            cell = QWidget(self); cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0); cell_layout.setSpacing(0)
            choice = decision_combo(cell, ())
            choice.setAccessibleName(f"AF control set for {row['image_name']}, stained region {row['number']}")
            choice.setEnabled(review['accepted']); cell_layout.addWidget(choice)
            error = text_label('', cell); error.setProperty('validationMessage', True); error.hide()
            cell_layout.addWidget(error)
            self.pairing_editors[field] = choice; self.validation_editors[field] = choice; self.field_errors[field] = error
            choice.currentIndexChanged.connect(self._update_status)
            self.positive_table.setCellWidget(index, 2, cell)
        self.positive_table.setFixedHeight(min(270, 48 + 52 * len(self._positive)))
        content.addWidget(self.positive_table); layout.addWidget(self._content)
        self._content.setVisible(bool(self._controls))
        self.option_editor = None
        self.set_selector.currentIndexChanged.connect(self._show_members)
        self.table.itemChanged.connect(self._membership_changed)
        self.set_values(values)

    def _new_set(self):
        self._sets.append(())
        self._refresh_choices()
        self.set_selector.setCurrentIndex(len(self._sets) - 1)

    def _refresh_choices(self, assignments=None):
        current = self.set_selector.currentIndex()
        blocker = QSignalBlocker(self.set_selector); self.set_selector.clear()
        for index in range(len(self._sets)): self.set_selector.addItem(f'AF control set {index + 1}', index)
        self.set_selector.setCurrentIndex(max(0, min(current, len(self._sets) - 1))); del blocker
        for field, editor in self.pairing_editors.items():
            value = assignments.get(field) if assignments is not None else editor.currentData()
            blocker = QSignalBlocker(editor); editor.clear(); editor.addItem('Choose…', None)
            for index in range(len(self._sets)): editor.addItem(f'AF control set {index + 1}', index)
            editor.setCurrentIndex(max(0, editor.findData(value))); del blocker
        self._show_members()

    def _show_members(self, *_):
        index = self.set_selector.currentIndex()
        selected = set(self._sets[index]) if index >= 0 else set()
        blocker = QSignalBlocker(self.table)
        for row, control in enumerate(self._controls):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if control['roi_id'] in selected else Qt.CheckState.Unchecked)
        del blocker
        self._update_status()

    def _membership_changed(self, item):
        if item.column() != 0 or self.set_selector.currentIndex() < 0: return
        self._sets[self.set_selector.currentIndex()] = tuple(row['roi_id'] for index, row in enumerate(self._controls)
            if self.table.item(index, 0).checkState() == Qt.CheckState.Checked)
        self._update_status()

    def _update_status(self, *_):
        self.status.setVisible(not self._review['accepted'] or not self._controls)
        self.status.setText('Load saved AF regions to form a control set.' if not self._controls else
                            'Review and accept the saved regions before choosing control sets.')
        index = self.set_selector.currentIndex()
        count = len(self._sets[index]) if index >= 0 else 0
        users = sum(editor.currentData() == index for editor in self.pairing_editors.values())
        self.membership_status.setText(f'{count} control region' + ('' if count == 1 else 's') +
            f' · Used by {users} stained region' + ('' if users == 1 else 's'))

    def values(self):
        assigned = dict.fromkeys(self._fields, ())
        assigned.update({field: self._sets[editor.currentData()] if editor.currentData() is not None else ()
                         for field, editor in self.pairing_editors.items()})
        used = {roi_id for members in assigned.values() for roi_id in members}
        return {'selected_roi_ids': tuple(roi_id for roi_id in self._selectable if roi_id in used), **assigned}

    def set_values(self, values):
        if self._sets:
            values = {**self.values(), **values}
        assignments, self._sets = {}, []
        explicit = any(field in values for field in self._fields)
        for field in self._fields:
            members = values.get(field) if explicit else values.get('selected_roi_ids', self._selectable)
            if isinstance(members, str): members = (members,)
            if any(roi_id not in self._selectable for roi_id in (members or ())): members = ()
            members = tuple(roi_id for roi_id in self._selectable if roi_id in (members or ()))
            if members:
                if members not in self._sets: self._sets.append(members)
                assignments[field] = self._sets.index(members)
            else: assignments[field] = None
        if not self._sets: self._sets.append(())
        self._refresh_choices(assignments)

    def owns_control(self, name):
        return name == 'selected_roi_ids' or name in self._fields

    def validation_errors(self):
        if not self._review['accepted']: return {}
        return {field: 'Choose a set with at least one control region.' for field, members in self.values().items()
                if field in self._fields and not members}

    def reveal(self, name):
        if name == 'selected_roi_ids': self.table.setFocus(); return
        if name not in self.pairing_editors: self.buttons['load'].setFocus(); return
        row = tuple(self.pairing_editors).index(name); self.positive_table.setCurrentCell(row, 2)
        self.pairing_editors[name].setFocus()

    def set_option_editor(self, editor):
        self.option_editor = editor; editor.hide()
