"""Shared ordered input cards for measured-cell feature selectors."""

from PySide6.QtCore import QEvent, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)


class SelectedMeasurements(QFrame):
    remove_requested = Signal(str)

    def __init__(self, captions, parent=None):
        super().__init__(parent)
        self._captions = captions
        self._chips = []
        self._columns = 0
        self._layout_pending = False
        self.maximum_list_height = 340
        self.setObjectName("selected-measurement-inputs")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setStyleSheet("QFrame#selected-measurement-inputs { background:#f1f6f4; border:1px solid #dbe7e2; border-radius:8px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary = QLabel(self)
        self.summary.setObjectName("selected-measurement-count")
        self.summary.setStyleSheet("font-weight:600; color:#173d47;")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.scroll = QScrollArea(self)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget(self.scroll)
        self.body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(6)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body)
        self.scroll.viewport().installEventFilter(self)
        layout.addWidget(self.scroll)
        self.empty = QLabel(self)
        self.empty.setWordWrap(True)
        self.empty.setProperty("fieldHelp", True)
        layout.addWidget(self.empty)

    def set_value(self, selected):
        for chip in self._chips:
            self.grid.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        self._chips = []
        for key in selected:
            caption, tooltip = self._captions[key]
            chip = QFrame(self.body)
            chip.setObjectName("selected-measurement-chip")
            chip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            chip.setStyleSheet("QFrame#selected-measurement-chip { background:white; border:1px solid #cbded5; border-radius:6px; }")
            row = QHBoxLayout(chip)
            row.setContentsMargins(8, 5, 4, 5)
            row.setSpacing(4)
            label = QLabel(caption, chip)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setToolTip(tooltip + "\n" + key)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(label, 1)
            remove = QToolButton(chip)
            remove.setText("×")
            remove.setObjectName("selected-measurement-remove-" + key)
            remove.setAccessibleName("Remove " + caption.replace("\n", " · "))
            remove.setToolTip(remove.accessibleName())
            remove.setStyleSheet("QToolButton { padding:2px 4px; font-size:18px; border:none; background:transparent; }")
            remove.clicked.connect(lambda _checked=False, feature_id=key: self.remove_requested.emit(feature_id))
            row.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)
            self._chips.append(chip)
            chip.show()
        count = len(selected)
        self.summary.setText(f"Inputs for each cell · {count:,} measurement{'s' if count != 1 else ''}")
        self.empty.setVisible(not count)
        self.scroll.setVisible(bool(count))
        self.fit_cards()

    def fit_cards(self):
        self._layout_pending = False
        width = max(1, self.scroll.viewport().width() - 4)
        columns = max(1, min(4, (width + 8) // 218))
        for column in range(max(columns, self._columns)):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        self._columns = columns
        for index, chip in enumerate(self._chips):
            self.grid.addWidget(chip, index // columns, index % columns)
        rows = (len(self._chips) + columns - 1) // columns
        chip_width = max(1, (width - (columns - 1) * 8) // columns)
        height = max((max(chip.minimumSizeHint().height(), chip.layout().totalHeightForWidth(chip_width))
                      for chip in self._chips), default=48)
        self.scroll.setFixedHeight(min(self.maximum_list_height, max(0, rows * (height + 6) - 6) + 2))

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize and not self._layout_pending:
            self._layout_pending = True
            QTimer.singleShot(0, self, self.fit_cards)
        return super().eventFilter(watched, event)


__all__ = ["SelectedMeasurements"]
