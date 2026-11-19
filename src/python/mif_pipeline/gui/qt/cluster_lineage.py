"""Show the latest refit scope and the fitted groups behind one phenotype.

The review summary knows the selected parent/child sets, not the cell flows
between each pair. Its refit diagram therefore connects the sets as a whole.
The contribution chart uses the exact current cluster counts.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QBoxLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLayout, QMenu, QSizePolicy,
    QStyledItemDelegate, QStyle, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout,
)

from .workbench import style_scientific_table


class ClusterRefitHistory(QFrame):
    """A bounded overview with exact group inventories available on demand."""

    def __init__(self, summary, counts, parent=None):
        super().__init__(parent)
        self.setObjectName('cluster-refit-history')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.layout_row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.layout_row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.layout_row.setContentsMargins(0, 6, 0, 6)
        self.layout_row.setSpacing(20)
        overview = QVBoxLayout()
        overview.setSpacing(4)
        title = QLabel('Latest reclustering', self)
        title.setProperty('stageFieldCaption', True)
        overview.addWidget(title)
        refitted = summary['cell_count']
        self.scope = QLabel(f'{refitted:,} cells refitted together.', self)
        self.scope.setProperty('fieldHelp', True)
        overview.addWidget(self.scope)
        self.overview_layout = overview
        self.layout_row.addLayout(overview)
        flow = QHBoxLayout()
        flow.setSpacing(18)
        for key, caption in (('parent_cluster_ids', 'Previous groups'), ('child_cluster_ids', 'Current fitted groups')):
            column = QVBoxLayout()
            column.setSpacing(3)
            label = QLabel(caption, self)
            label.setProperty('fieldHelp', True)
            column.addWidget(label)
            button = QToolButton(self)
            count=len(summary[key])
            button.setText(f'{count:,} '+('group' if count==1 else 'groups')+' ▾')
            button.setAutoRaise(True)
            button.setProperty('researchDisclosure', True)
            button.setStyleSheet('QToolButton {font-weight:600;padding:2px 0;} QToolButton::menu-indicator {image:none;}')
            button.setAccessibleName(caption)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(button)
            for identity in summary[key]:
                menu.addAction(identity + (f' · {counts[identity]:,} cells' if key=='child_cluster_ids' else ''))
            button.setMenu(menu)
            column.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
            flow.addLayout(column)
            if key=='parent_cluster_ids':
                method = 'FlowSOM' if summary['method']=='flowsom' else 'PhenoGraph'
                transition = QVBoxLayout()
                transition.setSpacing(0)
                self.method_label = QLabel(method, self)
                self.method_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.method_label.setStyleSheet('font-size:12px;color:#557c82;')
                transition.addWidget(self.method_label)
                self.transition_arrow = QLabel('⟶', self)
                self.transition_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.transition_arrow.setStyleSheet('font-size:32px;color:#557c82;')
                self.transition_arrow.setMinimumWidth(64)
                self.transition_arrow.setAccessibleName('Previous groups to current fitted groups')
                transition.addWidget(self.transition_arrow)
                flow.addLayout(transition)
        self.layout_row.addLayout(flow)
        self.flow_layout = flow
        self.layout_row.addStretch(1)

    def minimumSizeHint(self):
        size=super().minimumSizeHint()
        size.setWidth(max(self.overview_layout.minimumSize().width(),self.flow_layout.minimumSize().width()))
        return size

    def resizeEvent(self, event):
        super().resizeEvent(event)
        required=self.overview_layout.minimumSize().width()+self.flow_layout.minimumSize().width()+self.layout_row.spacing()
        self.layout_row.setDirection(QBoxLayout.Direction.LeftToRight if self.width()>=required else QBoxLayout.Direction.TopToBottom)


class _ContributionDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column()!=2:
            return super().paint(painter, option, index)
        fraction = float(index.data(Qt.ItemDataRole.UserRole) or 0.)
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(option.rect, option.palette.brush(QPalette.ColorRole.Highlight))
        painter.setFont(option.font)
        label = f'{100*fraction:.1f}%'
        label_width = max(50, option.fontMetrics.horizontalAdvance('100.0%')+8)
        bar = option.rect.adjusted(10, 0, -label_width-10, 0)
        bar.setTop(option.rect.center().y()-4)
        bar.setHeight(8)
        if bar.width()>0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#e2e9e9'))
            painter.drawRoundedRect(bar, 3, 3)
            filled = bar.adjusted(0, 0, 0, 0)
            filled.setWidth(round(bar.width()*fraction))
            painter.setBrush(QColor('#4a8b94'))
            painter.drawRoundedRect(filled, 3, 3)
        painter.setPen(option.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        painter.drawText(option.rect.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter, label)
        painter.restore()


class ClusterContributions(QFrame):
    """Inspect the exact constituent groups of a selected reporting phenotype."""
    inspect_requested = Signal(str)

    def __init__(self, counts, refit_summary=None, parent=None):
        super().__init__(parent)
        self.counts = dict(counts)
        self.refitted = set(refit_summary['child_cluster_ids']) if refit_summary else set()
        self.cluster_ids = ()
        self.total_cells = 0
        self.setObjectName('cluster-phenotype-contributions')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        self.heading = QLabel(self)
        self.heading.setWordWrap(True)
        self.heading.setProperty('stageFieldCaption', True)
        layout.addWidget(self.heading)
        self.description = QLabel(self)
        self.description.setWordWrap(True)
        self.description.setProperty('fieldHelp', True)
        layout.addWidget(self.description)
        self.table = QTableWidget(0, 4, self)
        self.table.setObjectName('cluster-contribution-table')
        self.table.setHorizontalHeaderLabels(('Fitted group', 'Cells', 'Share', 'Latest fit'))
        self.table.horizontalHeaderItem(2).setToolTip('Percentage of cells in this reporting phenotype.')
        self.table.horizontalHeaderItem(3).setToolTip('Whether the latest reclustering changed this group.')
        style_scientific_table(self.table)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setFixedHeight(34)
        self.table.horizontalHeader().setMinimumSectionSize(48)
        for column in (0, 2):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (1, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnHidden(3, refit_summary is None)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setItemDelegateForColumn(2, _ContributionDelegate(self.table))
        self.table.cellClicked.connect(lambda row, _column: self.inspect_requested.emit(self.cluster_ids[row]))
        self.table.activated.connect(lambda index: self.inspect_requested.emit(self.cluster_ids[index.row()]))
        self.table.setAccessibleName('Fitted groups contributing to the selected phenotype')
        layout.addWidget(self.table)

    def set_group(self, row):
        self.cluster_ids = tuple(row['cluster_ids'])
        self.total_cells = row['cell_count']
        count = len(self.cluster_ids)
        self.heading.setText(row['phenotype_name'])
        if row['status']=='Excluded':
            scope = f'{count:,} fitted group'+('s' if count!=1 else '')+' excluded from phenotyping.'
        elif count>1:
            scope = f'{count:,} fitted groups share this reporting name.'
        else:
            scope = ''
        self.description.setText((scope+' ' if scope else '')+'Select a group to inspect its profiles and staining.')
        self.table.setRowCount(count)
        for index, identity in enumerate(self.cluster_ids):
            fraction = self.counts[identity]/self.total_cells if self.total_cells else 0.
            values = (identity, f'{self.counts[identity]:,}', f'{100*fraction:.1f}%',
                      'Refitted' if identity in self.refitted else 'Retained')
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column==2:
                    item.setData(Qt.ItemDataRole.UserRole, fraction)
                if column==1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(index, column, item)
        self.table.setFixedHeight(34+min(6, count)*34+12)
