"""Software diagnostics without expanding or shifting the setup page."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QToolButton, QVBoxLayout


def software_details_button(parent):
    """A consistent text link to the component's copyable diagnostic report."""
    button = QToolButton(parent)
    button.setText('Details')
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setArrowType(Qt.ArrowType.RightArrow)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet('''
        QToolButton { color: #176a9a; background: transparent;
            border: 1px solid transparent; border-radius: 4px; padding: 6px 2px; }
        QToolButton:hover { color: #0c507b; text-decoration: underline; }
        QToolButton:focus { border-color: #176a9a; }
        QToolButton:disabled { color: #7d8d92; }
    ''')
    return button


def open_software_details(parent, title, summary, details):
    dialog = QDialog(parent)
    dialog.setObjectName('software-details-dialog')
    dialog.setWindowTitle(title)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(720, 480)
    body = QVBoxLayout(dialog)
    body.setContentsMargins(20, 18, 20, 18)
    body.setSpacing(12)
    heading = QLabel(title, dialog)
    heading.setProperty('panelHeading', True)
    heading.setWordWrap(True)
    body.addWidget(heading)
    explanation = QLabel(summary, dialog)
    explanation.setTextFormat(Qt.TextFormat.PlainText)
    explanation.setWordWrap(True)
    body.addWidget(explanation)
    report = QPlainTextEdit(dialog)
    report.setObjectName('software-details-text')
    report.setAccessibleName(title + ' diagnostic report')
    report.setReadOnly(True)
    report.setPlainText(details)
    body.addWidget(report, 1)
    actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
    copy = actions.addButton('Copy details', QDialogButtonBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(title+'\n\n'+summary+'\n\n'+report.toPlainText()))
    actions.rejected.connect(dialog.reject)
    body.addWidget(actions)
    dialog.open()
    return dialog
