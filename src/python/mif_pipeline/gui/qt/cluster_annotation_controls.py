"""Native annotation viewing for a current cluster review, with explicit return.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QCheckBox, QHBoxLayout
from .form_navigation import FormNavigation
from .form_sections import FormSection
from .researcher_widgets import ContentSizedButton

# ==============================================================================
#                          Cluster annotation controls
# ==============================================================================


class ClusterAnnotationControls(FormSection):
    def __init__(self, dialog):
        super().__init__(
            "Annotate in QuPath",
            "QuPath opens the selected cluster image on this computer. Save the annotations in the local draft before returning them to the cluster.",
            dialog,
        )
        self.dialog = dialog
        self.receipt = None
        self.local_project = None
        self.selection = None
        self.returned = True
        self._form_owns_selection = False
        self._automatic_flow = False
        self.images = QComboBox(self)
        self.images.setObjectName("cluster-annotation-image")
        self.image_field = self.field(
            "annotation-image",
            "Image to annotate",
            self.images,
            "",
        )
        self.auto_return = QCheckBox("Return saved annotations when QuPath closes", self)
        self.auto_return.setObjectName("cluster-annotation-auto-return")
        self.auto_return.setChecked(True)
        self.prepare_button = ContentSizedButton("Annotate image", self)
        self.open_button = ContentSizedButton("Open local QuPath", self)
        self.send_button = ContentSizedButton("Return saved annotations", self)
        self.prepare_button.setObjectName("cluster-annotations-prepare")
        self.open_button.setObjectName("cluster-annotations-open")
        self.send_button.setObjectName("cluster-annotations-send")
        self.send_button.setProperty("actionRole", "primary")
        actions = FormNavigation(self)
        for button in (self.prepare_button, self.open_button, self.send_button):
            actions.addWidget(button)
        controls = QHBoxLayout()
        controls.addWidget(actions, 1)
        controls.addWidget(self.auto_return)
        self.body.addLayout(controls)
        self.prepare_button.clicked.connect(self.begin)
        self.open_button.clicked.connect(self.open_project)
        self.send_button.clicked.connect(self.send)
        self._poll = QTimer(self)
        self._poll.setInterval(15000)
        self._poll.timeout.connect(self.poll)
        self._update_actions()
        self.hide()

    def set_form_owns_selection(self, integrated):
        self._form_owns_selection = integrated
        self._update_actions()

    def _update_actions(self):
        has_draft = self.local_project is not None and not self.returned
        idle = self.receipt is None
        self.setVisible(has_draft or (self.images.count() > 0 and not self._form_owns_selection))
        self.image_field.setVisible(not self._form_owns_selection)
        self.auto_return.setVisible(not self._form_owns_selection)
        self.images.setEnabled(idle and not has_draft)
        self.prepare_button.setVisible(not has_draft)
        self.prepare_button.setEnabled(idle and not has_draft)
        for button in (self.open_button, self.send_button):
            button.setVisible(has_draft)
            button.setEnabled(idle and has_draft)

    def _matches_selection(self, ticket):
        if self.selection is None:
            return False
        identity = (ticket.get("request_id"), ticket.get("state_head_sha256"), ticket.get("entry_id"))
        if identity != tuple(self.selection[:3]):
            return False
        if len(self.selection) == 3:
            return True
        reference = ticket.get("supervised_reference") or {}
        return all(reference.get(key) == value for key, value in self.selection[3].items())

    def configure(self, review):
        from mif_pipeline.orchestration.cluster.cluster_annotations import annotation_entry_ids

        if self.receipt is not None or (self.local_project is not None and not self.returned):
            return
        self.images.clear()
        seen = set()
        for group in review.page.groups:
            if not hasattr(group, "targets"):
                continue
            for target in group.targets:
                if target.stage_result.stage == "registration":
                    continue
                context = target.stage_result.resume_context or {}
                if context.get("schema_version") == "supervised-reference-review/v1":
                    for row in context["references"]:
                        role = {"training_plan_id": row["training_plan_id"], "split": row["split"]}
                        key = (target.interaction_request_id, row["project_entry_id"], row["training_plan_id"], row["split"])
                        if key in seen:
                            continue
                        seen.add(key)
                        title = {"training": "Training", "validation": "Validation", "test": "Independent test"}[row["split"]]
                        self.images.addItem(f"{row['sample_id']} · {title} · {', '.join(row['class_ids'])}",
                            (target.interaction_request_id, review.state_head_sha256,
                             row["project_entry_id"], role))
                    continue
                if target.stage_result.stage == "supervised_phenotype_training_plan_review":
                    continue
                for entry in annotation_entry_ids(target):
                    key = (target.interaction_request_id, entry)
                    if key in seen:
                        continue
                    seen.add(key)
                    self.images.addItem(
                        f"{target.sample_id} · image {entry}",
                        (target.interaction_request_id, review.state_head_sha256, entry),
                    )
        self._update_actions()

    def begin(self):
        self._automatic_flow = True
        self.prepare()

    def prepare(self):
        d = self.dialog
        if self.local_project is not None and not self.returned:
            d.message.setPlainText(
                "The current annotation draft is still local. Return it or keep it before preparing another view."
            )
            return
        if self.receipt is not None:
            return
        if d.file_operations is None or d.mounted_images is None:
            d.message.setPlainText(
                "Configure mounted image access and the local QuPath installation before preparing an annotation view."
            )
            return
        selected = self.images.currentData()
        if selected is None:
            return
        if not self.returned:
            d.message.setPlainText(
                "Send the current annotation draft before preparing a different image."
            )
            return
        self.selection = selected
        self.local_project = None
        preview = d.reviews.annotation_preview_inputs()
        reference_inputs = d.reviews.annotation_reference_inputs()

        def export():
            role = selected[3] if len(selected) == 4 else None
            if reference_inputs is not None:
                from mif_pipeline.gui.presenters.reviews import build_annotation_reference_role

                role = build_annotation_reference_role(*reference_inputs)
            if preview is not None:
                from mif_pipeline.gui.presenters.reviews import build_annotation_preview_request

                choice = build_annotation_preview_request(*preview, selected[2])
                return d.client.submit_annotation_export(*selected[:3], preview_selection=choice,
                    reference_role=role)
            return d.client.submit_annotation_export(*selected[:3],
                reference_role=role)

        d._start("annotation-export", export, write=True)

    def poll(self):
        if self.receipt is not None:
            receipt = dict(self.receipt)
            self.dialog._start(
                "annotation-result", lambda: self.dialog.client.annotation_result(receipt)
            )

    def open_project(self):
        if self.local_project is None or self.receipt is not None or self.returned:
            return
        self.returned = False
        project = self.local_project
        self.dialog._start(
            "annotation-open", lambda: self.dialog.file_operations.open_qupath(project), write=True
        )

    def send(self):
        if self.local_project is None or self.receipt is not None or self.returned:
            return
        d = self.dialog
        project = self.local_project

        def send():
            ticket, objects = d.file_operations.collect_cluster_annotations(project)
            if not self._matches_selection(ticket):
                raise ValueError("This annotation draft belongs to another review.")
            return d.client.submit_annotation_import(ticket["ticket_id"], objects)

        d._start("annotation-import", send, write=True)

    def completed(self, name, result):
        d = self.dialog
        if name in {"annotation-export", "annotation-import"}:
            self.receipt = result
            self._poll.start()
            self._update_actions()
            d.message.setPlainText(
                "Annotation work is queued on the cluster. This window will check when it is ready."
            )
        elif name == "annotation-result":
            if result.get("status") == "pending":
                return
            operation = self.receipt["operation"]
            self.receipt = None
            self._poll.stop()
            if operation == "annotations-export":
                if (
                    result.get("batch_id") != d.client.profile.batch_id
                    or not self._matches_selection(result)
                ):
                    raise ValueError("The annotation snapshot differs from the requested review.")
                mounted = Path(d.mounted_images.local_path(result["image_path"]))
                d._start(
                    "annotation-local",
                    lambda: d.file_operations.prepare_cluster_annotation_project(
                        d.review_folder, mounted, result, mounted_images=d.mounted_images
                    ),
                    write=True,
                )
            elif operation == "annotations-import":
                if result.get("status") != "annotations_saved":
                    raise ValueError("The cluster did not accept the edited annotations.")
                self.returned = True
                d.reviews.offer_classifier_reference_regions(result)
                self._update_actions()
                d.retained_draft.accepted(self.local_project)
                d.message.setPlainText(
                    "Annotations saved on the cluster. Complete the form to continue, or prepare a new annotation view to make further edits."
                )
        elif name == "annotation-local":
            self.local_project = Path(result)
            self.returned = False
            self._update_actions()
            d.message.setPlainText(
                "Annotation view ready. Save your annotations in QuPath before closing it."
            )
            if self._automatic_flow:
                self.open_project()
        elif name == "annotation-open":
            if (
                getattr(result, "returncode", None) != 0
                or getattr(getattr(result, "drain_evidence", None), "termination_reason", None)
                != "normal_exit"
            ):
                d.message.setPlainText(
                    "QuPath did not close normally. The annotation draft remains on this computer. Reopen it to check and save your annotations before returning them."
                )
                self._automatic_flow = False
                self._update_actions()
                return True
            if self._automatic_flow and (self._form_owns_selection or self.auto_return.isChecked()):
                d.message.setPlainText("Returning the saved annotation draft to the cluster…")
                self.send()
            else:
                d.message.setPlainText(
                    "QuPath has closed. Return the saved annotation draft to the cluster before continuing."
                )

    def failed(self):
        self._poll.stop()
        self.receipt = None
        self._update_actions()

    def stop(self):
        self._poll.stop()
