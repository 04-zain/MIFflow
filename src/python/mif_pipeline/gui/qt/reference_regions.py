"""Select the current region role with the remaining saved roles in context."""
import re

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QRect, QSize, QEvent
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QHeaderView, QSizePolicy, QStyledItemDelegate,
    QStyle, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from .reference_workspace import decision_combo, reference_table, text_label
from .researcher_widgets import ContentSizedButton, configure_disclosure


class _CenteredUseDelegate(QStyledItemDelegate):
    """Center the native check indicator and its existing click target together."""

    def _centered_option(self, option, index):
        centered = QStyleOptionViewItem(option)
        self.initStyleOption(centered, index)
        style = centered.widget.style() if centered.widget else QApplication.style()
        check = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                     centered, centered.widget)
        offset = centered.rect.center().x() - check.center().x()
        centered.rect.adjust(offset, 0, 0, 0)
        return centered

    def paint(self, painter, option, index):
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style = background.widget.style() if background.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, background.widget)
        if index.data(Qt.ItemDataRole.CheckStateRole) is not None:
            super().paint(painter, self._centered_option(option, index), index)

    def editorEvent(self, event, model, option, index):
        return super().editorEvent(event, model, self._centered_option(option, index), index)


class _ImageNameDelegate(QStyledItemDelegate):
    """Wrap complete filenames even when underscores leave no word boundaries."""

    @staticmethod
    def _wrapped_name(name, metrics, width):
        lines, line = [], ''
        for part in re.split(r'(?<=[_.-])', name):
            if line and metrics.horizontalAdvance(line + part) > width:
                lines.append(line)
                line = ''
            for letter in part:
                if line and metrics.horizontalAdvance(line + letter) > width:
                    lines.append(line)
                    line = ''
                line += letter
        return '\n'.join(lines + [line])

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        name, option.text = option.text, ''
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        painter.save()
        role = (option.palette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected
                else option.palette.ColorRole.Text)
        painter.setPen(option.palette.color(role))
        name = self._wrapped_name(name, option.fontMetrics, max(60, option.rect.width() - 24))
        painter.drawText(option.rect.adjusted(12, 8, -12, -8), Qt.AlignmentFlag.AlignVCenter, name)
        painter.restore()

    def sizeHint(self, option, index):
        width = max(60, self.parent().columnWidth(index.column()) - 24)
        name = self._wrapped_name(str(index.data()), option.fontMetrics, width)
        bounds = option.fontMetrics.boundingRect(QRect(0, 0, width, 1000),
                                                 Qt.AlignmentFlag.AlignLeft, name)
        return QSize(width + 24, max(52, bounds.height() + 16))


