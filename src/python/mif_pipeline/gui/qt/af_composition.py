"""One editable region inventory shared by control sets and final AF groups."""
import re
from PySide6.QtCore import Qt, Signal, QEvent, QTimer, QSignalBlocker, QSize
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QMenu,
    QSizePolicy, QTabBar, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
    QWidget, QScrollArea, QFrame,
)

from .reference_workspace import decision_combo, reference_table, text_label
from .researcher_widgets import ContentSizedButton, ElidedComboBox

LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def final_af_region_label(label):
    """Keep generated region numbers readable; full descriptions remain tooltips."""
    match = re.fullmatch(r'(?:Autofluorescence|AF|Control) region (\d+)(?:\s*[—–-]\s*.*)?', label)
    return 'Region ' + match.group(1) if match else label


class AfValidationMessage(QLabel):
    """Reserve every wrapped warning line in a scrollable composition workspace."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setProperty('validationMessage', True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def _fit_height(self):
        self.setMinimumHeight(0)
        self.setMinimumHeight(max(0, self.heightForWidth(self.width())))

    def setText(self, text):
        super().setText(text)
        if self.isVisible():
            self._fit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()


REGION_COLOURS = ('#0072b2', '#d55e00', '#009e73', '#cc79a7', '#e69f00', '#56b4e9', '#333333')


def region_curve_style(index):
    """Inventory order fixes a region's display identity across membership edits."""
    return dict(color=REGION_COLOURS[index % len(REGION_COLOURS)],
                linestyle=('--', '-.', ':')[(index // len(REGION_COLOURS)) % 3])


def style_af_axes(axes, widget):
    """Style only this live plot, using the desktop font and a transparent canvas."""
    from matplotlib.text import Text
    family = widget.font().family()
    for text in axes.figure.findobj(Text):
        text.set_fontfamily([family, 'DejaVu Sans'])
        text.set_fontsize(10)
    axes.figure.set_facecolor('none')
    axes.set_facecolor('none')
    axes.spines[['top', 'right']].set_visible(False)
    axes.spines[['left', 'bottom']].set_color('#a7b8c3')
    axes.tick_params(color='#a7b8c3', labelcolor='#344957')
    axes.xaxis.label.set_color('#344957')
    axes.yaxis.label.set_color('#344957')
    axes.set_axisbelow(True)
    axes.grid(axis='y', color='#d5dfe5', linewidth=.7, linestyle=(0, (3, 4)))


class _AfGroupStrip(QTabBar):
    """Scrollable native tabs retain keyboard access and stable group identities."""

    currentIndexChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setDrawBase(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet('QTabBar::tab { background: transparent; border: none; '
            'border-bottom: 2px solid transparent; padding: 9px 10px; color: #006b70; }'
            'QTabBar::tab:selected { background: #dcece7; border-bottom: 2px solid #006b70; }'
            'QTabBar::tab:hover { background: #e5efec; }'
            'QTabBar::scroller { width: 52px; }'
            'QTabBar QToolButton { padding: 0; border-radius: 0; }')
        self.currentChanged.connect(self.currentIndexChanged)

    def addItem(self, label, identity, detail=''):
        index = self.addTab(label)
        self.setTabData(index, (identity, label, detail))
        self.setTabToolTip(index, label + (' · ' + detail if detail else ''))

    def currentData(self):
        data = self.tabData(self.currentIndex())
        return data[0] if data else None

    def currentText(self):
        data = self.tabData(self.currentIndex())
        return data[1] if data else ''

    def findData(self, identity):
        return next((index for index in range(self.count())
                     if self.tabData(index)[0] == identity), -1)

    def clear(self):
        while self.count():
            self.removeTab(self.count() - 1)

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        return QSize(min(290, max(140, self.width() - 52), hint.width()), max(42, hint.height()))

    def minimumTabSizeHint(self, index):
        # Scroll complete tabs instead of compressing ordinary names into "A…".
        return self.tabSizeHint(index)


class AfCompositionEditor(QWidget):
    """Edit identities only; callers supply authoritative spectral projections."""

    changed = Signal()
    region_selected = Signal(str)
    group_selected = Signal(str)
    new_requested = Signal()
    rename_requested = Signal(str, str)
    name_validation_requested = Signal(str, str)
    merge_requested = Signal(str, str)
    name_error_changed = Signal(str)
    name_draft_changed = Signal()

    def __init__(self, regions, groups, memberships, parent=None, *, band_count,
                 group_label='Set', editable=True, rename=False, all_groups=False,
                 separate_sources=False):
        super().__init__(parent)
        self.setObjectName('af-composition-card')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('QWidget#af-composition-card { background: #ffffff; '
            'border: 1px solid #d5dfdc; border-radius: 0; }')
        self._regions = tuple(regions)
        self._by_id = {row['roi_id']: row for row in regions}
        self._groups, self._memberships = {}, {}
        self._name_drafts, self._name_errors = {}, {}
        self._editable, self._all_groups = editable, all_groups
        self._rename = rename
        self._group_label = group_label
        self._separate_sources = separate_sources
        self._band_count = band_count
        self.table_height_limit = 240
        self.legend_height_limit = 90
        self._updating = False
        self._selecting_region = False
        self._editing_group = None
        self._narrow = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        # Selection, creation, naming and contextual actions share one strip.
        self.inventory = QFrame(self)
        self.inventory.setObjectName('af-composition-inventory')
        self.inventory.setMinimumWidth(0)
        self.inventory.setStyleSheet('QFrame#af-composition-inventory { border: none; '
            'border-bottom: 1px solid #b8ccca; background: transparent; }')
        self.inventory_layout = QGridLayout(self.inventory)
        self.inventory_layout.setContentsMargins(0, 0, 0, 0)
        self.inventory_layout.setHorizontalSpacing(0)
        self.inventory_layout.setVerticalSpacing(2)
        self.group_choices = QWidget(self.inventory)
        self.group_choices.setMinimumWidth(0)
        self.choices_layout = QHBoxLayout(self.group_choices)
        self.choices_layout.setContentsMargins(0, 0, 0, 0)
        self.choices_layout.setSpacing(0)
        self.selector = _AfGroupStrip(self.group_choices)
        self.selector.setAccessibleName(group_label + ' shown in plot')
        self.choices_layout.addWidget(self.selector)
        self.new_button = ContentSizedButton('+ Set' if group_label == 'Set' else '+ Reference', self.group_choices)
        self.new_button.setObjectName('af-composition-new')
        self.new_button.setStyleSheet('QPushButton { background: transparent; border: none; '
            'border-radius: 0; padding: 8px; color: #006b70; } '
            'QPushButton:hover { background: #e5efec; }')
        self.new_button.clicked.connect(self.new_requested)
        self.new_button.setVisible(editable)
        self.choices_layout.addWidget(self.new_button)
        self.choices_layout.addStretch(1)
        self.group_controls = QWidget(self.inventory)
        self.controls_layout = QHBoxLayout(self.group_controls)
        self.controls_layout.setContentsMargins(0, 0, 0, 0)
        self.controls_layout.setSpacing(6)
        self.rename_button = ContentSizedButton('Rename…', self.group_controls)
        self.rename_button.setObjectName('af-composition-rename')
        self.rename_button.clicked.connect(self._start_name)
        self.controls_layout.addWidget(self.rename_button)
        self.name_controls = QWidget(self)
        naming = QHBoxLayout(self.name_controls)
        naming.setContentsMargins(0, 0, 0, 0)
        naming.setSpacing(8)
        self.name_label = text_label('Reference name', self.name_controls)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.name_label.setMaximumWidth(180)
        naming.addWidget(self.name_label)
        self.name_edit = QLineEdit(self.name_controls)
        self.name_edit.setObjectName('af-composition-name')
        self.name_edit.setAccessibleName('Selected AF reference name')
        self.name_edit.setPlaceholderText('Reference name')
        self.name_edit.setToolTip('Edit the selected AF reference name.')
        self.name_edit.returnPressed.connect(self._finish_name)
        self.name_edit.editingFinished.connect(self._validate_name_draft)
        self.name_edit.textEdited.connect(self._remember_name_draft)
        naming.addWidget(self.name_edit, 1)
        self.name_apply_button = ContentSizedButton('Apply name', self.name_controls)
        self.name_apply_button.clicked.connect(self._finish_name)
        naming.addWidget(self.name_apply_button)
        self.name_cancel_button = ContentSizedButton('Cancel', self.name_controls)
        self.name_cancel_button.clicked.connect(self._cancel_name)
        naming.addWidget(self.name_cancel_button)
        self.name_controls.hide()
        self.name_error = AfValidationMessage(self)
        self.name_error.hide()
        self.merge_button = QToolButton(self.group_controls)
        self.merge_button.setObjectName('af-composition-merge')
        self.merge_button.setText('Merge into')
        self.merge_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.merge_button.setToolTip('Move all regions from the selected group into another group.')
        self.merge_menu = QMenu(self.merge_button)
        self.merge_button.setMenu(self.merge_menu)
        self.controls_layout.addWidget(self.merge_button)
        self.export_button = ContentSizedButton('Export…', self.group_controls)
        self.export_button.setObjectName('af-composition-export')
        self.controls_layout.addWidget(self.export_button)
        self.inventory_layout.addWidget(self.group_choices, 0, 0)
        self.inventory_layout.addWidget(self.group_controls, 0, 1)
        self.inventory_layout.setColumnStretch(0, 1)
        layout.addWidget(self.inventory)
        layout.addWidget(self.name_controls)
        layout.addWidget(self.name_error)
        self.selection_hint = text_label('', self, supporting=True)
        layout.addWidget(self.selection_hint)
        # The selected group is already named in its tab.
        self.plot_title = text_label('', self, heading=True)
        self.plot_title.hide()
        self.summary = text_label('', self, supporting=True)
        self.summary.hide()
        self.spectra = QWidget(self)
        self.spectra.setMinimumWidth(0)
        self.spectra.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.plot_layout = QVBoxLayout(self.spectra)
        self.plot_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_layout.setSpacing(4)
        layout.addWidget(self.spectra)
        evidence = QFrame(self)
        evidence.setObjectName('af-composition-evidence')
        evidence_layout = QVBoxLayout(evidence)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        evidence_layout.setSpacing(8)
        evidence_layout.addWidget(text_label('Region membership' if editable else 'Regional evidence',
                                            evidence, heading=True))
        self.table = QTableWidget(len(regions), 8 if separate_sources else 7, self)
        self.table.setObjectName('af-composition-regions')
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        column_label = 'AF set' if group_label == 'Set' else group_label
        self.table.setHorizontalHeaderLabels(('Region', column_label, 'Weight', 'Pixels',
            'Signal (L2)', 'Clipped', 'Angle (°)', 'Control') if separate_sources else
            ('Region', column_label, 'Weight', 'Pixels', 'Signal (L2)', 'Clipped', 'Angle (°)'))
        reference_table(self.table)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setStyleSheet('QTableView[scientificTable="true"] { border: none; '
            'background: transparent; alternate-background-color: transparent; '
            'border-top: 1px solid #c7d4d3; border-bottom: 1px solid #c7d4d3; border-radius: 0; }')
        self.table.setIconSize(QSize(26, 12))
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(LEFT)
        header.setMinimumSectionSize(42)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        for column in range(2, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        if separate_sources:
            header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
            self.table.horizontalHeaderItem(7).setTextAlignment(LEFT)
            header.moveSection(7, 0)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        tips = ('Retained sampled region; select to emphasize its spectrum.',
                'Assign a region to one group, or exclude it while keeping its evidence.',
                'Equal regional-mean weight within this group, not a photon fraction or fitted abundance.',
                'Number of accepted sampled pixels.',
                'L2 length after image-background subtraction and zero clipping.',
                'Bands below zero after image-background subtraction.',
                'Spectral angle to the group result; no acceptance threshold is applied.')
        for column, tip in enumerate(tips):
            self.table.horizontalHeaderItem(column).setTextAlignment(LEFT)
            self.table.horizontalHeaderItem(column).setToolTip(tip)
        self.editors = {}
        for index, region in enumerate(regions):
            label = region['label'].replace('Autofluorescence region', 'AF region')
            if separate_sources:
                label = final_af_region_label(region['label'])
            source = region.get('source_label')
            if source and not separate_sources:
                label += '\n' + source
            usable = region.get('usable', region.get('status') != 'failed')
            if not usable:
                label += '\nNo usable AF spectrum'
            item = self._item(label)
            item.setToolTip('\n'.join(filter(None, (region.get('filename') or
                region.get('control_filename'), region['label'], region.get('diagnostic'),
                'Accepted pixels: ' + str(region.get('accepted_pixel_count', '—'))))))
            self.table.setItem(index, 0, item)
            self._set_region_swatch(item, index)
            self.table.setRowHeight(index, 56 if '\n' in label else 40)
            if separate_sources:
                source_item = self._item(source or '')
                source_item.setToolTip(region.get('source_description') or source or '')
                self.table.setItem(index, 7, source_item)
            editor = decision_combo(self.table, (), combo_type=ElidedComboBox)
            editor.setAccessibleName(group_label + ' for ' + region['label'])
            editor.setMinimumWidth(0)
            editor.setMinimumContentsLength(3)
            editor.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            editor.currentIndexChanged.connect(lambda _index, rid=region['roi_id']: self._change_membership(rid))
            self.table.setCellWidget(index, 1, editor)
            self.editors[region['roi_id']] = editor
        self.table.setFixedHeight(min(240, 43 + sum(self.table.rowHeight(i) for i in range(len(regions)))))
        evidence_layout.addWidget(self.table)
        layout.addWidget(evidence)
        self.table.currentCellChanged.connect(self._select_region)
        self.selector.currentIndexChanged.connect(self._select_group)
        self.set_memberships(groups, memberships)
        self.set_evidence(regions)
        self._arrange(False)

    def set_legend(self, legend):
        """Keep all clickable curves available without displacing the evidence."""
        self.legend_area = QScrollArea(self)
        self.legend_area.setFrameShape(QFrame.Shape.NoFrame)
        self.legend_area.setWidgetResizable(True)
        self.legend_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.legend_area.setWidget(legend)
        self.legend_area.setStyleSheet('QScrollArea { border: none; background: transparent; }')
        legend.setAutoFillBackground(False)
        legend.installEventFilter(self)
        self._legend = legend
        self.plot_layout.addWidget(self.legend_area)
        self.fit_legend()

    def fit_legend(self):
        if hasattr(self, '_legend') and isValid(self._legend) and isValid(self.legend_area):
            columns = max(1, self._legend._columns)
            buttons = [entry[0] for entry in self._legend._entries]
            spacing = self._legend._grid.verticalSpacing()
            height = 0
            for start in range(0, len(buttons), columns):
                row_height = max(button.minimumHeight() for button in buttons[start:start + columns])
                candidate = height + (spacing if height else 0) + row_height
                if height and candidate > self.legend_height_limit:
                    break
                height = candidate
            self.legend_area.setFixedHeight(max(26, height))

    def eventFilter(self, watched, event):
        if watched is getattr(self, '_legend', None) and event.type() in {QEvent.Type.Resize, QEvent.Type.LayoutRequest}:
            QTimer.singleShot(0, self.fit_legend)
        return super().eventFilter(watched, event)

    @staticmethod
    def _item(value):
        item = QTableWidgetItem(str(value))
        item.setTextAlignment(LEFT)
        item.setToolTip(str(value))
        return item

    def memberships(self):
        return dict(self._memberships)

    def grouped_roi_ids(self):
        return [members for group in self._groups
                if (members := [row['roi_id'] for row in self._regions
                                if self._memberships[row['roi_id']] == group])]

    def set_memberships(self, groups, memberships):
        if set(memberships) != set(self._by_id) or any(group and group not in groups for group in memberships.values()):
            raise ValueError('Every sampled region needs an available group or an explicit exclusion.')
        self._groups, self._memberships = dict(groups), dict(memberships)
        self._name_drafts = {key: value for key, value in self._name_drafts.items() if key in groups}
        self._name_errors = {key: value for key, value in self._name_errors.items() if key in groups}
        if self._editing_group not in groups:
            self._editing_group = None
        self._updating = True
        selected = self.selector.currentData()
        self.selector.clear()
        if self._all_groups and (self._group_label != 'Set' or len(groups) > 1):
            self.selector.addItem('All sets' if self._group_label == 'Set' else 'All spectra', '')
        for identity, label in groups.items():
            count = sum(group == identity for group in memberships.values())
            self.selector.addItem(label, identity, f'{count} region' + ('' if count == 1 else 's'))
        if any(not group for group in memberships.values()):
            count = sum(not group for group in memberships.values())
            self.selector.addItem('Excluded', '__excluded__', f'{count} region' + ('' if count == 1 else 's'))
        self.selector.setCurrentIndex(max(0, self.selector.findData(selected)))
        for identity, editor in self.editors.items():
            editor.clear()
            editor.addItem('Excluded', '')
            for group, label in groups.items():
                editor.addItem(label, group)
            editor.setCurrentIndex(editor.findData(memberships[identity]))
            region = self._by_id[identity]
            usable = region.get('usable', region.get('status') != 'failed')
            editor.setEnabled(self._editable and (bool(memberships[identity]) or usable))
            for index in range(editor.count()):
                editor.setItemData(index, LEFT, Qt.ItemDataRole.TextAlignmentRole)
        self._updating = False
        self._update_summary()

    def set_evidence(self, regions, *, weights=None, angles=None):
        facts = {row['roi_id']: row for row in regions}
        for index, original in enumerate(self._regions):
            region = facts.get(original['roi_id'], original)
            weight = (weights or {}).get(region['roi_id'])
            angle = (region.get('angle_to_combined_degrees') if angles is None
                     else angles.get(region['roi_id']))
            clipped = region.get('clipped_count')
            signal = region.get('corrected_l2')
            pixels = region.get('accepted_pixel_count')
            values = ('—' if weight is None else f'{weight:.3g}',
                      '—' if pixels is None else f'{pixels:,}',
                      '—' if signal is None else f'{signal:.3g}',
                      '—' if clipped is None else f'{clipped}/{self._band_count}',
                      '—' if angle is None else f'{angle:.2f}')
            for column, value in enumerate(values, 2):
                item = self._item(value)
                if column == 6 and angle is not None:
                    item.setToolTip(str(angle))
                self.table.setItem(index, column, item)

    def merge_groups(self, source, target):
        if source == target or source not in self._groups or target not in self._groups:
            raise ValueError('Choose two different groups to merge.')
        memberships = {rid: target if group == source else group for rid, group in self._memberships.items()}
        groups = {key: label for key, label in self._groups.items() if key != source}
        self.set_memberships(groups, memberships)
        self.selector.setCurrentIndex(self.selector.findData(target))
        self.changed.emit()

    def _change_membership(self, roi_id):
        if self._updating:
            return
        self._memberships[roi_id] = self.editors[roi_id].currentData()
        self.set_memberships(self._groups, self._memberships)
        self.table.setCurrentCell(tuple(self._by_id).index(roi_id), 0)
        self._select_region(self.table.currentRow())
        self._update_summary()
        self.changed.emit()

    def _select_region(self, row, *_):
        if self._updating or row < 0:
            return
        identity = self._regions[row]['roi_id']
        group = self._memberships[identity] or '__excluded__'
        self._selecting_region = True
        self.selector.setCurrentIndex(self.selector.findData(group))
        self._selecting_region = False
        self.region_selected.emit(identity)

    def _select_group(self, *_):
        if self._updating:
            return
        self._validate_name_draft()
        if not self._selecting_region:
            with QSignalBlocker(self.table):
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
        self._update_summary()
        self.group_selected.emit(self.selector.currentData() or '')

    def _update_summary(self):
        group = self.selector.currentData()
        rows = [row for row in self._regions if
                (self._memberships[row['roi_id']] == group if group and group != '__excluded__'
                 else not self._memberships[row['roi_id']] if group == '__excluded__'
                 else True)]
        count = len(rows)
        self.summary.setText(f'{count} region' + ('' if count == 1 else 's'))
        if self._separate_sources:
            sources = len({row['input_id'] for row in rows})
            self.summary.setText(self.summary.text() + f' · {sources} control' + ('' if sources == 1 else 's'))
        if not group:
            excluded = sum(not self._memberships[row['roi_id']] for row in rows)
            if excluded:
                self.summary.setText(self.summary.text() + f' · {excluded} excluded')
        self.plot_title.setText(self.selector.currentText())
        self._set_group_controls(group, count)

    def _set_group_controls(self, group, count):
        selected = group in self._groups
        naming = self._editable and self._rename and selected
        # A name edit stays bound to its original reference while the researcher
        # compares other spectra. Applying and cancelling remain explicit.
        self._named_group = self._editing_group or group
        named = self._named_group
        self.name_edit.setText(self._name_drafts.get(named, self._groups.get(named, '')))
        self.name_edit.setCursorPosition(0)
        self.rename_button.setVisible(self._editable and self._rename)
        self.rename_button.setEnabled(naming and self._editing_group is None)
        self._set_name_caption('Name for ' + self._groups[named] if self._editing_group else 'Reference name')
        self.name_controls.setVisible(self._editing_group is not None)
        self.selector.setMaximumWidth(self.selector.sizeHint().width())
        self._present_name_error()
        self.merge_menu.clear()
        for identity, label in self._groups.items():
            if identity != group:
                action = self.merge_menu.addAction(label)
                action.setData(identity)
                action.triggered.connect(lambda _checked=False, source=group, target=identity:
                                         self.merge_requested.emit(source, target))
        self.merge_button.setVisible(self._editable and len(self._groups) > 1)
        self.merge_button.setEnabled(selected and len(self._groups) > 1)
        hint = ''
        if self._editable and selected and not count:
            hint = ('Assign regions below to this set.' if self._group_label == 'Set'
                    else 'Assign regions below to this reference.')
        self.selection_hint.setText(hint)
        self.selection_hint.setVisible(bool(hint))
        self.refresh_group_controls()

    def refresh_group_controls(self):
        self.group_controls.setVisible(any(not self.controls_layout.itemAt(index).widget().isHidden()
                                          for index in range(self.controls_layout.count())))
        self._arrange(self.width() < 600)

    def _remember_name_draft(self, text):
        group = getattr(self, '_named_group', None)
        if group in self._groups:
            if text == self._groups[group]:
                self._name_drafts.pop(group, None)
            else:
                self._name_drafts[group] = text
            self.name_draft_changed.emit()

    def has_pending_name(self):
        return any(text != self._groups.get(group) for group, text in self._name_drafts.items())

    def _start_name(self):
        group = self.selector.currentData()
        if group not in self._groups:
            return
        self._editing_group = group
        self._named_group = group
        self._set_name_caption('Name for ' + self._groups[group])
        self.name_edit.setText(self._name_drafts.get(group, self._groups[group]))
        self.rename_button.setEnabled(False)
        self.name_controls.show()
        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_edit.selectAll()

    def _set_name_caption(self, text):
        self.name_label.setText(text)
        self.name_label.setMinimumWidth(min(180, self.name_label.fontMetrics().horizontalAdvance(text) + 2))

    def _cancel_name(self):
        group = self._named_group
        self._name_drafts.pop(group, None)
        self._name_errors.pop(group, None)
        self._editing_group = None
        self.name_edit.setText(self._groups.get(group, ''))
        self.name_controls.hide()
        self.rename_button.setEnabled(self.selector.currentData() in self._groups)
        self._present_name_error()
        self.name_draft_changed.emit()

    def _validate_name_draft(self):
        if self._updating or not self._rename or self._editing_group not in self._groups:
            return
        self.name_validation_requested.emit(self._editing_group, self.name_edit.text())

    def set_name_validation(self, message):
        group = self._named_group
        if message:
            self._name_errors[group] = message
        else:
            self._name_errors.pop(group, None)
        self._present_name_error()

    def _finish_name(self):
        if self._updating or not self._editable or not self._rename:
            return
        old = self._named_group
        if old in self._groups and (self.name_edit.text().strip() != self._groups[old] or old in self._name_errors):
            self.rename_requested.emit(old, self.name_edit.text())
        elif old in self._groups:
            self._cancel_name()

    def set_name_error(self, message):
        group = self._named_group
        if message and group in self._groups:
            self._name_drafts[group] = self.name_edit.text()
            self._name_errors[group] = message
        elif not message:
            self._name_drafts.pop(group, None)
            self._name_errors.pop(group, None)
            self.name_edit.setText(self._groups.get(group, ''))
            self._editing_group = None
            self.name_controls.hide()
            self.rename_button.setEnabled(self.selector.currentData() in self._groups)
        self._present_name_error()

    def _present_name_error(self):
        group = getattr(self, '_named_group', None)
        message = self._name_errors.get(group, '')
        if not message and self._name_errors:
            other, error = next(iter(self._name_errors.items()))
            message = self._groups[other] + ': ' + error
        changed = message != self.name_error.text()
        self.name_error.setText(message)
        self.name_error.setVisible(bool(message))
        if changed:
            self.name_error_changed.emit(message)

    @staticmethod
    def _set_region_swatch(item, index):
        style = region_curve_style(index)
        pixmap = QPixmap(26, 12)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        pen = QPen(QColor(style['color']), 1.6)
        pen.setStyle({'--': Qt.PenStyle.DashLine, '-.': Qt.PenStyle.DashDotLine,
                      ':': Qt.PenStyle.DotLine}[style['linestyle']])
        painter.setPen(pen)
        painter.drawLine(1, 6, 25, 6)
        painter.end()
        item.setIcon(QIcon(pixmap))

    def _arrange(self, narrow):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self.inventory_layout.removeWidget(self.group_controls)
        self.inventory_layout.addWidget(self.group_controls, 1 if narrow else 0,
                                        0 if narrow else 1, Qt.AlignmentFlag.AlignRight)
        self.inventory_layout.setColumnStretch(0, 1)
        self.inventory_layout.setColumnStretch(1, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(self.width() < 600)
        # Long labels elide with full tooltips; all source evidence remains visible.
        compact = self.width() < 920
        widths = ((126, 76, 82, 106, 82, 86) if compact else
                  (154, 85, 100, 126, 126, 140))
        if self._separate_sources:
            self.table.setColumnWidth(0, 124 if compact else 165)
            widths = (132, 76, 82, 106, 82, 86) if compact else (164, 85, 100, 126, 126, 140)
        for column, width in enumerate(widths, 1):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeaderItem(5).setText('Clipped' if compact else 'Clipped bands')
        self.table.horizontalHeaderItem(6).setText('Angle (°)' if compact else 'Angle to set (°)')
        height = self.table.horizontalHeader().sizeHint().height() + sum(
            self.table.rowHeight(row) for row in range(self.table.rowCount())) + 2 * self.table.frameWidth() + 4
        self.table.setFixedHeight(min(self.table_height_limit, height))
        self.fit_legend()
