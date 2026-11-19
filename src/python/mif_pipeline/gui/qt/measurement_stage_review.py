"""Review saved measurements in Run using the same distribution and tissue viewer as Results."""
from pathlib import Path

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QScrollArea, QMenu

from .researcher_widgets import ContentSizedButton
from .workbench import PageHeading
from .jobs import JobAccess


class MeasurementStageReview(QWidget):
    back_requested = Signal()
    continue_requested = Signal()
    settings_requested = Signal()
    distribution_requested = Signal()
    field_requested = Signal()

    def __init__(self, parent=None, *, stage_kind="measurements"):
        super().__init__(parent)
        self.stage_kind = stage_kind
        segmentation = stage_kind == "segmentation"
        self._plots = ()
        self._cohort_images = []
        self._preserve_interval = False
        self.revision = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.back = ContentSizedButton('← Cell segmentation' if segmentation else '← Cell measurements', self)
        self.back.clicked.connect(self.back_requested)
        outer.addWidget(self.back, alignment=Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        body = QVBoxLayout(content)
        body.setContentsMargins(12, 10, 12, 14)
        body.setSpacing(14)
        heading = PageHeading('measurement-review',
            'Review cell and nucleus outlines' if segmentation else 'Review cell measurements',
            'Compare cell and nuclear outlines with the staining. Select a shape measurement to locate cells by size or shape.'
            if segmentation else 'Choose an image and a saved intensity or shape measurement.', content)
        heading.findChild(QLabel, 'measurement-review-title').setProperty('stagePageHeading', True)
        body.addWidget(heading)
        choices = QHBoxLayout()
        self.image_label = QLabel('Image', content)
        choices.addWidget(self.image_label)
        self.images = QComboBox(content)
        self.images.setAccessibleName('Segmented image' if segmentation else 'Measured image')
        choices.addWidget(self.images, 1)
        self.scope = QComboBox(content)
        self.scope.addItem('All images', 'all')
        self.scope.addItem('Selected image', 'image')
        self.scope.setAccessibleName('Images included in the measurement plot')
        self.scope.currentIndexChanged.connect(self._distribution_changed)
        self.scope.hide()
        choices.addWidget(self.scope, 1)
        choices.addWidget(QLabel('Shape measurement' if segmentation else 'Measurement', content))
        self.measurements = QComboBox(content)
        self.measurements.setAccessibleName('Measurement to review')
        self.measurements.setMinimumWidth(220)
        choices.addWidget(self.measurements, 2)
        body.addLayout(choices)
        self.status = QLabel('Loading saved cell outlines…' if segmentation else 'Loading saved measurements…', content)
        self.status.setWordWrap(True)
        self.status.setProperty('fieldHelp', True)
        body.addWidget(self.status)
        from .measurement_distribution import MeasurementDistributionReview
        self.review = MeasurementDistributionReview(content)
        self.review.requested.connect(self._distribution_changed)
        self.review.field_requested.connect(self._field_changed)
        from .cohort_image_navigation import CohortImageNavigation
        self.image_navigation = CohortImageNavigation(content)
        self.image_navigation.image_changed.connect(self._navigate_image)
        field_index = self.review.layout().indexOf(self.review.cell_view)
        self.review.layout().insertWidget(field_index, self.image_navigation)
        body.addWidget(self.review)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        navigation = QHBoxLayout()
        change = ContentSizedButton('Change segmentation settings…' if segmentation else 'Change cell measurements…', self)
        change.clicked.connect(self.settings_requested)
        navigation.addWidget(change)
        export = ContentSizedButton('Export…', self)
        menu = QMenu(export)
        self.export_figure = menu.addAction('Measurement distribution…', lambda: self._export(False))
        self.export_data = menu.addAction('Distribution data…', lambda: self._export(True))
        menu.addSeparator()
        self.export_field = menu.addAction('Tissue view…', lambda: self.review._export_examples(False))
        self.export_field_data = menu.addAction('Tissue field data…', lambda: self.review._export_examples(True))
        for action in (self.export_figure, self.export_data, self.export_field, self.export_field_data):
            action.setEnabled(False)
        export.setMenu(menu)
        navigation.addWidget(export)
        navigation.addStretch()
        self.proceed = ContentSizedButton('Continue to measurements' if segmentation else 'Continue to phenotyping', self)
        self.proceed.setProperty('actionRole', 'primary')
        self.proceed.clicked.connect(self.continue_requested)
        navigation.addWidget(self.proceed)
        outer.addLayout(navigation)
        self.images.currentIndexChanged.connect(self._image_changed)
        self.measurements.currentIndexChanged.connect(self._measurement_changed)

    def selection(self):
        stage, figure = self.images.currentData(), self.measurements.currentData()
        return (stage.artifact_ref_sha256, figure.figure_id) if stage is not None and figure is not None else None

    def set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_results(self, results):
        """Offer completed measurement outputs before final export exists."""
        stages = [s for s in results.stages if s.stage_kind == self.stage_kind
                  and any(f.figure_id.startswith('distribution/') for f in s.figures)]
        self.images.blockSignals(True)
        self.images.clear()
        for stage in stages:
            self.images.addItem(', '.join(stage.sample_ids), stage)
        self.images.blockSignals(False)
        multiple = len(stages) > 1 and self.stage_kind == 'measurements'
        self.images.setVisible(not multiple)
        self.image_label.setVisible(not multiple)
        self.scope.setVisible(multiple)
        self._image_changed()
        if not stages:
            self.set_status('No completed cell outlines are available yet.' if self.stage_kind == 'segmentation'
                            else 'No completed cell measurements are available yet.')

    def _image_changed(self):
        previous = self.measurements.currentData()
        figure_id = previous.figure_id if previous is not None else None
        self.measurements.blockSignals(True)
        self.measurements.clear()
        stage = self.images.currentData()
        if stage is not None:
            for figure in stage.figures:
                if figure.figure_id.startswith('distribution/'):
                    self.measurements.addItem(figure.title.removeprefix('Measurement distribution · '), figure)
            if self._preserve_interval and figure_id is not None:
                for index in range(self.measurements.count()):
                    if self.measurements.itemData(index).figure_id == figure_id:
                        self.measurements.setCurrentIndex(index)
                        break
        self.measurements.blockSignals(False)
        self._measurement_changed()

    def _measurement_changed(self):
        if not self._preserve_interval:
            self.review._interval = None
        self.review._accepted_options = None
        self.review._field_bundle = None
        self.review._field_viewport = {}
        self.review._gallery_options = {}
        self.review.canvas.hide()
        self.review.cell_view.hide()
        self.review.example_exports.hide()
        self.export_field.setEnabled(False)
        self.export_field_data.setEnabled(False)
        self._distribution_changed()

    def _navigate_image(self, key):
        image = next((row for row in self._cohort_images if row['key'] == key), None)
        if image is None:
            return
        index = next((i for i in range(self.images.count())
                      if self.images.itemData(i).artifact_ref_sha256 == image['artifact_refs'][0]), -1)
        if index >= 0 and index != self.images.currentIndex():
            self._preserve_interval = True
            try:
                self.images.setCurrentIndex(index)
            finally:
                self._preserve_interval = False

    def distribution_options(self):
        options = self.review.options()
        if self.stage_kind == 'measurements' and self.images.count() > 1:
            options.update(cohort_artifact_refs=[self.images.itemData(i).artifact_ref_sha256
                for i in range(self.images.count())], plot_scope=self.scope.currentData())
        return options

    def _distribution_changed(self):
        self.revision += 1
        self._plots = ()
        self.export_figure.setEnabled(False)
        self.export_data.setEnabled(False)
        self.export_field.setEnabled(False)
        self.export_field_data.setEnabled(False)
        if self.selection() is not None:
            self.set_status('Loading measurement distribution…')
            self.distribution_requested.emit()

    def set_distribution(self, plots):
        self._plots = tuple(p for p in plots if p.status == 'available')
        options = next((p.display_options for p in self._plots if p.display_options is not None), None)
        if options is None:
            self.review.clear_field()
            self.review.canvas.hide()
            self.export_field.setEnabled(False)
            self.export_field_data.setEnabled(False)
            self.set_status('This measurement distribution is unavailable.')
            return
        self.review.set_resolved(options)
        self._cohort_images = options.get('cohort_images', [])
        self.image_navigation.set_images(self._cohort_images)
        self.scope.setVisible(len(self._cohort_images) > 1)
        if self.stage_kind == 'measurements':
            self.images.setVisible(len(self._cohort_images) <= 1)
            self.image_label.setVisible(len(self._cohort_images) <= 1)
        self.set_status('')
        self.export_figure.setEnabled(True)
        self.export_data.setEnabled(True)

    def set_field(self, bundle):
        self.review.set_field_bundle(bundle)
        self.review.example_exports.hide()
        self.export_field.setEnabled(True)
        self.export_field_data.setEnabled(True)

    def _field_changed(self):
        self.export_field.setEnabled(False)
        self.export_field_data.setEnabled(False)
        self.field_requested.emit()

    def field_options(self, appearance_path):
        stage = self.images.currentData()
        gallery = next((f for f in stage.figures if f.figure_id == 'cell-gallery'), None) if stage else None
        if gallery is None:
            return None
        from ..image_appearance import load_image_appearance
        appearance = load_image_appearance(appearance_path, gallery.settings.get('image_uri', ''))
        options = self.review.field_options(gallery.settings, appearance)
        active = next((row for row in self._cohort_images
                       if stage.artifact_ref_sha256 in row['artifact_refs']), None)
        if active is not None and len(active['artifact_refs']) > 1:
            options['image_artifact_refs'] = active['artifact_refs']
        return options

    def _export(self, data_only):
        from .figure_export import export_plot_files
        paths = {Path(p.path).suffix.lstrip('.'): Path(p.path) for p in self._plots}
        export_plot_files(self, paths, 'cell-measurement' + ('.csv' if data_only else '.svg'),
                          data_only=data_only,
                          data_choices={p.title: Path(p.path) for p in self._plots if Path(p.path).suffix in {'.csv', '.npz'}})


class MeasurementReviewNavigation:
    """Connect the Run page to existing read-only results and bounded field services."""
    def __init__(self, controller, *, stage_kind="measurements"):
        self.controller = controller
        self.stage_kind = stage_kind
        self.page = None
        self.jobs = {}
        self.field_pending = None
        self.distribution_pending = None
        self.field_revision = 0
        self._correction_field_pending = None
        self._correction_dialog = None
        self._correction_acceptances = {}
        self._correction_focus_identity = None
        self._saved_artifact_ref = None
        self._return_to_results = False

    def reset(self):
        if self.page is not None:
            self.page.deleteLater()
        self.page = None
        self.field_pending = None
        self.distribution_pending = None
        self._correction_field_pending = None
        self._correction_dialog = None
        self._correction_acceptances = {}
        self._correction_focus_identity = None
        self._saved_artifact_ref = None
        self._return_to_results = False

    def open_saved(self, artifact_ref, head):
        session = self.controller.session
        if session is None or session.current_state_head_sha256 != head or self.controller._close_pending:
            return
        self.open(saved_artifact_ref=artifact_ref)

    def _back(self):
        if self._return_to_results:
            self.controller.shell.show_route('results')
        else:
            self.controller.run_page.open_stage(self.stage_kind)

    def open(self, *, saved_artifact_ref=None):
        c = self.controller
        session = c.session
        if session is None or session.current_state_head_sha256 is None or c._close_pending:
            return
        self._return_to_results = saved_artifact_ref is not None
        self._saved_artifact_ref = saved_artifact_ref
        if self.page is None:
            if self.stage_kind == 'segmentation':
                from .segmentation_stage_review import SegmentationStageReview
                self.page = SegmentationStageReview(c.run_page)
                self.page.correction_requested.connect(self.correct_cell)
                self.page.corrections.edit_requested.connect(self.prepare_corrections)
                self.page.corrections.delete_requested.connect(self.prepare_corrections)
                self.page.corrections.review_requested.connect(lambda:self._show_correction(None))
                self.page.corrections.link_field(self.page.outlines)
                self.page.corrections.focus_requested.connect(self.focus_correction)
                self.page.corrections.changed.connect(lambda: self.page.corrections.update_field(self.page._bundle)
                    if self.page is not None and self.page._bundle is not None else None)
            else:
                self.page = MeasurementStageReview(c.run_page, stage_kind=self.stage_kind)
            self.page.back_requested.connect(self._back)
            self.page.continue_requested.connect(self.proceed)
            self.page.settings_requested.connect(self.settings)
            self.page.distribution_requested.connect(self.distribution)
            self.page.field_requested.connect(self.field)
            c.run_page._pages.addWidget(self.page)
        is_segmentation = self.stage_kind == 'segmentation'
        stage_title = 'Cell segmentation' if is_segmentation else 'Cell measurements'
        self.page.back.setText('← Results' if self._return_to_results else f'← {stage_title}')
        destination = 'Continue to measurements' if is_segmentation else 'Continue to phenotyping'
        if self._return_to_results:
            destination = 'Return to Results'
        if is_segmentation:
            self.page.set_continue_destination(destination)
        else:
            self.page.proceed.setText(destination)
        c.run_page._heading.hide()
        c.run_page._status_card.hide()
        c.run_page._appearance.hide()
        c.run_page._graph.legend.hide()
        c.run_page._footer_actions.hide()
        c.run_page._pages.setCurrentWidget(self.page)
        c.shell.show_route('run')
        head = session.current_state_head_sha256
        self._submit('catalog', lambda: session.service.saved_stage_results(session.batch_id, expected_head_sha256=head))

    def proceed(self):
        c = self.controller
        if self.stage_kind == 'segmentation' and self.page is not None and self.page.corrections.pending_count():
            self.page.set_status('Review or clear the collected corrections for each image before continuing.')
            self.page._scroll.ensureWidgetVisible(self.page.corrections)
            if self.page.corrections.identities():
                self.page.corrections.pending.list.setFocus()
            else:
                self.page.corrections.image_groups.setFocus()
            return
        if self._return_to_results:
            self._back()
            return
        c.run_page.open_stage('measurements' if self.stage_kind == 'segmentation' else 'phenotyping')

    def settings(self):
        c = self.controller
        c.run_page.open_stage(self.stage_kind)
        c.run_page._select_stage_options()

    def _submit(self, kind, function, *, access=JobAccess.READ):
        c = self.controller
        session = c.session
        token = (session, session.current_state_head_sha256, self.page, self.page.revision, self.field_revision)
        job = c.jobs.submit(self.stage_kind + '_review_' + kind, function, access=access)
        self.jobs[job] = (kind, token)

    def correct_cell(self, request):
        c, page = self.controller, self.page
        session = c.session
        if (session is None or c._close_pending or c.jobs.has_active_jobs
                or page.selection() is None or page._bundle is None):
            return
        adding=request['operation']=='add_cell'
        identity = request['identity'] if adding else page.outlines.selected_cell_identity()
        if identity != request['identity']:
            page.set_status('Select the cell again before opening its correction.')
            return
        row = request.get('row') if adding else next((row for row in page._bundle['metadata']['visible_cells']
                    if page.outlines._cell_key(row) == identity), None)
        if adding and tuple(page._bundle['metadata'].get(key) for key in
                ('sample_id','project_entry_id','slide_id','section_id'))!=identity[:4]:
            page.set_status('Reopen the image before adding its outline.')
            return
        if row is None:
            return
        page.corrections.set_scope(page.selection()[0], session.current_state_head_sha256,
            page._bundle['metadata'].get('cell_correction_available', False),image_name=page.images.currentText())
        page.corrections.update_field(page._bundle)
        try:
            page.corrections.add(identity, request['operation'], row)
        except ValueError as error:
            page.set_status(str(error))

    def prepare_corrections(self):
        c, page = self.controller, self.page
        session = c.session
        if (session is None or c._close_pending or c.jobs.has_active_jobs
                or not page.corrections.batch_ready()):
            return
        if page._bundle is not None and page.selection() and page.corrections._active_scope[0]==page.selection()[0]:
            page.corrections.update_field(page._bundle)
        groups=page.corrections.queued_images()
        head=session.current_state_head_sha256
        if any(group['head']!=head for group in groups):
            page.set_status('The analysis changed. Reopen the current cell corrections.')
            return
        outlines=any(item['operation']!='delete' for group in groups for item in group['operations'])
        if outlines and c.file_operations is None:
            page.set_status('Complete Software setup to open the native outline editor.')
            return
        if any(group.get('field_context') is None for group in groups):
            page.set_status('Open each queued image once before editing its outlines.')
            return
        recovery=page.corrections.native_recovery()
        if outlines:
            if recovery is None:
                recovery=dict(signature=page.corrections.native_signature(),pending=True)
                page.corrections.native_draft=recovery
            recovery['pending']=True
        def prepare():
            tickets=list(recovery['tickets']) if recovery and recovery.get('project') else []
            for group in (() if tickets else groups):
                metadata=group['field_context'];frame=metadata['source_frame']
                px,py=frame['level0_pixel_size_x_um'],frame['level0_pixel_size_y_um']
                view_bounds=[value/(px if i%2==0 else py) for i,value in enumerate(metadata['field_um'])]
                resumed=group['draft']
                ticket=session.service.prepare_cell_correction(session.batch_id,
                    artifact_ref_sha256=group['artifact_ref_sha256'],expected_head_sha256=head,
                    operations=group['operations'],review_bounds_level0=view_bounds,
                    **({'resume_selection':resumed['selection']} if resumed is not None else {}))
                scope=ticket['cell_correction']
                if any(ticket['entry_id']!=identity[1] or scope['section_id']!=identity[3] for identity in group['identities']):
                    raise ValueError('The queued cells differ from their accepted image and section.')
                tickets.append(ticket)
            if not outlines:
                candidates=[session.service.preview_cell_correction(session.batch_id,ticket_id=ticket['ticket_id']) for ticket in tickets]
            elif len(tickets)==1:
                candidates=[c.file_operations.edit_local_cell_correction(session.service,session.batch_id,
                    ticket=tickets[0],folder=Path(tickets[0]['project_file']).parent,
                    image_appearance_path=Path(session.output_root)/'image-appearance.json',recovery=recovery)]
            else:
                candidates=c.file_operations.edit_local_cell_corrections(session.service,session.batch_id,
                    tickets=tickets,folder=Path(tickets[0]['project_file']).parent,image_names=[group['name'] for group in groups],
                    image_appearance_path=Path(session.output_root)/'image-appearance.json',recovery=recovery)
            if recovery is not None:recovery['pending']=False
            return candidates[0] if len(candidates)==1 else candidates
        page.set_status('Preparing the selected deletions…' if not outlines
                        else 'Edit the queued images in QuPath, then return to review the changes together.')
        self._submit('correction_preview',prepare,access=JobAccess.MUTATION)
        page.setEnabled(False)

    def _show_correction(self, candidates):
        from .cell_correction_dialog import CellCorrectionDialog
        c,page,session=self.controller,self.page,self.controller.session
        if candidates is not None:
            returned=[candidates] if isinstance(candidates,dict) else list(candidates)
            groups=page.corrections.queued_images()
            for candidate in returned:
                artifact=candidate.get('ticket',{}).get('segmentation_result_ref_sha256')
                if artifact is None and len(groups)==1:artifact=groups[0]['artifact_ref_sha256']
                if candidate.get('selection') is None:page.corrections.clear_image(artifact)
                else:page.corrections.set_image_draft(artifact,candidate)
        images=[]
        for group in page.corrections.queued_images():
            candidate=group['draft']
            if candidate is None or candidate.get('selection') is None:continue
            images.append(dict(group,candidate=candidate))
        if not images:
            page.set_status('None of the queued outlines changed.')
            return
        first=images[0]
        impacts=any(image['candidate'].get('ticket',{}).get('revision_preview') is not None for image in images)
        bundle=page._bundle if page.selection() and page.selection()[0]==first['artifact_ref_sha256'] else None
        dialog=CellCorrectionDialog(bundle,first['candidate'],page,identities=first['identities'],images=images,
            workflow_nodes=session.last_status.workflow_nodes if impacts else ())
        self._correction_dialog=dialog
        self._correction_field_pending=None
        def closed():
            if self._correction_dialog is dialog:
                self._correction_dialog=None;self._correction_field_pending=None
            dialog.deleteLater()
        dialog.finished.connect(closed)
        head=session.current_state_head_sha256
        selections=tuple(image['candidate']['selection'] for image in images)
        def accept():
            if session is not c.session or head!=session.current_state_head_sha256 or c.jobs.has_active_jobs:
                page.set_status('The analysis changed. Reopen the current cell corrections.')
                dialog.reject();return
            admission=dict(session=session,page=page,accepted=False,
                submitted=tuple((image['artifact_ref_sha256'],image['candidate']['selection']) for image in images))
            def invoke():
                if len(selections)==1:
                    revised=session.service.accept_cell_correction(session.batch_id,
                        selected=selections[0],expected_head_sha256=head)
                else:
                    revised=session.service.accept_cell_corrections(session.batch_id,
                        selections=list(selections),expected_head_sha256=head)
                # The job runner joins this worker before delivering either
                # outcome on Qt's thread, including a later recalculation error.
                admission['accepted']=True
                return session.service.continue_run(session.batch_id,expected_head_sha256=revised.state_head_sha256)
            job=c.jobs.submit('correct_cell',invoke,access=JobAccess.MUTATION)
            self._correction_acceptances[job]=admission
            c._begin_mutation(session);c._status_epoch+=1;c._run_job_id=job
            dialog.accept();c.run_page.open_stage('segmentation');c.refresh_status()
        dialog.apply_requested.connect(accept)
        def edit_again():
            dialog.accept();self.prepare_corrections()
        dialog.edit_again_requested.connect(edit_again)
        dialog.field_requested.connect(lambda request:self._correction_field(dialog,request))
        dialog.open();dialog.request_selected_field()

    def focus_correction(self, entry):
        page, session = self.page, self.controller.session
        if (session is None or page.selection() is None
                or page.corrections._scope != (page.selection()[0], session.current_state_head_sha256)):
            return
        self._correction_focus_identity = entry['identity']
        page.task.setCurrentIndex(page.task.findData('overview'))
        page._browse_preset = False
        page._fit_anchor = None
        page.outlines.view._fit = False
        page.outlines.view.centerOn(*entry['center_um'])
        page.outlines._schedule_field()

    def _correction_field(self, dialog, request):
        if dialog is not self._correction_dialog or self.controller.session is None:
            return
        self._correction_field_pending = (dialog, dict(request))
        self._next_correction_field()

    def _next_correction_field(self):
        if self._correction_field_pending is None or any(kind == 'correction_field' for kind, _ in self.jobs.values()):
            return
        dialog, request = self._correction_field_pending
        self._correction_field_pending = None
        session, page = self.controller.session, self.page
        if dialog is not self._correction_dialog or not dialog.isVisible() or session is None:
            return
        options = dict(dialog._bundle['metadata'].get('display_options',{}))
        options.update(request['display_options'])
        options.pop('nuclear_coverage', None)
        options.pop('image_artifact_refs', None)
        options.update(field_um=request['field_um'], viewport_size=request['viewport_size'], inspection_mode='across_tissue')
        head, artifact = session.current_state_head_sha256, request['artifact_ref_sha256']
        self._submit('correction_field', lambda: (dialog, request['cell_id'], request['revision'], session.service.segmentation_field(session.batch_id,
            expected_head_sha256=head, artifact_ref_sha256=artifact, figure_options=options),artifact))

    def distribution(self):
        c = self.controller
        session = c.session
        if session is None or c._close_pending or self.page.selection() is None:
            return
        artifact, figure = self.page.selection()
        head, options = session.current_state_head_sha256, self.page.distribution_options()
        self.distribution_pending = (session, self.page, self.page.revision, head, artifact, figure, options)
        self._next_distribution()

    def _next_distribution(self):
        if self.distribution_pending is None or any(kind == 'distribution' for kind,_token in self.jobs.values()):
            return
        session, page, revision, head, artifact, figure, options = self.distribution_pending
        self.distribution_pending = None
        if session is not self.controller.session or page is not self.page or revision != page.revision:
            return
        self._submit('distribution', lambda: session.service.saved_stage_figures(session.batch_id,
            expected_head_sha256=head, artifact_ref_sha256=artifact, figure_id=figure, figure_options=options))

    def field(self):
        if any(kind == 'correction_preview' for kind,_token in self.jobs.values()):
            return
        c = self.controller
        session = c.session
        if session is None or c._close_pending or self.page.selection() is None:
            return
        if self.stage_kind=='segmentation' and self.page._bundle is None:
            self.page.corrections.set_scope(self.page.selection()[0],session.current_state_head_sha256,
                False,image_name=self.page.images.currentText())
        options = self.page.field_options(Path(session.output_root) / 'image-appearance.json')
        if options is None:
            self.page.set_status('The measured cells’ image is unavailable.')
            return
        self.field_revision += 1
        self.field_pending = (session, self.page, self.page.revision, self.page.selection()[0], options)
        self._next_field()

    def _next_field(self):
        if self.field_pending is None or any(kind == 'field' for kind, _token in self.jobs.values()):
            return
        session, page, revision, artifact, options = self.field_pending
        self.field_pending = None
        if session is not self.controller.session or page is not self.page or revision != page.revision:
            return
        head = session.current_state_head_sha256
        reader = session.service.segmentation_field if self.stage_kind == 'segmentation' else session.service.measurement_field
        self._submit('field', lambda: reader(session.batch_id,
            expected_head_sha256=head, artifact_ref_sha256=artifact, figure_options=options))

    def _current(self, kind, token):
        session, head, page, revision, field_revision = token
        return (session is self.controller.session and head == session.current_state_head_sha256
                and page is self.page and (kind == 'catalog' or revision == page.revision)
                and (kind != 'field' or field_revision == self.field_revision))

    def _finish_correction_acceptance(self,job):
        admission=self._correction_acceptances.pop(job,None)
        if (admission is None or not admission['accepted'] or admission['session'] is not self.controller.session
                or admission['page'] is not self.page):return
        queue=self.page.corrections
        for artifact,selection in admission['submitted']:
            draft=queue._groups.get(artifact,{}).get('draft')
            if draft is not None and draft.get('selection')==selection:queue.clear_image(artifact)

    def succeeded(self, job, value):
        self._finish_correction_acceptance(job)
        request = self.jobs.pop(job, None)
        if request is None:
            return False
        kind, token = request
        if kind == 'correction_preview' and token[2] is self.page:
            self.page.setEnabled(True)
            self.page.corrections._refresh()
        if self._current(kind, token):
            if kind == 'catalog':
                if self.stage_kind=='segmentation':
                    self.page.corrections.reconcile(
                        (stage.artifact_ref_sha256 for stage in value.stages if stage.stage_kind=='segmentation'),token[1])
                if self._saved_artifact_ref is not None:
                    if not any(stage.stage_kind == self.stage_kind and stage.artifact_ref_sha256 == self._saved_artifact_ref for stage in value.stages):
                        self.page.set_status('This segmentation result has changed. Reopen it from Results.')
                        return True
                    self.page.set_results(value, selected_artifact=self._saved_artifact_ref)
                    self._saved_artifact_ref = None
                else:
                    self.page.set_results(value)
            elif kind == 'distribution':
                self.page.set_distribution(value)
            elif kind == 'correction_preview':
                self.page.set_status('')
                self._show_correction(value)
            elif kind == 'correction_field':
                dialog = self._correction_dialog
                if (dialog is value[0] and dialog.isVisible() and dialog.selected_cell_id == value[1]
                        and dialog.field_revision == value[2] and dialog.artifact_ref_sha256 == value[4]):
                    dialog.set_field(value[3])
            else:
                self.page.set_field(value)
                if self.stage_kind == 'segmentation':
                    self.page.corrections.set_scope(self.page.selection()[0], token[1],
                        value['metadata'].get('cell_correction_available', False),image_name=self.page.images.currentText())
                    self.page.corrections.update_field(value)
                    if self._correction_focus_identity is not None:
                        self.page.outlines._inspect_cell([self._correction_focus_identity])
                        self._correction_focus_identity = None
                self.page.set_status('')
        self._next_field()
        self._next_distribution()
        self._next_correction_field()
        return True

    def failed(self, job, error):
        self._finish_correction_acceptance(job)
        request = self.jobs.pop(job, None)
        if request is None:
            return False
        kind, token = request
        if kind == 'correction_preview' and token[2] is self.page:
            self.page.setEnabled(True)
            self.page.corrections._refresh()
        if self._current(kind, token):
            if kind == 'correction_field':
                if (self._correction_dialog is not None and self._correction_dialog.isVisible()
                        and self._correction_field_pending is None):
                    self._correction_dialog.status.setText(error.message)
            else:
                self.page.set_status(error.message)
        self._next_field()
        self._next_distribution()
        self._next_correction_field()
        return True
