"""Highlight exceeded primary count limits in the existing read-only table."""
from collections import Counter

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QStyledItemDelegate, QVBoxLayout, QWidget,
)
from .researcher_widgets import ContentSizedButton
from .workbench import style_scientific_table

from .models import FormTableModel
from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope


def pending_count_choices(groups):
    """Pair each pending decision with its image and the count that triggered review."""
    choices = []
    for group in groups:
        for target in group.targets:
            context = resolve_resume_envelope(target.stage_result.resume_context).service_context
            counts = context["primary_count_outputs"]
            limits = [row for row in counts if row["maximum"] is not None and row["observed_count"] > row["maximum"]]
            row = next((r for r in limits if r["output_id"] != "primary_classifier"), limits[0])
            key = row["output_id"]
            name = "Total regions" if key == "primary_classifier" else context.get("primary_class_labels", {}).get(key, key)
            image = target.slide_id if target.slide_id == target.sample_id else f"{target.sample_id} · {target.slide_id}"
            label = f"{image} · {name} {row['observed_count']} (maximum {row['maximum']})"
            choices.append((label, group.group_id, target.target_id))
    choices.sort()
    # Two areas in one slide can exceed exactly the same limit by the same amount.
    totals = Counter(row[0] for row in choices)
    seen = Counter()
    for label, group_id, target_id in choices:
        seen[label] += 1
        if totals[label] > 1:
            label = f"{label} · review {seen[label]} of {totals[label]}"
        yield label, group_id, target_id


