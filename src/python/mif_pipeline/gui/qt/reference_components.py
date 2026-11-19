"""Reference sources and functional preparation paths for the current library."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QSizePolicy,
)

from .researcher_widgets import ContentSizedButton


CHECKPOINT_LABELS = {
    'positive_reference_rois': 'Select stained and background regions',
    'af_reference_rois': 'Assign AF controls',
    'negative_reference_rois': 'Select negative-control regions',
    'background_reference_rois': 'Select background regions',
    'signal_dark_measurements': 'Choose signal and AF-estimation bands',
    'extraction_qc': 'Review fluorophore extraction',
    'fluorophore_consensus': 'Review the combined spectrum',
    'target_af': 'Autofluorescence',
    'target_af_control_review': 'Autofluorescence review',
    'final_run_basis': 'Library review',
}


def preparation_progress(form, values):
    """Project live file choices, or decorate saved progress with offered routes."""
    context = form.context
    offered = next((control.choices for control in form.controls if control.name == 'option_value'), ())
    offered = context.get('reference_preparation_navigation_options', offered)
    if not context.get('reference_source_overview'):
        progress = context.get('reference_preparation_progress') or context.get('reference_component_progress')
        if progress is None:
            return None
        navigation = tuple(option for option in offered if option in {
            'revise_shared_control', 'retry_shared_control_preparation',
            'reselect_reference_rois', 'revise_signal_dark_measurements', 'review_control_sets'})
        summary = context.get('stage_result', {}).get('resume_context', {}).get('service_context', {}).get('operation_state', {}).get('review_summary', {})
        state = context.get('stage_result', {}).get('resume_context', {}).get('service_context', {}).get('operation_state', {})
        revisitable = next((field.get('value_choices', ())
            for requirement in state.get('operation_requirements', ())
            if requirement['option_value'] == 'revise_component_preparation'
            for field in requirement.get('form_fields', ()) if field['name'] == 'component'), ())
        independent = (progress.get('preparation_mode') == 'independent' or
            (progress.get('shared_control') or {}).get('preparation_mode') == 'independent')
        revisitable = tuple(name for name in revisitable if independent or any(
            row['name'] == name and (row.get('control') or {}).get('preparation_mode') == 'independent'
            for row in progress.get('components', ())))
        review = summary.get('control_preparation') or summary.get('shared_control_preparation') or {}

        def control(row):
            return dict(row, can_inspect=bool(row.get('accepted') and row.get('input_id')
                and (row.get('preparation_sha256') or review.get('input_id') == row['input_id'])))

        projected = dict(progress, navigation_options=navigation, revisitable_components=tuple(revisitable))
        if progress.get('shared_control'):
            projected['shared_control'] = control(progress['shared_control'])
        projected['components'] = [dict(row, control=control(row['control']))
            if row.get('control') else row for row in progress['components']]
        return projected

    state = context['stage_result']['resume_context']['service_context']['operation_state']
    inventory = state['review_summary']['reference_file_choices']
    fields = context['reference_file_bindings']
    controls = {control.name: control for control in form.controls}
    rows = {row['control']: row for group in context['reference_source_overview'] for row in group['rows']}
    labels = context.get('choice_labels', {})

    def source(field):
        value = values.get(field)
        ready = value is not None and value in controls[field].choices
        return {'source_field': field, 'input_id': value if ready else None,
                'label': labels.get(field, {}).get(value, '') if ready else '',
                'source_ready': ready, 'can_add': rows[field]['option'] is not None}

    mode = ('independent' if context.get('selected_option') ==
        'accept_reference_control_files_with_source_preparation' else 'per_fluorophore')
    components = []
    for index, (name, field) in enumerate(zip(inventory['fluorophores'], fields['positive_input_ids'], strict=True)):
        row = dict(source(field), name=name, status='not_started', can_open=False)
        if fields.get('negative_input_ids'):
            row['control'] = dict(source(fields['negative_input_ids'][index]), preparation_mode=mode)
        components.append(row)
    shared = None
    if fields.get('shared_unstained_input_id'):
        shared = dict(source(fields['shared_unstained_input_id'][0]), accepted=False, current=False, can_open=False,
            preparation_mode=mode)
    sources = [source(field) for names in fields.values() for field in names]
    return {'components': components, 'shared_control': shared, 'source_selection': True,
            'sources_ready': sum(row['source_ready'] for row in sources), 'source_count': len(sources),
            'current_checkpoint': 'reference_control_files', 'current_component': None, 'navigation_options': ()}


def _label(text, parent, *, style='', help=False):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    label.setMinimumWidth(0)
    if help:
        label.setProperty('fieldHelp', True)
    if style:
        label.setStyleSheet(style)
    return label


class _SourceLabel(QLabel):
    """Keep the exact source identity available when its filename is shortened."""

    def __init__(self, filename, parent):
        super().__init__(filename, parent)
        self._filename = filename
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setToolTip(filename)
        self.setAccessibleName(filename)
        self.setStyleSheet('color:#31535b; background:transparent;')
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def resizeEvent(self, event):
        self.setText(self.fontMetrics().elidedText(
            self._filename, Qt.TextElideMode.ElideMiddle, self.contentsRect().width()))
        super().resizeEvent(event)


class ReferenceComponentsOverview(QWidget):
    """Open a source, a live preparation phase, or an accepted spectrum."""
    resumed = Signal()
    region_requested = Signal(str)
    inspection_requested = Signal(str)
    control_inspection_requested = Signal(str)
    source_requested = Signal(str)
    sources_accepted = Signal()
    option_requested = Signal(str)
    preparation_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('reference-components-overview')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(20)
        self.count = _label('', self, style='color:#526971;')
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, 1)
        self.continue_button = ContentSizedButton('', self)
        self.continue_button.setObjectName('reference-components-continue')
        self.continue_button.setProperty('actionRole', 'primary')
        self.continue_button.clicked.connect(self._continue)
        layout.addWidget(self.continue_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.card_actions, self.statuses, self.control_actions = {}, {}, {}
        self.phase_actions, self.source_actions, self.revision_actions = {}, {}, {}
        self._paths, self._grids = [], []
        self._generation = 0

    def _continue(self):
        if self.progress.get('source_selection'):
            self.sources_accepted.emit()
        else:
            self.resumed.emit()

    def _grid(self, parent, *, header=False):
        grid = QGridLayout(parent)
        grid.setContentsMargins(16, 12 if header else 18, 16, 12 if header else 18)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(0, 100)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(2, self._phase_width)
        self._grids.append(grid)
        return grid

    def _status(self, text, parent, *, state):
        color = {'accepted': '#28665b', 'current': '#006b70', 'not_started': '#66797d'}[state]
        label = _label(text, parent, style=f'color:{color};')
        label.setWordWrap(False)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        return label

    def _button(self, title, parent, callback, *, primary=False, accessible=None):
        button = ContentSizedButton(title, parent)
        button.setProperty('actionRole', 'primary' if primary else 'secondary')
        button.setStyleSheet('QPushButton { border-radius:0; padding:6px 8px; }')
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Deferred Qt events from a removed row must not act on its successor.
        button.clicked.connect(lambda checked=False, generation=self._generation:
            callback(checked) if generation == self._generation else None)
        if accessible:
            button.setAccessibleName(accessible)
        return button

    def _source(self, parent, row, *, caption=None):
        source = QWidget(parent)
        source.setMinimumWidth(0)
        source.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(source)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        if caption:
            layout.addWidget(_label(caption, source, style='color:#526971;'))
        if row.get('label'):
            layout.addWidget(_SourceLabel(row['label'], source))
        field = row.get('source_field')
        if field:
            action = self._button('Change image…' if row.get('source_ready') else 'Add image…', source,
                lambda _checked=False: self.source_requested.emit(field),
                accessible='Choose reference image: ' + (caption or row.get('name') or 'Shared control'))
            action.setProperty('actionRole', 'navigation')
            layout.addWidget(action, alignment=Qt.AlignmentFlag.AlignLeft)
            self.source_actions[field] = action
        return source

    def _path(self, parent, steps, *, name=None):
        path = QWidget(parent)
        path.setFixedWidth(self._phase_width)
        layout = QGridLayout(path)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(0)
        layout.setVerticalSpacing(8)
        widgets, lines = [], []
        for index, step in enumerate(steps):
            if index:
                line = QFrame(path)
                line.setFixedSize(12, 1)
                line.setStyleSheet('background:#a7c6bf; border:none;')
                lines.append(line)
            callback = step.get('callback')
            if callback:
                widget = self._button(step['title'], path, callback,
                    primary=step['state'] == 'current', accessible=step['accessible'])
                widget.setToolTip(step['accessible'])
                if name:
                    self.phase_actions[(name, step['key'])] = widget
            else:
                color = '#28665b' if step['state'] == 'accepted' else '#718287'
                widget = _label(step['title'], path, style=f'color:{color}; padding:6px 4px;')
                widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
                widget.setAccessibleName(step.get('accessible', step['title']) +
                    (' · Complete' if step['state'] == 'accepted' else ' · Not started'))
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            widgets.append(widget)
            step['widget'] = widget
        self._paths.append((path, widgets, lines))
        return path

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_phase_sizes'):
            self._arrange_paths()

    def _arrange_paths(self):
        narrow = self.width() < 960
        sizes = self._phase_sizes
        self._phase_width = (max(sizes[::2]) + 12 + max(sizes[1::2])
            if narrow else sum(sizes) + 12 * (len(sizes) - 1))
        for grid in self._grids:
            grid.setColumnMinimumWidth(2, self._phase_width)
        for path, widgets, lines in self._paths:
            path.setFixedWidth(self._phase_width)
            layout = path.layout()
            for widget in (*widgets, *lines):
                layout.removeWidget(widget)
            for column in range(max(9, 2 * len(widgets) - 1)):
                layout.setColumnMinimumWidth(column, 0)
                layout.setColumnStretch(column, 0)
            wrap = narrow and len(widgets) > 2
            for index, widget in enumerate(widgets):
                row, column = (index // 2, 2 * (index % 2)) if wrap else (0, 2 * index)
                layout.addWidget(widget, row, column)
                layout.setColumnStretch(column, 1 if len(widgets) > 2 or narrow else 0)
                if len(widgets) > 2:
                    layout.setColumnMinimumWidth(column, max(sizes[index % 2::2]) if wrap else sizes[index])
                else:
                    layout.setColumnMinimumWidth(column, max(sizes[index], sizes[index + 2])
                        if narrow else sizes[0] if index == 0 else self._control_spectrum_width)
                if index:
                    line = lines[index - 1]
                    line.setVisible(not wrap or index % 2 != 0)
                    if not line.isHidden():
                        layout.addWidget(line, row, column - 1)
            if len(widgets) == 2 and not narrow:
                layout.setColumnStretch(4, 1)
            path.updateGeometry()

    @staticmethod
    def _active_stage(checkpoint):
        if checkpoint in {'positive_reference_rois', 'negative_reference_rois', 'background_reference_rois'}:
            return 0
        if checkpoint in {'af_reference_rois', 'signal_dark_measurements'}:
            return 1
        return {'extraction_qc': 2, 'fluorophore_consensus': 3}.get(checkpoint)

    def _control_path(self, control, parent):
        """The control has its own preparation and review, before stained regions."""
        active = control.get('current') and control.get('can_open')
        pending, accepted = control.get('proposal_available'), control.get('accepted')
        offered = self.progress.get('navigation_options', ())
        revision = ('retry_shared_control_preparation' if pending
            and 'retry_shared_control_preparation' in offered else 'revise_shared_control'
            if control.get('can_revise') and 'revise_shared_control' in offered else None)
        source = control.get('label') or 'AF control'
        steps = [dict(title='Regions', key='regions',
            state='accepted' if pending or accepted else 'current' if active else 'not_started'),
            dict(title='Review and combine', key='spectrum', state='accepted' if accepted
                else 'current' if active and pending else 'not_started')]
        if revision:
            steps[0].update(callback=lambda _checked=False: self.option_requested.emit(revision),
                accessible=('Edit AF regions and sets: ' if revision.startswith('retry') else
                    'Revise AF preparation and review dependent recomputation: ') + source)
        if active:
            steps[1 if pending else 0].update(callback=lambda _checked=False: self.resumed.emit(),
                accessible=('Review AF control spectra: ' if pending else 'Prepare AF regions and sets: ') + source)
        elif (accepted and control.get('can_revise') and 'review_control_sets' in offered):
            steps[1].update(callback=lambda _checked=False: self.option_requested.emit('review_control_sets'),
                accessible='Review and combine AF control spectra: ' + source)
        elif control.get('can_inspect'):
            steps[1].update(callback=lambda _checked=False: self.control_inspection_requested.emit(control['input_id']),
                accessible='View accepted AF control spectra: ' + source)
        path = self._path(parent, steps)
        if active or control.get('can_inspect'):
            self.control_actions[control['input_id']] = steps[1 if pending or accepted else 0]['widget']
        if revision:
            self.revision_actions[revision] = steps[0]['widget']
        return path

    def _control(self, control, parent):
        card = QFrame(parent)
        card.setObjectName('reference-shared-control-card')
        active = control.get('current') and control.get('can_open')
        card.setStyleSheet('QFrame#reference-shared-control-card { background:' +
            ('#edf6f2' if active else '#ffffff') + '; border:1px solid #cfded9; border-radius:0; }')
        grid = self._grid(card)
        identity = QVBoxLayout(); identity.setSpacing(8)
        heading = QHBoxLayout(); heading.setSpacing(14)
        heading.addWidget(_label('Shared AF control', card, style='font-weight:600; color:#173d47;'))
        independent = control.get('preparation_mode') == 'independent'
        selecting = self.progress.get('source_selection')
        status = ('Image ready' if control.get('source_ready') else 'Image needed') if selecting else (
            'Prepared' if independent and control.get('accepted') else
            'Ready to review' if control.get('proposal_available') else
            'In progress' if active else 'Source selected' if not independent else 'Not started')
        state = 'accepted' if control.get('accepted') else 'current' if active else 'not_started'
        heading.addWidget(self._status(status, card, state=state)); heading.addStretch(1)
        identity.addLayout(heading)
        identity.addWidget(self._source(card, control))
        grid.addLayout(identity, 0, 0, 1, 2, Qt.AlignmentFlag.AlignVCenter)
        if independent:
            grid.addWidget(self._control_path(control, card), 0, 2)
        else:
            label = _label('Control regions are selected with each fluorophore', card, style='color:#526971;')
            grid.addWidget(label, 0, 2)
        return card

    def _component(self, row, parent, *, checkpoint, detailed):
        name, status = row['name'], row['status']
        card = QFrame(parent)
        card.setObjectName('reference-component-row')
        card.setStyleSheet('QFrame#reference-component-row { background:' +
            ('#edf6f2' if status == 'current' else '#ffffff') +
            '; border:none; border-left:3px solid ' + ('#19817c' if status == 'current' else 'transparent') + '; }')
        grid = self._grid(card); grid.setContentsMargins(13, 16, 16, 16)
        grid.setVerticalSpacing(18)
        identity = QVBoxLayout(); identity.setSpacing(7)
        name_label = _label(name, card, style='font-weight:600; color:#173d47;')
        name_label.setFixedWidth(100); identity.addWidget(name_label)
        source_stage = self.progress.get('source_selection')
        control = row.get('control')
        ready = row.get('source_ready') and (not control or control.get('source_ready'))
        source_status = ('Images ready' if ready else 'Images needed') if control else ('Image ready' if ready else 'Image needed')
        status_text = source_status if source_stage else {
            'accepted': 'Accepted', 'current': 'In progress', 'not_started': 'Not started'}[status]
        badge = self._status(status_text, card, state=status)
        identity.addWidget(badge); self.statuses[name] = badge
        independent = control and control.get('preparation_mode') == 'independent'
        grid.addLayout(identity, 0, 0, 2 if independent else 1, 1, Qt.AlignmentFlag.AlignVCenter)
        if independent:
            grid.addWidget(self._source(card, control, caption='AF control'), 0, 1)
            grid.addWidget(self._control_path(control, card), 0, 2)
            grid.addWidget(self._source(card, row, caption='Stained image'), 1, 1)
        else:
            sources = QVBoxLayout(); sources.setSpacing(12)
            if control:
                sources.addWidget(self._source(card, control, caption='Negative control'))
            sources.addWidget(self._source(card, row, caption='Stained image' if control else None))
            grid.addLayout(sources, 0, 1)

        source_preparation = self.progress.get('preparation_mode') == 'independent' or (
            self.progress.get('shared_control') or {}).get('preparation_mode') == 'independent' or independent
        phase_keys = ('regions', 'assignments', 'bands', 'extraction', 'spectrum') if source_preparation else (
            'regions', 'bands', 'extraction', 'spectrum')
        active = ({'positive_reference_rois': 0, 'af_reference_rois': 1, 'signal_dark_measurements': 2,
            'extraction_qc': 3, 'fluorophore_consensus': 4}.get(checkpoint)
            if source_preparation else self._active_stage(checkpoint))
        can_open = row.get('can_open', status == 'current' and not detailed)
        # At a legacy control/background checkpoint the mounted control page is
        # the current region phase even if the stained page cannot open yet.
        control_open = control and not independent and control.get('current') and control.get('can_open')
        options = self.progress.get('navigation_options', ())
        revisitable = name in self.progress.get('revisitable_components', ())
        steps = []
        for index, key in enumerate(phase_keys):
            title = {'regions': 'Regions', 'assignments': 'AF controls', 'bands': 'Bands',
                'extraction': 'Extraction', 'spectrum': 'Combined spectrum'}[key]
            state = 'accepted' if status == 'accepted' or status == 'current' and active is not None and index < active else (
                'current' if status == 'current' and index == active else 'not_started')
            step = dict(key=key, title=title, state=state)
            if revisitable and (status == 'accepted' or status == 'current' and active is not None and index < active):
                phase = 'consensus' if key == 'spectrum' else key
                step.update(callback=lambda _checked=False, value=phase: self.preparation_requested.emit(name, value),
                    accessible='Open saved ' + title.lower() + ': ' + name)
            elif status == 'accepted' and key == 'spectrum':
                step.update(callback=lambda _checked=False: self.inspection_requested.emit(name),
                    accessible='View accepted spectrum for ' + name)
            elif status == 'current' and index == active and (can_open or index == 0 and control_open):
                entry_page = row.get('entry_page') if index == 0 else None
                if index == 0 and control_open and not control.get('accepted'):
                    entry_page = 'control'
                action_label = ('Select reference regions' if index == 0 and control and
                    control.get('combined_region_draft') else CHECKPOINT_LABELS.get(checkpoint, 'Continue reference preparation'))
                step.update(callback=(lambda _checked=False, page=entry_page: self.region_requested.emit(page))
                    if entry_page else lambda _checked=False: self.resumed.emit(),
                    accessible=action_label + ': ' + name)
            elif status == 'current' and active is not None and index < active and key in {'regions', 'bands'}:
                option = 'reselect_reference_rois' if key == 'regions' else 'revise_signal_dark_measurements'
                if option in options:
                    step.update(callback=lambda _checked=False, value=option: self.option_requested.emit(value),
                        accessible=('Edit reference regions' if key == 'regions' else 'Change signal and AF-estimation bands') + ': ' + name)
            steps.append(step)
        path = self._path(card, steps, name=name)
        grid.addWidget(path, 1 if independent else 0, 2)
        primary = (name, 'spectrum' if status == 'accepted' else
            phase_keys[active] if active is not None else None)
        if primary in self.phase_actions:
            action = self.phase_actions[primary]
            if not can_open and control_open and active == 0:
                self.control_actions[control['input_id']] = action
            else:
                self.card_actions[name] = action
        return card

    def set_progress(self, progress):
        self._generation += 1
        self.progress = progress
        rows = progress['components']
        accepted = sum(row['status'] == 'accepted' for row in rows)
        source_stage = progress.get('source_selection')
        self.count.setText(f"{progress['sources_ready']} of {progress['source_count']} images ready" if source_stage else
            f'{accepted} of {len(rows)} spectra accepted')
        probe = self._button('', self, lambda _checked=False: None)
        probe.hide()
        sizes = []
        independent = progress.get('preparation_mode') == 'independent' or (
            progress.get('shared_control') or {}).get('preparation_mode') == 'independent' or any(
                (row.get('control') or {}).get('preparation_mode') == 'independent' for row in rows)
        titles = ('Regions', 'AF controls', 'Bands', 'Extraction', 'Combined spectrum') if independent else (
            'Regions', 'Bands', 'Extraction', 'Combined spectrum')
        for title in titles:
            probe.setText(title)
            sizes.append(probe.sizeHint().width() + 4)
        self._phase_sizes = tuple(sizes)
        probe.setText('Review and combine')
        self._control_spectrum_width = probe.sizeHint().width() + 4
        self._phase_width = sum(sizes) + 36
        probe.deleteLater()
        body = QWidget(self.scroll)
        contents = QVBoxLayout(body); contents.setContentsMargins(0, 0, 0, 0)
        contents.setSpacing(22); contents.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card_actions, self.statuses, self.control_actions = {}, {}, {}
        self.phase_actions, self.source_actions, self.revision_actions = {}, {}, {}
        self._paths, self._grids = [], []
        if source_stage:
            contents.addWidget(self.count)
        if progress.get('shared_control'):
            contents.addWidget(self._control(progress['shared_control'], body))
        section = QWidget(body)
        section_layout = QVBoxLayout(section); section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(10)
        heading = QHBoxLayout(); heading.setSpacing(18)
        title = _label('Fluorophore references', section); title.setProperty('panelHeading', True)
        heading.addWidget(title, 1)
        if not source_stage:
            heading.addWidget(self.count, 0, Qt.AlignmentFlag.AlignRight)
        section_layout.addLayout(heading)
        library = QFrame(section); library.setObjectName('reference-component-library')
        library.setStyleSheet('QFrame#reference-component-library { background:#ffffff; border:1px solid #d5dfdc; border-radius:0; }')
        table = QVBoxLayout(library); table.setContentsMargins(1, 1, 1, 1); table.setSpacing(0)
        header = QWidget(library); columns = self._grid(header, header=True)
        for column, text in enumerate(('Fluorophore', 'Source images' if any(row.get('control') for row in rows) else 'Stained image', 'Preparation')):
            label = _label(text, header)
            columns.addWidget(label, 0, column)
        table.addWidget(header)
        checkpoint = progress.get('current_checkpoint') or ''
        for row in rows:
            divider = QFrame(library); divider.setFixedHeight(1)
            divider.setStyleSheet('background:#e1e8e5; border:none;'); table.addWidget(divider)
            table.addWidget(self._component(row, library, checkpoint=checkpoint, detailed='shared_control' in progress))
        section_layout.addWidget(library); contents.addWidget(section)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(body)
        self._arrange_paths()
        self.continue_button.setText('Continue to reference regions' if source_stage else
            'Continue to autofluorescence' if checkpoint.startswith('target_af') else 'Review library')
        self.continue_button.setVisible(bool(source_stage or rows and accepted == len(rows)))
        ready = not source_stage or progress['sources_ready'] == progress['source_count']
        self.continue_button.setEnabled(ready)
        self.continue_button.setToolTip('' if ready else 'Choose a reviewed image for each required source.')


class AcceptedSharedControl(QScrollArea):
    """Inspect exact retained control evidence without a response-capable form."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('accepted-shared-control')
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def prepare(self, row):
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.addWidget(_label(row['label'], body, style='font-weight:600;'))
        self.status = _label('Loading the accepted AF preparation…', body)
        layout.addWidget(self.status)
        layout.addStretch(1)
        old = self.takeWidget()
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        if hasattr(self, 'spectrum'):
            del self.spectrum
        self.setWidget(body)

    def show_preparation(self, review):
        from .af_control_review import AfControlSpectrumPanel
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(14)
        shared = review.get('source_role') != 'negative_control'
        layout.addWidget(_label('Accepted shared AF preparation' if shared else
            'Accepted AF preparation · ' + review.get('source_link_id', ''), body, style='font-weight:600;'))
        self.spectrum = AfControlSpectrumPanel(review, body, source=True)
        layout.addWidget(self.spectrum)
        layout.addStretch(1)
        old = self.takeWidget()
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        self.setWidget(body)


