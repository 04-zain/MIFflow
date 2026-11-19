"""Reopen an explicitly kept local draft without inventing cluster acceptance."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
import re
from PySide6.QtCore import Qt
from .form_sections import FormSection, label
from .researcher_widgets import ContentSizedButton

# ==============================================================================
#                              Retained annotation
# ==============================================================================


class RetainedAnnotationDraft(FormSection):
    def __init__(self, dialog, project=None):
        super().__init__("Saved annotation draft", parent=dialog)
        self.dialog = dialog
        self.project = Path(project) if project is not None else None
        self.body.addWidget(
            label(
                "A previous annotation draft is saved on this computer. Reopen it to continue the same review or inspect the earlier annotations.",
                self,
                role="choiceHelp",
            )
        )
        self.location = label(str(self.project or ""), self, role="choiceHelp")
        self.body.addWidget(self.location)
        self.reopen = ContentSizedButton("Reopen saved annotation draft", self)
        self.reopen.setObjectName("cluster-retained-draft-open")
        self.reopen.clicked.connect(self.open_saved)
        self.body.addWidget(self.reopen, alignment=Qt.AlignmentFlag.AlignLeft)
        self.setVisible(self.project is not None)

    def open_saved(self):
        d = self.dialog
        if self.project is None or d.file_operations is None:
            return
        if d.annotations.receipt is not None or not d.annotations.returned:
            d.message.setPlainText(
                "Finish the open annotation draft before reopening an earlier one."
            )
            return
        project = self.project
        d._start("retained-check", lambda: d.file_operations.collect_cluster_annotations(project))

    def completed(self, result):
        d = self.dialog
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], dict):
            raise ValueError("The saved annotation draft could not be checked.")
        ticket, _objects = result
        if ticket.get("batch_id") != d.client.profile.batch_id:
            raise ValueError("This saved annotation draft belongs to another analysis.")
        identity = (
            ticket.get("request_id"),
            ticket.get("state_head_sha256"),
            ticket.get("entry_id"),
        )
        if (
            not all(isinstance(x, str) and x for x in identity)
            or re.fullmatch("[0-9a-f]{64}", identity[1]) is None
        ):
            raise ValueError("The saved draft has an incomplete review identity.")
        matches = [
            i
            for i in range(d.annotations.images.count())
            if d.annotations.images.itemData(i) == identity
        ]
        if matches:
            annotations = d.annotations
            annotations.images.setCurrentIndex(matches[0])
            annotations.local_project = self.project
            annotations.selection = identity
            annotations.returned = False
            annotations._automatic_flow = True
            annotations.prepare_button.setEnabled(False)
            annotations.open_button.setEnabled(True)
            annotations.send_button.setEnabled(True)
            d.message.setPlainText(
                "The saved draft matches this review. Reopening QuPath to continue your annotations."
            )
            annotations.open_project()
        else:
            project = self.project
            d.message.setPlainText(
                "This draft is outside the current review page. It opens for reference; prepare a current annotation view before returning edits."
            )
            d._start("retained-open", lambda: d.file_operations.open_qupath(project), write=True)

    def accepted(self, project):
        if self.project is not None and Path(project) == self.project:
            self.hide()
            self.dialog.retained_draft_returned.emit(str(self.project))
