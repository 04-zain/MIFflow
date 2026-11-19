"""Select actual image channels while retaining their canonical identities."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QSize, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget, QLineEdit, QPushButton,
    QCheckBox, QGridLayout, QSizePolicy,
)

from .researcher_widgets import ContentSizedButton
from .wrapping_layout import WrappingLayout


class _ChannelButton(QPushButton):
    """Native checkable channel choice with a stable, legible multiline label."""

    def __init__(self, label, parent):
        super().__init__(parent)
        self.channel_label = label
        self.setCheckable(True)
        self.setAccessibleName(label)
        self.setIconSize(QSize(14, 14))
        self._icons = {}
        for checked in (False, True):
            pixmap = QPixmap(14, 14)
            pixmap.fill(Qt.GlobalColor.transparent)
            if checked:
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(QColor('#006970'), 1.8, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawLine(2, 7, 5, 10)
                painter.drawLine(5, 10, 12, 3)
                painter.end()
            self._icons[checked] = QIcon(pixmap)
        # Break only unusually long labels; normal channel names stay compact.
        lines, line = [], ''
        for word in label.split():
            for character in word + ' ':
                if line and self.fontMetrics().horizontalAdvance(line + character) > 220:
                    lines.append(line.rstrip())
                    line = ''
                line += character
        if line.strip():
            lines.append(line.rstrip())
        self.setText('\n'.join(lines).replace('&', '&&'))
        self.setIcon(self._icons[False])
        self.toggled.connect(lambda checked: self.setIcon(self._icons[checked]))


class _ChannelFlow(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.flow = WrappingLayout(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def fit_height(self):
        self.setMinimumHeight(self.flow.heightForWidth(max(1, self.width())))
        self.updateGeometry()


class _ChannelCheck(QCheckBox):
    def hitButton(self, position):
        return self.rect().contains(position)


class _ChannelChecklist(QWidget):
    """Plain aligned native checks, reflowed without changing their identities."""

    def __init__(self, parent, *, responsive=False):
        super().__init__(parent)
        self._responsive = responsive
        self.flow = QGridLayout(self)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(7 if responsive else 24)
        self.flow.setVerticalSpacing(5 if responsive else 0)
        self._choices = []
        self._arrangement = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def fit_height(self):
        visible = tuple(check for check in self._choices if not check.isHidden())
        minimum_column = max((check.minimumSizeHint().width() for check in visible), default=0)
        columns = 1
        if self._responsive and visible:
            gap = self.flow.horizontalSpacing()
            columns = min(4, len(visible), max(1, (self.width() + gap) // (max(135, minimum_column) + gap)))
        arrangement = columns, visible
        if arrangement != self._arrangement:
            self._arrangement = arrangement
            while self.flow.count():
                self.flow.takeAt(0)
            for index in range(4):
                self.flow.setColumnStretch(index, 1 if index < columns else 0)
            for index, check in enumerate(visible):
                self.flow.addWidget(check, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
        self.setMinimumHeight(self.flow.minimumSize().height())
        self.updateGeometry()


class ClassifierChannels(QWidget):
    """Compact channel choices retaining the exact scientific selection order."""

    changed = Signal()

    def __init__(self, choices, labels, selected=(), parent=None, *, preserve_order=False,
                 presentation='buttons'):
        super().__init__(parent)
        self._checks = {}
        self._kronos = presentation == 'kronos'
        self._checklist = self._kronos or presentation == 'checklist'
        self._preserve_order = preserve_order
        self._selection_order = []
        self.setObjectName("classifier-channel-selection")
        self.setStyleSheet(
            'QPushButton[channelChoice="true"] { padding: 8px 12px; border: 1px solid #c6d6d8; border-radius: 4px; background: white; text-align: left; }'
            'QPushButton[channelChoice="true"]:checked { background: #e5f1ee; color: #005b61; border-color: #70a8a8; }'
            'QPushButton[channelChoice="true"]:hover { border-color: #247e83; background: #f1f8f6; }'
            'QPushButton[channelChoice="true"]:focus { border: 1px dotted #173642; }'
            'QPushButton[channelBulk="true"] { border: none; background: transparent; padding: 4px 8px; color: #006970; }'
            'QPushButton[channelBulk="true"]:hover { background: #e5f1ee; }'
            'QPushButton[channelBulk="true"]:focus { border: 1px dotted #173642; }'
            'QPushButton[channelBulk="true"]:disabled { color: #7d8d92; background: transparent; }'
            'QCheckBox[channelChoice="true"] { padding: 9px 10px; border: none; border-bottom: 1px solid #dce6e2; background: white; }'
            'QCheckBox[channelChoice="true"]:checked { background: #eff5f2; }'
            'QCheckBox[channelChoice="true"]:hover { background: #e4f0eb; }'
            'QCheckBox[channelChoice="true"]:focus { border: 1px dotted #006b70; }'
        )
        if self._kronos:
            self.setStyleSheet(self.styleSheet() +
                'QCheckBox[channelChoice="true"] { padding: 6px 9px; background: white; border: 1px solid #dce4e4; border-radius: 4px; }'
                'QCheckBox[channelChoice="true"]:checked { color: #173d47; border-color: #bfd7d0; background: #edf3f1; }'
                'QCheckBox[channelChoice="true"]:hover { border-color: #70a8a8; }'
                'QCheckBox[channelChoice="true"]:focus { border: 1px dotted #006b70; }')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        toolbar = self._toolbar = QHBoxLayout()
        self.summary = QLabel(self)
        self.summary.setProperty("choiceHelp", True)
        toolbar.setSpacing(4)
        toolbar.addWidget(self.summary)
        if self._checklist:
            toolbar.addStretch()
        else:
            toolbar.addSpacing(12)
        self._bulk_buttons = []
        for text, selection in (("Select all", choices), ("Clear", ())):
            button = ContentSizedButton(text, self)
            button.setProperty("channelBulk", True)
            button.setObjectName("classifier-channels-" + text.lower().replace(" ", "-"))
            button.clicked.connect(lambda _checked=False, values=selection: self._bulk_selection(values))
            if self._checklist and len(choices) <= 4:
                button.hide()
            toolbar.addWidget(button)
            self._bulk_buttons.append(button)
        if not self._checklist:
            toolbar.addStretch()
        layout.addLayout(toolbar)
        self.search = QLineEdit(self)
        self.search.setObjectName("classifier-channel-search")
        self.search.setAccessibleName("Find image channels")
        self.search.setPlaceholderText("Find a channel…")
        self.search.setClearButtonEnabled(True)
        self.search.setVisible(len(choices) > 8)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        body = self._body = _ChannelChecklist(self, responsive=self._kronos) if self._checklist else _ChannelFlow(self)
        for channel_id in choices:
            label = labels.get(channel_id, channel_id)
            if self._checklist:
                check = _ChannelCheck(body)
                check.channel_label = label
                check.setAccessibleName(label)
                lines, line = [], ''
                for character in label:
                    if line and check.fontMetrics().horizontalAdvance(line + character) > 220:
                        lines.append(line.rstrip())
                        line = ''
                    line += character
                check.setText('\n'.join([*lines, line]).replace('&', '&&'))
                body._choices.append(check)
            else:
                check = _ChannelButton(label, body)
            check.setObjectName("classifier-channel-" + channel_id)
            check.setProperty("channelChoice", True)
            check.setToolTip(labels.get(channel_id, channel_id) + '\n' + channel_id)
            check.toggled.connect(self._changed)
            if not self._checklist:
                body.flow.addWidget(check)
            self._checks[channel_id] = check
        self._scroll = None
        if len(choices) > 8:
            scroll = self._scroll = QScrollArea(self)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(body)
            scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }')
            layout.addWidget(scroll)
        else:
            layout.addWidget(body)
        self.empty = QLabel("No channels match this search.", self)
        self.empty.setProperty("fieldHelp", True)
        self.empty.hide()
        layout.addWidget(self.empty)
        if self._checks:
            self.setFocusProxy(next(iter(self._checks.values())))
        self.set_value(selected)
        if self._checklist:
            body.fit_height()

    def set_heading(self, caption):
        """Keep the form caption, live count and selection actions on one row."""
        if self._checklist and self._toolbar.indexOf(caption) < 0:
            caption.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            self._toolbar.insertWidget(0, caption)
            self._toolbar.insertSpacing(1, 10)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_choices)

    def _fit_choices(self):
        self._body.fit_height()
        if self._scroll is not None:
            width = max(1, self._scroll.viewport().width())
            height = (self._body.flow.minimumSize().height() if self._checklist
                      else self._body.flow.heightForWidth(width))
            self._scroll.setFixedHeight(min(176 if self._kronos else 320, height + 4))

    def value(self) -> list[str]:
        """Return exact saved order when the encoder depends on channel order."""
        selected = [channel_id for channel_id, check in self._checks.items() if check.isChecked()]
        return [channel for channel in self._selection_order if channel in selected] if self._preserve_order else selected

    def set_value(self, selected) -> None:
        """Restore this form's choices without changing any channel identities."""
        ordered = list(dict.fromkeys(selected or ()))
        self._selection_order = [channel for channel in ordered if channel in self._checks]
        selected = set(ordered)
        for channel_id, check in self._checks.items():
            with QSignalBlocker(check):
                check.setChecked(channel_id in selected)
                if not self._checklist:
                    check.setIcon(check._icons[channel_id in selected])
        self._changed()

    def _changed(self):
        selected = [channel for channel, check in self._checks.items() if check.isChecked()]
        self._selection_order = [channel for channel in self._selection_order if channel in selected]
        self._selection_order.extend(channel for channel in selected if channel not in self._selection_order)
        self.summary.setText(f"{len(selected)} of {len(self._checks)} selected")
        if self._checklist:
            self._update_actions()
        self.changed.emit()

    def _update_actions(self):
        filtered = bool(self.search.text().strip())
        shown = [check for check in self._checks.values() if not check.isHidden()]
        select, clear = self._bulk_buttons
        select.setText("Select shown" if filtered else "Select all")
        clear.setText("Clear shown" if filtered else "Clear")
        select.setEnabled(any(not check.isChecked() for check in shown))
        clear.setEnabled(any(check.isChecked() for check in shown))

    def _bulk_selection(self, values):
        if self._checklist and self.search.text().strip():
            shown = [channel for channel, check in self._checks.items() if not check.isHidden()]
            current = self.value()
            selection = ([*current, *(channel for channel in shown if channel not in current)]
                         if values else [channel for channel in current if channel not in shown])
            self.set_value(selection)
            return
        if self._preserve_order and values:
            current = self.value()
            values = [*current, *(channel for channel in values if channel not in current)]
        self.set_value(values)

    def _filter(self, text):
        """Filter display only; selections and saved encoder order stay intact."""
        text = text.strip().casefold()
        if not self._checklist:
            while self._body.flow.count():
                self._body.flow.takeAt(0)
        visible = 0
        for channel, check in self._checks.items():
            matches = not text or text in check.channel_label.casefold() or text in channel.casefold()
            check.setVisible(matches)
            if matches and not self._checklist:
                self._body.flow.addWidget(check)
            visible += matches
        self.empty.setVisible(not visible)
        if self._checklist:
            self._update_actions()
        self._body.flow.invalidate()
        self._fit_choices()