class CountRegionSelection(QWidget):
    inspected = Signal(str, str)
    decided = Signal(str)
    open_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        title = QLabel("Analysis regions above a maximum", self)
        title.setProperty("panelHeading", True)
        heading.addWidget(title)
        heading.addStretch()
        layout.addLayout(heading)
        help_text = QLabel("Select a count to inspect the corresponding outlines in QuPath. Choose whether to keep or exclude each analysis region.", self)
        help_text.setWordWrap(True)
        help_text.setProperty("fieldHelp", True)
        layout.addWidget(help_text)
        self.table = QTableWidget(self)
        self.table.setAccessibleName(title.text())
        style_scientific_table(self.table)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setStyleSheet('QTableWidget { border: none; background: transparent; }'
            'QTableWidget::item { padding: 7px; border-bottom: 1px solid #d5dfde; }'
            'QTableWidget::item:selected { background: #dceee8; color: #164b48; }'
            'QHeaderView::section { padding: 7px; border: none; border-bottom: 1px solid #92aaa5; }')
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.selection = QLabel(self)
        self.inspect = ContentSizedButton("Inspect in QuPath", self)
        self.inspect.clicked.connect(self.open_requested)
        actions.addWidget(self.inspect)
        actions.addSpacing(10)
        actions.addWidget(self.selection)
        actions.addStretch()
        self.keep = ContentSizedButton("Keep selected", self)
        self.exclude = ContentSizedButton("Exclude selected", self)
        for button, action in ((self.keep, "accept_all_primary_structures"), (self.exclude, "exclude_analysis_domain")):
            actions.addWidget(button)
            button.clicked.connect(lambda checked=False, value=action: self.decided.emit(value))
        layout.addLayout(actions)
        self.table.currentCellChanged.connect(self._inspect)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.entries = []

    def set_entries(self, groups):
        previous = set(self.selected_ids())
        self.entries = list(pending_count_choices(groups))
        targets = {target.target_id: target for group in groups for target in group.targets}
        areas_per_image = Counter((t.sample_id, t.slide_id) for t in targets.values())
        seen = Counter()
        contexts = {key: resolve_resume_envelope(t.stage_result.resume_context).service_context
                    for key, t in targets.items()}
        def column_key(context, output):
            key = output["output_id"]
            return ("Total regions" if key == "primary_classifier" else
                    context.get("primary_class_labels", {}).get(key, key), key)
        columns = sorted({column_key(ctx, output) for ctx in contexts.values()
                          for output in ctx["primary_count_outputs"]},
                         key=lambda c: (c[1] != "primary_classifier", c[0], c[1]))
        with QSignalBlocker(self.table):
            self.table.clear()
            self.table.setColumnCount(len(columns) + 2)
            self.table.setHorizontalHeaderLabels(["Image and region"] + [label for label, _ in columns] + ["Decision"])
            self.table.setRowCount(len(self.entries))
            for row, (_, group, ident) in enumerate(self.entries):
                target = targets[ident]
                source = target.slide_id if target.sample_id == target.slide_id else f"{target.sample_id} · {target.slide_id}"
                ctx = contexts[ident]
                key = (target.sample_id, target.slide_id)
                seen[key] += 1
                image = f"{source} · Area {seen[key]}" if areas_per_image[key] > 1 else source
                name = QTableWidgetItem(image)
                name.setToolTip(f"{source} · Section {ctx.get('section_id')} · Region {ctx.get('analysis_domain_id')}")
                self.table.setItem(row, 0, name)
                by_class = {column_key(ctx, out): out for out in ctx["primary_count_outputs"]}
                for col, identity in enumerate(columns, start=1):
                    output = by_class.get(identity)
                    item = QTableWidgetItem("—")
                    if output is not None:
                        n, limit = output["observed_count"], output["maximum"]
                        item.setText(str(n) if limit is None else f"{n}   (max {limit})")
                        item.setData(Qt.ItemDataRole.UserRole, identity[1])
                        if limit is not None and n > limit:
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
                            item.setForeground(QColor("#784a18"))
                            item.setBackground(QColor("#fff1da"))
                        item.setToolTip(f"{image} · {identity[0]} · {n} detected" +
                                        (f" · maximum {limit}" if limit is not None else ""))
                    self.table.setItem(row, col, item)
                column = len(columns) + 1
                self.table.setItem(row, column, QTableWidgetItem())
                choice = QComboBox(self.table)
                choice.setAccessibleName(f"Decision for {image}")
                choice.setStyleSheet("QComboBox { border: none; background: transparent; padding-left: 6px; }")
                choice.addItem("Choose…", None)
                choice.addItem("Keep region", "accept_all_primary_structures")
                choice.addItem("Exclude region", "exclude_analysis_domain")
                choice.activated.connect(lambda index, i=row, combo=choice: self._decide_row(i, combo, index))
                self.table.setCellWidget(row, column, choice)
                if ident in previous:
                    for col in range(self.table.columnCount()):
                        self.table.item(row, col).setSelected(True)
            header = self.table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            required = sum(self.table.sizeHintForColumn(i) if i == 0 else
                           max(self.table.sizeHintForColumn(i), header.sectionSizeHint(i))
                           for i in range(len(columns) + 1))
            if len(columns) <= 3 and required <= self.window().width():
                header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setMinimumSectionSize(105)
            header.setSectionResizeMode(len(columns) + 1, QHeaderView.ResizeMode.ResizeToContents)
        height = self.table.horizontalHeader().sizeHint().height() + 42 * min(12, len(self.entries))
        self.table.setFixedHeight(height + self.table.horizontalScrollBar().sizeHint().height() + 4)
        self._selection_changed()

    def focus_target(self, ident):
        row = next((i for i, entry in enumerate(self.entries) if entry[2] == ident), -1)
        if row >= 0 and self.table.currentRow() != row:
            with QSignalBlocker(self.table):
                if self.table.item(row, 0).isSelected():
                    self.table.setCurrentItem(self.table.item(row, 0), self.table.selectionModel().SelectionFlag.NoUpdate)
                else:
                    self.table.setCurrentCell(row, 0)
        self._selection_changed()

    def selected_ids(self):
        return tuple(self.entries[index.row()][2] for index in self.table.selectionModel().selectedRows())

    def _selection_changed(self):
        count = len(self.selected_ids())
        self.selection.setVisible(count > 1)
        self.selection.setText(f"{count} regions selected")
        for button in (self.keep, self.exclude):
            button.setVisible(count > 1)

    def _decide_row(self, row, combo, index):
        action = combo.itemData(index)
        if action is None:
            return
        combo.setCurrentIndex(0)
        self.table.clearSelection()
        self.table.setCurrentCell(row, 0)
        self.table.selectRow(row)
        self.decided.emit(action)

    def inspection_class(self):
        item = self.table.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _inspect(self, row, *_):
        if row >= 0:
            _, group, ident = self.entries[row]
            self.inspected.emit(group, ident)


