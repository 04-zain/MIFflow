"""Open accepted assignments in the existing score and bounded tissue review."""
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QLabel, QLineEdit

from .jobs import JobAccess
from .researcher_widgets import ContentSizedButton
from .supervised_prediction_review import SupervisedPredictionReview


class SavedSupervisedReviewNavigation:
    def __init__(self, controller):
        self.controller = controller
        self.page = self.panel = None
        self.jobs = {}
        self.anchor = None
        self.field_pending = None

    def reset(self):
        if self.panel is not None:
            self.panel._timer.stop()
            self.panel.field._timer.stop()
        if self.page is not None:
            self.page.deleteLater()
        self.page = self.panel = None
        self.anchor = self.field_pending = None

    def current(self, artifact, head):
        c = self.controller
        return (self.anchor is not None and self.anchor == (c.session, head, artifact)
                and c.session.current_state_head_sha256 == head and not c._close_pending)

    def open(self, artifact, head):
        c, session = self.controller, self.controller.session
        if session is None or session.current_state_head_sha256 != head or c._close_pending:
            return
        self.reset()
        self.anchor = (session, head, artifact)
        self.page = QWidget(c.run_page)
        outer = QVBoxLayout(self.page)
        outer.setContentsMargins(0, 0, 0, 0)
        back = ContentSizedButton('← Results', self.page)
        back.clicked.connect(lambda: c.shell.show_route('results'))
        outer.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea(self.page)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(12, 10, 12, 14)
        self.status = QLabel('Loading saved cell assignments…', content)
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.retry = ContentSizedButton('Retry', content)
        self.retry.clicked.connect(self._context)
        self.retry.hide()
        self.body.addWidget(self.retry, alignment=Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        c.run_page._pages.addWidget(self.page)
        for widget in (c.run_page._heading, c.run_page._status_card, c.run_page._appearance,
                       c.run_page._graph.legend, c.run_page._footer_actions):
            widget.hide()
        c.run_page._pages.setCurrentWidget(self.page)
        c.shell.show_route('run')
        self._context()

    def _submit(self, kind, function, detail=None):
        job = self.controller.jobs.submit('saved_supervised_' + kind, function, access=JobAccess.READ)
        self.jobs[job] = (kind, self.anchor, self.page, detail)

    def _context(self):
        session, head, artifact = self.anchor
        if not self.current(artifact, head):
            return
        self.retry.hide()
        self.status.setText('Loading saved cell assignments…')
        self._submit('context', lambda: session.service.saved_supervised_review(session.batch_id,
            artifact_ref_sha256=artifact, expected_state_head_sha256=head))

    def _show_context(self, context):
        session, head, artifact = self.anchor
        self.status.hide()
        self.panel = SupervisedPredictionReview(context, QLineEdit(format(context['confidence_threshold'], '.12g')), self.page)
        self.panel.scoped_assignment_available = True
        self.panel.field.cell_view.image_appearance_path = Path(session.output_root) / 'image-appearance.json'
        self.panel.page_requested.connect(self._page)
        self.panel.field.requested.connect(self._field)
        self.panel.assign_requested.connect(lambda selection: self.controller.assign_supervised_cutoff(
            None, head, selection, artifact_ref_sha256=artifact))
        self.body.addWidget(self.panel)
        # The initial query belongs to the saved image selected in Results.
        request = dict(self.panel._settings(), image_identity=context['image_identity'], offset=0,
                       page_size=self.panel.model.page_size)
        self.panel._active_page = request
        self._page(request)

    def _page(self, settings):
        session, head, artifact = self.anchor
        if not self.current(artifact, head):
            return
        self._submit('page', lambda: session.service.supervised_prediction_page(session.batch_id,
            artifact_ref_sha256=artifact, expected_state_head_sha256=head, settings=settings), dict(settings))

    def _field(self):
        if self.panel is None or self.anchor is None:
            return
        options = self.panel.field.options()
        if options is not None:
            self.field_pending = (self.anchor, self.page, self.panel.field.revision, options)
            self._next_field()

    def _next_field(self):
        if self.field_pending is None or any(kind == 'field' for kind, *_ in self.jobs.values()):
            return
        anchor, page, revision, options = self.field_pending
        self.field_pending = None
        session, head, artifact = anchor
        if page is not self.page or not self.current(artifact, head):
            return
        self._submit('field', lambda: session.service.marker_field(session.batch_id,
            artifact_ref_sha256=artifact, expected_state_head_sha256=head, figure_options=options), revision)

    def succeeded(self, job, value):
        pending = self.jobs.pop(job, None)
        if pending is None:
            return False
        kind, anchor, page, detail = pending
        if page is self.page and anchor == self.anchor and self.current(anchor[2], anchor[1]):
            if kind == 'context':
                self._show_context(value)
            elif kind == 'page':
                if not self.panel._cohort_images and value.get('cohort_images'):
                    navigation = self.panel.image_navigation
                    navigation.set_images(value['cohort_images'])
                    with QSignalBlocker(navigation.images):
                        navigation.images.setCurrentIndex(navigation.images.findData(value['active_image']))
                    navigation._buttons()
                self.panel.set_page(value)
            elif detail == self.panel.field.revision:
                self.panel.field.set_bundle(value)
        self._next_field()
        return True

    def failed(self, job, error):
        pending = self.jobs.pop(job, None)
        if pending is None:
            return False
        kind, anchor, page, detail = pending
        if page is self.page and anchor == self.anchor and self.current(anchor[2], anchor[1]):
            if kind == 'context':
                self.status.setText(error.message)
                self.retry.show()
            elif kind == 'page':
                self.panel.page_failed(detail, error.message)
            elif detail == self.panel.field.revision:
                self.panel.field.set_error(error.message)
        self._next_field()
        return True

    def assignment_finished(self, page, message):
        if page is self.page and self.panel is not None:
            self.panel.status.setText(message)
            self.panel.setEnabled(False)
            self.status.setText('Return to Results to review the current saved assignments.')
            self.status.show()
