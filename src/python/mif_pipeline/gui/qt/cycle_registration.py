"""Select a repeated stain, reuse the native registration editor, review a crop."""
from __future__ import annotations
import json

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QStackedWidget, QSpinBox, QToolButton, QSizePolicy, QFrame, QStyle,
    QStyleOptionComboBox, QStylePainter)

from .jobs import ManagedJobBridge, JobAccess
from .registration_editor import RegistrationEditor
from .workbench import style_scientific_table
from .researcher_widgets import ContentSizedButton, configure_disclosure
from mif_pipeline.registration.cycle_session import CycleRegistrationSession, _write
from mif_pipeline.io.cycle_registration import validate_crop


def _image_footprint(grid, transform):
    """Draw original pixel-centre bounds in the saved reference physical frame."""
    a, b, tx, c, d, ty = transform.matrix
    width, height = (grid.width-1)*grid.x_um, (grid.height-1)*grid.y_um
    return tuple((a*x+b*y+tx, c*x+d*y+ty)
                 for x, y in ((0., 0.), (width, 0.), (width, height), (0., height)))


class _SourceFileLabel(QLabel):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._filename = path.name
        self.setAccessibleName(path.name)
        self.setToolTip(str(path))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setProperty('choiceHelp', True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self._filename, Qt.TextElideMode.ElideMiddle, self.width()))


