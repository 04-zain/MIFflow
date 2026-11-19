"""Choose one cell region and a matrix of intensity statistics for each channel."""
import re
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QSizePolicy, QLabel,
    QStyle, QStyleOptionViewItem, QStyledItemDelegate)
from .researcher_widgets import ContentSizedButton


class _RegionCellDelegate(QStyledItemDelegate):
    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class _MeasurementHeader(QHeaderView):
    """Keep the six statistic headings readable in a compact channel matrix."""
    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumSectionSize(36)

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(max(48, self.fontMetrics().lineSpacing()*2+12))
        return size

    def paintSection(self, painter, rect, column):
        if not rect.isValid():
            return
        painter.save()
        painter.fillRect(rect, self.palette().alternateBase())
        painter.setPen(self.palette().text().color())
        text = self.model().headerData(column, self.orientation(), Qt.ItemDataRole.DisplayRole)
        if text == 'Std. dev.':
            text = 'Std.\ndev.'
        alignment = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if column < 2 else Qt.AlignmentFlag.AlignHCenter)
        painter.drawText(rect.adjusted(10 if column < 2 else 3, 4, -4, -4),
            alignment | Qt.TextFlag.TextWordWrap, str(text or ''))
        painter.setPen(QPen(self.palette().mid().color(), .5))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


