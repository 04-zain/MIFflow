"""Local manual alignment with cluster-owned fitting and publication.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
from urllib.parse import urlsplit, unquote
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QMessageBox
from .form_sections import FormSection
from .form_navigation import FormNavigation
from .researcher_widgets import ContentSizedButton
from ..cluster_landmark_state import load_state, save_state

# ==============================================================================
#                           Cluster landmark controls
# ==============================================================================


class ClusterLandmarkControls(FormSection):
    def __init__(self, dialog):
        super().__init__(
            "Manual alignment",
            "Set the starting position in the alignment editor. The cluster fits the global alignment, then returns it for review and optional local refinement.",
            dialog,
        )
        self.dialog = dialog
        self.request = None
        self.local_project = None
        self.receipt = None
        self.returned = True
        self.submission_uncertain = False
        self.storage_error = None
        self._restored = False
        self._unsaved_state = False
        profile = dialog.client.profile
        self.scope = {
            key: getattr(profile, key, None) for key in ("host", "output_root", "batch_id")
        }
        self.state_path = (
            dialog.review_folder / "landmark-session-state.json"
            if dialog.review_folder is not None
            else None
        )
        self.auto_return = QCheckBox("Submit saved alignment when the editor closes", self)
        self.auto_return.setChecked(True)
        self.body.addWidget(self.auto_return)
        self.open_button = ContentSizedButton("Open manual alignment", self)
        self.send_button = ContentSizedButton("Submit saved alignment", self)
        self.send_button.setProperty("actionRole", "primary")
        self.check_button = ContentSizedButton("Check progress", self)
        self.restart_button = ContentSizedButton("Keep draft and start again", self)
        self.open_button.setObjectName("cluster-landmarks-open")
        self.send_button.setObjectName("cluster-landmarks-send")
        row = FormNavigation(self)
        for button in (self.open_button, self.send_button, self.check_button, self.restart_button):
            row.addWidget(button)
        self.body.addWidget(row)
        self.open_button.clicked.connect(self.open_project)
        self.send_button.clicked.connect(self.send)
        self.check_button.clicked.connect(self.poll)
        self.restart_button.clicked.connect(self.keep_and_restart)
        self._poll = QTimer(self)
        self._poll.setInterval(15000)
        self._poll.timeout.connect(self.poll)
        self.hide()
        self._buttons()

    @property
    def unfinished(self):
        return not self.returned or self.receipt is not None or self.submission_uncertain

    def _buttons(self):
        idle = self.receipt is None and not self.submission_uncertain and self.storage_error is None
        self.open_button.setEnabled(idle and self.local_project is not None and not self.returned)
        self.send_button.setEnabled(idle and self.local_project is not None and not self.returned)
        self.check_button.setEnabled(self.receipt is not None and not self.storage_error)
        self.restart_button.setEnabled(idle and not self.returned)

    def _persist(self):
        if self.state_path is None:
            raise ValueError("Open this review from its saved analysis folder.")
        try:
            save_state(
                self.state_path,
                {
                    "schema": "landmark-session-state/v1",
                    "scope": self.scope,
                    "request": self.request,
                    "local_project": str(self.local_project) if self.local_project else None,
                    "receipt": self.receipt,
                    "returned": self.returned,
                    "submission_uncertain": self.submission_uncertain,
                },
            )
            self.storage_error = None
            self._unsaved_state = False
        except (OSError, ValueError) as error:
            self.storage_error = str(error)
            self._unsaved_state = True
            self._buttons()
            raise

    def restore(self):
        if self._restored:
            return
        self._restored = True
        if self.state_path is None:
            return
        try:
            value = load_state(self.state_path, self.scope)
            if value is None:
                return
            self.request = value["request"]
            self.receipt = value["receipt"]
            self.returned = value["returned"]
            self.local_project = Path(value["local_project"]) if value["local_project"] else None
            self.submission_uncertain = value["submission_uncertain"]
            if self.unfinished:
                self.show()
                self._buttons()
                if self.receipt is not None:
                    self._poll.start()
                elif self.submission_uncertain:
                    self.dialog.message.setPlainText(
                        "The last alignment job was not confirmed. Check the cluster job list before submitting another job. The saved draft is retained."
                    )
                else:
                    self.dialog.message.setPlainText("The saved manual alignment is ready to reopen.")
        except (OSError, ValueError, TypeError) as error:
            self.storage_error = str(error)
            self.show()
            self._buttons()
            self.dialog.message.setPlainText(
                "The saved alignment session needs attention: " + str(error)
            )

    def begin(self, request):
        from mif_pipeline.orchestration.cluster.cluster_review_pages import _plain

        d = self.dialog
        self.restore()
        if d._pending or d.jobs.has_active_jobs:
            return
        if self.storage_error:
            raise ValueError(self.storage_error)
        if self.unfinished:
            if (
                self.request == _plain(request)
                and self.local_project is not None
                and self.receipt is None
            ):
                self.open_project()
            else:
                d.message.setPlainText(
                    "Finish the saved alignment session or keep its draft before starting another."
                )
            return
        if d.file_operations is None or d.mounted_images is None or self.state_path is None:
            raise ValueError(
                "Configure local QuPath and mounted image access before opening manual alignment."
            )
        if any(not isinstance(value, str) or not value for value in self.scope.values()):
            raise ValueError("Save the cluster connection before preparing manual alignment.")
        self.request = _plain(request)
        self.local_project = None
        self.returned = False
        self.submission_uncertain = True
        self._persist()
        self.show()
        self._buttons()
        d._start("landmark-export", lambda: d.client.submit_landmark_export(request), write=True)

    def poll(self):
        if self.receipt is not None:
            receipt = dict(self.receipt)
            self.dialog._start(
                "landmark-result", lambda: self.dialog.client.annotation_result(receipt)
            )

    def open_project(self):
        if (
            self.receipt is not None
            or self.local_project is None
            or self.returned
            or self.submission_uncertain
        ):
            return
        project = self.local_project
        self.dialog.open_registration_editor(
            lambda: self.dialog.file_operations.prepare_cluster_registration_editor(project),
            kind="manual",
        )

    def send(self):
        d = self.dialog
        if (
            self.receipt is not None
            or self.local_project is None
            or self.returned
            or self.submission_uncertain
            or d._pending
            or d.jobs.has_active_jobs
        ):
            return
        project = self.local_project
        d._start(
            "landmark-collect",
            lambda: d.file_operations.collect_cluster_landmarks(project),
            write=True,
        )

    def completed(self, name, result):
        """

        Advances the saved alignment through fitting, local review and final application.

        The draft and its global-fit receipt own the scientific phase. A global
        result prepares and reopens the same local project without advancing the
        registration review. Only an accepted final application marks the session
        returned. We retain completed job receipts until local preparation succeeds,
        so mounted-image or transform-copy failures can retry result retrieval
        without submitting the calculation again.

        Args:
            name: Completed managed desktop operation.
            result: Its job receipt, cluster result, local project or process result.

        Raises:
            ValueError: The returned pair or final application differs from this
                session, or local preparation lacks a saved project.
        """

        d = self.dialog
        if name in {"landmark-export", "landmark-import"}:
            self.receipt = result
            self.submission_uncertain = False
            self._persist()
            self._poll.start()
            d.message.setPlainText("Alignment work is queued on the cluster.")
        elif name == "landmark-result":
            if result.get("status") == "pending":
                return
            operation = self.receipt["operation"]
            if operation == "landmark-export":
                from mif_pipeline.orchestration.cluster.cluster_landmark_exchange import validate_ticket

                ticket = validate_ticket(result)
                if (
                    ticket["batch_id"] != self.scope["batch_id"]
                    or ticket["request"] != self.request
                ):
                    raise ValueError(
                        "The returned image pair differs from the requested alignment review."
                    )
                images = {}
                for role, row in ticket["pair"].items():
                    uri = urlsplit(row["source_uri"])
                    if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
                        raise ValueError("The image pair requires cluster-resident files.")
                    images[role] = Path(d.mounted_images.local_path(unquote(uri.path)))
                self._poll.stop()
                folder = d.review_folder / "landmarks" / ticket["ticket_id"]
                d._start(
                    "landmark-local",
                    lambda: d.file_operations.prepare_cluster_landmark_project(
                        folder, images, ticket
                    ),
                    write=True,
                )
            elif operation == "landmark-import" and result.get("schema") == "manual-global-result/v1":
                if self.local_project is None:
                    raise ValueError("The global alignment has no saved local project to reopen.")
                self._poll.stop()
                project = self.local_project
                d.message.setPlainText(
                    "Global alignment is fitted. Preparing the local project for review and optional refinement."
                )
                d._start(
                    "landmark-global-local",
                    lambda: d.file_operations.prepare_cluster_global_alignment(
                        project, result, d.mounted_images
                    ),
                    write=True,
                )
            elif operation == "landmark-import":
                rows = result.get("applications", ())
                if not rows or any(
                    row.get("status")
                    not in {"accepted", "accepted_for_retry", "accepted_for_application"}
                    for row in rows
                ):
                    raise ValueError(
                        "The cluster did not accept the final alignment for registration review. The draft is retained."
                    )
                self.receipt = None
                self._poll.stop()
                self.returned = True
                self._persist()
                d.reviews.set_response_status("accepted_for_retry")
                self.hide()
                d.refresh_reviews()
        elif name in {"landmark-local", "landmark-global-local"}:
            project = Path(result)
            if name == "landmark-global-local" and project != self.local_project:
                raise ValueError("The global alignment belongs to a different local project.")
            self.local_project = project
            self.receipt = None
            self._persist()
            self.open_project()
        elif name == "landmark-open":
            normal = (
                getattr(result, "returncode", None) == 0
                and getattr(getattr(result, "drain_evidence", None), "termination_reason", None)
                == "normal_exit"
            )
            if not normal:
                d.message.setPlainText(
                    "The alignment reader did not close normally. Reopen the saved alignment before submitting it."
                )
            elif self.auto_return.isChecked():
                self.send()
            else:
                d.message.setPlainText("The alignment remains saved locally. Submit it when ready.")
        elif name == "landmark-collect":
            self.submission_uncertain = True
            self._persist()
            d._start("landmark-import", lambda: d.client.submit_landmark_import(result), write=True)
        self._buttons()

    def failed(self, name):
        """Keep retrievable results and block duplicate submissions after uncertainty."""

        self._poll.stop()
        # Keep the exact receipt after an interrupted result check. A new
        # submission is forbidden when its previous outcome is uncertain.
        if name in {"landmark-export", "landmark-import"}:
            self.submission_uncertain = True
        if self.state_path is not None:
            try:
                self._persist()
            except (OSError, ValueError):
                pass
        self._buttons()

    def keep_and_restart(self):
        if self.receipt is not None or self.submission_uncertain:
            return
        answer = QMessageBox.question(
            self,
            "Keep alignment draft?",
            "Keep this draft and return to the current registration review?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.returned = True
        self.request = None
        self.local_project = None
        self._persist()
        self.hide()
        self.dialog.refresh_reviews()

    def may_close(self):
        if not self._unsaved_state:
            return True
        try:
            self._persist()
        except (OSError, ValueError):
            self.dialog.message.setPlainText(
                "The alignment session could not be saved. Restore free space and write access to this local analysis folder, then close again: "
                + str(self.state_path.parent)
            )
            return False
        return True

    def stop(self):
        self._poll.stop()
