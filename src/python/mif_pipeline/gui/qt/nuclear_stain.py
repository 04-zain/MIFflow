"""One shared stain choice following the current output-component identities."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QLabel


class NuclearStainChoice(QComboBox):
    selection_state_changed = Signal()

    def __init__(self, value=None, *, reviewed=False, parent=None):
        super().__init__(parent)
        self.setObjectName("shared-nuclear-stain")
        self.setAccessibleName("Nuclear stain")
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(16)
        self._initial = (value, reviewed)
        self._states = {}
        self._mode = None
        self._ambiguous = False
        self._unavailable = False
        self.activated.connect(self._chosen)
        self.currentIndexChanged.connect(self._chosen)

    def _chosen(self, *_):
        self._states[self._mode] = (self.currentData(), True)
        self._unavailable = False
        self.selection_state_changed.emit()

    @property
    def reviewed(self):
        return self._states.get(self._mode, self._initial)[1]

    def restore_choice(self, value, *, reviewed=True):
        self.setCurrentIndex(max(0, self.findData(value)))
        self._pending_restore = (value, reviewed)

    def refresh(self, mode, choices):
        """Preserve an explicit None; infer only one exact DAPI identity initially."""
        restoring = hasattr(self, "_pending_restore")
        if self._mode is not None and not restoring:
            old = self._states.get(self._mode, self._initial)
            self._states[self._mode] = (self.currentData(), old[1])
        self._mode = mode
        selected, reviewed = self._states.get(mode, self._initial)
        if restoring:
            selected, reviewed = self._pending_restore
            del self._pending_restore
        offered = [identity for identity, label, dapi in choices]
        self._unavailable = selected is not None and selected not in offered
        if selected is not None and selected not in offered:
            selected = None
        self._ambiguous = sum(bool(is_dapi) for _identity, _label, is_dapi in choices) > 1
        if not reviewed:
            dapi = [identity for identity, label, is_dapi in choices if is_dapi]
            selected = dapi[0] if len(dapi) == 1 else None
        self.blockSignals(True)
        self.clear()
        self.addItem("No nuclear stain", None)
        for identity, label, dapi in choices:
            self.addItem(label, identity)
        self.setCurrentIndex(max(0, self.findData(selected)))
        self.blockSignals(False)
        self._states[mode] = (selected, reviewed)
        self.selection_state_changed.emit()


class NuclearStainSection(QFrame):
    """One compact stain choice; additional guidance only when a decision is unresolved."""

    def __init__(self, value=None, *, reviewed=False, parent=None):
        super().__init__(parent)
        self.setObjectName("shared-nuclear-stain-section")
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 8, 0, 4)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(6)
        title = QLabel("Nuclear stain", self)
        title.setProperty("fieldCaption", True)
        title.setMinimumWidth(title.fontMetrics().horizontalAdvance("Channel content"))
        self.choice = NuclearStainChoice(value, reviewed=reviewed, parent=self)
        title.setBuddy(self.choice)
        layout.addWidget(title, 0, 0)
        layout.addWidget(self.choice, 0, 1)
        layout.setColumnStretch(1, 1)
        self.guidance = QLabel(self)
        self.guidance.setObjectName("nuclear-stain-guidance")
        self.guidance.setProperty("fieldHelp", True)
        self.guidance.setWordWrap(True)
        layout.addWidget(self.guidance, 1, 1)
        self.choice.selection_state_changed.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        choice = self.choice
        entity = "component" if choice._mode == "raw" else "channel"
        if choice._unavailable:
            text = "The previous selection is no longer available. Review the nuclear stain."
        elif choice.currentData() is not None:
            text = "Used by segmentation methods that require a nuclear channel."
        elif not choice.reviewed and choice._ambiguous:
            text = f"More than one DAPI {entity} is available. Choose the nuclear stain to use."
        elif not choice.reviewed:
            text = f"Choose the DNA-stain {entity} if present; otherwise keep No nuclear stain."
        else:
            text = f"No {entity} is designated as the nuclear stain."
        self.guidance.setText(text)
        choice.setAccessibleDescription(text)
