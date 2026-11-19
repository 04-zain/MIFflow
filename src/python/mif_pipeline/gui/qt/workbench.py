"""Small shared presentation pieces for the six-route Qt workbench."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QTableView, QVBoxLayout, QWidget

# ==============================================================================
#                                   Workbench
# ==============================================================================


def _fit_bounded_table_height(table: QTableView) -> None:
    """Keep a bounded evidence table's first row legible at the current font."""

    table.resizeRowsToContents()
    height = (
        table.horizontalHeader().sizeHint().height()
        + sum(table.rowHeight(row) for row in range(table.model().rowCount()))
        + table.horizontalScrollBar().sizeHint().height()
        + 2 * table.frameWidth()
        + 8
    )
    table.setFixedHeight(min(280, height))


class PageHeading(QWidget):
    """A route heading and one accessible, wrapping summary."""

    def __init__(
        self,
        route_id: str,
        title: str,
        summary: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("page-heading")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        heading = QLabel(title, self)
        heading.setObjectName(f"{route_id}-title")
        heading.setAccessibleName(f"{title} heading")
        heading.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        information = QLabel(summary, self)
        information.setObjectName(f"{route_id}-information")
        information.setAccessibleName(summary)
        information.setWordWrap(True)
        information.setVisible(bool(summary))
        layout.addWidget(heading)
        layout.addWidget(information)


__all__ = ["PageHeading"]


def style_scientific_table(table: QTableView) -> None:
    """Apply a restrained research-table treatment without changing its model."""
    from PySide6.QtWidgets import QHeaderView

    table.setProperty("scientificTable", True)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(max(38, table.fontMetrics().lineSpacing() + 18))
    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    table.horizontalHeader().setMinimumSectionSize(75)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    table.setWordWrap(False)


def fit_scientific_columns(table: QTableView) -> None:
    """Bound automatic widths; exact cell content remains available by tooltip/copy."""
    if table.model() is None:
        return
    table.resizeColumnsToContents()
    for column in range(table.model().columnCount()):
        if not table.isColumnHidden(column):
            table.setColumnWidth(column, min(360, max(110, table.columnWidth(column))))
