"""Optional read-only SSHFS connection preparation inside cluster settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QMessageBox
from .form_sections import FormSection, label
from .researcher_widgets import ContentSizedButton
from ..mounted_images import MOUNTED_IMAGE_GUIDANCE

# ==============================================================================
#                                 Mounted images
# ==============================================================================


def build_mounted_images_section(page, parent, layout):
    section = FormSection(
        "Image access",
        "Connect the cluster folder read-only using your SSHFS client. Enter the cluster path and its corresponding folder or drive on this computer.",
        parent,
    )
    page.mounted_fields = {}
    rows = (
        (
            "host",
            "Separate image-access connection (optional)",
            "Enter the file-access server when your institution uses a different host for image storage. Otherwise, the cluster connection above is used.",
        ),
        (
            "cluster_folder",
            "Image folder on the cluster",
            "Choose a parent folder containing your images and the location where this analysis will be saved.",
        ),
        (
            "local_folder",
            "Mount folder on this computer",
            "Enter the mounted folder or drive supplied by your SSHFS client.",
        ),
    )
    for name, title, help_text in rows:
        editor = QLineEdit(section)
        page.mounted_fields[name] = editor
        section.field("mounted-" + name, title, editor, help_text)
    prepare = ContentSizedButton("Prepare connection command", section)
    prepare.setObjectName("compute-prepare-mount")
    prepare.clicked.connect(page.prepare_mounted_images)
    section.body.addWidget(prepare, alignment=Qt.AlignmentFlag.AlignLeft)
    page.mount_command = QPlainTextEdit(section)
    page.mount_command.setObjectName("compute-mount-command")
    page.mount_command.setReadOnly(True)
    page.mount_command.setMinimumHeight(100)
    page.mount_command.setMaximumHeight(160)
    page.mount_command.hide()
    section.body.addWidget(page.mount_command)
    section.body.addWidget(
        label("Select images from the mounted folder on Samples.", section, role="choiceHelp")
    )
    help_button = ContentSizedButton("Image access and annotation return", section)
    help_button.clicked.connect(
        lambda: QMessageBox.information(
            page, "Local viewing of cluster images", MOUNTED_IMAGE_GUIDANCE
        )
    )
    section.body.addWidget(help_button, alignment=Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(section)
