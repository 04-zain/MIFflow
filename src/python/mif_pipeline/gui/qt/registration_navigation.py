"""Keep the registration page alive until its native and image jobs have ended."""
from .jobs import JobAccess


def _editor(session, parent):
    from .registration_editor import RegistrationEditor
    return RegistrationEditor(session, parent)


class RegistrationNavigation:
    def __init__(self, controller):
        self.controller = controller
        self.session = self.editor = None
        self.setup_job = self.worker_job = self.cleanup_job = None
        self._current = None
        self._saved = False

    @property
    def pending(self):
        return self.setup_job is not None or self.session is not None

    def prepare(self, operation, current):
        """Prepare an authenticated pair off-thread, then mount its Qt editor."""
        if self.pending:
            return
        self._current = current
        self.setup_job = self.controller.jobs.submit(
            'prepare_registration_editor', operation, access=JobAccess.READ)

    def close(self):
        if self.editor is not None:
            request = getattr(self.editor, 'request_close', None)
            if request is not None and not getattr(self.editor, '_closing', False):
                request()
        if self.session is not None:
            self.session.close()

    def succeeded(self, job_id, value):
        c = self.controller
        if job_id == self.setup_job:
            self.setup_job = None
            self.session = value
            if c._close_pending or not self._current():
                value.worker_finished()
                self._cleanup()
                return True
            try:
                self.editor = _editor(value, c.reviews_page)
                self.editor.close_requested.connect(self.close)
                self.editor.finished.connect(self._editor_finished)
                saved = getattr(self.editor, 'candidate_saved', None)
                if saved is not None:
                    saved.connect(self._candidate_saved)
                self.worker_job = c.jobs.submit(
                    'registration_editor', lambda: c.file_operations.run_registration_editor(value),
                    access=JobAccess.MUTATION)
                c.reviews_page.show_registration_editor(self.editor)
                c.shell.show_route('reviews')
            except (ValueError, RuntimeError, OSError) as error:
                c.operation_failed.emit(str(error))
                if self.worker_job is not None:
                    self.close()
                else:
                    self._worker_finished(str(error))
            return True
        if job_id == self.worker_job:
            self.worker_job = None
            self._worker_finished()
            return True
        if job_id == self.cleanup_job:
            self.cleanup_job = None
            refresh = not c._close_pending and self._current is not None and self._current()
            self.session = self._current = None
            if refresh and hasattr(c, 'refresh_status'):
                c.refresh_status()
            c._advance_close()
            return True
        return False

    def _candidate_saved(self, result):
        self._saved = True

    def _worker_finished(self, error=None):
        self.session.worker_finished(error)
        if self.editor is not None:
            # The editor emits finished only after its own image jobs are joined.
            self.editor.worker_finished(error)
        else:
            self._cleanup()

    def _editor_finished(self):
        c = self.controller
        editor, self.editor = self.editor, None
        if editor is not None:
            c.reviews_page.finish_registration_editor(editor)
            if self._saved:
                c.reviews_page._set_status_message('Alignment candidate saved. Import it in this form, then inspect the checks and accept or reject it.')
        self._saved = False
        self._cleanup()

    def _cleanup(self):
        if self.cleanup_job is None:
            self.cleanup_job = self.controller.jobs.submit(
                'close_registration_editor', self.session.cleanup, access=JobAccess.READ)

    def failed(self, job_id, error):
        c = self.controller
        if job_id == self.setup_job:
            self.setup_job = None
            self._current = None
        elif job_id == self.worker_job:
            self.worker_job = None
            self._worker_finished(error.message)
        elif job_id == self.cleanup_job:
            self.cleanup_job = None
            self.session = self._current = None
        else:
            return False
        c.operation_failed.emit(error.message)
        c._advance_close()
        return True
