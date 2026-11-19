"""Native, copyable evidence tables for classifier annotation and first review."""
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QAbstractItemView, QHeaderView, QMenu, QStyledItemDelegate, QStyle, QStyleOptionViewItem


def configure_evidence_table(table, *, select_rows=False):
    """Keep saved evidence immutable while supporting ordinary cell/row copying."""
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    table.setWordWrap(True)
    table.setTextElideMode(Qt.TextElideMode.ElideNone)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows if select_rows
                               else QAbstractItemView.SelectionBehavior.SelectItems)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection if select_rows
                           else QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    table.setItemDelegate(EvidenceDelegate(table))
    table.verticalHeader().hide()
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.setStyleSheet(
        f'QTableView#{table.objectName()} {{ background: transparent; border: none; '
        'border-radius: 0; padding: 0; gridline-color: #d9e3e3; }'
        f'QTableView#{table.objectName()}::item {{ border-bottom: 1px solid #dce5e5; padding: 7px 9px; }}'
        f'QTableView#{table.objectName()} QHeaderView::section {{ background: #e8eeee; '
        'border: none; border-bottom: 1px solid #bdced0; padding: 8px 9px; }'
        f'QTableView#{table.objectName()} QScrollBar {{ background: #edf1f1; }}')

    def copy_selection():
        indexes=table.selectionModel().selectedIndexes() if table.selectionModel() else ()
        if not indexes:
            return
        selected={(index.row(),index.column()):('' if index.data() is None else str(index.data())) for index in indexes}
        rows=sorted({row for row,_ in selected})
        columns=sorted({column for _,column in selected})
        QApplication.clipboard().setText('\n'.join('\t'.join(selected.get((row,column),'')
            for column in columns) for row in rows))

    shortcut=QShortcut(QKeySequence(QKeySequence.StandardKey.Copy),table)
    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
    shortcut.activated.connect(copy_selection)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def context_menu(position):
        menu=QMenu(table)
        menu.addAction('Copy',copy_selection).setEnabled(bool(table.selectionModel().selectedIndexes()))
        menu.exec(table.viewport().mapToGlobal(position))

    table.customContextMenuRequested.connect(context_menu)


def fit_evidence_table(table, *, maximum=320):
    """Size wrapped saved rows after the native layout has assigned column widths."""
    if table.model() is None:
        return
    table.ensurePolished()
    table.resizeRowsToContents()
    for row in range(table.model().rowCount()):
        wrapped=0
        for column in range(table.model().columnCount()):
            if table.isColumnHidden(column):
                continue
            index=table.model().index(row,column)
            option=QStyleOptionViewItem()
            table.initViewItemOption(option)
            delegate=table.itemDelegateForColumn(column) or table.itemDelegate()
            delegate.initStyleOption(option,index)
            option.rect=QRect(0,0,table.columnWidth(column),100000)
            text_rect=table.style().subElementRect(QStyle.SubElement.SE_ItemViewItemText,option,table)
            # Native item text also reserves a focus margin inside the styled
            # text rectangle. Omitting it can turn three lines into two.
            margin=2*(table.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin,option,table)+1)
            bounds=option.fontMetrics.boundingRect(
                QRect(0,0,max(1,text_rect.width()-margin),100000),Qt.TextFlag.TextWordWrap,option.text)
            wrapped=max(wrapped,bounds.height()+option.rect.height()-text_rect.height()+1)
        table.setRowHeight(row,max(38,table.rowHeight(row),wrapped))
    height=table.horizontalHeader().sizeHint().height()+sum(table.rowHeight(row)
        for row in range(table.model().rowCount()))+2*table.frameWidth()+2
    if table.horizontalScrollBar().maximum():
        height+=table.horizontalScrollBar().sizeHint().height()
    table.setFixedHeight(min(maximum,height))


class EvidenceDelegate(QStyledItemDelegate):
    """Keep evidence headers and values on the same left alignment."""
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
