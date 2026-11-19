"""Annotate one reference source and choose its tissue/background regions."""
from collections import Counter
from PySide6.QtCore import Qt, Signal, QSignalBlocker, QEvent
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QMenu, QSizePolicy,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)
from .reference_workspace import decision_combo, reference_table, text_label
from .researcher_widgets import ContentSizedButton


CLASSES = {'positive': 'Stained tissue', 'af': 'AF tissue',
           'negative': 'Negative tissue', 'background': 'Image background'}
# QuPath's drawing guide uses the titles above while assigning these classes.
NATIVE_CLASSES = {'positive': 'Positive reference', 'af': 'Autofluorescence control',
                  'negative': 'Negative reference', 'background': 'Image background'}
PURPOSES = {'positive': 'Clear fluorophore signal',
            'af': 'Unstained tissue',
            'negative': 'Tissue comparable to the stained reference',
            'background': 'Outside tissue in this image'}
LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class _RegionAreaDelegate(QStyledItemDelegate):
    """Show area units without changing the numeric cells copied by researchers."""
    def displayText(self, value, locale):
        text = super().displayText(value, locale)
        return text + ' µm²' if text else text


class UnifiedReferenceRegionsWorkspace(QWidget):
    """Source navigation never edits the selection or accepts a native export."""
    native_action_requested = Signal(str)
    page_changed = Signal(str)
    FIELD = 'reference_region_selection'

    def __init__(self, review, values, parent=None):
        super().__init__(parent)
        self.setObjectName('reference-regions-workspace')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._review = review
        self._accepted = bool(review['accepted'])
        self._preparation = review.get('preparation_role')
        self._negative = (review.get('control_roi_role') == 'negative' if review.get('control_roi_role')
            else self._preparation is None and review.get('control_strategy') == 'per_stain_negative_controls')
        self._rows = tuple(review['rows'])
        self._by_id = {row['roi_id']: row for row in self._rows}
        self._positive = tuple(row for row in self._rows if row['role'] == 'positive')
        self._controls = tuple(row for row in self._rows if row['role'] == ('negative' if self._negative else 'af'))
        self._backgrounds = tuple(row for row in self._rows if row['role'] == 'background')
        self._sets, self._assignments = [], {}
        self._positive_selected, self._background_selected = (), ()
        self._initialized = self._fitting = self._ready = False
        self._narrow = None
        self.option_editor = None
        self.pairing_editors = {}
        self.page = self._preparation or review.get('initial_page', 'control')
        self._source_by_page, self._sources = {}, {}
        for page, rows in (('control', self._controls), ('stained', self._positive)):
            roles = ({'positive', 'single_component_reference'} if page == 'stained' else
                     {'negative', 'single_stain_negative_control', 'negative_control'} if self._negative else
                     {'af', 'unstained_control', 'negative_control', 'single_stain_negative_control'}
                     if self._preparation == 'control' else {'af', 'unstained_control'})
            for source in review.get('project_images', ()):
                if source['role'] in roles:
                    self._sources.setdefault(source['input_id'], {'page': page, 'filename': source['filename']})
            for row in rows:
                self._sources.setdefault(self._source(row), {'page': page, 'filename': row['image_name']})
        if self._preparation:
            self._sources = {key: source for key, source in self._sources.items() if source['page'] == self._preparation}

        if self._preparation:
            self._init_preparation(values)
            return

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.surface = QFrame(self)
        self.surface.setObjectName('reference-annotation-surface')
        self.surface.setStyleSheet('QFrame#reference-annotation-surface {'
            'background: #ffffff; border: 1px solid #d5e0df; border-radius: 0; }'
            'QFrame#reference-annotation-surface QLabel { background: transparent; }')
        self._body = QVBoxLayout(self.surface)
        self._body.setContentsMargins(20, 20, 20, 20)
        self._body.setSpacing(16)
        self._layout.addWidget(self.surface)
        self.source_header = QWidget(self.surface)
        self._header = QGridLayout(self.source_header)
        self._header.setContentsMargins(0, 0, 0, 0)
        self._header.setHorizontalSpacing(16)
        self._header.setVerticalSpacing(5)
        self._header.setColumnStretch(0, 1)
        self.image_selector = QComboBox(self.source_header)
        self.image_selector.setObjectName('reference-region-source')
        self.image_selector.setAccessibleName('Reference image')
        self.image_selector.setMinimumContentsLength(12)
        self.image_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.image_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        counts, seen = Counter(source['filename'] for source in self._sources.values()), Counter()
        for key, source in self._sources.items():
            seen[source['filename']] += 1
            source['display_name'] = source['filename'] + (f" · image {seen[source['filename']]}" if counts[source['filename']] > 1 else '')
            self.image_selector.addItem(source['display_name'], key)
        self._header.addWidget(self.image_selector, 1, 0)
        self.source_name = text_label('', self.source_header, heading=True)
        self.source_name.setMinimumWidth(0)
        self._header.addWidget(self.source_name, 1, 0)
        self.image_selector.setVisible(len(self._sources) > 1)
        self.source_name.setVisible(len(self._sources) <= 1)
        self.source_role = text_label('', self.source_header, supporting=True)
        self._header.addWidget(self.source_role, 0, 0)
        self._native_actions = QWidget(self.source_header)
        actions = QHBoxLayout(self._native_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(7)
        edit = ContentSizedButton('Annotate regions…', self._native_actions)
        edit.setObjectName('reference-regions-edit')
        edit.setProperty('actionRole', 'primary' if not self._rows else 'secondary')
        edit.setToolTip('Open the reference annotation guide in QuPath. Save and return to load your regions.')
        edit.clicked.connect(lambda: self.native_action_requested.emit('edit'))
        actions.addWidget(edit)
        self.recovery = QToolButton(self._native_actions)
        self.recovery.setText('⋯')
        self.recovery.setAccessibleName('Reference annotation recovery')
        self.recovery.setToolTip('Recover saved reference regions')
        self.recovery.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.recovery)
        menu.addAction('Refresh saved regions', lambda: self.native_action_requested.emit('load'))
        self.recovery.setMenu(menu)
        actions.addWidget(self.recovery)
        self._body.addWidget(self.source_header)

        self.regions = QWidget(self.surface)
        self.regions.setObjectName('reference-returned-regions')
        regions_layout = QVBoxLayout(self.regions)
        regions_layout.setContentsMargins(0, 0, 0, 0)
        regions_layout.setSpacing(0)
        self.pages = {}
        for page in ('control', 'stained'):
            panel = QWidget(self.regions)
            panel.setObjectName('reference-region-page-' + page)
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            self.pages[page] = panel
            regions_layout.addWidget(panel)
        self.control_panel, self.stained_panel = self.pages['control'], self.pages['stained']
        if not self._negative and self._preparation is None:
            self.control_panel.layout().setContentsMargins(0, 0, 0, 10)
            row = QHBoxLayout()
            row.addWidget(text_label('AF control set', self.control_panel))
            self.set_choice = QComboBox(self.control_panel)
            self.set_choice.setObjectName('reference-control-set')
            self.set_choice.setAccessibleName('AF control set to define')
            self.set_choice.setMinimumWidth(130)
            self.set_choice.setMaximumWidth(280)
            self.set_choice.currentIndexChanged.connect(self._show_members)
            row.addWidget(self.set_choice)
            self.new_set_button = ContentSizedButton('New set', self.control_panel)
            self.new_set_button.clicked.connect(self._new_set)
            self.new_set_button.setEnabled(self._accepted)
            row.addWidget(self.new_set_button)
            row.addStretch(1)
            self.control_panel.layout().addLayout(row)
            self.control_panel.layout().addWidget(text_label('Select the tissue regions that represent this AF set and the tissue-free regions used to measure image background.', self.control_panel, supporting=True))

        self.region_table = self._make_table(len(self._rows) + 4, ('Use', 'Region', 'Area (µm²)',
            'Matched negative control' if self._negative else 'AF control set'), 'reference-source-regions')
        self.region_table.setAccessibleName('Regions in this reference image')
        self.table = self.region_table
        self._indices = {}
        self.role_titles, self.role_descriptions = {}, {}
        self._empty_rows = {}
        tissue_rows = tuple(row for row in self._rows if row['role'] != 'background')
        index = 0
        for group, rows in (('tissue', tissue_rows), ('background', self._backgrounds)):
            header = QWidget(self.region_table)
            header.setObjectName('reference-region-role-' + group)
            header.setStyleSheet('background: #f3f7f6;')
            header_layout = QVBoxLayout(header)
            header_layout.setContentsMargins(10, 9, 10, 8)
            header_layout.setSpacing(2)
            title = text_label('', header, heading=True)
            description = text_label('', header, supporting=True)
            header_layout.addWidget(title)
            header_layout.addWidget(description)
            self.role_titles[group], self.role_descriptions[group] = title, description
            self._ledger_context_row(index, header)
            index += 1
            for row in rows:
                self._indices[row['roi_id']] = index
                index += 1
            self._empty_rows[group] = index
            empty = text_label('No regions saved yet', self.region_table, supporting=True)
            empty.setContentsMargins(10, 6, 10, 6)
            self._ledger_context_row(index, empty)
            index += 1
        for row in self._rows:
            roi_id = row['roi_id']
            index = self._indices[roi_id]
            use = QTableWidgetItem()
            use.setData(Qt.ItemDataRole.UserRole, roi_id)
            use.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                (Qt.ItemFlag.ItemIsUserCheckable if self._accepted and row['role'] != 'negative' else Qt.ItemFlag.NoItemFlags))
            if row['role'] != 'negative':
                use.setCheckState(Qt.CheckState.Unchecked)
            self.region_table.setItem(index, 0, use)
            label = {'positive': 'Stained', 'af': 'AF tissue', 'negative': 'Negative', 'background': 'Background'}[row['role']]
            use.setTextAlignment(LEFT)
            for column, text in ((1, f"{label} {row['number']}"), (2, f"{row['area_um2']:,.1f}")):
                item = QTableWidgetItem(text)
                item.setTextAlignment(RIGHT if column == 2 else LEFT)
                item.setToolTip(row['image_name'])
                self.region_table.setItem(index, column, item)
            if row['role'] == 'positive' and self._preparation is None:
                editor = decision_combo(self.region_table, ())
                editor.setObjectName('reference-control-for-' + roi_id)
                editor.setAccessibleName(f"Control for stained region {row['number']}")
                editor.setEnabled(self._accepted)
                editor.currentIndexChanged.connect(lambda _index, key=roi_id: self._assignment_changed(key))
                self.region_table.setCellWidget(index, 3, editor)
                self.pairing_editors[roi_id] = editor
        self.region_table.itemChanged.connect(self._membership_changed)
        regions_layout.addWidget(self.region_table)
        self.pending_row = QWidget(self.regions)
        pending = QHBoxLayout(self.pending_row)
        pending.setContentsMargins(0, 14, 0, 0)
        pending.setSpacing(12)
        accept = ContentSizedButton('Use saved regions', self.pending_row)
        accept.setObjectName('reference-regions-accept')
        accept.setProperty('actionRole', 'primary')
        accept.setToolTip('Accept this saved region revision before selecting the regions to use.')
        accept.clicked.connect(lambda: self.native_action_requested.emit('accept'))
        pending.addWidget(accept)
        pending.addStretch(1)
        regions_layout.addWidget(self.pending_row)
        self.buttons = {'edit': edit, 'accept': accept}
        self._body.addWidget(self.regions)
        error = text_label('', self)
        error.setObjectName('error-' + self.FIELD)
        error.setProperty('validationMessage', True)
        error.hide()
        self._layout.addWidget(error)
        self.validation_editors = {self.FIELD: self.region_table}
        self.field_errors = {self.FIELD: error}
        self._ready = True
        self.image_selector.currentIndexChanged.connect(self._source_changed)
        self.set_values(values)
        self.show_page(self.page)
        self._arrange()

    def _init_preparation(self, values):
        """Review one native annotation export as an editable region worksheet."""
        from PySide6.QtGui import QKeySequence, QShortcut
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.surface = QFrame(self)
        self.surface.setObjectName('reference-preparation-surface')
        self.surface.setStyleSheet('QFrame#reference-preparation-surface {'
            'background: #ffffff; border: 1px solid #d5dfdc; border-radius: 0; }'
            'QFrame#reference-preparation-surface QLabel { background: transparent; }')
        self._body = QVBoxLayout(self.surface)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(0)
        self._layout.addWidget(self.surface)
        self.source_header = QWidget(self.surface)
        self._header = QGridLayout(self.source_header)
        self._header.setContentsMargins(18, 16, 18, 16)
        self._header.setHorizontalSpacing(20)
        self._header.setVerticalSpacing(5)
        self._header.setColumnStretch(0, 1)
        self.source_role = text_label('', self.source_header, heading=True)
        self.source_name = text_label('', self.source_header)
        self.source_name.setMinimumWidth(0)
        self.image_selector = QComboBox(self.source_header)
        self.image_selector.setObjectName('reference-region-source')
        self.image_selector.setAccessibleName('Reference image')
        self.image_selector.setMinimumContentsLength(12)
        self.image_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.image_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        counts, seen = Counter(source['filename'] for source in self._sources.values()), Counter()
        for key, source in self._sources.items():
            seen[source['filename']] += 1
            label = source['filename'] + (f" · image {seen[source['filename']]}" if counts[source['filename']] > 1 else '')
            self.image_selector.addItem(label, key)
            self.image_selector.setItemData(self.image_selector.count() - 1, source['filename'], Qt.ItemDataRole.ToolTipRole)
        self._header.addWidget(self.source_role, 0, 0)
        self._header.addWidget(self.source_name, 1, 0)
        self._header.addWidget(self.image_selector, 1, 0)
        self.image_selector.setVisible(len(self._sources) > 1)
        self.source_name.setVisible(len(self._sources) <= 1)
        self._native_actions = QWidget(self.source_header)
        actions = QHBoxLayout(self._native_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(7)
        edit = ContentSizedButton('Annotate in QuPath', self._native_actions)
        edit.setObjectName('reference-regions-edit')
        edit.setProperty('actionRole', 'secondary' if self._rows else 'primary')
        edit.setToolTip('Open the reference annotation guide in QuPath. Save and close QuPath to return the regions.')
        edit.clicked.connect(lambda: self.native_action_requested.emit('edit'))
        actions.addWidget(edit)
        self.recovery = QToolButton(self._native_actions)
        self.recovery.setText('⋯')
        self.recovery.setAccessibleName('Reference annotation recovery')
        self.recovery.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.recovery)
        menu.addAction('Refresh saved regions', lambda: self.native_action_requested.emit('load'))
        self.recovery.setMenu(menu)
        actions.addWidget(self.recovery)
        self._body.addWidget(self.source_header)

        self.region_table = self._make_table(len(self._rows) + 4,
            ('Region', 'Area (µm²)', ''), 'reference-preparation-regions')
        self.table = self.region_table
        self.region_table.setStyleSheet('QTableView#reference-preparation-regions {'
            'background: #ffffff; border: none; border-radius: 0; padding: 0; }'
            'QTableView#reference-preparation-regions::item { padding-left: 24px; padding-right: 12px; }')
        self.region_table.setAlternatingRowColors(False)
        self.region_table.horizontalHeader().hide()
        self.region_table.setItemDelegateForColumn(1, _RegionAreaDelegate(self.region_table))
        for column in range(self.region_table.columnCount()):
            self.region_table.horizontalHeaderItem(column).setTextAlignment(LEFT)
        self.region_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self.region_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.region_table.installEventFilter(self)
        self.region_table.setAccessibleName('Saved reference regions')
        self.region_table.setAccessibleDescription('Tissue and image-background regions are grouped by type. '
            'Select cells with Shift or Ctrl and copy with Ctrl+C. Remove selected regions with Delete. '
            'Undo removals in this workspace with Ctrl+Z. QuPath annotations are unchanged.')
        self.region_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.region_table.customContextMenuRequested.connect(self._preparation_context_menu)
        self._indices, self.remove_buttons, self._empty_rows = {}, {}, {}
        self._removal_history = []
        self.region_table.setColumnHidden(2, not self._accepted)
        self.role_titles, self.role_descriptions, self.role_status = {}, {}, {}
        tissue_role = 'positive' if self._preparation == 'stained' else 'af'
        index = 0
        for group, role in (('tissue', tissue_role), ('background', 'background')):
            header = QWidget(self.region_table)
            header.setObjectName('reference-preparation-group-' + group)
            header.setStyleSheet('QWidget#reference-preparation-group-' + group
                + ' { background: #ffffff; }')
            content = QVBoxLayout(header)
            content.setContentsMargins(12, 12, 12, 10)
            content.setSpacing(3)
            title = text_label(CLASSES[role], header, heading=True)
            title.setWordWrap(False)
            native_class = NATIVE_CLASSES[role]
            title.setToolTip('QuPath class: ' + native_class)
            title.setAccessibleDescription('The drawing guide assigns the QuPath class ' + native_class + '.')
            purpose = ('Tissue-free regions with negligible fluorophore signal.' if group == 'background'
                       else 'Tissue with clear fluorophore signal.' if role == 'positive'
                       else 'Tissue representing control autofluorescence.')
            description = text_label(purpose, header, supporting=True)
            content.addWidget(title)
            content.addWidget(description)
            self.role_titles[group], self.role_descriptions[group] = title, description
            self._ledger_context_row(index, header)
            index += 1
            for row in self._rows:
                if (row['role'] == 'background') != (group == 'background'):
                    continue
                identity = row['roi_id']
                self._indices[identity] = index
                for column, value in enumerate((f"Region {row['number']}", f"{row['area_um2']:,.1f}", '')):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, identity)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                        (Qt.ItemFlag.ItemIsSelectable if column < 2 else Qt.ItemFlag.NoItemFlags))
                    item.setTextAlignment(LEFT)
                    item.setToolTip(str(row['area_um2']) + ' µm² · Geometric region area before sampling.'
                        if column == 1 else row['image_name'] + '\nQuPath class: ' + row.get('label', NATIVE_CLASSES[row['role']]))
                    self.region_table.setItem(index, column, item)
                if self._accepted:
                    action_cell = QWidget(self.region_table)
                    action_layout = QHBoxLayout(action_cell)
                    action_layout.setContentsMargins(4, 3, 8, 3)
                    button = QToolButton(action_cell)
                    button.setText('×')
                    button.setAutoRaise(True)
                    button.setFixedSize(28, 28)
                    button.setObjectName('reference-remove-region-' + identity)
                    button.setAccessibleName(f"Remove {CLASSES[row['role']]} region {row['number']} from preparation")
                    button.setToolTip('Remove from this preparation (Delete). Undo with Ctrl+Z. QuPath annotations are unchanged.')
                    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                    button.clicked.connect(lambda _checked=False, key=identity: self._remove_preparation_regions((key,)))
                    action_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
                    self.region_table.setCellWidget(index, 2, action_cell)
                    self.remove_buttons[identity] = button
                index += 1
            self._empty_rows[group] = index
            empty = text_label('No saved regions', self.region_table, supporting=True)
            empty.setContentsMargins(12, 8, 12, 8)
            self.role_status[group] = empty
            self._ledger_context_row(index, empty)
            index += 1
        for sequence, handler in ((QKeySequence.StandardKey.Copy, self._copy_preparation_cells),
                                  (QKeySequence.StandardKey.Undo, self._undo_preparation_removal)):
            shortcut = QShortcut(QKeySequence(sequence),
                self if sequence == QKeySequence.StandardKey.Undo else self.region_table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
        self._body.addWidget(self.region_table)
        self.pending_row = QWidget(self.surface)
        pending = QHBoxLayout(self.pending_row)
        pending.setContentsMargins(18, 14, 18, 14)
        pending.setSpacing(18)
        pending.addWidget(text_label('Accept the annotations from QuPath to select regions for the reference spectrum.', self.pending_row, supporting=True), 1)
        accept = ContentSizedButton('Use saved regions', self.pending_row)
        accept.setObjectName('reference-regions-accept')
        accept.setProperty('actionRole', 'primary')
        accept.clicked.connect(lambda: self.native_action_requested.emit('accept'))
        pending.addWidget(accept)
        self._body.addWidget(self.pending_row)
        self.buttons = {'edit': edit, 'accept': accept}
        self.footer = QWidget(self.surface)
        self.footer_content = QVBoxLayout(self.footer)
        self.footer_content.setContentsMargins(12, 10, 12, 12)
        self.footer_content.setSpacing(6)
        self.overview_actions = QHBoxLayout()
        self.overview_actions.setContentsMargins(0, 0, 0, 0)
        self.overview_actions.setSpacing(18)
        self.overview_actions.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.footer_content.addLayout(self.overview_actions)
        self._body.addWidget(self.footer)
        error = text_label('', self)
        error.setObjectName('error-' + self.FIELD)
        error.setProperty('validationMessage', True)
        error.hide()
        self._layout.addWidget(error)
        self.validation_editors, self.field_errors = {self.FIELD: self.region_table}, {self.FIELD: error}
        self._ready = True
        self.image_selector.currentIndexChanged.connect(self._source_changed)
        self.set_values(values)
        self.show_page(self.page)
        self._arrange()

    def attach_settings(self, settings):
        """Keep form-owned editors and validation in the worksheet footer."""
        for form in settings.findChildren(QFormLayout):
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            form.setHorizontalSpacing(20)
            form.setVerticalSpacing(14)
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if item is not None and item.widget() is not None:
                    item.widget().setMinimumWidth(220)
        settings._advanced_toggle.setText('Advanced settings')
        settings.attach_to_review_footer(self.overview_actions, self.footer_content)

    def _show_preparation_page(self, page, source_id):
        if page != self._preparation:
            raise ValueError('Unknown reference-region source page')
        source_id = source_id or self._source_by_page.get(page) or next(iter(self._sources), None)
        if source_id is not None and source_id not in self._sources:
            raise ValueError('Reference image does not belong to this source page')
        if self._source_by_page.get(page) != source_id:
            self.region_table.clearSelection()
            self.region_table.setCurrentCell(-1, -1)
        self.page = page
        self._source_by_page[page] = source_id
        with QSignalBlocker(self.image_selector):
            self.image_selector.setCurrentIndex(self.image_selector.findData(source_id))
        source = self._sources.get(source_id)
        self.source_name.setText(source['filename'] if source else 'Reference image')
        self.image_selector.setToolTip(source['filename'] if source else '')
        self.source_role.setText('Single-stain image' if page == 'stained' else
            'AF control for ' + str(self._review.get('source_link_id') or self._review.get('component') or '')
            if self._review.get('source_role') in {'negative_control', 'single_stain_negative_control'}
            else 'Unstained control image')
        self._refresh_preparation_summary()
        self.page_changed.emit(page)

    def _preparation_included(self, row):
        selected = (self._background_selected if row['role'] == 'background' else self._positive_selected
                    if row['role'] == 'positive' else self._sets[0] if self._sets else ())
        return row['roi_id'] in selected

    def _set_preparation_included(self, identities, included):
        if not self._accepted or not self._ready:
            return
        identities = set(identities)
        selected = {row['roi_id'] for row in self._rows if self._preparation_included(row)}
        if included:
            selected.update(identities & self._by_id.keys())
        else:
            selected.difference_update(identities)
        self._background_selected = tuple(row['roi_id'] for row in self._backgrounds if row['roi_id'] in selected)
        self._positive_selected = tuple(row['roi_id'] for row in self._positive if row['roi_id'] in selected)
        if self._preparation == 'control':
            self._sets = [tuple(row['roi_id'] for row in self._controls if row['roi_id'] in selected)]
        self._refresh_preparation_summary()

    def _selected_preparation_cells(self):
        return sorted((item for item in self.region_table.selectedItems()
            if not self.region_table.isRowHidden(item.row()) and item.data(Qt.ItemDataRole.UserRole) in self._by_id), key=lambda item: (item.row(), item.column()))

    def _remove_preparation_regions(self, identities):
        if not self._accepted:
            return
        requested = set(identities)
        removed = tuple(row['roi_id'] for row in self._rows if row['roi_id'] in requested
            and self._source(row) == self._source_by_page.get(self.page) and self._preparation_included(row))
        if not removed:
            return
        self._removal_history.append(removed)
        self._set_preparation_included(removed, False)
        self.region_table.clearSelection()
        self.region_table.setCurrentCell(-1, -1)
        self.region_table.setFocus(Qt.FocusReason.OtherFocusReason)

    def _remove_preparation_selection(self):
        self._remove_preparation_regions(item.data(Qt.ItemDataRole.UserRole)
            for item in self._selected_preparation_cells())

    def _undo_preparation_removal(self):
        if not self._accepted or not self._removal_history:
            return
        restored = self._removal_history.pop()
        self._set_preparation_included(restored, True)
        self.show_page(self.page, self._source(self._by_id[restored[0]]))
        self.region_table.setCurrentCell(self._indices[restored[0]], 0)
        self.region_table.setFocus(Qt.FocusReason.OtherFocusReason)

    def _copy_preparation_cells(self):
        from PySide6.QtWidgets import QApplication
        cells = self._selected_preparation_cells()
        if not cells:
            return
        selected = {(item.row(), item.column()):
            str(self._by_id[item.data(Qt.ItemDataRole.UserRole)]['area_um2']) if item.column() == 1 else
            item.text() for item in cells}
        columns = range(min(item.column() for item in cells), max(item.column() for item in cells) + 1)
        QApplication.clipboard().setText('\n'.join('\t'.join(selected.get((row, column), '') for column in columns)
            for row in sorted({item.row() for item in cells})))

    def _preparation_context_menu(self, position):
        item = self.region_table.itemAt(position)
        if item is not None and not item.isSelected():
            self.region_table.setCurrentItem(item)
        menu = QMenu(self.region_table)
        selected = bool(self._selected_preparation_cells())
        menu.addAction('Copy', self._copy_preparation_cells).setEnabled(selected)
        if self._accepted:
            menu.addSeparator()
            menu.addAction('Remove selected regions', self._remove_preparation_selection).setEnabled(selected)
        menu.exec(self.region_table.viewport().mapToGlobal(position))

    def _refresh_preparation_summary(self):
        if not self._ready:
            return
        counts, saved = Counter(), Counter()
        source = self._source_by_page.get(self.page)
        for identity, index in self._indices.items():
            row = self._by_id[identity]
            group = 'background' if row['role'] == 'background' else 'tissue'
            belongs = self._source(row) == source
            visible = belongs and (not self._accepted or self._preparation_included(row))
            self.region_table.setRowHidden(index, not visible)
            if belongs:
                saved[group] += 1
            if visible:
                counts[group] += 1
        for group, index in self._empty_rows.items():
            self.region_table.setRowHidden(index, bool(counts[group]))
            self.role_status[group].setText('No regions included' if saved[group] else 'No saved regions')
        available = bool(sum(counts.values()))
        edit = self.buttons['edit']
        action_role = 'secondary' if sum(saved.values()) else 'primary'
        if edit.property('actionRole') != action_role:
            edit.setProperty('actionRole', action_role)
            edit.style().unpolish(edit)
            edit.style().polish(edit)
            edit.update()
        self.pending_row.setVisible(available and not self._accepted)
        self.region_table.horizontalHeader().hide()
        self._fit_preparation_table()

    def _arrange_preparation(self):
        narrow = self.width() < 600
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._header.removeWidget(self._native_actions)
        self._header.addWidget(self._native_actions, 2 if narrow else 0, 0 if narrow else 1,
            1 if narrow else 2, 1, LEFT)

    def _fit_preparation_table(self):
        if not self._ready or self._fitting:
            return
        self._fitting = True
        table = self.region_table
        metrics = table.fontMetrics()
        area = max(metrics.horizontalAdvance('Area (µm²)') + 36,
            max((metrics.horizontalAdvance(table.item(index, 1).text() + ' µm²') + 36
                 for index in self._indices.values()), default=100))
        action = 44 if self._accepted else 0
        region = max((metrics.horizontalAdvance(table.item(index, 0).text()) + 36
                      for index in self._indices.values()), default=100)
        minimums = (region, area, action)
        extra = max(0, table.viewport().width() - sum(minimums))
        widths = [minimum + int(extra * fraction) for minimum, fraction in zip(minimums, (.6, .4, 0))]
        widths[0] += max(0, table.viewport().width() - sum(widths))
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)
        for row in range(table.rowCount()):
            widget = table.cellWidget(row, 0)
            height = (widget.heightForWidth(table.viewport().width()) if widget.hasHeightForWidth()
                else widget.sizeHint().height()) if widget is not None else metrics.height() + 18
            table.setRowHeight(row, max(42, height))
        height = (0 if table.horizontalHeader().isHidden() else table.horizontalHeader().sizeHint().height())
        height += sum(table.rowHeight(row) for row in range(table.rowCount()) if not table.isRowHidden(row)) + 2
        if sum(widths) > table.viewport().width():
            height += table.horizontalScrollBar().sizeHint().height()
        table.setFixedHeight(min(480, height))
        self._fitting = False

    @staticmethod
    def _source(row):
        return row.get('source', row.get('source_id', row.get('input_id', row['image_name'])))

    def _make_table(self, rows, columns, name):
        table = QTableWidget(rows, len(columns), self)
        table.setObjectName(name)
        table.setHorizontalHeaderLabels(columns)
        reference_table(table, clipboard=name != 'reference-preparation-regions')
        table.setStyleSheet('QTableView[scientificTable="true"] {'
            'border-radius: 0; padding: 0; }')
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setDefaultAlignment(LEFT)
        table.horizontalHeader().setMinimumSectionSize(35)
        for column in range(len(columns)):
            table.horizontalHeaderItem(column).setTextAlignment(RIGHT if column == 2 else LEFT)
        table.viewport().installEventFilter(self)
        return table

    def _ledger_context_row(self, index, widget):
        """Section headings and empty messages are never saved-region targets."""
        for column in range(self.region_table.columnCount()):
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.region_table.setItem(index, column, item)
        self.region_table.setSpan(index, 0, 1, self.region_table.columnCount())
        self.region_table.setCellWidget(index, 0, widget)

    def show_page(self, page, source_id=None):
        """Show an exact source without submitting or modifying its region choices."""
        if self._preparation:
            self._show_preparation_page(page, source_id)
            return
        if page not in self.pages or self._preparation not in (None, page):
            raise ValueError('Unknown reference-region source page')
        eligible = [key for key, source in self._sources.items() if source['page'] == page]
        source_id = source_id or self._source_by_page.get(page) or (eligible[0] if eligible else None)
        if source_id is not None and source_id not in eligible:
            raise ValueError('Reference image does not belong to this source page')
        old_source = self._source_by_page.get(self.page)
        self.page = page
        self._source_by_page[page] = source_id
        if old_source != source_id:
            self.region_table.clearSelection()
            self.region_table.setCurrentCell(-1, -1)
        with QSignalBlocker(self.image_selector):
            self.image_selector.setCurrentIndex(self.image_selector.findData(source_id))
        source = self._sources.get(source_id)
        self.source_name.setText(source['filename'] if source else 'Reference image')
        self.source_role.setText('Single-stain image' if page == 'stained' else 'Negative-control image' if self._negative
            else 'AF control for ' + str(self._review.get('source_link_id') or self._review.get('component') or '')
            if self._review.get('source_role') in {'negative_control', 'single_stain_negative_control'}
            else 'Unstained control image')
        self.image_selector.setToolTip(source['filename'] if source else '')
        tissue = 'positive' if page == 'stained' else 'negative' if self._negative else 'af'
        for group, role in (('tissue', tissue), ('background', 'background')):
            self.role_titles[group].setText(CLASSES[role])
            self.role_descriptions[group].setText(PURPOSES[role])
        visible = Counter()
        for row in self._rows:
            show = self._source(row) == source_id
            self.region_table.setRowHidden(self._indices[row['roi_id']], not show)
            if show:
                visible['background' if row['role'] == 'background' else 'tissue'] += 1
        for group, index in self._empty_rows.items():
            self.region_table.setRowHidden(index, bool(visible[group]))
        for key, panel in self.pages.items():
            panel.setVisible(key == page and bool(visible) and self._accepted)
        self.pending_row.setVisible(bool(visible) and not self._accepted)
        self.region_table.setColumnHidden(0, not self._accepted)
        self.region_table.setColumnHidden(3, not self._accepted or page != 'stained' or self._preparation is not None or
            (not self._negative and len(self._sets) == 1 and all(self._assignments.get(key) == 0 for key in self._positive_selected)))
        self._fit_tables()
        self.page_changed.emit(page)

    def _source_changed(self, *_):
        key = self.image_selector.currentData()
        if key in self._sources:
            self.show_page(self._sources[key]['page'], key)

    def _new_set(self):
        self._sets.append(())
        self._refresh_choices()
        self.set_choice.setCurrentIndex(len(self._sets) - 1)

    def _refresh_choices(self):
        if hasattr(self, 'set_choice'):
            current = self.set_choice.currentIndex()
            with QSignalBlocker(self.set_choice):
                self.set_choice.clear()
                for index in range(len(self._sets)):
                    self.set_choice.addItem(f'Set {index + 1}', index)
                self.set_choice.setCurrentIndex(max(0, min(current, len(self._sets) - 1)))
        for roi_id, editor in self.pairing_editors.items():
            with QSignalBlocker(editor):
                editor.clear()
                editor.addItem('Choose control…' if self._negative else 'Choose set…', None)
                if self._negative:
                    multiple = len({self._source(row) for row in self._controls}) > 1
                    for row in self._controls:
                        label = (self._sources[self._source(row)]['display_name'] + ' · ' if multiple else '') + f"Negative {row['number']}"
                        editor.addItem(label, row['roi_id'])
                else:
                    for index in range(len(self._sets)):
                        editor.addItem(f'AF control set {index + 1}', index)
                editor.setCurrentIndex(max(0, editor.findData(self._assignments.get(roi_id))))
        self._show_members()
        if self._ready:
            self.show_page(self.page)

    def _show_members(self, *_):
        if self._preparation:
            self._refresh_preparation_summary()
            return
        if not self._ready:
            return
        members = self._sets[self.set_choice.currentIndex() if hasattr(self, 'set_choice') else 0] if self._sets else ()
        with QSignalBlocker(self.region_table):
            for row in self._rows:
                if row['role'] == 'negative':
                    continue
                selected = (self._positive_selected if row['role'] == 'positive' else
                            self._background_selected if row['role'] == 'background' else members)
                self.region_table.item(self._indices[row['roi_id']], 0).setCheckState(
                    Qt.CheckState.Checked if row['roi_id'] in selected else Qt.CheckState.Unchecked)

    def _membership_changed(self, item):
        if item.column() != 0 or not self._ready:
            return
        row = self._by_id.get(item.data(Qt.ItemDataRole.UserRole))
        if row is None or not self._accepted:
            return
        selected = lambda rows: tuple(candidate['roi_id'] for candidate in rows
            if self.region_table.item(self._indices[candidate['roi_id']], 0).checkState() == Qt.CheckState.Checked)
        if row['role'] == 'positive':
            self._positive_selected = selected(self._positive)
        elif row['role'] == 'background':
            self._background_selected = selected(self._backgrounds)
        elif row['role'] == 'af' and hasattr(self, 'set_choice'):
            self._sets[self.set_choice.currentIndex()] = selected(self._controls)
        elif row['role'] == 'af' and self._preparation == 'control':
            self._sets = [selected(self._controls)]

    def _assignment_changed(self, roi_id):
        self._assignments[roi_id] = self.pairing_editors[roi_id].currentData()

    def values(self):
        pairings = {roi_id: ([value] if value is not None else []) if self._negative else
                    (list(self._sets[value]) if value is not None else []) for roi_id, value in self._assignments.items()}
        return {self.FIELD: {'positive_roi_ids': list(self._positive_selected), 'pairings': pairings,
            'background_roi_ids': list(self._background_selected),
            'control_sets': [list(members) for members in self._sets],
            'control_set_assignments': {} if self._negative else dict(self._assignments)}}

    def set_values(self, values):
        if self.FIELD not in values and self._initialized:
            return
        self._initialized = True
        if self._preparation:
            self._removal_history.clear()
        data = values.get(self.FIELD) or self._review.get('saved_selection') or {}
        ordered = lambda rows, ids: tuple(row['roi_id'] for row in rows if row['roi_id'] in ids)
        self._positive_selected = ordered(self._positive, data.get('positive_roi_ids', self._by_id))
        self._background_selected = ordered(self._backgrounds, data.get('background_roi_ids', self._by_id))
        available = tuple(row['roi_id'] for row in self._controls)
        def members(ids):
            return tuple(key for key in available if key in ids) if all(key in available for key in ids) else ()
        self._sets = [] if self._negative or self._preparation == 'stained' else [members(ids) for ids in data.get('control_sets', ())]
        if self._preparation == 'control' and not self._sets:
            self._sets = [available]
        elif self._preparation == 'control':
            selected = {key for group in self._sets for key in group}
            self._sets = [tuple(key for key in available if key in selected)]
        self._assignments = {}
        pairings = data.get('pairings')
        for roi_id in self.pairing_editors:
            chosen = members(pairings.get(roi_id, ())) if pairings is not None else available
            if self._negative:
                self._assignments[roi_id] = chosen[0] if len(chosen) == 1 else None
            elif chosen:
                if chosen not in self._sets:
                    self._sets.append(chosen)
                self._assignments[roi_id] = self._sets.index(chosen)
            else:
                self._assignments[roi_id] = None
        if not self._sets and not self._negative and self._preparation != 'stained':
            self._sets = [()]
        if not self._negative:
            for roi_id, index in data.get('control_set_assignments', {}).items():
                if roi_id in self._assignments:
                    chosen = members((pairings or {}).get(roi_id, ()))
                    self._assignments[roi_id] = index if type(index) is int and 0 <= index < len(self._sets) and self._sets[index] == chosen else None
        self._refresh_choices()

    def owns_control(self, name):
        return name == self.FIELD

    def validation_errors(self):
        if not self._accepted:
            return {}
        errors = []
        data = self.values()[self.FIELD]
        if self._preparation == 'control':
            if not self._sets or any(not members for members in self._sets):
                errors.append('Select at least one AF tissue region.')
        else:
            if not self._positive_selected:
                errors.append('Select at least one stained tissue region.')
            if self._preparation is None and any(not data['pairings'].get(key) for key in self._positive_selected):
                errors.append('Choose a matched negative control for each stained region.' if self._negative else
                              'Choose a nonempty AF control set for each stained region.')
        background_sources = {self._source(self._by_id[key]) for key in self._background_selected}
        if set(self._sources) - background_sources or not self._background_selected:
            errors.append('Select image background for each reference image.')
        return {self.FIELD: ' '.join(errors)} if errors else {}

    def reveal(self, name):
        if self._preparation == 'control' and any(not members for members in self._sets):
            self.show_page('control')
            if hasattr(self, 'set_choice'):
                self.set_choice.setCurrentIndex(next(index for index, members in enumerate(self._sets) if not members))
        elif self._preparation != 'control' and not self._positive_selected:
            self.show_page('stained')
        elif self._preparation is None and any(self._assignments.get(key) is None for key in self._positive_selected):
            key = next(key for key in self._positive_selected if self._assignments.get(key) is None)
            self.show_page('stained', self._source(self._by_id[key]))
            self.pairing_editors[key].setFocus(Qt.FocusReason.OtherFocusReason)
            return
        else:
            backgrounds = {self._source(self._by_id[key]) for key in self._background_selected}
            for key, source in self._sources.items():
                if key not in backgrounds:
                    self.show_page(source['page'], key)
                    break
        if self._preparation:
            group = 'tissue' if (self._preparation == 'control' and not any(self._sets)
                or self._preparation == 'stained' and not self._positive_selected) else 'background'
            candidates = [row for row in self._rows if self._source(row) == self._source_by_page.get(self.page)
                and (row['role'] == 'background') == (group == 'background') and self._preparation_included(row)]
            if candidates:
                self.region_table.setCurrentCell(self._indices[candidates[0]['roi_id']], 0)
                self.region_table.setFocus(Qt.FocusReason.OtherFocusReason)
            else:
                self.buttons['edit'].setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.region_table.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_option_editor(self, editor):
        self.option_editor = editor
        editor.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()
        self._fit_tables()

    def _arrange(self):
        if self._preparation:
            self._arrange_preparation()
            return
        narrow = self.width() < 640
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._header.removeWidget(self._native_actions)
        self._header.addWidget(self._native_actions, 2 if narrow else 0, 0 if narrow else 1,
            1 if narrow else 2, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def eventFilter(self, watched, event):
        if (self._preparation and self._ready and watched is self.region_table
                and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Delete):
            self._remove_preparation_selection()
            return True
        if event.type() == QEvent.Type.Resize:
            self._fit_tables()
        return super().eventFilter(watched, event)

    def _fit_tables(self):
        if self._preparation:
            self._fit_preparation_table()
            return
        if not self._ready or self._fitting:
            return
        self._fitting = True
        table = self.region_table
        width = table.viewport().width()
        use = max(52, table.fontMetrics().horizontalAdvance('Use') + 24) if not table.isColumnHidden(0) else 0
        area = max(124, table.fontMetrics().horizontalAdvance('Area (µm²)') + 28)
        match = min(max(170, round(width * .36)), max(100, width - use - area - 85)) if not table.isColumnHidden(3) else 0
        widths = (use, max(60, width - use - area - match), area, match)
        for column, value in enumerate(widths):
            if value:
                table.setColumnWidth(column, value)
        for index in range(table.rowCount()):
            widget = table.cellWidget(index, 0)
            height = 40
            if widget is not None:
                height = widget.heightForWidth(width) if widget.hasHeightForWidth() else widget.sizeHint().height()
            table.setRowHeight(index, max(40, height))
        height = table.horizontalHeader().sizeHint().height() + sum(table.rowHeight(index)
            for index in range(table.rowCount()) if not table.isRowHidden(index)) + 6
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if height <= 480 else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setFixedHeight(min(480, height))
        self._fitting = False
