"""Secondary save-and-pause action that follows its primary action's state."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import QEvent
from .researcher_widgets import ContentSizedButton

# ==============================================================================
#                                 Paired action
# ==============================================================================


class SavePauseButton(ContentSizedButton):
    def __init__(self, primary, submit, parent=None):
        super().__init__("Save and pause", parent)
        self._primary = primary
        self.setProperty("actionRole", "secondary")
        self.clicked.connect(submit)
        primary.installEventFilter(self)
        self._sync()

    def _sync(self):
        self.setVisible(not self._primary.isHidden())
        self.setEnabled(self._primary.isEnabled())

    def eventFilter(self, obj, event):
        if obj is self._primary and event.type() in {
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.EnabledChange,
        }:
            self._sync()
        return super().eventFilter(obj, event)
