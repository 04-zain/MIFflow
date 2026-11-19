"""Shared affected-results page for completed-step revisions."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QPushButton, QSizePolicy
from .revision_summary import RevisionSummary


class RevisionReview(QWidget):
    """Display an existing preview with fixed return and application actions.

    The owning editor supplies the preview and connects these buttons to its
    existing handlers. This page does not parse choices or submit revisions.
    """

    def __init__(self, parent=None, *, review_only=False):
        super().__init__(parent)
        self._review_only = review_only
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 2, 8, 4)
        content.setSpacing(14)
        content.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary = RevisionSummary(body)
        self.summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.summary.hide()
        content.addWidget(self.summary)
        self.notice = QLabel("Checking the affected results…", body)
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setProperty("choiceHelp", True)
        content.addWidget(self.notice)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 8, 0, 0)
        actions.setSpacing(8)
        self.back = QPushButton("Back to choices", self)
        self.back.setVisible(not review_only)
        actions.addWidget(self.back)
        self.cancel = QPushButton("Cancel", self)
        actions.addWidget(self.cancel)
        self.details = QPushButton("Error details", self)
        self.details.hide()
        actions.addWidget(self.details)
        self.retry = QPushButton("Review again", self)
        self.retry.hide()
        actions.addWidget(self.retry)
        actions.addStretch(1)
        self.apply = QPushButton("Apply change", self)
        self.apply.setProperty("actionRole", "primary")
        self.apply.setEnabled(False)
        actions.addWidget(self.apply)
        layout.addLayout(actions)

    def show_preview(self, preview, nodes, current, proposed):
        """Show the supplied choices and consequences without changing their values."""
        self.summary.set_choices(current, proposed, compare=not self._review_only)
        self.summary.set_preview(preview, nodes)
        self.summary.show()
        self.set_notice(preview.blocked_reason or "")
        self.apply.setEnabled(preview.blocked_reason is None)

    def set_notice(self, message):
        """Show a current blocker, error or progress message when one is present."""
        self.notice.setText(message)
        self.notice.setVisible(bool(message))

    def clear_preview(self):
        """Remove obsolete consequences until the owner supplies a fresh preview."""
        self.summary.clear_preview()
        self.summary.hide()
        self.apply.setEnabled(False)
        self.details.hide()
        self.retry.hide()
