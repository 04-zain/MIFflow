"""Local presentation of current cluster forms; scientific acceptance stays remote.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QMessageBox
from .jobs import JobAccess
from .reviews_training import ReviewsTrainingPage
from .form_sections import label, WorkbenchMessage
from .researcher_widgets import ContentSizedButton

# ==============================================================================
#                                Cluster reviews
# ==============================================================================


class ClusterReviewsDialog(QDialog):
    back_to_run_requested = Signal()
    retained_draft_returned = Signal(str)
    umap_requested = Signal(str, str, object)

    def __init__(
        self,
        client,
        jobs,
        parent=None,
        *,
        file_operations=None,
        mounted_images=None,
        review_folder=None,
        embedded=False,
        retained_draft=None,
    ):
        super().__init__(parent)
        self.embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.client = client
        self.jobs = jobs
        self._pending = {}
        self._accepted_continue = False
        self._alignment_receipt = None
        self._registration_session = None
        self._registration_editor = None
        self._registration_kind = None
        self._registration_result = None
        self._registration_error = None
        self._registration_selection = None
        self._registration_saved = False
        self._registration_close_pending = False
        self.file_operations = file_operations
        self.mounted_images = mounted_images
        self.review_folder = Path(review_folder) if review_folder is not None else None
        self.setWindowTitle("Cluster reviews")
        self.resize(1120, 850)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, not embedded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        if not embedded:
            layout.addWidget(label("Review the cluster analysis", self, role="dialogHeading"))
            layout.addWidget(
                label(
                    "Complete the review on this computer. Continuing from its final page sends the decision to the cluster and requests the next processing job.",
                    self,
                    role="choiceHelp",
                )
            )
        actions = QHBoxLayout()
        self.refresh_button = ContentSizedButton("Refresh pending reviews", self)
        self.refresh_button.setObjectName("cluster-review-refresh")
        self.refresh_button.clicked.connect(self.refresh_reviews)
        self.reviews = ReviewsTrainingPage(
            jobs=jobs, response_allowed=lambda: not self._pending and self._registration_session is None, parent=self
        )
        self.reviews.remote_execution = True
        self.reviews.streamed_annotations_available = file_operations is not None and mounted_images is not None
        self.reviews.secondary_back_caption = "Back to Run" if embedded else "Refresh pending reviews"
        self.reviews.secondary_definition_back_requested.connect(self.reviews._leave_form)
        self.reviews.set_workflow_context("Current review" if embedded else "Cluster review")
        self.reviews.response_requested.connect(self._submit)
        self.reviews.back_to_workflow_requested.connect(
            self.back_to_run_requested.emit if embedded else self.refresh_reviews
        )
        self.reviews.directory_requested.connect(self.refresh_reviews)
        self.reviews.page_requested.connect(self.request_page)
        self.reviews.open_qupath_requested.connect(self._annotation_notice)
        self.reviews.open_plot_requested.connect(self._open_plot)
        self.reviews.review_plots_requested.connect(self._request_figures)
        self._figures_waiting = None
        self._figures_loading = None
        self._local_figure_paths = set()
        from .cluster_annotation_controls import ClusterAnnotationControls

        self.annotations = ClusterAnnotationControls(self)
        self.reviews.streamed_reference_requested.connect(self._open_form_reference)
        self.reviews.streamed_reference_context_changed.connect(self.annotations.set_form_owns_selection)
        from .cluster_landmark_controls import ClusterLandmarkControls

        self.landmarks = ClusterLandmarkControls(self)
        from .retained_annotation import RetainedAnnotationDraft

        self.retained_draft = RetainedAnnotationDraft(self, retained_draft)
        layout.addWidget(self.retained_draft)
        layout.addWidget(self.annotations)
        layout.addWidget(self.landmarks)
        layout.addWidget(self.reviews, 1)
        self.message = WorkbenchMessage(self)
        layout.addWidget(self.message)
        close = ContentSizedButton("Close", self)
        close.clicked.connect(self.reject)
        actions.addStretch(1)
        actions.addWidget(self.refresh_button)
        actions.addWidget(close)
        layout.addLayout(actions)
        close.setVisible(not embedded)
        self.jobs.job_succeeded.connect(self._completed)
        self.jobs.job_failed.connect(self._failed)
        self._poll = QTimer(self)
        self._poll.setInterval(15000)
        self._poll.timeout.connect(self.refresh_reviews)

    @property
    def _annotation_job(self):
        return self.annotations.receipt

    @property
    def _local_annotation_project(self):
        return self.annotations.local_project

    def prepare_annotations(self):
        self.annotations.prepare()

    def poll_annotation_job(self):
        self.annotations.poll()

    def send_annotations(self):
        self.annotations.send()

    def _start(self, name, fn, write=False):
        if self._pending or self.jobs.has_active_jobs:
            return
        identity = self.jobs.submit(
            "cluster-review-" + name, fn, access=JobAccess.MUTATION if write else JobAccess.READ
        )
        self._pending[identity] = name
        self.refresh_button.setEnabled(False)
        self.message.setPlainText("Contacting the cluster…")

    def open_registration_editor(self, prepare, *, kind):
        """Prepare a verified cluster pair for the existing embedded Qt editor."""
        if self._registration_session is not None or self._pending or self.jobs.has_active_jobs:
            return
        self._registration_kind = kind
        self._registration_saved = False
        self._registration_result = None
        self._registration_error = None
        self._registration_selection = (self.reviews._current_view, self.reviews._target.currentData())
        self._start("registration-prepare", prepare, write=True)

    def _mount_registration_editor(self, session):
        from .registration_editor import RegistrationEditor
        self._registration_session = session
        view, target = self._registration_selection
        if self.reviews._current_view is not view or self.reviews._target.currentData() != target:
            self._registration_error = "The selected review changed. Open the alignment from its current review again."
            session.worker_finished()
            self._start("registration-cleanup", session.cleanup)
            return
        try:
            editor = RegistrationEditor(session, self.reviews)
            self._registration_editor = editor
            editor.close_requested.connect(session.close)
            editor.candidate_saved.connect(self._registration_candidate_saved)
            editor.finished.connect(self._registration_editor_finished)
            self._start("registration-worker", lambda: self.file_operations.run_registration_editor(session), write=True)
            self.reviews.show_registration_editor(editor)
            self._registration_previous_visibility = [(widget, not widget.isHidden())
                for widget in (self.annotations, self.landmarks, self.refresh_button)]
            for widget, visible in self._registration_previous_visibility:
                widget.hide()
            self.message.setPlainText("Viewing the mounted image pair.")
        except (OSError, ValueError, RuntimeError):
            if self._pending:
                session.close()
            else:
                session.worker_finished()
                self._registration_editor_finished()
            raise

    def _registration_candidate_saved(self, _result):
        self._registration_saved = True

    def _registration_editor_finished(self):
        editor, self._registration_editor = self._registration_editor, None
        if editor is not None:
            self.reviews.finish_registration_editor(editor)
        self._start("registration-cleanup", self._registration_session.cleanup)

    def _registration_completed(self, name, result):
        if name == "registration-prepare":
            self._mount_registration_editor(result)
        elif name == "registration-worker":
            self._registration_result = result
            self._registration_session.worker_finished()
            self._registration_editor.worker_finished()
        elif name == "registration-cleanup":
            kind, completed, saved = self._registration_kind, self._registration_result, self._registration_saved
            self._registration_session = self._registration_kind = self._registration_result = None
            self._registration_saved = False
            for widget, visible in getattr(self, "_registration_previous_visibility", ()):
                widget.setVisible(visible)
            if self.landmarks.storage_error:
                self.landmarks.show()
            if self._registration_close_pending:
                self._registration_close_pending = False
                self.reject()
            elif kind == "manual" and saved and completed is not None:
                # The editor's image jobs and native process have both drained;
                # preserve the existing auto-return and receipt handling here.
                self.landmarks.completed("landmark-open", completed)
            else:
                self.message.setPlainText(getattr(self, "_registration_error", None) or (
                    "Alignment editor closed. Submit a saved request when ready."
                    if kind == "manual" else "Alignment preview closed. Accept or reject the candidate in this review."))

    def refresh_reviews(self):
        self.landmarks.restore()
        if self.landmarks.receipt is not None:
            self.landmarks.poll()
            return
        if self.landmarks.unfinished or self.landmarks.storage_error:
            self.message.setPlainText(
                self.landmarks.storage_error
                or "Finish the saved landmark session or keep its draft before refreshing reviews."
            )
            return
        if self._alignment_receipt is not None:
            receipt = dict(self._alignment_receipt)
            self._start("alignment-result", lambda: self.client.annotation_result(receipt))
            return
        if self.annotations.receipt is not None or not self.annotations.returned:
            self.message.setPlainText(
                "Return or finish the current annotation draft before refreshing the review."
            )
            return
        self._start("load", self.client.review_page)

    def request_page(self, query):
        if (
            self.landmarks.unfinished
            or self._alignment_receipt is not None
            or self.annotations.receipt is not None
            or not self.annotations.returned
        ):
            self.message.setPlainText(
                "Finish the current annotation or alignment step before changing review pages."
            )
            return
        self._start("load", lambda: self.client.review_page(query))

    def _submit(self, request):
        # User intents are tied to the exact displayed request instance. Keep it
        # while translating only the transport copy to cluster file locations.
        displayed_request = request
        from mif_pipeline.gui.cluster_form_paths import map_response_paths
        from dataclasses import replace

        view = self.reviews._current_view
        try:
            if view is not None and hasattr(view, "form"):
                response = map_response_paths(
                    view.form,
                    self.reviews._renderer.raw_values(),
                    request.response,
                    self.mounted_images,
                )
                request = replace(request, response=response)
        except ValueError as error:
            self.reviews.set_response_status("rejected")
            self.message.setPlainText(str(error))
            return
        if self.annotations.receipt is not None or not self.annotations.returned:
            self.reviews.set_response_status("rejected")
            self.message.setPlainText(
                "Finish returning the annotation draft before continuing this review."
            )
            return
        self._accepted_continue = self.reviews.take_continuation_intent(displayed_request)
        from mif_pipeline.gui.cluster_paths import cluster_response_values

        # The response has already been translated once using the form controls.
        prepared = request
        alignment = self.reviews.take_alignment_options(displayed_request)
        if alignment == {"method": "landmarks"}:
            try:
                self.landmarks.begin(prepared)
            except (OSError, ValueError) as error:
                self.reviews.set_response_status("rejected")
                self.message.setPlainText(str(error))
            return
        if self.landmarks.unfinished:
            self.reviews.set_response_status("rejected")
            self.message.setPlainText(
                "Finish the current landmark session before submitting another review."
            )
            return
        if alignment is not None:
            try:
                options = cluster_response_values(alignment, self.mounted_images)
            except ValueError as error:
                self.reviews.set_response_status("rejected")
                self.message.setPlainText(str(error))
                return
            self._start(
                "alignment", lambda: self.client.submit_alignment(prepared, options), write=True
            )
        else:
            self._start("save", lambda: self.client.submit_review(prepared), write=True)

    def _request_figures(self, request_id, head, options=None):
        if options and options.get("umap_settings") is not None:
            self._allocated_plot_request = (request_id, head, options)
            self.umap_requested.emit(request_id, head, options)
            return
        if self.review_folder is None or not hasattr(self.client, "review_figures"):
            self.reviews.set_review_plots_failure(request_id, head)
            return
        self._figures_waiting = (request_id, head, options)
        if not self._pending and not self.jobs.has_active_jobs:
            self._load_figures()
        else:
            QTimer.singleShot(50, self._load_figures)

    def _load_figures(self):
        if self._figures_waiting is None:
            return
        if self._pending or self.jobs.has_active_jobs:
            QTimer.singleShot(50, self._load_figures)
            return
        identity = self._figures_waiting
        if not self._current_figure_request(identity):
            self._figures_waiting = None
            return

        def load():
            from mif_pipeline.orchestration.cluster.cluster_review_figures import materialize_figures

            return materialize_figures(
                self.client.review_figures(*identity[:2],
                    attribute=(identity[2] or {}).get("attribute", "cluster_id")),
                self.review_folder / "figures"
            )

        self._figures_waiting = None
        self._figures_loading = identity
        self._start("figures", load)

    def _current_figure_request(self, request):
        identity = self.reviews.current_review_plot_identity()
        return (request is not None and identity is not None
                and identity[:2] == request[:2]
                and self.reviews.review_plot_options() == request[2])

    def receive_allocated_figures(self, value):
        """Display a retained allocation result only for the current review."""
        from mif_pipeline.orchestration.cluster.cluster_review_figures import materialize_figures
        expected = getattr(self, "_allocated_plot_request", None)
        identity = self.reviews.current_review_plot_identity()
        if (expected is None or identity is None or identity[:2] != expected[:2]
                or self.reviews.review_plot_options() != expected[2]):
            self.message.setPlainText("The UMAP calculation finished. Select Calculate UMAP view for the current settings; saved coordinates will be reused when they match.")
            return
        result = materialize_figures(value, self.review_folder / "figures")
        if self.reviews.set_review_plots(result):
            self._local_figure_paths = {plot.path for plot in result.plots}
            self.message.setPlainText("UMAP view ready. Cluster labels remain available for review.")
        else:
            self.message.setPlainText("The UMAP calculation finished for an earlier review. Open the current review to calculate its view.")

    def _open_plot(self, path):
        if Path(path) in self._local_figure_paths and self.file_operations is not None:
            self._start("plot", lambda: self.file_operations.open_plot(Path(path)))
            return
        if self.file_operations is None or self.mounted_images is None:
            self.message.setPlainText("Configure the mounted cluster folder to open this result.")
            return
        try:
            local = Path(self.mounted_images.local_path(str(path)))
        except ValueError as error:
            self.message.setPlainText(str(error))
            return
        self._start("plot", lambda: self.file_operations.open_plot(local))

    def _open_form_reference(self, selected):
        view = self.reviews._current_view
        group = self.reviews._groups.get(getattr(view, "group_id", None))
        target_id = self.reviews._target.currentData()
        target = next((row for row in getattr(group, "targets", ()) if row.target_id == target_id), None)
        if target is None or selected is None:
            return
        entry = (selected,) if isinstance(selected, str) else tuple(selected)
        identity = (target.interaction_request_id, view.expected_state_head_sha256, *entry)
        for i in range(self.annotations.images.count()):
            if tuple(self.annotations.images.itemData(i)) == identity:
                self.annotations.images.setCurrentIndex(i)
                self.annotations.begin()
                return
        self.message.setPlainText("The selected reference is no longer available. Refresh pending reviews.")

    def _annotation_notice(self, _path):
        authoring = self.reviews.take_tissue_authoring_request()
        if authoring is not None:
            self.message.setPlainText(
                "Live tissue-detector training uses the cluster desktop. In streaming mode, select a saved detector or import a compatible detector for this analysis."
            )
            return
        view = self.reviews._current_view
        group = self.reviews._groups.get(getattr(view, "group_id", None))
        target_id = self.reviews._target.currentData()
        target = next((t for t in getattr(group, "targets", ()) if t.target_id == target_id), None)
        if target is None:
            self.message.setPlainText("Select a current image review before opening annotations.")
            return
        if hasattr(view, "form") and view.form.context.get("adapter") == "alignment_setup":
            from mif_pipeline.gui.presenters.reviews import build_batch_interaction_request

            try:
                request = build_batch_interaction_request(
                    view,
                    target_id=target_id,
                    action="import_warpy_transform",
                    raw_values=self.reviews._renderer.raw_values(),
                )
                self.landmarks.begin(request)
            except (OSError, ValueError) as error:
                self.message.setPlainText(str(error))
            return
        display = self.reviews.reference_display_request()
        if (
            display is not None
            and display.get("mode") == "candidate_overlay"
            and self.landmarks.local_project is not None
        ):
            pending = view.form.context["resume_context"]["pending_registration"]
            project = self.landmarks.local_project
            if self.file_operations is None or self.mounted_images is None:
                self.message.setPlainText(
                    "Configure local QuPath and mounted image access to review this alignment."
                )
                return
            self.open_registration_editor(
                lambda: self.file_operations.prepare_cluster_alignment_candidate(
                    project, pending, self.mounted_images
                ),
                kind="candidate",
            )
            return
        from mif_pipeline.orchestration.cluster.cluster_annotations import annotation_entry_ids

        context = target.stage_result.resume_context or {}
        if context.get("schema_version") == "supervised-reference-review/v1":
            entries = {row["project_entry_id"] for row in context["references"]}
        else:
            entries = annotation_entry_ids(target)
        offered = {
            (target.interaction_request_id, view.expected_state_head_sha256, entry)
            for entry in entries
        }
        indices = [
            i
            for i in range(self.annotations.images.count())
            if tuple(self.annotations.images.itemData(i)[:3]) in offered
        ]
        if not indices:
            self.message.setPlainText(
                "This review has no current annotation view. Refresh its inputs before continuing."
            )
            return
        if len(indices) > 1 and self.annotations.images.currentIndex() not in indices:
            self.annotations.images.setCurrentIndex(indices[0])
            self.message.setPlainText(
                "Choose the image under Annotate in QuPath, then select Annotate image."
            )
            return
        if len(indices) == 1:
            self.annotations.images.setCurrentIndex(indices[0])
        self.annotations.begin()

    def _completed(self, identity, result):
        name = self._pending.pop(identity, None)
        if name is None:
            return
        self.refresh_button.setEnabled(True)
        if name.startswith("registration-"):
            try:
                self._registration_completed(name, result)
            except (OSError, ValueError, RuntimeError) as error:
                self.message.setPlainText(str(error))
            return
        if name.startswith("landmark-"):
            try:
                self.landmarks.completed(name, result)
            except (OSError, ValueError, TypeError, KeyError) as error:
                self.landmarks.failed(name)
                self.message.setPlainText(str(error))
            return
        if name == "retained-check":
            try:
                self.retained_draft.completed(result)
            except (OSError, ValueError, TypeError, KeyError) as error:
                self.message.setPlainText(str(error))
            return
        if name == "retained-open":
            self.message.setPlainText(
                "Saved draft closed. It remains available for reference; return annotations only through the current review."
            )
            return
        if name == "figures":
            requested = self._figures_loading
            self._figures_loading = None
            if self._current_figure_request(requested) and self.reviews.set_review_plots(result):
                self._local_figure_paths = {plot.path for plot in result.plots}
                self.message.setPlainText("Current review figures are ready.")
            if self._figures_waiting is not None:
                QTimer.singleShot(0, self._load_figures)
            return
        if name == "alignment":
            self._alignment_receipt = result
            self._poll.start()
            self.message.setPlainText(
                "Alignment queued on the cluster. The next registration review will open when the calculation finishes."
            )
            return
        if name == "alignment-result":
            if result.get("status") == "pending":
                return
            self._alignment_receipt = None
            self._poll.stop()
            self.reviews.set_response_status("accepted_for_retry")
            self.refresh_reviews()
            return
        if name.startswith("annotation-"):
            try:
                self.annotations.completed(name, result)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.annotations.failed()
                self.message.setPlainText(str(error))
            return
        if name == "load":
            # A cluster project path is not a local editable project.
            self.annotations.configure(result)
            self.reviews.remote_kronos_setup = result.kronos_setup or {}
            self.reviews.remote_segmentation_settings = {
                "model_path": result.segmentation_model_directory,
                "model_directories": result.segmentation_model_directories or {},
                "device": {"cuda": "gpu"}.get(self.client.profile.device, self.client.profile.device),
            }
            page = replace(result.page, qupath_project_path=None)
            self.reviews.set_page(page, expected_state_head_sha256=result.state_head_sha256)
            if page.groups:
                self._poll.stop()
                self.reviews.show_group(page.groups[0].group_id)
                self.message.setPlainText(
                    ""
                    if self.embedded
                    else "Review the current choices below. Image processing continues on the cluster."
                )
            else:
                self.message.setPlainText(
                    "No input is currently waiting. Processing may be running or complete; check analysis progress for its current state."
                )
        elif name == "save":
            accepted = all(
                item.get("status") in {"accepted_for_retry", "accepted", "accepted_for_application"}
                for item in result.get("applications", ())
            ) and bool(result.get("applications"))
            if not accepted:
                self.reviews.set_response_status("rejected")
                self.message.setPlainText(
                    "The cluster did not accept this choice. Your entries are retained; refresh to compare the current review."
                )
                return
            self.reviews.set_response_status("accepted_for_retry")
            self.message.setPlainText("Review saved on the cluster.")
            if self._accepted_continue:
                self._start("resume", lambda: self.client.submit("resume"), write=True)
            else:
                self.refresh_reviews()
        elif name == "plot":
            self.message.setPlainText("Result opened from the mounted cluster folder.")
        elif name == "resume":
            self.message.setPlainText(
                "Processing queued on the cluster. Pending reviews will refresh while this window remains open."
            )
            self._poll.start()

    def _failed(self, identity, error):
        name = self._pending.pop(identity, None)
        if name is None:
            return
        self.refresh_button.setEnabled(True)
        self._poll.stop()
        if name.startswith("registration-"):
            message = getattr(error, "message", None) or str(error)
            self._registration_error = "The alignment editor could not finish. " + message
            if name == "registration-worker":
                self._registration_result = None
                if self._registration_session.draft_error:
                    self.landmarks.storage_error = self._registration_session.draft_error
                    self.landmarks._buttons()
                self._registration_session.worker_finished(message)
                self._registration_editor.worker_finished(message)
            elif name == "registration-prepare":
                self._registration_kind = None
            elif name == "registration-cleanup":
                # Keep the session until its cleanup succeeds; it still owns
                # temporary exchange files and must not be silently discarded.
                self.message.setPlainText("The alignment exchange could not be released. " + message)
                return
            self.message.setPlainText("The alignment editor could not finish. " + message)
            return
        if name == "figures":
            requested = self._figures_loading
            self._figures_loading = None
            if self._current_figure_request(requested):
                self.reviews.set_review_plots_failure(*requested[:2],
                    getattr(error, "message", None) or str(error))
            if self._figures_waiting is not None:
                QTimer.singleShot(0, self._load_figures)
            return
        if name.startswith("annotation-"):
            self.annotations.failed()
        if name.startswith("landmark-"):
            self.landmarks.failed(name)
        if name in {"save", "alignment", "alignment-result"}:
            self._alignment_receipt = None
            self.reviews.set_response_status("rejected")
        message = getattr(error, "message", None) or str(error)
        self.message.setPlainText("The cluster operation could not be completed. " + message[:1600])

    def reject(self):
        if self._registration_session is not None:
            self._registration_close_pending = True
            if self._registration_editor is not None:
                self._registration_editor.request_close()
            elif not self._pending:
                self._start("registration-cleanup", self._registration_session.cleanup)
            return
        if not self.landmarks.may_close():
            return
        if self._pending or self.jobs.has_active_jobs:
            self.message.setPlainText(
                "Wait for the current cluster operation to finish before closing."
            )
            return
        self._figures_waiting = None
        self._poll.stop()
        self.annotations.stop()
        self.landmarks.stop()
        super().reject()

    def closeEvent(self, event):
        if self._registration_session is not None:
            event.ignore()
            self.reject()
            return
        if not self.landmarks.may_close():
            event.ignore()
            return
        if self._pending or self.jobs.has_active_jobs:
            self.message.setPlainText(
                "Wait for the current cluster operation to finish before closing."
            )
            event.ignore()
            return
        self._figures_waiting = None
        self._poll.stop()
        self.annotations.stop()
        self.landmarks.stop()
        super().closeEvent(event)
