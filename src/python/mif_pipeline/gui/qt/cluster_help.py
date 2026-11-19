"""On-demand cluster setup instructions using the shared desktop palette."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QDialogButtonBox
from ..cluster_guidance import CLUSTER_SETUP, CLUSTER_DESKTOP_SETUP

# ==============================================================================
#                                  Cluster help
# ==============================================================================


def show_cluster_setup(parent, *, mode="streaming"):
    if mode not in {"streaming", "desktop"}:
        raise ValueError("Unknown cluster processing mode")
    title = (
        "Cluster desktop requirements" if mode == "desktop" else "Cluster streaming requirements"
    )
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(820, 700)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(12)
    from .form_sections import label

    layout.addWidget(label(title, dialog, role="dialogHeading"))
    text = QTextBrowser(dialog)
    text.setObjectName("cluster-setup-guide")
    text.setPlainText(CLUSTER_SETUP if mode == "streaming" else CLUSTER_DESKTOP_SETUP)
    text.setOpenExternalLinks(False)
    layout.addWidget(text, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    parent._cluster_help_dialog = dialog
    dialog.show()
    return dialog
