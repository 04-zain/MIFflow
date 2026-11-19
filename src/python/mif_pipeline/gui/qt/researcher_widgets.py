"""Small presentation components shared by scientific workbench pages.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ==============================================================================
#                               Researcher widgets
# ==============================================================================


class HelpDisclosure(QWidget):
    def __init__(self, title: str, text: str = "", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.toggle = QToolButton(self)
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setProperty("researchDisclosure", True)
        self.body = QLabel(text, self)
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setProperty("helpBody", True)
        self.body.hide()
        self.toggle.toggled.connect(self._toggle)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)

    def _toggle(self, opened):
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.body.setVisible(opened)

    def set_text(self, text):
        self.body.setText(text)
        self.toggle.setChecked(False)
        self.setVisible(bool(text))


class SampleScopeStrip(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stage-sample-context")
        self._samples = ()
        self._dialog = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(14)
        self.summary = QLabel(self)
        self.summary.setObjectName("stage-sample-summary")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton("View all samples", self)
        self.button.setObjectName("stage-view-samples")
        self.button.setProperty("actionRole", "secondary")
        self.button.clicked.connect(self._show_samples)
        layout.addWidget(self.summary, 1)
        layout.addWidget(self.button)

    def set_samples(self, samples):
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None
        self._samples = tuple(samples)
        if len(samples) == 1:
            text = "Sample  ·  " + samples[0]
        elif len(samples) <= 3:
            text = f"{len(samples)} samples  ·  " + ", ".join(samples)
        else:
            text = f"{len(samples)} samples in this step"
        self.summary.setText(text if samples else "No samples are included in this step.")
        self.button.setVisible(len(samples) > 3)
        self.summary.setAccessibleName(text)

    def _show_samples(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Samples in this step")
        dialog.resize(520, 400)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialog = dialog

        def clear_dialog():
            if self._dialog is dialog:
                self._dialog = None

        dialog.destroyed.connect(clear_dialog)
        layout = QVBoxLayout(dialog)
        search = QLineEdit(dialog)
        search.setPlaceholderText("Find a sample")
        search.setAccessibleName("Find a sample")
        listing = QListWidget(dialog)
        listing.setObjectName("stage-sample-list")
        listing.addItems(self._samples)

        def filter_samples(text):
            for row in range(listing.count()):
                item = listing.item(row)
                item.setHidden(text.casefold() not in item.text().casefold())

        search.textChanged.connect(filter_samples)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(search)
        layout.addWidget(listing, 1)
        layout.addWidget(buttons)
        dialog.open()


class ContentSizedButton(QPushButton):
    """Keep native text, padding and focus borders inside the allocated action."""

    def _refresh_minimum(self):
        if getattr(self, "_sizing_content", False):
            return
        self._sizing_content = True
        try:
            self.ensurePolished()
            from PySide6.QtCore import QSize

            hint = super().sizeHint() + QSize(4, 4)
            if hint.isValid() and hint != self.minimumSize():
                self.setMinimumSize(hint)
        finally:
            self._sizing_content = False

    def showEvent(self, event):
        self._refresh_minimum()
        super().showEvent(event)

    def changeEvent(self, event):
        from PySide6.QtCore import QEvent

        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self._refresh_minimum()

    def setText(self, text):
        super().setText(text)
        self._refresh_minimum()


class ElidedComboBox(QComboBox):
    """Fit the selected label while retaining full choices and the native arrow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumContentsLength(1)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.currentTextChanged.connect(self.setToolTip)

    def paintEvent(self, event):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        area = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, max(0, area.width() - 4))
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


def configure_disclosure(button: QToolButton) -> None:
    """Use one visible expansion affordance for advanced form settings."""
    if button.property("researchDisclosure"):
        return
    button.setProperty("researchDisclosure", True)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    def update(checked: bool) -> None:
        button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    button.toggled.connect(update)
    update(button.isChecked())
