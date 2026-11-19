"""Choose the execution location before assigning images and sample identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt, Signal
from pathlib import PurePosixPath
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QButtonGroup, QRadioButton
from .form_sections import FormSection, label, scroll_content, WorkbenchMessage
from .researcher_widgets import ContentSizedButton
from .action_bar import WorkflowActionBar

MODE_TEXT = {
    "local": ("This computer", "Process and review images on this computer. Results are saved in this analysis folder."),
    "desktop": (
        "Cluster desktop",
        "Process and review images in a cluster desktop session. Images, annotations, and results stay on the cluster.",
    ),
    "streaming": (
        "Cluster streaming",
        "Process images on the cluster and review them in QuPath on this computer. We load the image regions you request; accepted results stay on the cluster.",
    ),
}
MODE_REQUIREMENTS = {
    "local": "Open Software setup from Home to check the required software. Make sure this computer has enough space for your images and results.",
    "desktop": "Requires a cluster desktop with the pipeline, QuPath and the selected analysis tools installed. Open the application there and choose This computer in that session.",
    "streaming": "Requires SSH access, a matching pipeline installation and Slurm on the cluster. Local QuPath and a read-only mounted cluster folder provide the annotation view.",
}


class _ComputingModeCard(FormSection):
    """Keep the roomy Computing sections and a whole-card selection target."""

    def __init__(self, title, description, parent=None):
        super().__init__("", parent=parent)
        self.body.itemAt(0).widget().hide()
        self.setProperty("modeCard", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button = QRadioButton(title, self)
        self.button.setAccessibleDescription(description)
        self.body.addWidget(self.button)
        help_text = label(description, self, role="choiceHelp")
        help_text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.body.addWidget(help_text)
        self.setFocusProxy(self.button)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.button.isEnabled():
            self.button.setFocus(Qt.FocusReason.MouseFocusReason)
            self.button.setDown(True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.button.isDown():
            self.button.setDown(False)
            if self.rect().contains(event.position().toPoint()):
                self.button.click()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

# ==============================================================================
#                                 Computing page
# ==============================================================================


class ComputingPage(QWidget):
    configure_requested = Signal(str)
    continue_requested = Signal(str)
    help_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = "local"
        self._locked = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll, content, layout = scroll_content(self)
        layout.addWidget(
            label("Computing", content, role="dialogHeading", name="computing-title")
        )
        layout.addWidget(
            label(
                "Choose where the analysis will run, then add your images on Samples.",
                content,
                role="choiceHelp",
                name="computing-information",
            )
        )
        self.group = QButtonGroup(self)
        self.choices = {}
        self.cards = {}
        for mode, (title, description) in MODE_TEXT.items():
            section = _ComputingModeCard(title, description, content)
            button = section.button
            button.setObjectName("computing-mode-" + mode)
            button.setAccessibleName(title)
            self.group.addButton(button)
            layout.addWidget(section)
            self.choices[mode] = button
            self.cards[mode] = section
            button.toggled.connect(lambda checked, key=mode: self._select(key) if checked else None)
        self.requirements = FormSection("Before you add images", parent=content)
        self.requirement_text = label("", self.requirements, role="choiceHelp")
        self.requirements.body.addWidget(self.requirement_text)
        self.summary = label("", self.requirements, name="computing-saved-summary")
        self.requirements.body.addWidget(self.summary)
        row = QHBoxLayout()
        self.configure = ContentSizedButton("Settings…", self)
        self.configure.setObjectName("computing-configure")
        self.configure.clicked.connect(lambda: self.configure_requested.emit(self._selected))
        self.help = ContentSizedButton("Setup requirements", self)
        self.help.clicked.connect(lambda: self.help_requested.emit(self._selected))
        row.addWidget(self.configure)
        row.addWidget(self.help)
        row.addStretch(1)
        self.requirements.body.addLayout(row)
        layout.addWidget(self.requirements)
        self.message = WorkbenchMessage(self)
        layout.addWidget(self.message)
        layout.addStretch(1)
        outer.addWidget(scroll, 1)
        footer = WorkflowActionBar(self)
        self.next = ContentSizedButton("Next: Samples", self)
        self.next.setObjectName("computing-next-samples")
        self.next.setProperty("actionRole", "primary")
        self.next.clicked.connect(lambda: self.continue_requested.emit(self._selected))
        footer.add_primary(self.next)
        outer.addWidget(footer)
        self.choices["local"].setChecked(True)

    def _select(self, mode):
        self._selected = mode
        self.requirement_text.setText(MODE_REQUIREMENTS[mode])
        self.configure.setText(
            "Local settings…"
            if mode == "local"
            else "Streaming connection…" if mode == "streaming" else "Cluster desktop requirements"
        )
        self.next.setEnabled(not self._locked and mode != "desktop")
        self.next.setText(
            "Next: Samples" if mode != "desktop" else "Continue in the cluster desktop"
        )
        for key, card in self.cards.items():
            card.setProperty("modeSelected", key == mode)
            card.style().unpolish(card)
            card.style().polish(card)
            card.update()

    def set_location(self, value, *, locked=False):
        """Restore the saved mode, cluster folder and prepared-run action state.

        We update the controls once even when the radio selection is unchanged.
        A prepared run locks mode changes and continuation; reopening an editable
        setup restores the corresponding actions without changing its settings.
        """
        self._locked = locked
        mode = value.get("mode", "local")
        self.choices[mode].setChecked(True)
        self._select(mode)
        for card in self.cards.values():
            card.setEnabled(not locked)
        self.configure.setEnabled(not locked)
        remote = value.get("remote")
        if value.get("mode") == "streaming" and remote:
            self.summary.setText(
                "Cluster: " + remote["host"] + "\nCluster analysis folder: "
                + str(PurePosixPath(remote["output_root"]) / remote["batch_id"])
            )
            self.summary.show()
        else:
            self.summary.hide()
        if locked:
            self.message.setPlainText(
                "Processing has been prepared for this location. Start a new analysis to select another location."
            )