class PrimaryCountReviewModel(FormTableModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and 0 <= index.row() < len(self._rows):
            _, detected, maximum = self._rows[index.row()]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            if isinstance(maximum, int) and detected > maximum and index.column() == 1:
                if role in (Qt.ItemDataRole.AccessibleDescriptionRole, Qt.ItemDataRole.ToolTipRole):
                    return f"{detected} regions detected, maximum {maximum}"
                if role == Qt.ItemDataRole.FontRole:
                    font = QFont()
                    font.setBold(True)
                    return font
                if role == Qt.ItemDataRole.BackgroundRole:
                    return QColor("#fff2d5")
                if role == Qt.ItemDataRole.ForegroundRole:
                    return QColor("#78530b")
        return super().data(index, role)


class _CountComparison(QStyledItemDelegate):
    """Place the observed count beside a common linear scale and mark its limit."""

    def __init__(self, table, parent):
        super().__init__(parent)
        self.scale = max(1, *(v for row in table.rows for v in row.values[1:] if isinstance(v, int)))
        self.digits = parent.fontMetrics().horizontalAdvance(str(self.scale))

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        return QSize(max(hint.width(), self.digits + 114), hint.height())

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        count = index.data(Qt.ItemDataRole.UserRole)
        limit = index.siblingAtColumn(2).data(Qt.ItemDataRole.UserRole)
        left = option.rect.left() + self.digits + 24
        width = option.rect.right() - left - 12
        if width < 28 or not isinstance(count, int):
            return
        y = option.rect.center().y()
        painter.save()
        painter.fillRect(left, y - 2, width, 4, QColor("#d6e2df"))
        extent = round(count * width / self.scale)
        painter.fillRect(left, y - 2, extent, 4, QColor("#397d79"))
        if isinstance(limit, int):
            tick = round(limit * width / self.scale)
            if extent > tick:
                painter.fillRect(left + tick, y - 2, extent - tick, 4, QColor("#ad7128"))
            painter.fillRect(left + tick, y - 5, 1, 10, QColor("#294c4e"))
        painter.restore()


def prepare_count_table(view, table):
    """Focus the first class requiring inspection and keep the other counts in view."""
    from PySide6.QtWidgets import QHeaderView
    view.setSelectionBehavior(view.SelectionBehavior.SelectRows)
    view.setSelectionMode(view.SelectionMode.SingleSelection)
    view.setAlternatingRowColors(False)
    view.setStyleSheet(
        'QTableView[scientificTable="true"] { background: transparent; border: none; '
        'border-top: 1px solid #a6bcb9; border-bottom: 1px solid #a6bcb9; padding: 0; }'
        'QTableView::item { border-bottom: 1px solid #d4dedd; padding: 5px 10px; }'
        'QTableView::item:selected { background: #dceee8; color: #17474a; }'
        'QHeaderView::section { border: none; border-bottom: 1px solid #90aaa6; padding: 6px 10px; }')
    header = view.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in (1, 2):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    view.setItemDelegateForColumn(1, _CountComparison(table, view))
    first_class = next((i for i, row in enumerate(table.rows)
                        if row.row_id != 'primary_classifier' and isinstance(row.values[2], int)
                        and row.values[1] > row.values[2]), 0)
    view.selectRow(first_class)
