"""Open the input, training and reference pages for each structure classifier."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .researcher_widgets import ContentSizedButton


class SecondaryClassifierPreparations(QWidget):
    open_requested = Signal(int, str)

    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self.setObjectName("secondary-classifier-overview")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.summaries = []
        self.issues = []
        self.actions = {}
        for index, names in enumerate(groups):
            card = QFrame(self)
            card.setObjectName("secondary-classifier-preparation-card")
            card.setStyleSheet(
                "QFrame#secondary-classifier-preparation-card { background: white; "
                "border: 1px solid #ccdcd7; border-radius: 4px; }")
            body = QVBoxLayout(card)
            body.setContentsMargins(16, 14, 16, 14)
            body.setSpacing(7)
            label = names[0] if len(names) == 1 else f"Classifier {index + 1} · {len(names)} structures"
            title = QLabel(label, card)
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setProperty("panelHeading", True)
            title.setWordWrap(True)
            title.setToolTip(", ".join(names))
            title.setAccessibleName(", ".join(names))
            body.addWidget(title)
            summary = QLabel(card)
            summary.setProperty("supportingText", True)
            summary.setTextFormat(Qt.TextFormat.PlainText)
            summary.setWordWrap(True)
            body.addWidget(summary)
            self.summaries.append(summary)
            path = QHBoxLayout()
            path.setContentsMargins(0, 4, 0, 0)
            path.setSpacing(9)
            for position, (section, caption, help_text) in enumerate((
                ("inputs", "Inputs", "Choose image channels, the classifier and image features."),
                ("training", "Subjects", "Allocate reference subjects and review validation folds."),
                ("references", "References", "Open images and annotate references for this classifier."),
            )):
                if position:
                    connector = QFrame(card)
                    connector.setFixedSize(14, 1)
                    connector.setStyleSheet("background: #a7c6bf; border: none;")
                    path.addWidget(connector)
                button = ContentSizedButton(caption, card)
                button.setProperty("actionRole", "navigation")
                button.setObjectName(f"secondary-classifier-{index}-{section}")
                button.setAccessibleName(f"{caption} for {', '.join(names)}")
                button.setToolTip(help_text)
                button.clicked.connect(lambda checked=False, i=index, key=section:
                                       self.open_requested.emit(i, key))
                path.addWidget(button)
                self.actions[(index, section)] = button
            path.addStretch()
            body.addLayout(path)
            issue = QLabel(card)
            issue.setProperty("validationError", True)
            issue.setTextFormat(Qt.TextFormat.PlainText)
            issue.setWordWrap(True)
            issue.hide()
            body.addWidget(issue)
            self.issues.append(issue)
            layout.addWidget(card)

    def set_state(self, index, *, method, channel_count, issue=""):
        channels = f"{channel_count} channel{'s' if channel_count != 1 else ''} selected"
        self.summaries[index].setText(f"{method} · {channels}")
        self.issues[index].setText(issue)
        self.issues[index].setVisible(bool(issue))