class _CycleComboBox(QComboBox):
    """Keep long selected file/channel labels legible without widening the page."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentTextChanged.connect(self.setToolTip)

    def paintEvent(self, event):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text_area = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                               QStyle.SubControl.SC_ComboBoxEditField, self)
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideMiddle, max(0, text_area.width()-4))
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class CycleRegistrationDialog(QDialog):
    """One draft session; Accepted publishes only the reviewed registration value."""
    def __init__(self, collection, workspace, parent=None, *, native_operations=None):
        super().__init__(parent)
        self.setWindowTitle('Register imaging cycles')
        self.setObjectName('cycle-registration')
        self.resize(1240, 850)
        self.registration = None
        self.session = None
        self.editor = None
        self.native_session = None
        self._closing = False
        self._moving = None
        self._return_page = 0
        self._updating = False
        self._crop = None
        self._native_error = None
        self._editor_mode = None
        self._after_close = None
        self._crop_valid = False
        self._accepting = False
        self.collection = collection
        controller = getattr(parent.window(), 'controller', None) if parent is not None else None
        self.native_operations = native_operations or getattr(controller, 'file_operations', None)
        self.jobs = ManagedJobBridge(self)
        self.jobs.job_succeeded.connect(self._ready)
        self.jobs.job_failed.connect(self._failed)
        self._actions = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.pages = QStackedWidget(self)
        layout.addWidget(self.pages, 1)
        self._build_cycles()
        self._build_crop()
        self.message = QLabel('Checking original cycle images and any saved registration…', self)
        self.message.setWordWrap(True)
        self.message.setProperty('choiceHelp', True)
        layout.addWidget(self.message)
        footer = QHBoxLayout()
        self.footer_layout = footer
        self.cancel_button = ContentSizedButton('Cancel', self)
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        self.back_button = ContentSizedButton('Back to cycle alignments', self)
        self.back_button.setProperty('actionRole', 'navigation')
        self.back_button.clicked.connect(self._back_to_cycles)
        self.back_button.hide()
        footer.addWidget(self.back_button)
        footer.addStretch()
        self.continue_button = ContentSizedButton('Review common area', self)
        self.continue_button.setProperty('actionRole', 'primary')
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self._review_crop)
        footer.addWidget(self.continue_button)
        layout.addLayout(footer)
        self._submit('open', lambda: CycleRegistrationSession.open(collection, workspace))

    def _submit(self, action, operation, access=JobAccess.READ):
        job = self.jobs.submit('cycle_registration_'+action, operation, access=access)
        self._actions[job] = action
        return job

    def _build_cycles(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QLabel('Register cycles to one reference', page)
        heading.setProperty('dialogHeading', True)
        layout.addWidget(heading)
        intro = QLabel('Choose the same biological stain in every cycle. Then align each moving cycle '
            'to the reference by rotation and translation.', page)
        intro.setWordWrap(True)
        intro.setProperty('choiceHelp', True)
        layout.addWidget(intro)
        choices = QFrame(page)
        choices.setProperty('workbenchSection', True)
        form = QHBoxLayout(choices)
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(24)
        self.stain = QLineEdit(choices)
        self.stain.setPlaceholderText('For example, DAPI or Hoechst')
        self.stain.setAccessibleName('Shared biological stain')
        self.stain.setToolTip('Name the biological stain present in every cycle. A matching dye name alone '
                             'does not identify corresponding tissue structures.')
        self.stain.setMaxLength(200)
        self.reference = _CycleComboBox(choices)
        self.reference.setAccessibleName('Fixed reference cycle')
        self.reference.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.reference.setMinimumContentsLength(12)
        self.reference.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for i, member in enumerate(self.collection.members):
            self.reference.addItem(self._cycle_label(i), i)
            self.reference.setItemData(i, str(member.path), Qt.ItemDataRole.ToolTipRole)
        for title, editor, stretch in (('Shared stain', self.stain, 1), ('Fixed reference', self.reference, 2)):
            field = QVBoxLayout()
            field.setSpacing(6)
            label = QLabel(title, choices)
            label.setProperty('fieldCaption', True)
            label.setBuddy(editor)
            field.addWidget(label)
            field.addWidget(editor)
            form.addLayout(field, stretch)
        layout.addWidget(choices)
        context_row = QHBoxLayout()
        context = QLabel(f'{self.collection.name} · {len(self.collection.members)} cycles', page)
        context.setTextFormat(Qt.TextFormat.PlainText)
        context.setProperty('panelHeading', True)
        context.setWordWrap(True)
        context_row.addWidget(context, 1)
        self.review_progress = QLabel(page)
        self.review_progress.setProperty('choiceHelp', True)
        context_row.addWidget(self.review_progress, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addLayout(context_row)
        self.table = QTableWidget(len(self.collection.members), 5, page)
        self.table.setObjectName('registration-cycle-files')
        self.table.setAccessibleName('Cycle files, shared stain channels and alignment review')
        self.table.setHorizontalHeaderLabels(['Cycle image', 'Shared-stain channel', 'Pixel size', 'Alignment', ''])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        style_scientific_table(self.table)
        self.table.setStyleSheet('QTableWidget#registration-cycle-files::item { padding: 0; }')
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 124)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        layout.addWidget(self.table, 1)
        layout.addStretch()
        self.channel_selectors = []
        self.align_buttons = []
        self.review_labels = []
        for i, member in enumerate(self.collection.members):
            item = QTableWidgetItem('')
            item.setToolTip(str(member.path))
            self.table.setItem(i, 0, item)
            file_cell = QWidget(self.table)
            file_layout = QVBoxLayout(file_cell)
            file_layout.setContentsMargins(12, 6, 8, 6)
            file_layout.setSpacing(2)
            cycle_label = QLabel(self._cycle_label(i).split(' · ', 1)[0], file_cell)
            cycle_label.setProperty('fieldCaption', True)
            filename = _SourceFileLabel(member.path, file_cell)
            file_cell.setToolTip(str(member.path))
            file_layout.addWidget(cycle_label)
            file_layout.addWidget(filename)
            self.table.setCellWidget(i, 0, file_cell)
            selector = _CycleComboBox(self.table)
            selector.addItem('Select corresponding channel', -1)
            selector.setAccessibleName(f'{self._cycle_label(i)}: shared stain channel')
            selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            selector.setMinimumContentsLength(12)
            selector.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            selector.setStyleSheet(
                'QComboBox { background: transparent; border: 1px solid transparent; border-radius: 0; '
                'margin: 0; padding: 10px 12px; padding-right: 34px; } '
                'QComboBox:hover { background: #eaf3f0; border-color: #c5d8d2; } '
                'QComboBox:focus { border: 2px solid #006b70; background: #ffffff; } ')
            self.channel_selectors.append(selector)
            self.table.setCellWidget(i, 1, selector)
            status_cell = QWidget(self.table)
            status_layout = QHBoxLayout(status_cell)
            status_layout.setContentsMargins(10, 0, 6, 0)
            status = QLabel(status_cell)
            status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status_layout.addWidget(status, alignment=Qt.AlignmentFlag.AlignVCenter)
            status_layout.addStretch()
            self.review_labels.append(status)
            self.table.setCellWidget(i, 3, status_cell)
            button = ContentSizedButton('Align cycle', self.table)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, index=i: self._align(index))
            self.align_buttons.append(button)
            action_cell = QWidget(self.table)
            action_layout = QHBoxLayout(action_cell)
            action_layout.setContentsMargins(6, 6, 10, 6)
            action_layout.addWidget(button)
            self.table.setCellWidget(i, 4, action_cell)
            self.table.setRowHeight(i, 64)
        self.pages.addWidget(page)
        self.stain.textChanged.connect(self._choices_changed)
        self.reference.currentIndexChanged.connect(self._choices_changed)
        for selector in self.channel_selectors:
            selector.currentIndexChanged.connect(self._choices_changed)

    def _cycle_label(self, index):
        cycles = {c.imaging_cycle for c in self.collection.channels if c.member_index == index}
        cycle = next(iter(cycles)) + 1
        return f'Cycle {cycle} · {self.collection.members[index].path.name}'

    def _build_crop(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QLabel('Retain the common image area', page)
        heading.setProperty('dialogHeading', True)
        layout.addWidget(heading)
        text = QLabel('The retained rectangle is used for every output channel. Compare the aligned cycles before accepting the sample.', page)
        text.setWordWrap(True)
        text.setProperty('choiceHelp', True)
        layout.addWidget(text)
        selection = QHBoxLayout()
        selection.setSpacing(10)
        self.crop_reference = QLabel(page)
        self.crop_reference.setProperty('fieldCaption', True)
        selection.addWidget(self.crop_reference)
        inspect_label = QLabel('Compare with', page)
        inspect_label.setProperty('fieldCaption', True)
        selection.addWidget(inspect_label)
        self.inspect_cycle = _CycleComboBox(page)
        self.inspect_cycle.setAccessibleName('Aligned cycle to inspect in the common-area overlay')
        self.inspect_cycle.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.inspect_cycle.setMinimumContentsLength(10)
        self.inspect_cycle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.inspect_cycle.currentIndexChanged.connect(self._inspect_cycle_changed)
        inspect_label.setBuddy(self.inspect_cycle)
        selection.addWidget(self.inspect_cycle, 1)
        self.inspect_button = ContentSizedButton('Edit this alignment', page)
        self.inspect_button.clicked.connect(self._edit_crop_alignment)
        selection.addWidget(self.inspect_button)
        layout.addLayout(selection)
        self.crop_viewer = QWidget(page)
        self.crop_viewer.setObjectName('cycle-common-area-viewer')
        self.crop_viewer_layout = QVBoxLayout(self.crop_viewer)
        self.crop_viewer_layout.setContentsMargins(0, 0, 0, 0)
        self.crop_viewer_layout.setSpacing(0)
        self.crop_loading = QLabel('Opening the registered stain overlay…', self.crop_viewer)
        self.crop_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_loading.setWordWrap(True)
        self.crop_loading.setProperty('choiceHelp', True)
        self.crop_viewer_layout.addWidget(self.crop_loading, 1)
        self.crop_retry = ContentSizedButton('Reload overlay', self.crop_viewer)
        self.crop_retry.clicked.connect(self._open_crop_view)
        self.crop_retry.hide()
        self.crop_viewer_layout.addWidget(self.crop_retry, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.crop_viewer, 1)

        summary = QHBoxLayout()
        details = QVBoxLayout()
        details.setSpacing(3)
        self.crop_summary = QLabel(page)
        self.crop_summary.setWordWrap(True)
        self.crop_summary.setProperty('panelHeading', True)
        self.crop_summary.setToolTip('The same accepted transform applies to all channels in each cycle. '
            'Original pixels are sampled once with linear interpolation into floating-point output.')
        details.addWidget(self.crop_summary)
        self.crop_origin = QLabel(page)
        self.crop_origin.setWordWrap(True)
        self.crop_origin.setProperty('choiceHelp', True)
        details.addWidget(self.crop_origin)
        summary.addLayout(details, 1)
        self.crop_coordinates = QToolButton(page)
        self.crop_coordinates.setText('Crop coordinates')
        self.crop_coordinates.setCheckable(True)
        configure_disclosure(self.crop_coordinates)
        summary.addWidget(self.crop_coordinates, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(summary)
        self.crop_fields = QFrame(page)
        self.crop_fields.setProperty('workbenchSection', True)
        form = QGridLayout(self.crop_fields)
        form.setContentsMargins(12, 8, 12, 8)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        crop_caption = QLabel('Reference pixels', self.crop_fields)
        crop_caption.setProperty('fieldCaption', True)
        form.addWidget(crop_caption, 0, 0)
        self.crop_spins = []
        for index, title in enumerate(('Left', 'Top', 'Width', 'Height')):
            spin = QSpinBox(self.crop_fields)
            spin.setRange(0, 2_000_000_000)
            spin.setKeyboardTracking(False)
            spin.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            spin.setMinimumWidth(110)
            spin.setAccessibleName(f'Common area {title.lower()} in reference pixels')
            spin.setToolTip(('Horizontal position of the retained rectangle’s top-left pixel, counted from zero in the reference image.',
                             'Vertical position of the retained rectangle’s top-left pixel, counted from zero in the reference image.',
                             'Number of reference-image pixels retained across each output channel.',
                             'Number of reference-image pixels retained down each output channel.')[index])
            spin.valueChanged.connect(self._crop_changed)
            self.crop_spins.append(spin)
            label = QLabel(title, self.crop_fields)
            label.setProperty('fieldCaption', True)
            label.setBuddy(spin)
            form.addWidget(label, 0, index*2+1)
            form.addWidget(spin, 0, index*2+2)
            form.setColumnStretch(index*2+2, 1)
        self.crop_fields.hide()
        self.crop_coordinates.toggled.connect(self.crop_fields.setVisible)
        layout.addWidget(self.crop_fields)
        self.pages.addWidget(page)

    def _choices_changed(self, *_):
        if self._updating or self.session is None or self.editor is not None:
            return
        choices = dict(reference_member=self.reference.currentData(),
            registration_channels=[s.currentData() for s in self.channel_selectors], shared_stain=self.stain.text())
        try:
            _write(self.session.root / 'choices.json', choices)
            self.session.configure(choices['reference_member'], tuple(choices['registration_channels']), choices['shared_stain'])
            self.message.setText('Changing the stain, reference or channels resets the reviewed alignments.')
            valid = True
        except Exception as error:
            self.message.setText(str(error))
            valid = False
        self._refresh(valid)

    def _refresh(self, valid=True):
        reviewed_count = 0
        for i, button in enumerate(self.align_buttons):
            reference = i == self.reference.currentData()
            reviewed = valid and self.session.reviewed[i]
            reviewed_count += bool(reviewed and not reference)
            status = self.review_labels[i]
            status.setText('Fixed' if reference else 'Reviewed' if reviewed else 'Needs review')
            colour, background = (('#486169', '#edf2f1') if reference else
                                  ('#006b70', '#e3f1ed') if reviewed else ('#805d1d', '#f7efdc'))
            status.setStyleSheet(f'QLabel {{ color: {colour}; background: {background}; '
                                 'border-radius: 4px; padding: 4px 8px; font-weight: 500; }')
            status.setToolTip('This cycle defines the output coordinate grid.' if reference else
                              'This alignment has been reviewed and used.' if reviewed else
                              'Align this cycle and use its reviewed position before continuing.')
            button.setText('Review alignment' if reviewed else 'Align cycle')
            button.setVisible(not reference)
            button.setEnabled(valid and not reference and self.editor is None)
            self.table.setRowHeight(i, max(64, button.minimumHeight()+12))
        self.table.setColumnWidth(4, max(max(button.minimumWidth(), button.sizeHint().width())
                                        for button in self.align_buttons)+24)
        table_height = self.table.horizontalHeader().sizeHint().height() + sum(
            self.table.rowHeight(i) for i in range(self.table.rowCount())) + 8
        self.table.setMinimumHeight(min(180, table_height))
        self.table.setMaximumHeight(table_height)
        moving_count = len(self.align_buttons)-1
        self.review_progress.setText(f'{reviewed_count} of {moving_count} moving '
                                     f'{"cycle" if moving_count == 1 else "cycles"} reviewed')
        self.reference.setToolTip(self.reference.currentText())
        self.continue_button.setEnabled(valid and all(self.session.reviewed) and self.editor is None)

    def _align(self, member_index, *, crop=False):
        if self.session is None or member_index is None:
            return
        if self.editor is not None or 'prepare' in self._actions.values():
            if self._editor_mode == 'crop':
                self._close_editor_then(lambda: self._align(member_index, crop=crop))
            return
        if self.native_operations is None or not callable(getattr(self.native_operations, 'run_registration_editor', None)):
            self.message.setText('The native image service is unavailable. Open this session from the application Samples page.')
            self.message.show()
            return
        self._moving = member_index
        self._native_error = None
        self._editor_mode = 'alignment'
        self._return_page = 1 if crop else 0
        self._crop = tuple(s.value() for s in self.crop_spins) if crop else None
        self.continue_button.setEnabled(False)
        self.pages.setEnabled(False)
        self.message.setText('Opening the original reference and moving cycle…')
        self.message.show()
        self._submit('prepare', lambda: self.session.create_editor(member_index))

    def _edit_crop_alignment(self):
        self._align(self.inspect_cycle.currentData(), crop=True)

    def _close_editor_then(self, operation):
        """Serialize native ownership; retain only the latest navigation intent."""
        self._after_close = operation
        self.continue_button.setEnabled(False)
        if self.editor is not None:
            self.editor.request_close()
        elif 'prepare' not in self._actions.values():
            self._run_after_close()

    def _run_after_close(self):
        operation, self._after_close = self._after_close, None
        if operation is not None and not self._closing:
            operation()

    def _inspect_cycle_changed(self, *_):
        if self._updating or self.session is None or self._closing:
            return
        self.inspect_cycle.setToolTip(self.inspect_cycle.currentText())
        if self.editor is not None or 'prepare' in self._actions.values():
            self._close_editor_then(self._open_crop_view)
        else:
            self._open_crop_view()

    def _open_crop_view(self):
        if self._closing or self._accepting or self.pages.currentIndex() != 1:
            return
        member_index = self.inspect_cycle.currentData()
        if member_index is None or self.editor is not None:
            return
        if self.native_operations is None or not callable(getattr(self.native_operations, 'run_registration_editor', None)):
            self.crop_loading.setText('The native image service is unavailable. Open this session from the application Samples page.')
            self.crop_loading.show()
            self._crop_changed()
            return
        self._moving = member_index
        self._editor_mode = 'crop'
        self._native_error = None
        self.crop_retry.hide()
        self.crop_loading.setText('Opening the registered stain overlay…')
        self.crop_loading.show()
        self.continue_button.setEnabled(False)
        self._submit('prepare', lambda: self.session.create_editor(member_index))

    def _ready(self, job, value):
        action = self._actions.pop(job, None)
        if action == 'open':
            self.session = value
            self._updating = True
            choices = dict(reference_member=value.reference_member, registration_channels=value.registration_channels,
                           shared_stain=value.shared_stain)
            saved = value.root / 'choices.json'
            if saved.is_file() and saved.stat().st_size < 16384:
                choices.update(json.loads(saved.read_text()))
            self.reference.setCurrentIndex(self.reference.findData(choices['reference_member']))
            self.stain.setText(choices['shared_stain'])
            for i, selector in enumerate(self.channel_selectors):
                for c, name in enumerate(value.channel_names[i]): selector.addItem(f'{c+1} · {name}', c)
                selector.setCurrentIndex(selector.findData(choices['registration_channels'][i]))
                grid = value.grids[i]
                pixel_text = f'{grid.x_um:g} × {grid.y_um:g} µm'
                pixel_item = QTableWidgetItem(pixel_text)
                pixel_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                pixel_item.setSizeHint(QSize(self.table.fontMetrics().horizontalAdvance(pixel_text)+24, 64))
                self.table.setItem(i, 2, pixel_item)
            self._updating = False
            self._choices_changed()
        elif action == 'prepare':
            self.native_session = value
            if self._closing or self._after_close is not None:
                value.worker_finished()
                self.native_session = None
                if self._closing:
                    self._finish_reject()
                else:
                    self._run_after_close()
                return
            preview = self._editor_mode == 'crop'
            self.editor = RegistrationEditor(value, self, global_only=True, read_only=preview)
            self.editor.cycle_names = {role: self._cycle_label(index).split(' · ', 1)[0] + ' · ' + self.session.shared_stain
                for role, index in (('fixed', self.session.reference_member), ('moving', self._moving))}
            if preview:
                self.editor.set_review_crop(tuple(s.value() for s in self.crop_spins), valid=self._crop_valid)
            else:
                self.editor.review_crop = self._crop
            self.editor.close_requested.connect(value.close)
            self.editor.finished.connect(self._editor_finished)
            if preview:
                self.crop_loading.hide()
                self.crop_viewer_layout.addWidget(self.editor, 1)
                self._crop_changed()
            else:
                self.pages.addWidget(self.editor)
                self.pages.setCurrentWidget(self.editor)
                self.footer_layout.removeWidget(self.cancel_button)
                self.editor.cycle_result.layout().insertWidget(0, self.cancel_button)
                self.back_button.hide()
                self.continue_button.hide()
                self.message.hide()
            self.pages.setEnabled(True)
            self._submit('native', lambda: self.native_operations.run_registration_editor(value), JobAccess.RUN)
        elif action == 'native':
            self.native_session.worker_finished()
            self.editor.worker_finished()
        if self._closing and not self.jobs.has_active_jobs and self.editor is None:
            self._finish_reject()

    def _failed(self, job, error):
        action = self._actions.pop(job, None)
        self.message.setText(error.message)
        self.message.show()
        self.pages.setEnabled(True)
        if action == 'native':
            self._native_error = error.message
            self.native_session.worker_finished(error.message)
            self.editor.worker_finished(error.message)
        elif action == 'prepare' and self.pages.currentIndex() == 1:
            self._native_error = error.message
            self.crop_loading.setText('The registered stain overlay could not be opened.')
            self.crop_loading.show()
            self.crop_retry.show()
            self._crop_changed()
            self.message.setText(error.message)
            self.message.show()
        elif self.session is not None:
            self._refresh()
        if self.editor is None and self._after_close is not None:
            self._run_after_close()
        if self._closing and not self.jobs.has_active_jobs and self.editor is None:
            self._finish_reject()

    def _editor_finished(self):
        editor, self.editor = self.editor, None
        preview = self._editor_mode == 'crop'
        if preview:
            self.crop_viewer_layout.removeWidget(editor)
        else:
            editor.cycle_result.layout().removeWidget(self.cancel_button)
            self.footer_layout.insertWidget(0, self.cancel_button)
            self.pages.removeWidget(editor)
        editor.deleteLater()
        self.native_session = None
        self._editor_mode = None
        self.message.show()
        if self._closing:
            self._finish_reject()
            return
        if self._after_close is not None:
            self._run_after_close()
            return
        if preview:
            self.crop_loading.setText('The registered stain overlay has closed.')
            self.crop_loading.show()
            self.crop_retry.show()
            self._crop_changed()
            if self._native_error:
                self.message.setText(self._native_error)
                self.message.show()
            return
        self.pages.setCurrentIndex(self._return_page)
        self.continue_button.show()
        if self._return_page == 1:
            # Recalculate after a changed fit; an unchanged crop stays selected.
            self.continue_button.setText('Review common area')
            self._review_crop()
        else:
            self._back_to_cycles()
        if self._native_error:
            self.message.setText(self._native_error)
            self.message.show()

    def _back_to_cycles(self):
        if self.editor is not None or 'prepare' in self._actions.values():
            self._close_editor_then(self._back_to_cycles)
            return
        self._crop = None
        self.pages.setCurrentIndex(0)
        self.back_button.hide()
        self.continue_button.setText('Review common area')
        self._refresh()
        self.message.setText('Applied positions and reviewed cycles are saved in this registration draft.')
        self.message.show()

    def _review_crop(self):
        if self.pages.currentIndex() == 1 and self.continue_button.text() == 'Accept registered sample':
            try:
                self.session.accept(tuple(s.value() for s in self.crop_spins))
            except Exception as error:
                self.message.setText(str(error))
                self.message.show()
                return
            self._accepting = True
            self.pages.setEnabled(False)
            self.back_button.setEnabled(False)
            self._close_editor_then(self._accept_after_close)
            return
        try:
            registration = self.session.accept()
        except Exception as error:
            self._back_to_cycles()
            self.message.setText(str(error))
            return
        self._updating = True
        selected_crop = registration.crop
        reference = self.session.reference_member
        self.crop_reference.setText('Reference: ' + self._cycle_label(reference).split(' · ', 1)[0])
        self.crop_reference.setToolTip(str(self.collection.members[reference].path))
        if self._crop is not None:
            try:
                validate_crop(self.session.grids, self.session.transforms, self.session.reference_member, self._crop)
                selected_crop = self._crop
            except ValueError:
                pass
        for spin, value in zip(self.crop_spins, selected_crop): spin.setValue(value)
        selected_cycle = self.inspect_cycle.currentData()
        self.inspect_cycle.clear()
        for i in range(len(self.collection.members)):
            if i != self.session.reference_member:
                self.inspect_cycle.addItem(self._cycle_label(i), i)
                self.inspect_cycle.setItemData(self.inspect_cycle.count()-1,
                    str(self.collection.members[i].path), Qt.ItemDataRole.ToolTipRole)
        if selected_cycle is not None:
            self.inspect_cycle.setCurrentIndex(max(0, self.inspect_cycle.findData(selected_cycle)))
        self._updating = False
        self.pages.setCurrentIndex(1)
        self.back_button.show()
        self.continue_button.setText('Accept registered sample')
        self._crop_changed()
        self._inspect_cycle_changed()

    def _accept_after_close(self):
        try:
            # Recheck source identities and the chosen geometry after all native
            # requests have drained. Only this explicit action publishes a value.
            self.registration = self.session.accept(tuple(s.value() for s in self.crop_spins))
        except Exception as error:
            self._accepting = False
            self.pages.setEnabled(True)
            self.back_button.setEnabled(True)
            self._crop_changed()
            self.message.setText(str(error))
            self.message.show()
            return
        super().accept()

    def _crop_changed(self, *_):
        if self._updating or self.session is None: return
        crop = tuple(s.value() for s in self.crop_spins)
        try:
            validate_crop(self.session.grids, self.session.transforms, self.session.reference_member, crop)
            reference = self.session.grids[self.session.reference_member]
            fraction = 100*crop[2]*crop[3]/(reference.width*reference.height)
            self.crop_summary.setText(f'Retains {fraction:.1f}% of the reference image '
                f'({crop[2]:,} × {crop[3]:,} pixels)')
            self.crop_origin.setText('All cycles cover the retained rectangle.')
            self.crop_origin.setToolTip('The retained rectangle is covered by every image. '
                'This is an image-bounds check, not a measurement of tissue coverage.')
            if self._native_error:
                self.message.setText(self._native_error)
                self.message.show()
            else:
                self.message.hide()
            if self.session.crop != crop:
                self.session.crop = crop
                self.session.save()
            valid = True
        except Exception as error:
            self.message.setText(str(error))
            self.message.show()
            self.crop_summary.setText('The retained rectangle needs adjustment')
            self.crop_origin.setText('Keep the rectangle inside every aligned image footprint.')
            valid = False
        self._crop_valid = valid
        self.inspect_button.setEnabled(not self._closing and not self._accepting)
        if self.editor is not None and self._editor_mode == 'crop':
            self.editor.set_review_crop(crop, valid=valid)
        self.continue_button.setEnabled(valid and all(self.session.reviewed) and not self._closing
            and not self._accepting and self._after_close is None and 'prepare' not in self._actions.values())

    def reject(self):
        self._closing = True
        self._accepting = False
        self._after_close = None
        self.continue_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        if self.editor is not None:
            self.editor.request_close()
        elif not self.jobs.has_active_jobs:
            self._finish_reject()
        else:
            self.message.setText('Finishing the current source check…')

    def _finish_reject(self):
        if self.jobs.has_active_jobs or self.editor is not None:
            return
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
