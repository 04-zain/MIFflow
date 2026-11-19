"""Route local and cluster presentation through the existing execution owners.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import replace
from pathlib import Path
import hashlib, json, time, uuid
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget
from mif_pipeline.orchestration.remote_compute import RemoteProfile, RemoteComputeClient
from ..execution_location import load_location, save_location, streaming_image_access
from ..mounted_images import MountedImageAccess
from ..cluster_sample_projection import cluster_samples
from .computing_page import ComputingPage
from .cluster_workflow_pages import ClusterPlanPage, ClusterRunPage, ExecutionRoute
from .form_sections import label, WorkbenchMessage
from .jobs import JobAccess

# ==============================================================================
#                                 Execution flow
# ==============================================================================


class ExecutionFlow(QObject):
    def __init__(self, controller, draft, setup_path, plan, run, reviews, results):
        super().__init__(controller)
        self.controller = controller
        self.jobs = controller.jobs
        self._pending = {}
        self._stage_dialog = None
        self._stage_workflow = ()
        self._cycle_review_route = None
        self._stage_poll = QTimer(self)
        self._stage_poll.setInterval(5000)
        self._stage_poll.timeout.connect(self._poll_stage_operation)
        folder = (
            Path(setup_path).parent
            if setup_path is not None
            else draft.output_root / draft.batch_id
        )
        self.settings_path = folder / "analysis.computing.json"
        self.valid = True
        try:
            self.settings = load_location(self.settings_path)
        except (OSError, ValueError) as error:
            self.settings = {"mode": "local", "device": "auto"}
            self.valid = False
            self._load_error = str(error)
        self.local_plan = plan
        self.computing = ComputingPage()
        self.plan = ClusterPlanPage()
        self.run = ClusterRunPage()
        self.reviews = None
        self.review_host = QStackedWidget()
        placeholder = WorkbenchMessage(self.review_host)
        placeholder.setPlainText(
            "Complete Computing before opening the cluster reviews."
        )
        self.review_host.addWidget(placeholder)
        self.result_message = WorkbenchMessage(controller.shell)
        self.result_message.setPlainText(
            "Cluster results are written to the selected analysis folder. Open completed images and review figures from the mounted folder."
        )
        self.routes = {
            "plan": ExecutionRoute(plan, self.plan),
            "run": ExecutionRoute(run, self.run),
            "reviews": ExecutionRoute(reviews, self.review_host),
            "results": ExecutionRoute(results, self.result_message),
        }
        self.computing.configure_requested.connect(self.open_settings)
        controller.workspace_opened.connect(self._sync_pages)
        self.computing.help_requested.connect(self.show_help)
        self.computing.continue_requested.connect(self.continue_to_samples)
        self.plan.start_requested.connect(self.start_cluster)
        self.run.start_requested.connect(self.start_cluster)
        self.run.reviews_requested.connect(lambda: controller.shell.show_route("reviews"))
        self.run.refresh_requested.connect(self.refresh_progress)
        self.run.resume_requested.connect(self.resume_cluster)
        self.run.options_requested.connect(lambda: self._submit_stage_operation("stage-options"))
        self.run.retry_requested.connect(lambda: self._submit_stage_operation("list-retries"))
        self.run.continue_requested.connect(self._continue_saved_cluster)
        self.run.reconcile_requested.connect(self.check_submission)
        self.run.processors_requested.connect(lambda: self._submit_stage_operation("runtime-capabilities"))
        self.jobs.job_succeeded.connect(self._completed)
        self.jobs.job_failed.connect(self._failed)
        self._sync_pages()
        if not self.valid:
            self.computing.message.setPlainText(self._load_error)

    def dispose(self):
        """Stop polling and disconnect this analysis from shared application signals."""
        self._stage_poll.stop()
        self.controller.workspace_opened.disconnect(self._sync_pages)
        self.jobs.job_succeeded.disconnect(self._completed)
        self.jobs.job_failed.disconnect(self._failed)
        if self._stage_dialog is not None:
            self._stage_dialog.reject()
            self._stage_dialog.deleteLater()
        self.deleteLater()

    @property
    def mode(self):
        return self.settings.get("mode", "local")

    @property
    def prepared(self):
        return self.settings.get("remote_prepared") is not None

    def _sync_pages(self):
        for plan in (self.local_plan, self.plan):
            plan.set_computing(self.settings, self.settings_path.parent, valid=self.valid)
        for page in self.routes.values():
            page.use_remote(self.mode != "local")
        self.computing.set_location(self.settings, locked=self.prepared or self.controller.session is not None)
        self.run.start.setEnabled(not self.prepared)
        self.run.resume.setVisible(self.prepared)
        self.run.resume.setEnabled(
            self.prepared
            and bool(self.settings.get("remote_receipt") or self.settings.get("remote_opened"))
            and not self.settings.get("submission_uncertain")
            and not self._pending
        )
        stage_pending = self._stage_receipt() is not None
        uncertain = bool(self.settings.get("submission_uncertain"))
        self.run.reconcile.setVisible(self.mode == "streaming" and uncertain)
        self.run.reconcile.setEnabled(not self._pending)
        for button in (self.run.options, self.run.retry, self.run.continue_saved, self.run.processors):
            button.setVisible(self.mode == "streaming" and self.prepared)
            button.setEnabled(self.prepared and not self._pending and not stage_pending
                              and not self.settings.get("submission_uncertain"))
        if stage_pending and not uncertain:
            self.run.resume.setEnabled(False)
            self._stage_poll.start()
        if uncertain:
            self.run.start.setEnabled(False)
            self.run.status.setPlainText(
                "The last submission could not be confirmed. Choose Check submission to reconnect to that job before submitting again."
            )
        elif self.settings.get("remote_opened"):
            self.run.start.setEnabled(False)
            self.run.status.setPlainText(
                "Saved cluster analysis opened. Refresh progress or open the current reviews. Resume continues from its saved state."
            )
        elif self.settings.get("remote_receipt"):
            self.run.status.setPlainText(
                "The cluster job has been submitted. Open the current review from Run, or refresh its progress."
            )
        elif self.prepared:
            self.run.status.setPlainText(
                "The cluster setup is saved. Submit the prepared job to start processing."
            )
            self.run.start.setEnabled(True)
        if self.mode == "desktop":
            self.run.status.setPlainText(
                "Open this application inside the cluster desktop and select This computer there to run the analysis."
            )


    def show_help(self, mode):
        from .cluster_help import show_cluster_setup

        if mode == "local":
            self.computing.message.setPlainText(
                "Open Software setup on Home to check and install the software required for this computer."
            )
        else:
            show_cluster_setup(self.controller.shell, mode=mode)

    def open_settings(self, mode=None):
        controller = self.controller
        mode = mode or self.mode
        if self.jobs.has_active_jobs or controller._close_pending:
            return
        if controller.session is not None or self.prepared:
            self.computing.message.setPlainText(
                "This analysis already has a prepared execution. Its saved processor and processing location remain in use. "
                "Start a new analysis to choose different Computing settings."
            )
            controller.shell.show_route("computing")
            return
        if mode == "desktop":
            self.show_help(mode)
            return
        if controller.samples_page is not None and not controller.samples_page.commit_pending():
            controller.shell.show_route("samples")
            return
        from .compute_workbench import ComputeWorkbench

        dialog = ComputeWorkbench(
            self.jobs,
            controller.shell,
            settings_path=self.settings_path,
            workspace=self.settings_path.parent,
            file_operations=controller.file_operations,
            sample_draft=controller.samples_page.draft,
            selected_mode=mode,
            embedded_workflow=True,
            analysis_setup=controller.setup_document,
        )
        dialog.settings_saved.connect(self.apply_settings)
        dialog.cluster_analysis_opened.connect(self._associated)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        controller._compute_dialog = dialog
        dialog.show()

    def _associated(self, setup, settings):
        c = self.controller
        self.settings = settings
        self.valid = True
        c._setup = setup
        c._setup_dirty = False
        c.samples_page.load_cluster_samples(setup.draft)
        c._setup_dirty = False
        self._sync_pages()
        c.shell.show_route("run")

    def apply_settings(self, value):
        if self.controller.session is not None or self.prepared:
            raise ValueError("The execution location is already in use.")
        self.settings = save_location(self.settings_path, value)
        self.valid = True
        self._sync_pages()
        self.computing.message.setPlainText(
            "Computing settings saved. Continue to Samples to select images once."
        )

    def continue_to_samples(self, mode):
        if mode == "desktop":
            self.show_help(mode)
            return
        try:
            if not self.valid:
                raise ValueError("Review and save Computing settings before continuing.")
            value = dict(self.settings, mode=mode)
            if mode == "streaming":
                streaming_image_access(value)
            self.apply_settings(value)
        except (OSError, ValueError) as error:
            self.computing.message.setPlainText(str(error))
            return
        self.controller.shell.show_route("samples")

    def _current_remote_profile(self):
        """Apply the latest Software setup path to new cluster requests."""
        from ..cluster_paths import with_selected_kronos_model
        return with_selected_kronos_model(
            RemoteProfile.from_mapping(self.settings["remote"]),
            streaming_image_access(self.settings),
        )

    def _selection(self):
        profile = self._current_remote_profile()
        draft = self.controller.samples_page.draft
        if profile.batch_id != draft.batch_id and not self.settings.get("remote_opened"):
            raise ValueError(
                "The cluster analysis name differs from this analysis. Use its own local setup to continue."
            )
        mounted = streaming_image_access(self.settings)
        rows = cluster_samples(draft, mounted)
        material = {
            "host": profile.host,
            "manifest": profile.manifest,
            "output_root": profile.output_root,
            "samples": rows,
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        previous = self.settings.get("remote_prepared")
        if previous is not None and previous.get("selection_sha256") != digest:
            raise ValueError(
                "These samples differ from the setup already prepared on the cluster. Open the saved analysis or create a new one."
            )
        return profile, rows, digest

    def handle_route(self, route):
        if route == "computing":
            if self.prepared or self.controller.session is not None:
                self.computing.set_location(self.settings, locked=True)
            return True
        if self.computing._selected != self.mode and route not in {"home"}:
            self.computing.message.setPlainText(
                "Save the selected computing mode before choosing images or starting an analysis."
            )
            self.controller.shell.show_route("computing")
            return True
        if self.mode == "local" and self.valid:
            return False
        if not self.valid:
            self.controller.shell.show_route("computing")
            return True
        if self.mode == "desktop":
            return route not in {"home", "samples"}
        if route in {"plan", "run"}:
            try:
                if not self.controller.samples_page.commit_pending():
                    return True
                profile, rows, _ = self._selection()
                self.plan.set_samples(rows, profile, prepared=self.prepared)
            except (OSError, ValueError, KeyError) as error:
                self.plan.message.setPlainText(str(error))
                self.plan.start.setEnabled(False)
                self.run.status.setPlainText(str(error))
        elif route == "reviews":
            self.open_reviews()
        elif route == "results":
            p = self.settings["remote"]
            self.result_message.setPlainText(
                "Results are stored on the cluster in "
                + p["output_root"]
                + "/"
                + p["batch_id"]
                + ". Review figures are available in the current review from Run."
            )
        return route not in {"home", "samples"}

    def _start(self, name, function):
        if self._pending or self.jobs.has_active_jobs or self.controller._close_pending:
            return False
        identity = self.jobs.submit(
            "workspace-cluster-" + name,
            function,
            access=JobAccess.READ if name in {"status", "resume_check", "reconcile-submission"} else JobAccess.MUTATION,
        )
        self._pending[identity] = name
        self.run.start.setEnabled(False)
        self.plan.start.setEnabled(False)
        self.run.resume.setEnabled(False)
        for button in (self.run.options, self.run.retry, self.run.continue_saved, self.run.processors):
            button.setEnabled(False)
        if name not in {"status", "resume_check"}:
            self.controller.samples_page.setEnabled(False)
        return True

    def _stage_receipt(self):
        """Retain an allocated control job until its saved result has been received."""
        from mif_pipeline.orchestration.cluster.cluster_annotation_jobs import STAGE_OPERATIONS
        receipt = self.settings.get("remote_receipt")
        return receipt if (receipt and receipt.get("operation") in STAGE_OPERATIONS
                           and not receipt.get("result_received")) else None

    def _retain_submission(self, operation, *, resume_head=None):
        """Save a recoverable identity before any scheduler submission can occur."""
        pending = {"token": uuid.uuid4().hex, "operation": operation, "requested_at": int(time.time())}
        if resume_head is not None:
            pending["resume_from_state_head_sha256"] = resume_head
        self.settings = save_location(self.settings_path, {**self.settings,
            "submission_uncertain": True, "pending_submission": pending})
        return pending["token"]

    def _clear_submission(self, *, receipt=None):
        values = {**self.settings, "submission_uncertain": False}
        values.pop("pending_submission", None)
        if receipt is not None:
            values["remote_receipt"] = receipt
        self.settings = save_location(self.settings_path, values)

    def check_submission(self):
        if self._pending or not self.settings.get("submission_uncertain"):
            return
        pending = self.settings.get("pending_submission")
        if pending is None:
            self.run.status.setPlainText("This submission has no saved identifier. Check its job ID with the cluster administrator before continuing.")
            return
        client = RemoteComputeClient(RemoteProfile.from_mapping(self.settings["remote"]))
        self._start("reconcile-submission", lambda: client.reconcile_submission(pending))

    def _submission_checked(self, value):
        """Reconnect to one known job; an empty query requires explicit review."""
        self.controller.samples_page.setEnabled(True)
        if value.get("status") == "found":
            self._clear_submission(receipt=value["receipt"])
            self._sync_pages()
            self.run.status.setPlainText("Submission recovered. The existing job will be followed; no new job was submitted.")
            if self._stage_receipt() is not None:
                self._stage_poll.start()
            return
        if value.get("status") != "not_found":
            raise ValueError("The cluster did not return a submission lookup result.")
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self.controller.shell)
        box.setWindowTitle("Check cluster submission")
        box.setText("The scheduler has not returned a job for " + value["job_name"] + ".")
        box.setInformativeText("Scheduler accounting can be delayed. Check again after the connection recovers. "
            "Allow another submission only after checking the cluster job history and verifying that this job was not submitted; otherwise the work could run twice.")
        again = box.addButton("Keep waiting", QMessageBox.ButtonRole.RejectRole)
        allow = box.addButton("Allow another submission", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(again)
        box.exec()
        if box.clickedButton() is allow:
            self._clear_submission()
            self._sync_pages()
            self.run.status.setPlainText("You can submit the operation again. No new job has been submitted.")
        else:
            self._sync_pages()
            self.run.status.setPlainText("Submission remains unconfirmed. Choose Check submission again when scheduler records are available.")

    def _submit_stage_operation(self, operation, **selection):
        """Queue the selected shared-service operation once, retaining submission uncertainty."""
        if (self.mode != "streaming" or not self.valid or not self.prepared or self._pending
                or self.jobs.has_active_jobs or self.controller._close_pending
                or self.settings.get("submission_uncertain") or self._stage_receipt() is not None):
            if self._stage_dialog is not None:
                self._stage_dialog.show_error("Wait for the submitted cluster operation. Refresh progress before submitting another change.")
            return False
        if self.reviews is not None and (self.reviews.landmarks.unfinished
                or not self.reviews.annotations.returned or self.reviews.annotations.receipt is not None
                or self.reviews._alignment_receipt is not None):
            self.run.status.setPlainText("Finish or retain the current annotation or alignment review before changing saved work.")
            return False
        try:
            client = RemoteComputeClient(RemoteProfile.from_mapping(self.settings["remote"]))
            token = self._retain_submission(operation)
            if not self._start("stage-submit", lambda: client.submit_stage_operation(operation, token=token, **selection)):
                self._clear_submission()
                return False
            self._sync_pages()
            return True
        except (OSError, ValueError, KeyError) as error:
            self.run.status.setPlainText(str(error))
            return False

    def _poll_stage_operation(self):
        if self.settings.get("submission_uncertain"):
            return
        receipt = self._stage_receipt()
        if receipt is None:
            self._stage_poll.stop()
            return
        if self._pending or self.jobs.has_active_jobs or self.controller._close_pending:
            return
        client = RemoteComputeClient(RemoteProfile.from_mapping(self.settings["remote"]))
        self._start("stage-result", lambda: client.stage_operation_result(receipt))

    def _continue_saved_cluster(self):
        # The current head is obtained in an allocation, too: opening the
        # installed runtime may inspect native classes on first use.
        self._submit_stage_operation("status")

    def _choose_stage(self, rows, title):
        from PySide6.QtWidgets import QInputDialog
        from ..presenters.workflow import BY_KIND
        labels = []
        for index, row in enumerate(rows, 1):
            kind = row.get("kind", row.get("stage_kind", ""))
            stage = BY_KIND.get(kind)
            label = stage.title if stage else kind.replace("_", " ").capitalize()
            samples = row.get("sample_ids", ()) or ((row["sample_id"],) if row.get("sample_id") else ())
            labels.append(f"{index}. {label}" + (" — " + ", ".join(samples) if samples else ""))
        if not labels:
            self.run.status.setPlainText("No current steps are available for this action. Complete the pending review or refresh progress.")
            return None
        text, accepted = QInputDialog.getItem(self.run, title, "Step", labels, 0, False)
        return rows[labels.index(text)] if accepted else None

    def _open_acquisition_cycle_options(self, review_node_id, expected_head):
        """Resolve the channel review's Inspection using the existing host inventory."""
        if self._submit_stage_operation('stage-options', expected_head=expected_head):
            self._cycle_review_route = (review_node_id, expected_head)

    def _open_cluster_options(self, value, *, inspection_action='change_source'):
        from mif_pipeline.orchestration.batch_stage_options_transport import decode_options, decode_workflow
        from .stage_options import StageOptionsDialog
        options = decode_options(value["options"], remote=True)
        if value.get("workflow_nodes") is not None:
            self._stage_workflow = decode_workflow(value["workflow_nodes"])
        if self._stage_dialog is not None:
            if self._stage_dialog.options == options:
                return self._stage_dialog
            self._stage_dialog.set_applying(False)
            self._stage_dialog.reject()
        mounted = MountedImageAccess.from_mapping(self.settings["mounted_images"])
        dialog = StageOptionsDialog(options, self._stage_workflow, self.run,
            remote=True, mounted_images=mounted, inspection_action=inspection_action)
        self._stage_dialog = dialog
        selection = {"node_id": options.node_id, "expected_head": options.state_head_sha256}
        dialog.preview_requested.connect(lambda settings: self._submit_stage_operation(
            "preview-stage-revision", settings=settings, **selection))
        def apply(preview):
            if self._submit_stage_operation("revise-stage", settings=preview.settings, **selection):
                dialog.set_applying(True)
        dialog.apply_requested.connect(apply)
        if options.kind in {"inspection", "registration"}:
            dialog.form.inspection_requested.connect(lambda path: self._submit_stage_operation(
                "stage-options", source_image=str(path), **selection))
        dialog.finished.connect(lambda _result: setattr(self, "_stage_dialog", None))
        dialog.show()
        return dialog

    def _show_stage_result(self, operation, value):
        """Present one received result without replaying its native or scientific work."""
        if operation == "review-figures":
            if self.reviews is not None:
                self.reviews.receive_allocated_figures(value)
            else:
                self.run.status.setPlainText("UMAP coordinates are saved. Open the cluster review to display them.")
        elif operation == "runtime-capabilities":
            from ..compute_copy import processor_report

            self.run.status.setPlainText(processor_report(value))
        elif operation == "status":
            if value.get("continuation_available"):
                self._submit_stage_operation("continue", expected_head=value["state_head_sha256"])
            else:
                self.run.status.setPlainText("Use Resume for a paused analysis, or complete its current review before continuing.")
        elif operation == "stage-options":
            cycle_route = self._cycle_review_route
            if cycle_route is not None:
                from mif_pipeline.orchestration.batch_stage_options_transport import decode_workflow
                from mif_pipeline.orchestration.batch_stage_settings import inspection_review_target
                review_node, head = cycle_route
                self._stage_workflow = decode_workflow(value['workflow_nodes'])
                node_id = inspection_review_target(self._stage_workflow, review_node)
                if 'choices' in value:
                    if value['state_head_sha256'] != head:
                        raise ValueError('The analysis changed. Reopen its current channel review.')
                    if not self._submit_stage_operation('stage-options', node_id=node_id, expected_head=head):
                        self._cycle_review_route = None
                else:
                    options = value['options']
                    if (options['node_id'] != node_id or options['state_head_sha256'] != head
                            or options['kind'] != 'inspection'):
                        raise ValueError('The image inspection changed. Reopen its current review.')
                    self._cycle_review_route = None
                    self._open_cluster_options(value, inspection_action='review_acquisition_cycles')
            elif "choices" in value:
                selected = self._choose_stage(value["choices"], "Change a completed step")
                if selected is not None:
                    self._submit_stage_operation("stage-options", node_id=selected["node_id"],
                        expected_head=value["state_head_sha256"])
            else:
                dialog = self._open_cluster_options(value)
                if "candidates" in value:
                    dialog.form.set_inventory(value)
        elif operation == "preview-stage-revision":
            from mif_pipeline.orchestration.batch_stage_options_transport import decode_preview
            if self._stage_dialog is None:
                self.run.status.setPlainText("The preview completed. Open the saved step again to review and apply its options.")
                return
            dialog = self._stage_dialog
            dialog.set_preview(decode_preview(value, settings=dialog._selected_settings(), remote=True))
        elif operation == "revise-stage":
            if self._stage_dialog is not None:
                self._stage_dialog.set_applying(False)
                self._stage_dialog.accept()
            self.run.status.setPlainText("Revised options saved. Choose Continue saved work to open the next review or process affected steps.")
        elif operation == "list-retries":
            selected = self._choose_stage(value["choices"], "Retry a failed step")
            if selected is not None:
                self._submit_stage_operation("retry", node_id=selected["node_id"],
                    expected_head=value["state_head_sha256"], terminal_record=selected["terminal_record_sha256"])
        elif operation in {"retry", "continue"}:
            outcome = value.get("outcome", "")
            self.run.status.setPlainText(
                "Processing reached its next review. Open the current review from Run to continue." if outcome == "needs_interaction"
                else "Analysis completed. Its results are saved in the cluster analysis folder." if outcome == "completed"
                else "Processing stopped. Refresh progress for the recorded cause, then retry or change the relevant saved step.")

    def save_workspace_draft(self, draft, path):
        if self.mode != "streaming":
            self.computing.message.setPlainText(
                "Create and save this analysis inside the cluster desktop."
            )
            return
        if self.prepared:
            self.plan.message.setPlainText("The sample list is already prepared on the cluster.")
            return
        expected = self.settings_path.parent / "analysis.manifest.json"
        if Path(path) != expected:
            self.plan.message.setPlainText(
                "Save this streaming workspace in its current analysis folder so its connection settings remain associated with it."
            )
            return
        from ..manifest_draft import write_manifest_draft

        self._start("save_draft", lambda: write_manifest_draft(draft, path))

    def _queue_submission(self):
        if self.settings.get("submission_uncertain") or self.settings.get("remote_receipt"):
            return
        token = self._retain_submission("run")
        if not self._start("submit", lambda: self._submission_client.submit("run", token=token)):
            self._clear_submission()

    def resume_cluster(self):
        if (
            self.mode != "streaming"
            or not self.valid
            or not self.prepared
            or self._pending
            or self.jobs.has_active_jobs
            or self.controller._close_pending
            or self.settings.get("submission_uncertain")
        ):
            return
        if self.reviews is not None and (
            self.reviews.landmarks.unfinished
            or not self.reviews.annotations.returned
            or self.reviews.annotations.receipt is not None
            or self.reviews._alignment_receipt is not None
        ):
            self.run.status.setPlainText(
                "Finish the current annotation or alignment review before resuming."
            )
            return
        try:
            self._submission_client = RemoteComputeClient(self._current_remote_profile())
            self._submitted_profile = self._submission_client.profile
            self._start("resume_check", self._submission_client.resume_readiness)
        except (ValueError, KeyError) as error:
            self.run.status.setPlainText(str(error))

    def _resume_checked(self, value):
        import re

        if (
            not isinstance(value, dict)
            or type(value.get("can_resume")) is not bool
            or not re.fullmatch("[0-9a-f]{64}", str(value.get("state_head_sha256", "")))
        ):
            raise ValueError("Refresh the current cluster status before resuming.")
        if not value["can_resume"]:
            self._sync_pages()
            self.run.status.setPlainText(value["reason"])
            return
        head = value["state_head_sha256"]
        if self.settings.get("remote_receipt", {}).get("resume_from_state_head_sha256") == head:
            self._sync_pages()
            self.run.status.setPlainText(
                "A resume job was already submitted from this saved state. Refresh progress or inspect its job log."
            )
            return
        if self.controller._close_pending:
            return
        token = self._retain_submission("resume", resume_head=head)
        self._resume_from_head = head
        if not self._start("resume_submit", lambda: self._submission_client.submit("resume", token=token)):
            self._clear_submission()

    def start_cluster(self):
        c = self.controller
        if self.settings.get("remote_opened"):
            self.resume_cluster()
            return
        if (
            self.mode != "streaming"
            or not self.valid
            or self._pending
            or self.jobs.has_active_jobs
            or c._close_pending
        ):
            return
        if self.settings.get("remote_receipt") or self.settings.get("submission_uncertain"):
            self._sync_pages()
            self.run.status.setPlainText(
                "A job was submitted or its submission could not be confirmed. Check its progress or open the current reviews."
            )
            return
        try:
            if not c.samples_page.commit_pending():
                return
            profile, rows, digest = self._selection()
            client = RemoteComputeClient(profile)
            self._submission_client = client
            self._selection_digest = digest
            self._submitted_profile = profile
            if self.prepared:
                self._queue_submission()
            else:
                setup = c.setup_document
                snapshot = replace(setup, draft=c.samples_page.draft) if setup is not None else None
                self._prepare_call = lambda: (
                    client.prepare_analysis(rows),
                    digest,
                    len(rows),
                )
                if snapshot is not None:
                    from ..analysis_setup import save_analysis_setup

                    self._start("save_setup", lambda: save_analysis_setup(snapshot))
                else:
                    self._start("prepare", self._prepare_call)
            self.run.status.setPlainText(
                "Preparing the cluster analysis and submitting its processing job…"
            )
            c.shell.show_route("run")
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            self.run.status.setPlainText(str(error))

    def _completed(self, identity, value):
        name = self._pending.pop(identity, None)
        if name is None:
            return
        try:
            if name == "reconcile-submission":
                self._submission_checked(value)
                return
            if name == "stage-submit":
                if value.get("host") != self.settings["remote"]["host"] or value.get("batch_id") != self.settings["remote"]["batch_id"]:
                    raise ValueError("The operation receipt belongs to another cluster analysis.")
                self._clear_submission(receipt=value)
                self._sync_pages()
                self.run.status.setPlainText("Operation queued on the cluster. Progress and its result remain available after reconnecting.")
                self._stage_poll.start()
                return
            if name == "stage-result":
                if value.get("status") == "pending":
                    self.run.status.setPlainText("Waiting for the submitted cluster operation. No additional job has been queued.")
                    return
                receipt = self._stage_receipt()
                self._stage_poll.stop()
                self.settings = save_location(self.settings_path, {**self.settings,
                    "remote_receipt": {**receipt, "result_received": True}})
                self._sync_pages()
                self.controller.samples_page.setEnabled(True)
                self._show_stage_result(receipt["operation"], value)
                return
            if name == "save_draft":
                self.controller.samples_page.setEnabled(True)
                self.plan.message.setPlainText(
                    "Sample list saved. Start the analysis from Plan to prepare it on the cluster."
                )
                self.run.status.setPlainText(
                    "Sample list saved on this computer; cluster processing has not started."
                )
                return
            if name == "save_setup":
                self.controller._setup = value
                self.controller._setup_dirty = self.controller.samples_page.draft != value.draft
                if self.controller._setup_dirty:
                    raise ValueError(
                        "Samples changed while their setup was saving. Review Plan before starting."
                    )
                if not self.controller._close_pending:
                    self._start("prepare", self._prepare_call)
                return
            if name == "prepare":
                result, digest, count = value
                if (
                    result.get("manifest") != self._submitted_profile.manifest
                    or result.get("batch_id") != self._submitted_profile.batch_id
                    or result.get("sample_count") != count
                ):
                    raise ValueError("The cluster preparation returned a different analysis.")
                self.settings = save_location(
                    self.settings_path,
                    {
                        **self.settings,
                        "remote_prepared": {"selection_sha256": digest, "result": result},
                    },
                )
                self.controller.samples_page.lock_for_plan()
                self.controller.samples_page.setEnabled(True)
                self._sync_pages()
                if not self.controller._close_pending:
                    self._queue_submission()
            elif name == "resume_check":
                self._resume_checked(value)
            elif name in {"submit", "resume_submit"}:
                if name == "resume_submit":
                    value = {**value, "resume_from_state_head_sha256": self._resume_from_head}
                if (
                    value.get("batch_id") != self._submitted_profile.batch_id
                    or value.get("host") != self._submitted_profile.host
                ):
                    raise ValueError("The submitted job belongs to a different cluster analysis.")
                RemoteComputeClient._job(value["job_id"])
                self._clear_submission(receipt=value)
                self.controller.samples_page.setEnabled(True)
                self._sync_pages()
                self.run.status.setPlainText(
                    "Analysis submitted on the cluster. Open the current review from Run to complete the next required input."
                )
            elif name == "status":
                from ..compute_copy import status_message

                self._sync_pages()
                self.run.status.setPlainText(status_message("analysis_status", value))
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._cycle_review_route = None
            self.controller.samples_page.setEnabled(True)
            self._sync_pages()
            self.run.status.setPlainText(str(error))
            if name == "stage-result" and self._stage_dialog is not None:
                self._stage_dialog.set_applying(False)
                self._stage_dialog.show_error(str(error))
            self.run.start.setEnabled(
                not bool(
                    self.settings.get("remote_receipt") or self.settings.get("submission_uncertain")
                )
            )

    def _failed(self, identity, error):
        name = self._pending.pop(identity, None)
        if name is None:
            return
        if name in {'stage-submit', 'stage-result'}:
            self._cycle_review_route = None
        if name == "stage-result" and getattr(error, "error_type", "") == "ClusterJobFailed":
            receipt = self._stage_receipt()
            self._stage_poll.stop()
            if receipt is not None:
                self.settings = save_location(self.settings_path, {**self.settings,
                    "remote_receipt": {**receipt, "result_received": True}})
        if self._stage_dialog is not None and name in {"stage-submit", "stage-result"}:
            self._stage_dialog.set_applying(False)
            self._stage_dialog.show_error(getattr(error, "message", str(error)),
                                          diagnostic=getattr(error, "diagnostic", None))
        self.controller.samples_page.setEnabled(True)
        self._sync_pages()
        self.run.status.setPlainText(getattr(error, "message", str(error)))
        if name in {"stage-submit", "stage-result"} and self.reviews is not None:
            identity = self.reviews.reviews.current_review_plot_identity()
            if identity is not None:
                self.reviews.reviews.set_review_plots_failure(identity[0], identity[1],
                    getattr(error, "message", str(error)) + " Check submission on Run if the submission could not be confirmed.")
        if name in {"stage-submit", "submit", "resume_submit", "reconcile-submission"} and self.settings.get("submission_uncertain"):
            self.run.status.setPlainText(getattr(error, "message", str(error))
                + " Submission could not be confirmed. Choose Check submission to reconnect before submitting again.")
        self.run.start.setEnabled(
            not bool(
                self.settings.get("remote_receipt") or self.settings.get("submission_uncertain")
            )
        )
        self.plan.start.setEnabled(not self.prepared)

    def refresh_progress(self):
        if self.mode != "streaming":
            return
        if self.settings.get("submission_uncertain"):
            self.check_submission()
            return
        if self._stage_receipt() is not None:
            self._poll_stage_operation()
            return
        try:
            client = RemoteComputeClient(RemoteProfile.from_mapping(self.settings["remote"]))
            self._start("status", client.analysis_status)
        except (ValueError, KeyError) as error:
            self.run.status.setPlainText(str(error))

    def open_reviews(self):
        if self.mode != "streaming" or not self.valid:
            return
        if not self.prepared:
            self.review_host.widget(0).setPlainText(
                "Start this cluster analysis from Plan or Run. Its forms will appear here when input is required."
            )
            return
        try:
            client = RemoteComputeClient(self._current_remote_profile())
        except (ValueError, KeyError) as error:
            if self.reviews is not None:
                self.reviews.message.setPlainText(str(error))
            else:
                self.review_host.widget(0).setPlainText(str(error))
            return
        if self.reviews is None:
            from .cluster_reviews import ClusterReviewsDialog

            mounted = MountedImageAccess.from_mapping(self.settings.get("mounted_images"))
            self.reviews = ClusterReviewsDialog(
                client,
                self.jobs,
                self.review_host,
                file_operations=self.controller.file_operations,
                mounted_images=mounted,
                review_folder=self.settings_path.parent / "cluster-annotation-drafts",
                embedded=True,
                retained_draft=self.settings.get("retained_annotation_draft"),
            )
            self.reviews.back_to_run_requested.connect(lambda: self.controller.shell.show_route("run"))
            self.reviews.retained_draft_returned.connect(self._retained_returned)
            self.reviews.reviews.acquisition_cycle_review_requested.connect(self._open_acquisition_cycle_options)
            self.reviews.umap_requested.connect(lambda request, head, options:
                self._submit_stage_operation("review-figures", review_request_id=request,
                    expected_head=head, plot_options=options))
            self.review_host.addWidget(self.reviews)
        if not self.reviews._pending:
            self.reviews.client = client
        self.review_host.setCurrentWidget(self.reviews)
        self.reviews.refresh_reviews()

    def _retained_returned(self, project):
        if self.settings.get("retained_annotation_draft") != project:
            return
        value = dict(self.settings)
        value.pop("retained_annotation_draft")
        try:
            self.settings = save_location(self.settings_path, value)
        except (OSError, ValueError) as error:
            self.reviews.message.setPlainText(
                "Annotations were accepted, but the local draft notice could not be cleared: "
                + str(error)
            )

    def may_close(self):
        if self.reviews is not None and not self.reviews.landmarks.may_close():
            return False
        if self.jobs.has_active_jobs:
            return True
        if self.reviews is not None and not self.reviews.annotations.returned:
            project = self.reviews.annotations.local_project
            if project is None:
                return True
            if self.settings.get("retained_annotation_draft") == str(project):
                return True
            from PySide6.QtWidgets import QMessageBox

            answer = QMessageBox.question(
                self.controller.shell,
                "Keep annotation draft and close?",
                "The saved annotations have not been accepted by the cluster. Keep the local draft and close this analysis?\n\nDraft: "
                + str(project),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    self.settings = save_location(
                        self.settings_path,
                        {**self.settings, "retained_annotation_draft": str(project)},
                    )
                except (OSError, ValueError) as error:
                    self.reviews.message.setPlainText(str(error))
                    return False
                return True
            self.controller.shell.show_route("reviews")
            return False
        return True
