"""Retain one accepted H&E reader while a cell or tissue-field review is visible.

The ordinary paired-image service supplies acceptance and source identity. The
existing managed native registration session owns readers and transformations.
All response threads use the controller's job bridge, so application shutdown
and review responses can drain the process and its reads before cleanup.
"""
from __future__ import annotations

import math
from pathlib import Path
import weakref

from PySide6.QtCore import QTimer
from shiboken6 import isValid

from .jobs import JobAccess


def _create_session(*args, **kwargs):
    from mif_pipeline.qupath_runtime.registration_editor import create_registration_session
    return create_registration_session(*args, **kwargs)


def _accepted_pair(views, context):
    """Select only an accepted H&E pair in this exact prepared cell frame."""
    frame = context['source_frame']
    matches = []
    for view in views:
        if (view.get('sample_id') != context.get('sample_id')
                or view.get('moving_modality') != 'he'
                or view.get('fixed_image_id') != frame.get('image_id')
                or view.get('fixed_image_sha256') != frame.get('image_sha256')):
            continue
        calibration = view.get('fixed_physical_calibration', {})
        if all(math.isclose(calibration.get(f'{axis}_um_per_px', 0.),
                            frame[f'level0_pixel_size_{axis}_um'], rel_tol=1e-10, abs_tol=0.)
               for axis in ('x', 'y')):
            matches.append(view)
    if len(matches) > 1:
        raise ValueError('More than one accepted H&E pair matches this cell image.')
    return matches[0] if matches else None


def _field_request(metadata, context):
    """Bound a physical cell field in fixed original pixels without rescaling it."""
    fixed, moving = metadata['fixed'], metadata['moving']
    left, top, right, bottom = map(float, context['field_um'])
    if not all(map(math.isfinite, (left, top, right, bottom))) or right <= left or bottom <= top:
        raise ValueError('The selected cell field has invalid physical bounds.')
    x = max(0, math.floor(left / fixed['x_um']))
    y = max(0, math.floor(top / fixed['y_um']))
    end_x = min(fixed['width'], math.ceil(right / fixed['x_um']))
    end_y = min(fixed['height'], math.ceil(bottom / fixed['y_um']))
    width, height = end_x - x, end_y - y
    if width <= 0 or height <= 0:
        raise ValueError('This cell field is outside the aligned image.')
    viewport = [min(2048, max(1, int(v))) for v in context['viewport_size']]
    # Permit finer H&E sampling than the fluorescence pixel size. The native
    # reader chooses its own source pyramid; returned origin/spacing are kept.
    downsample = max(moving['x_um'] / fixed['x_um'], moving['y_um'] / fixed['y_um'],
                     width / viewport[0], height / viewport[1])
    return dict(x=x, y=y, width=width, height=height, downsample=downsample,
                sources=['moving'], moving_channels=[0, 1, 2], mask_outside_moving=True)


