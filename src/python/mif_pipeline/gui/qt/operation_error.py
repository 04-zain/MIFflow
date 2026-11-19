"""Compact error dialog with literal, copyable diagnostics and explicit recovery."""
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)


class OperationErrorDialog(QDialog):
    """Explain one failed action without discarding the underlying form or error."""

    def __init__(self, parent: QWidget, summary: str, diagnostic: str) -> None:
        super().__init__(parent)
        self.setObjectName("operation-error")
        self.setWindowTitle("Action needs attention")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet("QDialog#operation-error { background: #ffffff; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        caption = QLabel("ACTION STOPPED", self)
        caption.setProperty("supportingText", True)
        layout.addWidget(caption)
        heading = QLabel("This action needs attention", self)
        heading.setProperty("dialogHeading", True)
        layout.addWidget(heading)
        message = QLabel(summary, self)
        message.setObjectName("operation-error-summary")
        message.setWordWrap(True)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        viewport = QScrollArea(self)
        viewport.setWidgetResizable(True)
        viewport.setWidget(message)
        viewport.setStyleSheet("QScrollArea, QScrollArea > QWidget, QScrollArea QLabel { background: #ffffff; border: none; }")
        width = max(360, min(620, parent.width() - 48, self.screen().availableGeometry().width() - 48))
        viewport.setFixedHeight(min(210, max(64, message.heightForWidth(width - 60) + 8)))
        layout.addWidget(viewport)
        self._toggle = QPushButton("Show details", self)
        self._toggle.setObjectName("operation-error-toggle")
        self._toggle.setCheckable(True)
        self._toggle.setAutoDefault(False)
        self._toggle.setAccessibleDescription("Expand the original error and technical context.")
        layout.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._details_section = QWidget(self)
        details_layout = QVBoxLayout(self._details_section)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        self._details = QPlainTextEdit(self._details_section)
        self._details.setObjectName("operation-error-details")
        self._details.setAccessibleName("Original error and technical details")
        self._details.setReadOnly(True)
        self._details.setPlainText(diagnostic)
        self._details.setMinimumHeight(160)
        self._details.setMaximumHeight(240)
        details_layout.addWidget(self._details)
        self._details_section.hide()
        layout.addWidget(self._details_section)
        self._toggle.toggled.connect(self._toggle_details)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        copy = QPushButton("Copy details", self._details_section)
        copy.setObjectName("operation-error-copy")
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._details.toPlainText()))
        details_layout.addWidget(copy, 0, Qt.AlignmentFlag.AlignRight)
        actions.addStretch(1)
        self._close = QPushButton("Close", self)
        self._close.setObjectName("operation-error-close")
        self._close.clicked.connect(self.reject)
        self._close.setDefault(True)
        actions.addWidget(self._close)
        layout.addLayout(actions)
        self._actions = actions
        self.resize(width, self.sizeHint().height())
        self._close.setFocus()

    def add_recovery_action(self, label: str, callback: Callable[[], None]) -> None:
        """Use an existing page action; its owner rechecks authority when invoked."""
        button = QPushButton(label, self)
        button.setObjectName("operation-error-recovery")
        button.setProperty("actionRole", "primary")
        button.setAutoDefault(False)

        def choose() -> None:
            self.accept()
            callback()

        button.clicked.connect(choose)
        self._actions.addWidget(button)

    def _toggle_details(self, expanded: bool) -> None:
        self._details_section.setVisible(expanded)
        self._toggle.setText("Hide details" if expanded else "Show details")
        self.layout().activate()
        height = min(self.sizeHint().height(), self.screen().availableGeometry().height() - 64)
        self.resize(self.width(), height)


__all__ = ["OperationErrorDialog"]
