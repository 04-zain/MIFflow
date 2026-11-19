"""Progressive disclosure for explicitly configured reference fitting values."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLabel, QToolButton, QSizePolicy
from .researcher_widgets import configure_disclosure

# ==============================================================================
#                               Reference settings
# ==============================================================================


class ReferenceSettingsPanel(QWidget):
    def __init__(self, rows, groups, parent=None):
        """Group existing reference controls below one advanced disclosure.

        Multiple control groups keep their supplied headings. Every control
        keeps its original editor, and validation can reveal that area through
        the field-to-toggle mapping.
        """
        super().__init__(parent)
        self.setObjectName("reference-settings-panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self._toggle_by_field = {}
        sections = {}
        toggle = QToolButton(self)
        toggle.setText("Advanced settings")
        toggle.setCheckable(True)
        toggle.setObjectName("reference-settings-advanced")
        self._advanced_toggle = toggle
        configure_disclosure(toggle)
        body = QWidget(self)
        content = QVBoxLayout(body)
        content.setContentsMargins(14, 8, 0, 10)
        content.setSpacing(12)
        toggle.toggled.connect(body.setVisible)
        body.hide()
        layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(body)
        for name, label, field in rows:
            title = groups[name]
            if title not in sections:
                if len(set(groups.values())) > 1:
                    heading = QLabel(title, body)
                    heading.setProperty("panelHeading", True)
                    heading.setWordWrap(True)
                    content.addWidget(heading)
                form = QFormLayout()
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                content.addLayout(form)
                sections[title] = form
            form = sections[title]
            label.setWordWrap(True)
            label.setMaximumWidth(16777215)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            form.addRow(label)
            form.addRow(field)
            self._toggle_by_field[name] = toggle

    def attach_to_review_footer(self, disclosures, content):
        """Share a review's disclosure row while retaining its original editors."""
        self.layout().removeWidget(self._advanced_toggle)
        self.layout().setContentsMargins(0, 0, 0, 0)
        disclosures.addWidget(self._advanced_toggle)
        content.addWidget(self)

    def reveal(self, name):
        """Open the shared disclosure for the named invalid reference control."""
        self._toggle_by_field[name].setChecked(True)