class CellAlignmentNavigation:
    def __init__(self, controller):
        self.controller = controller
        self._viewer_ref = self._binding = self._current = None
        self._slots = ()
        self._context = self._frame = self._pair = self._project = None
        self._session = self._metadata = None
        self._jobs = {}
        self._generation = 0
        self._closing = False
        self._requested = self._next_binding = self._after = None

    @property
    def pending(self):
        return bool(self._jobs) or self._session is not None

    def _viewer(self):
        viewer = self._viewer_ref() if self._viewer_ref is not None else None
        return viewer if viewer is not None and isValid(viewer) else None

    def _valid(self):
        if self._closing or self.controller._close_pending or self._viewer() is None or self._current is None:
            return False
        return self._current()

    def bind(self, viewer, binding, current):
        """Attach after the ordinary authenticated review has loaded its pixels."""
        if viewer is self._viewer() and binding == self._binding and not self._closing:
            self._context_changed(viewer.alignment_context())
            return
        self.close()
        self._next_binding = (weakref.ref(viewer), binding, current)
        self._drain()

    def _activate(self, viewer, binding, current):
        self._closing = False
        self._viewer_ref, self._binding, self._current = weakref.ref(viewer), binding, current
        context_slot = self._context_changed
        request_slot = self._request
        destroyed_slot = lambda *_: self.close()
        self._slots = ((viewer.alignment_context_changed, context_slot),
                       (viewer.alignment_requested, request_slot), (viewer.destroyed, destroyed_slot))
        for signal, slot in self._slots:
            signal.connect(slot)
        viewer.set_alignment_available(False)
        self._context_changed(viewer.alignment_context())

    def _context_changed(self, context):
        if context is None or not self._valid():
            self.close()
            return
        frame = (context['sample_id'], tuple(sorted(context['source_frame'].items())))
        if self._frame is not None and frame != self._frame:
            viewer, binding, current = self._viewer(), self._binding, self._current
            self.close()
            self._next_binding = (weakref.ref(viewer), binding, current)
            self._drain()
            return
        self._context = context
        if self._frame is None:
            self._frame = frame
            workspace, head = self._binding[:2]
            def lookup():
                service = workspace.service
                project = service.saved_project_path(workspace.batch_id, expected_head_sha256=head)
                service.require_current_saved_results(workspace.batch_id,
                    expected_head_sha256=head, project_path=project)
                views = service.paired_image_views(workspace.batch_id,
                    expected_head_sha256=head, project_path=project)
                return project, _accepted_pair(views, context)
            self._submit('lookup', 'check_cell_alignment', lookup)

    def _submit(self, kind, name, operation, *, access=JobAccess.READ, identity=None):
        job = self.controller.jobs.submit(name, operation, access=access)
        self._jobs[job] = (kind, self._generation, identity)

    def _request(self, context):
        if not self._valid() or self._pair is None:
            return
        viewer = self._viewer()
        self._requested = (viewer.alignment_field_identity(), dict(context))
        if self._session is None:
            if any(item[0] == 'prepare' for item in self._jobs.values()):
                return
            pair, project = self._pair, self._project
            workspace = self._binding[0]
            self._submit('prepare', 'prepare_cell_alignment', lambda: _create_session(
                project, dict(mode='overlay', fixed_entry_id=pair['fixed_entry_id'],
                              moving_entry_id=pair['moving_entry_id']), paired_views=(pair,),
                appearance_path=Path(workspace.output_root) / 'image-appearance.json'))
        else:
            self._render()

    def _render(self):
        if (not self._valid() or self._metadata is None or self._requested is None
                or any(item[0] == 'render' for item in self._jobs.values())):
            return
        identity, context = self._requested
        self._requested = None
        try:
            request = _field_request(self._metadata, context)
            native = self._session
            self._submit('render', 'read_cell_alignment', lambda: native.request('render', **request),
                         identity=identity)
        except (RuntimeError, ValueError) as error:
            self._fail(str(error))

    def close(self):
        """Signal native close; keep every response job until its thread is joined."""
        viewer = self._viewer()
        if viewer is not None:
            for signal, slot in self._slots:
                signal.disconnect(slot)
            viewer.set_alignment_available(False)
        self._slots = ()
        self._viewer_ref = self._binding = self._current = None
        self._context = self._frame = self._pair = self._project = None
        self._requested = self._next_binding = self._after = None
        self._closing = True
        self._generation += 1
        if self._session is not None:
            self._session.close()
        self._drain()

    def finish_then(self, callback, *, current=None):
        """Drain an optional reader before applying the user's review response."""
        pending, current = self.pending, current or self._current
        self.close()
        if pending:
            self._after = (callback, current)
        return pending

    def _fail(self, message):
        viewer = self._viewer()
        if viewer is not None:
            viewer.set_alignment_failure(message)
        self.close()

    def _worker_finished(self, error=None):
        self._session.worker_finished(error)
        if not self._closing:
            self._fail(error or 'The aligned H&E reader closed. Select the cell again to reopen it.')

    def succeeded(self, job_id, value):
        record = self._jobs.pop(job_id, None)
        if record is None:
            return False
        kind, generation, identity = record
        current = generation == self._generation and self._valid()
        try:
            if kind == 'lookup' and current:
                self._project, self._pair = value
                self._viewer().set_alignment_available(self._pair is not None)
            elif kind == 'prepare':
                self._session = value
                if current:
                    self._submit('worker', 'cell_alignment_reader',
                        lambda: self.controller.file_operations.run_registration_editor(value), access=JobAccess.MUTATION)
                    self._submit('ready', 'open_cell_alignment', value.wait_ready)
                else:
                    value.close()
                    value.worker_finished()
            elif kind == 'ready' and current:
                pair = self._pair
                if (not value['moving']['rgb']
                        or any(value[name]['image_uri'].rstrip('/') != pair[f'{name}_image_uri'].rstrip('/')
                               for name in ('fixed', 'moving'))
                        or any(not math.isclose(value['fixed'][f'{axis}_um'],
                            pair['fixed_physical_calibration'][f'{axis}_um_per_px'], rel_tol=1e-10, abs_tol=0.)
                               for axis in ('x', 'y'))):
                    raise ValueError('The native H&E view differs from the accepted cell frame.')
                self._metadata = value
                self._render()
            elif kind == 'render' and current:
                viewer = self._viewer()
                if identity == viewer.alignment_field_identity():
                    if (len(value['arrays']) != 1 or value['images'][0]['source'] != 'moving'
                            or not value['images'][0]['rgb']):
                        raise ValueError('The aligned H&E reader did not return an RGB field.')
                    fixed, spacing = self._metadata['fixed'], value['downsample']
                    viewer.set_alignment_image(identity, value['arrays'][0],
                        (value['x'] * fixed['x_um'], value['y'] * fixed['y_um']),
                        (spacing * fixed['x_um'], spacing * fixed['y_um']), dict(self._pair))
                self._render()
            elif kind == 'worker':
                self._worker_finished()
            elif kind == 'cleanup':
                self._session = self._metadata = None
        except (RuntimeError, ValueError, OSError) as error:
            if kind == 'prepare' and not any(item[0] == 'worker' for item in self._jobs.values()):
                value.worker_finished(str(error))
            self._fail(str(error))
        if not current and not self._closing:
            self.close()
        self._drain()
        return True

    def failed(self, job_id, error):
        record = self._jobs.pop(job_id, None)
        if record is None:
            return False
        kind, generation, _ = record
        if kind == 'worker':
            self._worker_finished(error.message)
        elif kind == 'cleanup':
            self._session = self._metadata = None
            self.controller.operation_failed.emit(error.message)
        elif generation == self._generation and not self._closing:
            self._fail(error.message)
        self._drain()
        return True

    def _drain(self):
        # A worker result wakes blocked response threads; their bridge results
        # must also arrive before the exchange directory can be removed.
        if self._closing and self._session is not None and not self._jobs:
            self._submit('cleanup', 'close_cell_alignment', self._session.cleanup)
        if self.pending:
            return
        following, self._next_binding = self._next_binding, None
        after, self._after = self._after, None
        if not self.controller._close_pending:
            if following is not None:
                reference, binding, current = following
                viewer = reference()
                if viewer is not None and isValid(viewer) and current():
                    self._activate(viewer, binding, current)
            elif after is not None:
                callback, current = after
                if current is not None and current():
                    generation = self._generation
                    QTimer.singleShot(0, lambda: callback() if generation == self._generation
                        and not self.controller._close_pending and current() else None)
        self.controller._advance_close()
