"""Keep saved choices until their normal revision produces a current review."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import QTimer

from mif_pipeline.gui.tissue_setup import observe_tissue_classifier_source, normalized_tissue_source_path
from mif_pipeline.gui.presenters.reviews import build_batch_interaction_form, build_batch_interaction_request, BatchInteractionForm
from .jobs import JobAccess
from .tissue_saved_settings import SavedTissueEditor


class TissueNavigation:
    def __init__(self, controller):
        self.controller = controller
        self.editor = None
        self.session = None
        self.jobs = {}
        self.draft = None
        self.pending_node_id = None
        self.revision_job = None
        self.authoring_editor = None
        self.feature_editor = None

    def reset(self):
        """Discard this session's presentation draft when its workspace is replaced."""
        if self.editor is not None:
            self.editor.deleteLater()
        self.editor = self.session = self.draft = self.pending_node_id = None
        self.authoring_editor = self.revision_job = None
        self.feature_editor = None
        self.jobs.clear()

    @property
    def visible(self):
        page = getattr(self.controller, "reviews_page", None)
        return bool(self.editor is not None and page is not None and page._workspace.currentWidget() is self.editor
                    and self.controller.shell.current_route_id == "reviews")

    def open(self, options):
        c = self.controller
        if options.saved_tissue is None and options.saved_unmixing is None:
            return False
        # One retained editor per sample/head. Back and workflow viewing keep its draft.
        if (self.editor is None or self.session is not c.session
                or self.editor.options.kind == "secondary_structure_definition"
                or (self.editor.options.saved_unmixing is None) != (options.saved_unmixing is None)
                or self.editor.options.sample_id != options.sample_id
                or self.editor.options.state_head_sha256 != options.state_head_sha256):
            if self.editor is not None:
                self.editor.deleteLater()
            if options.saved_unmixing is not None:
                from .unmixing_saved_settings import SavedUnmixingEditor
                self.editor = SavedUnmixingEditor(options, c.reviews_page)
            else:
                self.editor = SavedTissueEditor(options, c.reviews_page)
            self.session = c.session
            self.draft = None
            self.pending_node_id = None
            self.editor.finished.connect(self.finish_view)
            self.editor.changed_save.connect(self.save)
            self.editor.preview_requested.connect(self.preview)
            if options.saved_tissue is not None:
                self.editor.source_selected.connect(self.observe_source)
                self.editor.authoring_requested.connect(self.author)
                self.editor.feature_inspection_requested.connect(self.inspect_features)
            c.reviews_page._workspace.addWidget(self.editor)
        if options.saved_tissue is not None:
            self.editor.set_within_node(self._unfinished_tissue(options))
        c._workflow_review_node_ids = ()
        c.reviews_page.set_workflow_context("Spectral unmixing" if options.saved_unmixing is not None else "Tissue detection")
        c.reviews_page._workspace.setCurrentWidget(self.editor)
        c.shell.show_route("reviews")
        return True

    def back_to_definition(self):
        from .secondary_navigation import definition_predecessor
        c = self.controller
        page, session = c.reviews_page, c.session
        view = page._current_view
        if (type(view) is not BatchInteractionForm or not view.form.context.get("secondary")
                or session is None or session.last_status is None or c._close_pending
                or view.expected_state_head_sha256 != session.last_status.state_head_sha256):
            return
        group = page._groups.get(view.group_id)
        target = next((item for item in getattr(group, "targets", ())
                       if item.target_id == page._target.currentData()), None)
        if target is None or target.target_id != view.representative_target_id:
            return
        node_id = definition_predecessor(session.last_status.workflow_nodes, target.node_id)
        if node_id is None:
            c.return_to_workflow()
            return
        head = view.expected_state_head_sha256
        if any(operation == "open_secondary" for operation, _ in self.jobs.values()):
            return
        job = c.jobs.submit("stage_options", lambda: session.service.stage_options(
            session.batch_id, node_id=node_id, expected_head_sha256=head), access=JobAccess.READ)
        self.jobs[job] = ("open_secondary", (session, view, target, node_id))

    def _open_secondary(self, options, origin):
        from .secondary_navigation import SavedSecondaryEditor
        c = self.controller
        session, view, target, node_id = origin
        page = c.reviews_page
        if (c._close_pending or c.shell.current_route_id != "reviews" or session is not c.session
                or session.last_status is None or session.last_status.state_head_sha256 != view.expected_state_head_sha256
                or page._current_view is not view or page._target.currentData() != target.target_id):
            return
        if (options.node_id != node_id or options.state_head_sha256 != view.expected_state_head_sha256
                or options.saved_secondary is None):
            page._set_status_message("Reopen the current structure definitions before editing.")
            return
        try:
            editor = SavedSecondaryEditor(options, target.node_id, page._workspace.currentWidget(), page)
        except (OSError, TypeError, ValueError) as error:
            page._set_status_message(str(error))
            return
        self.reset()
        self.session, self.editor = session, editor
        editor.finished.connect(self.finish_view)
        editor.changed_save.connect(self.save)
        page._workspace.addWidget(editor)
        page._workspace.setCurrentWidget(editor)
        editor.form._heading.hide()
        editor.definitions.page_changed.connect(page._sync_reference_review_heading)
        page._sync_reference_review_heading()

    def leave_secondary(self, *, discard=False):
        if self.editor is not None and self.editor.options.kind == "secondary_structure_definition":
            self.controller.reviews_page._workspace.setCurrentWidget(self.editor.return_widget)
            self.controller.reviews_page._sync_reference_review_heading()
            if discard or (self.pending_node_id is None and self.revision_job is None):
                self.reset()

    def finish_view(self):
        c = self.controller
        if self.session is not c.session or c.session.last_status is None:
            return
        if self.editor.options.kind == "secondary_structure_definition":
            current = self._current()
            self.leave_secondary()
            if not current:
                c.return_to_workflow()
            return
        if self.editor.options.saved_unmixing is not None:
            c.return_to_workflow()
            return
        sample = self.editor.options.sample_id
        nodes = tuple(node.node_id for node in c.session.last_status.workflow_nodes
                      if node.kind in {"tissue_resolution", "tissue_candidate"}
                      and sample in node.sample_ids and node.status == "needs_interaction" and not node.deferred)
        if nodes:
            c.open_workflow_review(nodes)
        else:
            c.return_to_workflow()

    def back(self):
        c = self.controller
        view = c.reviews_page._current_view
        if type(view) is not BatchInteractionForm:
            return
        status = c.session.last_status if c.session is not None else None
        if status is None or view.expected_state_head_sha256 != status.state_head_sha256:
            return
        group = c.reviews_page._groups.get(view.group_id)
        if group is None:
            return
        target = next((t for t in group.targets if t.target_id == c.reviews_page._target.currentData()), None)
        if target is not None:
            c.open_stage_options((target.node_id,))

    def _current(self):
        c = self.controller
        return (self.editor is not None and self.session is c.session and c.session.last_status is not None
                and c.session.last_status.state_head_sha256 == self.editor.options.state_head_sha256)

    def _unfinished_tissue(self, options):
        status = self.controller.session.last_status
        if options.saved_tissue is None or status is None:
            return False
        nodes = tuple(node for node in status.workflow_nodes
                      if tuple(node.sample_ids) == (options.sample_id,) and not node.deferred)
        return (any(node.kind in {"tissue_resolution", "tissue_candidate"}
                    and node.status == "needs_interaction" for node in nodes)
                and not any(node.kind in {"tissue_candidate", "tissue_acceptance"}
                            and node.status == "completed" for node in nodes))

    def _can_continue_tissue(self, preview):
        """Skip the impact page only for the current unfinished tissue task itself."""
        options = self.editor.options
        if (not self._unfinished_tissue(options) or preview.blocked_reason
                or preview.saved_result_retirement_sources
                or preview.options.state_head_sha256 != options.state_head_sha256
                or preview.options.node_id not in {
                    options.saved_tissue.setup_node_id, options.saved_tissue.resolution_node_id}):
            return False
        nodes = {node.node_id: node for node in self.controller.session.last_status.workflow_nodes
                 if not node.deferred}
        if preview.options.node_id not in preview.affected_node_ids:
            return False
        for identity in preview.affected_node_ids:
            node = nodes.get(identity)
            if (node is None or tuple(node.sample_ids) != (options.sample_id,)
                    or node.kind not in {"tissue_setup", "tissue_resolution", "tissue_channel", "tissue_candidate"}
                    or (node.kind == "tissue_candidate" and node.status == "completed")):
                return False
        return True

    def save(self, kind, action, raw):
        self._request_preview(kind, action, raw, apply_after_preview=True)

    def preview(self, kind, action, raw):
        self._request_preview(kind, action, raw, apply_after_preview=False)

    def _request_preview(self, kind, action, raw, *, apply_after_preview):
        c = self.controller
        if not self._current():
            self.editor.show_error("The analysis changed. Reopen its saved settings before saving.")
            return
        if c.jobs.has_active_jobs or c._close_pending:
            self.editor.show_error("Wait for the current work to finish, then save these choices.")
            return
        saved = self.editor.saved
        node_id = (self.editor.options.node_id if kind in {"reference_unmixing", "secondary_structure_definition"}
                   else saved.setup_node_id if kind == "tissue_setup" else saved.resolution_node_id)
        if node_id is None:
            self.editor.show_error("This resolution has not been saved. Continue to its current review.")
            return
        options = self.editor.options
        self.draft = {"kind": kind, "action": action, "raw": dict(raw), "saved": saved,
                      "sample_id": options.sample_id, "prior_node_ids": {n.node_id for n in c.session.last_status.workflow_nodes}}
        service, batch_id, head = c.session.service, c.session.batch_id, options.state_head_sha256
        def preview():
            target = (options if node_id == options.node_id else
                      service.stage_options(batch_id, node_id=node_id, expected_head_sha256=head))
            return service.preview_stage_revision(batch_id, node_id=target.node_id,
                expected_head_sha256=head, settings=target.settings)
        try:
            job = c.jobs.submit("preview_stage_revision", preview, access=JobAccess.READ)
        except RuntimeError as error:
            self.editor.show_error(str(error))
            return
        self.editor.set_busy(True)
        operation = "preview" if apply_after_preview else "preview_only"
        if apply_after_preview and kind != "reference_unmixing" and self.editor.within_node:
            operation = "continue_secondary" if kind == "secondary_structure_definition" else "continue_tissue"
        self.jobs[job] = (operation, self.editor)

    def observe_source(self, path):
        c = self.controller
        editor = self.editor
        editor.form.begin_tissue_source_check()
        try:
            job = c.jobs.submit("observe_tissue_classifier_source", lambda: observe_tissue_classifier_source(path), access=JobAccess.READ)
        except RuntimeError as error:
            editor.form.set_tissue_observation_result(success=False, message=str(error))
            return
        self.jobs[job] = ("source", editor)

    def author(self, method):
        c = self.controller
        if not self._current() or c.jobs.has_active_jobs or method not in {"threshold", "rf", "svm"}:
            self.editor.show_error("Wait for current work to finish and reopen these settings if the analysis changed.")
            return
        saved = self.editor.saved
        selection = {"entry_id": saved.project_entry_id, "method": method}
        if method in {"rf", "svm"}:
            try:
                selection["setup"] = self.editor.form.tissue_authoring_setup()
            except ValueError as error:
                self.editor.form._present_error(str(error), "select_tissue_detector")
                return
        self.authoring_editor = self.editor
        c.reviews_page._pending_tissue_authoring = selection
        c.open_qupath(Path(saved.project_path))

    def inspect_features(self, index):
        c = self.controller
        if not self._current() or c.jobs.has_active_jobs:
            self.editor.show_error("Wait for current work to finish and reopen these settings if the analysis changed.")
            return
        try:
            request = self.editor.form.feature_inspection_values(index, self.editor.saved.project_entry_id)
        except ValueError as error:
            self.editor.form._present_error(str(error), "select_tissue_detector")
            return
        self.feature_editor = self.editor
        c.inspect_image_features(request)

    def feature_inspection_finished(self, result):
        if result.get("classifier_index") != -1:
            return False
        editor, self.feature_editor = self.feature_editor, None
        if editor is None:
            return False
        if (result.get("status") == "feature_settings_saved"
                and editor is self.editor and self._current()):
            editor.form.apply_pixel_features(-1, result["pixel_features"])
        return True

    def authoring_finished(self, *, success=True, result=None):
        editor, self.authoring_editor = self.authoring_editor, None
        if editor is None:
            return False
        if success and result is not None and editor is self.editor and self._current():
            editor.form.offer_tissue_authoring_result(result)
        return True

    def succeeded(self, job_id, value):
        c = self.controller
        if job_id == self.revision_job:
            self.revision_job = None
            # Match the introduced node returned by the real revision, not the
            # latest node of this kind or an unrelated sample's pending prompt.
            matches = [n.node_id for n in value.workflow_nodes
                       if n.kind == self.draft["kind"] and self.draft["sample_id"] in n.sample_ids
                       and n.node_id not in self.draft["prior_node_ids"] and not n.deferred]
            if len(matches) != 1:
                c._continue_session = None
                self.editor.show_error("The changed step could not be identified. The draft is retained; open the current review.")
            else:
                self.pending_node_id = matches[0]
            return False  # The existing run completion handler refreshes status.
        pending = self.jobs.pop(job_id, None)
        if pending is None:
            return False
        kind, editor = pending
        if kind == "open_secondary":
            self._open_secondary(value, editor)
            return True
        if editor is not self.editor or self.session is not c.session:
            return True
        if kind == "source":
            try:
                if normalized_tissue_source_path(editor.form._raw_values().get("source_path")) != value.path:
                    raise ValueError("The detector path changed during its check. Select the file again.")
                editor.form.observed_source = value
                editor.form.set_tissue_observation_result(success=True)
            except ValueError as error:
                editor.form.set_tissue_observation_result(success=False, message=str(error))
            return True
        if not self._current():
            editor.show_error("The analysis changed. Reopen its saved settings before saving.")
            return True
        if kind in {"continue_tissue", "continue_secondary"}:
            if value.blocked_reason:
                editor.show_error(value.blocked_reason)
                return True
            if kind == "continue_secondary":
                # Only the accepted choice and this exact unfinished training form may change silently.
                safe = (value.options.node_id == editor.options.node_id
                        and value.options.state_head_sha256 == editor.options.state_head_sha256
                        and set(value.affected_node_ids) == {editor.options.node_id, editor.training_id}
                        and not value.saved_result_retirement_sources)
            else:
                safe = self._can_continue_tissue(value)
            if safe:
                QTimer.singleShot(0, editor, lambda: self._apply(value, editor))
                return True
            # An unknown closure or affected completed work keeps the explicit review.
            editor.show_impact(value, c.session.last_status.workflow_nodes)
            self.draft = None
            editor.set_busy(False)
            return True
        editor.show_impact(value, c.session.last_status.workflow_nodes)
        if value.blocked_reason:
            editor.show_error(value.blocked_reason)
            return True
        if kind == "preview_only":
            self.draft = None
            editor.set_busy(False)
            return True
        # Let the completed read job leave the bridge before admitting mutation.
        QTimer.singleShot(0, editor, lambda: self._apply(value, editor))
        return True

    def _apply(self, preview, editor):
        c = self.controller
        if editor is not self.editor or not self._current() or c._close_pending:
            editor.show_error("The analysis changed before saving. Your draft is retained.")
            return
        if c.jobs.has_active_jobs:
            editor.show_error("Another operation started. Wait for it to finish, then save again.")
            return
        session = c.session
        try:
            job = c.jobs.submit("revise_stage", lambda: session.service.revise_stage(session.batch_id,
                node_id=preview.options.node_id, expected_head_sha256=preview.options.state_head_sha256,
                settings=preview.settings), access=JobAccess.MUTATION)
        except RuntimeError as error:
            editor.show_error(str(error))
            return
        self.revision_job = job
        c._run_job_id = job
        c._continue_session, c._continue_accepted = session, True
        c._continue_origin_head = preview.options.state_head_sha256
        c._begin_mutation(session)
        c._status_epoch += 1
        c.refresh_status()

    def failed(self, job_id, error):
        pending = self.jobs.pop(job_id, None)
        if pending is not None:
            kind, editor = pending
            if kind == "open_secondary":
                if editor[0] is self.controller.session and editor[1] is self.controller.reviews_page._current_view:
                    self.controller.reviews_page._set_status_message(error.message)
                return True
            if editor is self.editor:
                if kind == "source":
                    editor.form.set_tissue_observation_result(success=False, message=error.message)
                else:
                    editor.show_error(error.message)
                    self.controller.operation_failed.emit(error.message)
            return True
        if job_id == self.revision_job:
            self.revision_job = None
            self.editor.show_error(error.message)
        return False

    def bind_pending(self, page, head):
        """Submit the changed draft only through the new prompt's normal binder."""
        c = self.controller
        if self.draft is None or self.pending_node_id is None or self.session is not c.session:
            return
        matches = [(group, target) for group in page.groups for target in getattr(group, "targets", ())
                   if target.node_id == self.pending_node_id and target.sample_id == self.draft["sample_id"]]
        if len(matches) != 1:
            return
        group, target = matches[0]
        try:
            view = build_batch_interaction_form(group, expected_state_head_sha256=head, target_id=target.target_id)
            if type(view) is not BatchInteractionForm:
                raise ValueError("The current review cannot accept the saved draft.")
            if self.draft["kind"] == "tissue_setup":
                saved = self.draft["saved"]
                context = view.form.context["tissue_image_context"]
                raw = self.draft["raw"]
                if raw.get("detector_source") == "saved" and raw.get("saved_classifier_name") == saved.parameters.classifier_name:
                    offered = {row["classifier_name"]: row["sha256"] for row in context["available_classifiers"]}
                    if offered.get(saved.parameters.classifier_name) != saved.classifier_sha256:
                        raise ValueError("The saved detector changed. Review its current file before saving.")
                # The real prompt supplies current channels/inventory and request identity;
                # only the previously accepted parameter defaults carry forward.
                form = replace(view.form, context={**view.form.context, "seed": saved.parameters.to_mapping()})
                view = replace(view, form=form)
                # Display the retained draft on the real new form as well, so a
                # failed admission can be corrected without losing entered values.
                c.reviews_page._current_view = view
                c.reviews_page._renderer._form = form
                c.reviews_page._renderer.restore_values(raw)
                c.reviews_page._renderer._set_tissue_step(2)
            if self.draft["kind"] == "reference_unmixing":
                if view.form.context.get("adapter") != "batch_reference_start":
                    raise ValueError("The revised unmixing settings form is not available yet.")
                c.reviews_page._current_view = view
                c.reviews_page._renderer.restore_values(self.draft["raw"])
            if self.draft["kind"] == "secondary_structure_definition":
                if view.form.context.get("adapter") != "secondary_definition":
                    raise ValueError("Reopen the revised structure definition review.")
                context = view.form.context["definition_context"]
                saved_context = self.draft["saved"].resume_context
                if any(context.get(key) != saved_context.get(key) for key in
                       ("participant_ids", "validation_domain_id", "initial_secondary_structures")):
                    raise ValueError("The revised review belongs to a different structure selection.")
                if c.reviews_page._displayed_group_id != group.group_id:
                    c.reviews_page.show_group(group.group_id)
                if c.reviews_page._target.currentData() != target.target_id:
                    c.reviews_page._target.setCurrentIndex(c.reviews_page._target.findData(target.target_id))
                if (type(c.reviews_page._current_view) is not BatchInteractionForm
                        or c.reviews_page._current_view.representative_target_id != target.target_id):
                    raise ValueError("Open the revised structure definition to continue.")
                definitions = c.reviews_page._renderer._editors["definitions"]
                definitions.set_value(self.draft["raw"]["definitions"])
                if definitions.needs_preparation():
                    definitions._set_page("preparation")
            request = build_batch_interaction_request(view, target_id=target.target_id,
                action=self.draft["action"], raw_values=self.draft["raw"])
            if request is None:
                raise ValueError("The current review did not accept the changed choices.")
        except (TypeError, ValueError) as error:
            self.pending_node_id = None
            self.editor.show_error(str(error))
            c.reviews_page._set_status_message(str(error) + " Review the retained choices in this current form.")
            return
        self.pending_node_id = None
        # Keep the draft until admission succeeds. Existing response/continuation
        # guards handle head changes, native jobs and any processing-side rejection.
        session = self.session
        QTimer.singleShot(0, c.reviews_page, lambda: self._apply_bound_request(request, session, head))

    def _apply_bound_request(self, request, session, head):
        c = self.controller
        if c.session is not session or self.draft is None:
            return
        if self.draft["kind"] == "secondary_structure_definition":
            view = c.reviews_page._current_view
            if (c.shell.current_route_id != "reviews" or type(view) is not BatchInteractionForm
                    or view.group_sha256 != request.group_sha256
                    or c.reviews_page._target.currentData() not in request.selection.target_ids):
                return
        if session.current_state_head_sha256 != head or c._close_pending:
            c.reviews_page._set_status_message("The analysis changed before saving. Review the retained entries in the current form.")
            return
        c.reviews_page._submitted_continuation = (request, True)
        c.apply_review_response(request)
