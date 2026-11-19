"""Revisit a saved structure definition while its classifier setup stays mounted."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from mif_pipeline.gui.secondary_setup import build_secondary_setup_form
from .forms import FormRenderer
from .revision_review import RevisionReview
from .revision_summary import choice_changes


def definition_predecessor(workflow_nodes, training_id):
    nodes = {node.node_id: node for node in workflow_nodes if not node.deferred}
    training = nodes.get(training_id)
    if (training is None or training.kind != "secondary_classifier_training_plan_review"
            or training.status != "needs_interaction"):
        return None
    matches = [key for key in training.dependency_ids if key in nodes
               and nodes[key].kind == "secondary_structure_definition"
               and nodes[key].status == "completed"]
    return matches[0] if len(matches) == 1 else None


class SavedSecondaryEditor(QWidget):
    finished = Signal()
    changed_save = Signal(str, str, object)

    def __init__(self, options, training_id, return_widget, parent):
        super().__init__(parent)
        self.options = options
        self.saved = options.saved_secondary
        self.training_id = training_id
        self.return_widget = return_widget
        self.within_node = True
        layout = QVBoxLayout(self)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.pages = QStackedWidget(self)
        layout.addWidget(self.pages)
        self.choices = QScrollArea(self.pages)
        self.choices.setWidgetResizable(True)
        self.form = FormRenderer(parent=self.choices)
        self.form.set_form(build_secondary_setup_form(self.saved))
        self.definitions = self.form._editors["definitions"]
        if self.definitions.needs_preparation():
            self.definitions._set_page("preparation")
        self.baseline = self.definitions.value()
        self.choices.setWidget(self.form)
        self.pages.addWidget(self.choices)
        self.review = RevisionReview(self.pages)
        self.pages.addWidget(self.review)
        self.form.cancelled.connect(self.finished)
        self.form.submission_ready.connect(self._continue)
        self.form.secondary_marker_requested.connect(lambda kind, options, receiver:
            receiver.set_error(kind, options, "Open the current structure definition review to inspect marker thresholds."))
        self.review.cancel.clicked.connect(self.finished)
        self.review.back.clicked.connect(self._edit)
        self.review.apply.clicked.connect(lambda: self.form._submit("accept"))

    def _edit(self):
        self.within_node = True
        self.pages.setCurrentWidget(self.choices)

    def _continue(self, action, submission):
        value = {"selection": "define" if submission["secondary_structures"] else "none",
                 "structures": submission["secondary_structures"]}
        if value == self.baseline:
            self.finished.emit()
        else:
            self.changed_save.emit("secondary_structure_definition", action, {"definitions": value})

    def set_busy(self, busy):
        self.pages.setEnabled(not busy)

    def show_error(self, message):
        self.set_busy(False)
        self.notice.setText(str(message))
        self.notice.show()

    def show_impact(self, preview, nodes):
        self.within_node = False
        def by_structure(choice):
            return {row["structure_id"]: row for row in choice["structures"]}
        current, proposed = choice_changes(by_structure(self.baseline), by_structure(self.definitions.value()))
        self.review.show_preview(preview, nodes, current, proposed)
        self.pages.setCurrentWidget(self.review)