class ReferenceRegionsWorkspace(QWidget):
    """Keep membership and pairings bound to the current immutable region export."""

    native_action_requested = Signal(str)

    def __init__(self, review, values, parent=None):
        super().__init__(parent)
        self.setObjectName('reference-regions-workspace')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._review = review
        self._rows = tuple(review['rows'])
        self._selectable = tuple(review['selectable_ids'])
        self._fields = dict(review['pairing_fields'])
        self._owns_selection = review['has_selection']
        self._selection_from_pairings = bool(self._fields) and review['checkpoint'] in {
            'af_reference_rois', 'negative_reference_rois'}
        self._selected = set(values.get('selected_roi_ids') or ())
        self._by_id = {row['roi_id']: row for row in self._rows}
        short_labels = [self._region_label(self._by_id[roi_id]) for roi_id in self._selectable]
        self._control_labels = {
            roi_id: (f"{self._by_id[roi_id]['image_name']} · {label}" if short_labels.count(label) > 1 else label)
            for roi_id, label in zip(self._selectable, short_labels, strict=True)
        }
        self._pairing_rows = {roi_id: name for name, roi_id in self._fields.items()}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.validation_editors = {}
        self.field_errors = {}
        self._pairing_indices = {}

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.buttons = {}
        for action, label in (('edit', 'Edit regions in QuPath'),
                              ('load', 'Load saved regions'),
                              ('accept', 'Accept displayed regions')):
            button = ContentSizedButton(label, self)
            button.setObjectName('reference-regions-' + action)
            button.clicked.connect(lambda _checked=False, name=action: self.native_action_requested.emit(name))
            button.setVisible(action != 'edit' or review['checkpoint'] == 'positive_reference_rois')
            if action == 'accept':
                button.setVisible(bool(self._rows) and not review['accepted'])
                button.setEnabled(bool(self._rows) and not review['accepted'])
                button.setProperty('actionRole', 'primary')
                button.style().unpolish(button)
                button.style().polish(button)
            self.buttons[action] = button
            actions.addWidget(button)
        actions.addStretch(1)
        self._layout.addLayout(actions)

        decision_heading = QHBoxLayout()
        title = ('Controls for each stained region' if self._selection_from_pairings else
                 {'positive_reference_rois': 'Stained tissue',
                  'background_reference_rois': 'Image background',
                  'af_reference_rois': 'AF control regions',
                  'negative_reference_rois': 'Negative-control regions'}[review['checkpoint']])
        decision_heading.addWidget(text_label(title, self, heading=True))
        self.status = text_label('', self, supporting=True)
        self.status.setObjectName('reference-region-selection-status')
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        decision_heading.addWidget(self.status)
        self._layout.addLayout(decision_heading)
        self.table = QTableWidget(len(self._rows), 5 if self._fields else 4, self)
        self.table.setObjectName('reference-region-selection-table')
        self.table.setAccessibleName('Saved reference regions and selection')
        columns = ['Use', 'Image', 'Region', 'Area (µm²)']
        if self._fields:
            columns.append('Matched control')
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setColumnHidden(0, self._selection_from_pairings)
        reference_table(self.table)
        self.table.setItemDelegateForColumn(0, _CenteredUseDelegate(self.table))
        self.table.setItemDelegateForColumn(1, _ImageNameDelegate(self.table))
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.horizontalHeader().setMinimumSectionSize(42)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.horizontalHeaderItem(3).setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._duplicate_control_labels = len(set(short_labels)) < len(short_labels)
        self._fitting_table = False
        self.table.viewport().installEventFilter(self)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.pairing_editors = {}
        for index, row in enumerate(self._rows):
            use = QTableWidgetItem()
            use.setData(Qt.ItemDataRole.UserRole, row['roi_id'])
            use.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if self._owns_selection and not self._selection_from_pairings and row['roi_id'] in self._selectable:
                if review['accepted']:
                    use.setFlags(use.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                use.setCheckState(Qt.CheckState.Checked if row['roi_id'] in self._selected else Qt.CheckState.Unchecked)
                use.setToolTip('Include this region in sampling.' if review['accepted']
                               else 'Accept the saved regions before choosing which to sample.')
            self.table.setItem(index, 0, use)
            region = self._region_label(row)
            if row['state'] != 'accepted':
                region += '\n' + row['state'].title()
            for column, text in ((1, row['image_name']), (2, region), (3, f"{row['area_um2']:,.1f}")):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{row['image_name']} · {row['label']} {row['number']}\n"
                                f"Area: {row['area_um2']:.1f} µm² · {row['state']}")
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(index, column, item)
            field = self._pairing_rows.get(row['roi_id'])
            if field is not None:
                cell = QWidget(self.table)
                cell_layout = QVBoxLayout(cell)
                cell_layout.setContentsMargins(0, 0, 0, 0)
                cell_layout.setSpacing(0)
                choice = decision_combo(cell, ())
                choice.setObjectName('reference-region-pairing-' + field)
                choice.setAccessibleName(f"Matched control for {row['image_name']}, stained region {row['number']}")
                choice.setEnabled(review['accepted'])
                choice.currentIndexChanged.connect(lambda _index, editor=choice: self._pairing_changed(editor))
                self.pairing_editors[field] = choice
                self.validation_editors[field] = choice
                self._pairing_indices[field] = index
                error = self._error_label(field, cell)
                error.setWordWrap(False)
                error.setContentsMargins(8, 0, 4, 2)
                cell_layout.addWidget(choice, 1)
                cell_layout.addWidget(error)
                self.table.setCellWidget(index, 4, cell)
        self.table.itemChanged.connect(self._selection_changed)
        self.table.setVisible(bool(self._rows))
        self._layout.addWidget(self.table)
        active_ids = set(self._fields.values()) if self._selection_from_pairings else set(self._selectable)
        for index, row in enumerate(self._rows):
            self.table.setRowHidden(index, row['roi_id'] not in active_ids)
        self.context_tables = []
        self._context_roles = tuple(role for role in ('positive', 'af', 'negative', 'background')
                                    if any(row['role'] == role and row['roi_id'] not in active_ids
                                           for row in self._rows))
        self._context_rows = {role: tuple(row for row in self._rows
                                         if row['role'] == role and row['roi_id'] not in active_ids)
                              for role in self._context_roles}
        if self._owns_selection and not self._selection_from_pairings:
            self.validation_editors['selected_roi_ids'] = self.table
            error = self._error_label('selected_roi_ids', self)
            self._layout.addWidget(error)
        if not self._rows:
            self._layout.addWidget(text_label(
                'Load saved regions to review their images, classes and areas here.', self, supporting=True))

        for role in self._context_roles:
            self._add_context_group(role, self._context_rows[role])

        self.option_editor = None
        if review['checkpoint'] == 'positive_reference_rois':
            toggle = QToolButton(self)
            toggle.setText('QuPath annotation classes')
            toggle.setCheckable(True)
            configure_disclosure(toggle)
            self._layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
            guide = text_label(review['drawing_guide'], self, supporting=True)
            guide.hide()
            toggle.toggled.connect(guide.setVisible)
            self._layout.addWidget(guide)
        self.set_values(values)

    def _add_context_group(self, role, rows):
        """Keep available controls and image backgrounds distinct from this decision."""
        names = {'positive': 'Saved stained regions', 'af': 'Available AF control regions',
                 'negative': 'Available negative-control regions', 'background': 'Saved background regions'}
        toggle = QToolButton(self)
        toggle.setText(names[role])
        toggle.setObjectName('reference-region-context-' + role)
        toggle.setCheckable(True)
        configure_disclosure(toggle)
        self._layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        table = QTableWidget(len(rows), 3, self)
        table.setObjectName('reference-region-context-table-' + role)
        table.setAccessibleName(names[role])
        table.setHorizontalHeaderLabels(('Image', 'Region', 'Area (µm²)'))
        reference_table(table)
        table.setItemDelegateForColumn(0, _ImageNameDelegate(table))
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 160), (2, 118)):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(column, width)
        table.horizontalHeaderItem(2).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for index, row in enumerate(rows):
            for column, value in enumerate((row['image_name'], self._region_label(row), f"{row['area_um2']:,.1f}")):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row['roi_id'])
                item.setToolTip(f"{row['image_name']} · {self._region_label(row)} · {row['state']}")
                if column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(index, column, item)
        table.setFixedHeight(min(310, 44 + 52 * len(rows)))
        self.context_tables.append(table)
        self._layout.addWidget(table)
        toggle.toggled.connect(table.setVisible)
        toggle.toggled.connect(lambda _: self._fit_table())
        expanded = not self._review['accepted'] or (self._selection_from_pairings and role in {'af', 'negative'})
        toggle.setChecked(expanded)
        table.setVisible(expanded)

    @staticmethod
    def _region_label(row):
        role = {'positive': 'Stained', 'af': 'AF control', 'negative': 'Negative control',
                'background': 'Background'}[row['role']]
        return f"{role} {row['number']}"

    def owns_control(self, name):
        return (name == 'selected_roi_ids' and self._owns_selection) or name in self._fields

    def _error_label(self, name, parent):
        error = text_label('', parent)
        error.setObjectName('error-' + name)
        error.setProperty('validationMessage', True)
        error.hide()
        self.field_errors[name] = error
        return error

    def validation_errors(self):
        """Report incomplete membership and every unmatched stained row together."""
        if not self._review['accepted']:
            return {}
        errors = {}
        if self._owns_selection and not self._selection_from_pairings and not self._selected:
            errors['selected_roi_ids'] = 'Select at least one region to sample.'
        errors.update({name: 'Choose a control.' for name, editor in self.pairing_editors.items()
                       if editor.currentData() not in self._selected})
        return errors

    def reveal(self, name):
        if name == 'selected_roi_ids':
            index = next((index for index, row in enumerate(self._rows)
                          if row['roi_id'] in self._selectable), 0)
            column, editor = 0, self.table
        else:
            index, column = self._pairing_indices[name], 4
            editor = self.pairing_editors[name]
        self.table.setCurrentCell(index, column)
        self.table.scrollToItem(self.table.item(index, 1))
        editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_option_editor(self, editor):
        """Retain the form binding; native actions and the footer own navigation."""
        self.option_editor = editor
        editor.hide()

    def _selection_changed(self, item):
        if item.column() != 0:
            return
        self._selected = {self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                          for row in range(self.table.rowCount())
                          if self.table.item(row, 0).checkState() == Qt.CheckState.Checked}
        self._refresh_pairings()
        self._update_status()

    def _refresh_pairings(self, values=None):
        for field, editor in self.pairing_editors.items():
            selected = (values[field] if values is not None and field in values else editor.currentData())
            blocker = QSignalBlocker(editor)
            editor.clear()
            editor.addItem('Choose…', None)
            for roi_id in self._selectable:
                if self._selection_from_pairings or roi_id in self._selected:
                    editor.addItem(self._control_labels[roi_id], roi_id)
            editor.setCurrentIndex(max(0, editor.findData(selected)))
            editor.view().setMinimumWidth(min(640, max(160, max(
                (editor.fontMetrics().horizontalAdvance(editor.itemText(index)) + 36
                 for index in range(editor.count())), default=160))))
            del blocker
            self._pairing_tooltip(editor)
        if self._selection_from_pairings:
            self._selected = {editor.currentData() for editor in self.pairing_editors.values()
                              if editor.currentData() in self._selectable}

    def _pairing_changed(self, editor):
        self._pairing_tooltip(editor)
        if self._selection_from_pairings:
            self._selected = {choice.currentData() for choice in self.pairing_editors.values()
                              if choice.currentData() in self._selectable}
            self._update_status()

    def _pairing_tooltip(self, editor):
        row = self._by_id.get(editor.currentData())
        editor.setToolTip('Choose a saved control region.' if row is None else
                         f"{row['image_name']} · {self._region_label(row)} · {row['area_um2']:.1f} µm²")

    def _update_status(self):
        self.status.setVisible(not (self._selection_from_pairings and self._review['accepted']))
        if not self._rows:
            self.status.setText('No saved regions loaded')
        elif not self._review['accepted']:
            self.status.setText('Awaiting acceptance')
        elif self._owns_selection:
            self.status.setText(f'{len(self._selected)} of {len(self._selectable)} selected')
        else:
            self.status.setText('Saved regions accepted')

    def values(self):
        values = ({'selected_roi_ids': tuple(roi_id for roi_id in self._selectable if roi_id in self._selected)}
                  if self._owns_selection else {})
        values.update({field: editor.currentData() for field, editor in self.pairing_editors.items()})
        return values

    def set_values(self, values):
        if 'selected_roi_ids' in values:
            self._selected = set(values['selected_roi_ids'] or ()) & set(self._selectable)
            blocker = QSignalBlocker(self.table)
            for index, row in enumerate(self._rows):
                if self._owns_selection and not self._selection_from_pairings and row['roi_id'] in self._selectable:
                    self.table.item(index, 0).setCheckState(
                        Qt.CheckState.Checked if row['roi_id'] in self._selected else Qt.CheckState.Unchecked)
            del blocker
        self._refresh_pairings(values)
        self._update_status()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_table()

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_table()
        return super().eventFilter(watched, event)

    def _fit_table(self):
        """Share spare width between names and decisions instead of one column."""
        if self._fitting_table:
            return
        self._fitting_table = True
        width = self.table.viewport().width()
        metrics = self.table.fontMetrics()
        area = max(108, metrics.horizontalAdvance('Area (µm²)') + 24,
                   max((metrics.horizontalAdvance(f"{row['area_um2']:,.1f}") + 24
                        for row in self._rows), default=0))
        region = max(128, max((metrics.horizontalAdvance(self._region_label(row)) + 24
                              for row in self._rows), default=0))
        control = 218 if self._duplicate_control_labels else 158
        use_width = 0 if self._selection_from_pairings else 48
        available = max(0, width - use_width - area)
        image = min(240, max(80, available - region - (control if self._fields else 0)))
        surplus = max(0, available - image - region - (control if self._fields else 0))
        region += round(surplus * (.2 if self._fields else .25))
        if self._fields:
            control += round(surplus * .25)
        image = max(0, available - region - (control if self._fields else 0))
        for column, size in enumerate((48, image, region, area, control)[:self.table.columnCount()]):
            self.table.setColumnWidth(column, size)
        self.table.resizeRowsToContents()
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(52, self.table.rowHeight(row)))
        height = self.table.horizontalHeader().height() + sum(
            self.table.rowHeight(row) for row in range(self.table.rowCount())
            if not self.table.isRowHidden(row)) + 8
        self.table.setFixedHeight(min(480, height))
        for table in getattr(self, 'context_tables', ()):
            table.resizeRowsToContents()
            for row in range(table.rowCount()):
                table.setRowHeight(row, max(52, table.rowHeight(row)))
            table.setFixedHeight(min(310, table.horizontalHeader().height() +
                sum(table.rowHeight(row) for row in range(table.rowCount())) + 8))
        self._fitting_table = False
