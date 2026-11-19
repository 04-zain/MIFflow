"""Wrapping layout for inline choices and marker conditions."""
from PySide6.QtCore import Qt, QSize, QRect
from PySide6.QtWidgets import QLayout, QStyle, QStyleOptionButton


class WrappingLayout(QLayout):
    """Wrap content-sized controls without splitting a control across rows."""

    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, max(1, width), 0), False)

    def minimumSize(self):
        return QSize(0, max((item.sizeHint().height() for item in self._items), default=0))

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            widget = item.widget()
            navigation = getattr(widget, "navigation_button", widget)
            full_name = getattr(navigation, "full_name", None)
            natural_width = item.sizeHint().width()
            text_padding = 0
            if full_name is not None:
                # Include the styled button padding, menu and frame in the
                # width; these vary with the font and display scale.
                metrics = navigation.fontMetrics()
                option = QStyleOptionButton()
                navigation.initStyleOption(option)
                option.text = full_name
                full_size = navigation.style().sizeFromContents(
                    QStyle.ContentsType.CT_PushButton, option,
                    metrics.size(Qt.TextFlag.TextShowMnemonic, full_name), navigation)
                outside = 0
                if navigation is not widget:
                    layout = widget.layout()
                    margins = layout.contentsMargins()
                    frame = widget.contentsMargins()
                    outside = margins.left() + margins.right() + frame.left() + frame.right()
                    outside += max(0, layout.spacing()) * max(0, layout.count() - 1)
                    outside += sum(layout.itemAt(i).sizeHint().width() for i in range(layout.count())
                                   if layout.itemAt(i).widget() is not navigation)
                natural_width = full_size.width() + outside + 4
                text_padding = natural_width - metrics.horizontalAdvance(full_name) - 4
            width = min(max(1, rect.width()), natural_width)
            height = item.sizeHint().height()
            if x > rect.x() and x + width > rect.x() + rect.width():
                x, y, line_height = rect.x(), y + line_height + self.spacing(), 0
            if apply:
                if full_name is not None:
                    text = navigation.fontMetrics().elidedText(full_name, Qt.TextElideMode.ElideRight, max(1, width - text_padding))
                    if navigation.text() != text:
                        navigation.setText(text)
                item.setGeometry(QRect(x, y, width, height))
            x += width + self.spacing()
            line_height = max(line_height, height)
        return y + line_height - rect.y()