class AcceptedReferenceSpectrum(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('accepted-reference-spectrum')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 16)
        self.heading = QLabel(self)
        self.heading.setProperty('panelHeading', True)
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.heading)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.plot = None
        layout.addStretch(1)

    def prepare(self, component):
        self.heading.setText(component + ' · Accepted spectrum')
        self.status.setText('Loading the accepted spectrum…')
        if self.plot is not None:
            self.layout().removeWidget(self.plot)
            self.plot.deleteLater()
            self.plot = None

    def show_spectrum(self, review):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis, plot_spectral_profile, label_spectral_axis
        from .spectral_review import SpectralPlotToolbar
        self.status.setText('Accepted spectrum · ' + {'unit_l1': 'Sum normalized to 1', 'unit_l2': 'L2 norm normalized to 1'}[review['normalization_convention']])
        self.plot = QWidget(self)
        layout = QVBoxLayout(self.plot)
        layout.setContentsMargins(0, 0, 0, 0)
        figure = Figure(figsize=(7, 4), layout='constrained')
        figure.set_facecolor(self.palette().window().color().name())
        canvas = FigureCanvasQTAgg(figure)
        canvas.setMinimumHeight(300)
        axes = figure.add_subplot(111)
        axes.set_facecolor(self.palette().window().color().name())
        names = review['measurement_names']
        x, groups, unit = spectral_axis(names, wavelengths=review.get('measurement_wavelengths'),
            wavelength_unit=review.get('wavelength_unit'), optical_settings=review.get('measurement_optical_settings'))
        plot_spectral_profile(axes, x, review['values'], groups, color='#087f80', linewidth=2)
        label_spectral_axis(axes, names, x, groups, unit)
        axes.set_ylabel('Normalized intensity')
        layout.addWidget(SpectralPlotToolbar(canvas, self.plot))
        layout.addWidget(canvas, 1)
        self.layout().insertWidget(self.layout().count() - 1, self.plot, 1)
        canvas.draw_idle()