class MeasurementChannelGrid(QWidget):
    """Reparent the original checkboxes without changing form values or binding.

    The original statistic editor retains its checkbox references and value
    methods. Its validation message remains below the channel's region editor,
    while each visible checkbox belongs to one labelled statistic column.
    """
    def __init__(self, form, parent=None):
        super().__init__(parent)
        self.channels = form.tables[0].rows
        self.editors = {}
        self.fields = {}
        self.included = set()
        self._cells = {}
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit)
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Find a channel or marker…')
        self.search.setAccessibleName('Filter channels for intensity measurements')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        toolbar.addWidget(self.search, 1)
        self.copy = ContentSizedButton('Copy settings…', self)
        self.copy.setToolTip('Apply one channel’s cell region and intensity measurements to all channels shown below.')
        self.menu = QMenu(self.copy)
        self.menu.aboutToShow.connect(self._copy_choices)
        self.copy.setMenu(self.menu)
        toolbar.addWidget(self.copy)
        body.addLayout(toolbar)
        self.table = QTableWidget(len(self.channels), 8, self)
        self.table.setObjectName('measurement-intensity-grid')
        self.table.setProperty('scientificTable', True)
        self.table.setAccessibleName('Intensity measurement matrix by channel and cell region')
        self.table.setHorizontalHeader(_MeasurementHeader(self.table))
        self.labels = ('Mean', 'Median', 'Min', 'Max', 'Std. dev.', 'Variance')
        self.table.setHorizontalHeaderLabels(('Channel', 'Cell region', *self.labels))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 126)
        self.table.setColumnWidth(1, 176)
        self.table.horizontalHeaderItem(4).setToolTip('Minimum intensity')
        self.table.horizontalHeaderItem(5).setToolTip('Maximum intensity')
        self.table.horizontalHeaderItem(6).setToolTip('Intensity standard deviation')
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setItemDelegateForColumn(1, _RegionCellDelegate(self.table))
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        for row, channel in enumerate(self.channels):
            item = QTableWidgetItem(str(channel.values[0]))
            item.setToolTip(' · '.join(str(v) for v in channel.values if v))
            self.table.setItem(row, 0, item)
        body.addWidget(self.table)

    def add_control(self, name, field, editor):
        """Mount an existing region editor or its six exact statistic checkboxes."""
        match = re.fullmatch(r'channel_(\d+)_(compartment|statistics)', name)
        if match is None:
            raise ValueError('Expected a cellular region or intensity measurement control')
        row, kind = int(match[1]), match[2]
        self.editors[row, kind] = editor
        self.fields[name] = (row, field)
        editor.setMinimumWidth(0)
        editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        if kind == 'compartment':
            field.layout().setContentsMargins(0, 0, 0, 0)
            field.layout().setSpacing(0)
            editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            editor.setStyleSheet(
                'QComboBox { border: 1px solid transparent; border-radius: 0; '
                'padding: 0 30px 0 10px; min-height: 0; background: transparent; }'
                'QComboBox:focus { border-color: #006b70; background: #ffffff; }'
            )
            self.table.setCellWidget(row, 1, field)
        else:
            original_layout = editor.layout()
            while original_layout.count():
                original_layout.takeAt(0)
            for index, (key, check) in enumerate(editor._checks.items()):
                cell = QWidget(self.table)
                cell.setObjectName(f'measurement-cell-{row}-{key}')
                layout = QHBoxLayout(cell)
                layout.setContentsMargins(0, 0, 0, 0)
                check.setText('')
                description = {'Std.Dev.':'intensity standard deviation', 'Min':'Minimum intensity',
                    'Max':'Maximum intensity'}.get(key, self.labels[index]+' intensity')
                check.setAccessibleName(f'{self.channels[row].values[0]}: {description}')
                check.setToolTip(check.accessibleName())
                cell.setToolTip(check.accessibleName())
                layout.addWidget(check, alignment=Qt.AlignmentFlag.AlignCenter)
                self._cells[cell] = check
                cell.installEventFilter(self)
                self.table.setCellWidget(row, index+2, cell)
            editor.hide()
            field.layout().setContentsMargins(0, 0, 0, 0)
            field.layout().setSpacing(0)
            self.fields[f'channel_{row}_compartment'][1].layout().addWidget(field)
            # These are the form's own error labels, retained at their source row.
            for error in field.findChildren(QLabel):
                if error.property('validationMessage'):
                    error.installEventFilter(self)
        field.installEventFilter(self)
        self._fit_timer.start(0)

    def set_included(self, rows):
        self.included = set(rows)
        self._filter()

    def _filter(self, *_args):
        query = self.search.text().strip().casefold()
        for row, channel in enumerate(self.channels):
            self.table.setRowHidden(row, row not in self.included or query not in ' '.join(map(str, channel.values)).casefold())
        self._fit_timer.start(0)

    def _copy_choices(self):
        self.menu.clear()
        self.menu.addSection('Apply to all shown channels')
        for row in sorted(self.included):
            if not self.table.isRowHidden(row):
                action = self.menu.addAction('Use '+str(self.channels[row].values[0])+' settings')
                action.triggered.connect(lambda _checked=False, source=row: self._copy_row(source))

    def _copy_row(self, source):
        """Copy only to included rows visible under the current channel filter."""
        region = self.editors[source, 'compartment'].currentData()
        statistics = self.editors[source, 'statistics'].value()
        for row in sorted(self.included):
            if not self.table.isRowHidden(row):
                editor = self.editors[row, 'compartment']
                editor.setCurrentIndex(editor.findData(region))
                self.editors[row, 'statistics'].set_value(statistics)

    def reveal(self, name):
        if name in self.fields:
            row, _ = self.fields[name]
            self.search.clear()
            self.table.scrollTo(self.table.model().index(row, 0))

    def validation_focus(self, name):
        """Return the visible statistic checkbox for this unchanged form control."""
        match = re.fullmatch(r'channel_(\d+)_statistics', name)
        if match is not None:
            return next(iter(self.editors[int(match[1]), 'statistics']._checks.values()))
        return None

    def _fit(self):
        """Fit short catalogs exactly; retain bounded scrolling for longer panels."""
        self.table.ensurePolished()
        for row in range(self.table.rowCount()):
            field = self.fields.get(f'channel_{row}_compartment', (None, None))[1]
            # Validation messages wrap inside the fixed region column. Its
            # unconstrained size hint can fit fewer lines than the actual cell.
            height = 48 if field is None else max(
                48, field.sizeHint().height()+4, field.heightForWidth(field.width())+4)
            if field is not None:
                option = QStyleOptionViewItem()
                option.initFrom(self.table)
                option.rect = self.table.visualRect(self.table.model().index(row, 1))
                content = self.table.style().subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, self.table)
                height += option.rect.height() - content.height()
            self.table.setRowHeight(row, height)
        height = self.table.horizontalHeader().sizeHint().height()+2*self.table.frameWidth()+sum(
            self.table.rowHeight(row) for row in range(self.table.rowCount()) if not self.table.isRowHidden(row))
        if self.table.horizontalScrollBar().isVisible():
            height += self.table.horizontalScrollBar().sizeHint().height()
        self.table.setFixedHeight(min(510, max(100, height)))

    def eventFilter(self, watched, event):
        if watched in self._cells and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            check = self._cells[watched]
            if check.isEnabled() and watched.rect().contains(event.position().toPoint()):
                check.toggle()
                check.setFocus(Qt.FocusReason.MouseFocusReason)
            return True
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize):
            self._fit_timer.start(0)
        return super().eventFilter(watched, event)
