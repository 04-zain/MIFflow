"""Image-led review of accepted segmentation using the shared field renderer."""
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QRectF, QEvent, QTimer
from PySide6.QtGui import QImage, QPixmap, QTransform
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox, QScrollArea, QPushButton, QMenu, QFrame, QSizePolicy

from .cell_image_field import CellImageFieldReview
from .researcher_widgets import ContentSizedButton
from .workbench import PageHeading
from .segmentation_coverage_review import NuclearCoverageReview, coverage_rgba
from .segmentation_relationship_review import StainRelationshipReview
from .cell_correction_queue import CellCorrectionQueue


class SegmentationStageReview(QWidget):
    back_requested = Signal()
    continue_requested = Signal()
    settings_requested = Signal()
    distribution_requested = Signal()
    field_requested = Signal()
    correction_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.revision = 0
        self._bundle = None
        self._options = {}
        self._viewport = {}
        self._spatial = []
        self._field_index = 0
        self._missing_index = 0
        self._missing_count = None
        self._browse_preset = True
        self._browse_request_size = None
        self._browse_resize = QTimer(self)
        self._browse_resize.setSingleShot(True)
        self._browse_resize.setInterval(180)
        self._browse_resize.timeout.connect(self._refresh_browse_width)
        self._fit_next = True
        self._fit_anchor = None
        self._updating = False
        self.image_appearance_path = None
        self._coverage_request = None
        self._coverage_target = None
        self._coverage_loading = False
        self._coverage_overlay = None
        self._coverage_fit_timer = QTimer(self)
        self._coverage_fit_timer.setSingleShot(True)
        self._coverage_fit_timer.timeout.connect(self._fit_coverage_selection)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        back = ContentSizedButton('← Cell segmentation', self)
        self.back = back
        back.clicked.connect(self.back_requested)
        outer.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        body = QVBoxLayout(content)
        body.setContentsMargins(12, 10, 12, 14)
        body.setSpacing(10)
        heading = PageHeading('segmentation-review', 'Review cell and nucleus outlines', '', content)
        self._heading = heading.findChild(QLabel, 'segmentation-review-title')
        self._heading.setProperty('stagePageHeading', True)
        self._heading.setWordWrap(True)
        body.addWidget(heading)
        choices = QHBoxLayout()
        choices.setSpacing(10)
        self.image_label = QLabel('Image', content)
        self.image_label.setProperty('stageFieldCaption', True)
        choices.addWidget(self.image_label)
        self.images = QComboBox(content)
        self.images.setAccessibleName('Segmented image')
        self.images.setMinimumContentsLength(12)
        self.images.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.previous_image=ContentSizedButton('‹',content)
        self.previous_image.setAccessibleName('Previous segmented image')
        self.previous_image.clicked.connect(lambda:self.images.setCurrentIndex(max(0,self.images.currentIndex()-1)))
        choices.addWidget(self.previous_image)
        choices.addWidget(self.images, 1)
        self.next_image=ContentSizedButton('›',content)
        self.next_image.setAccessibleName('Next segmented image')
        self.next_image.clicked.connect(lambda:self.images.setCurrentIndex(min(self.images.count()-1,self.images.currentIndex()+1)))
        choices.addWidget(self.next_image)
        body.addLayout(choices)
        self.status = QLabel(content)
        self.status.setWordWrap(True)
        self.status.setProperty('fieldHelp', True)
        body.addWidget(self.status)

        self.outlines = CellImageFieldReview(content)
        self.outlines.view.setAccessibleName('Staining and cell outlines')
        self.outlines.view.setAccessibleDescription('Drag to pan; Control and scroll zooms. Toggle cell and nuclear outlines to inspect the staining.')
        self.outlines.view.setMinimumWidth(0)
        self.outlines.setMinimumWidth(0)
        for control in (self.outlines.cell, self.outlines.channel, self.outlines.appearance,
                        self.outlines.cells, self.outlines.nuclei, *self.outlines.findChildren(QPushButton)):
            control.hide()
        self.outlines.layout().setSpacing(0)
        self.outlines.field_requested.connect(self._pan)
        self.outlines.display_changed.connect(self._display)
        self.outlines.load_requested.connect(self._load_channels)
        self.outlines.field_cell_selected.connect(self._cell)

        # The two toolbar rows keep location navigation separate from display controls.
        toolbar = QFrame(content)
        toolbar.setProperty("stageInfoPanel", True)
        toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        toolbar_body = QVBoxLayout(toolbar)
        toolbar_body.setContentsMargins(12, 8, 12, 8)
        toolbar_body.setSpacing(8)
        navigation = QHBoxLayout()
        navigation.setSpacing(16)
        location_choices = QWidget(toolbar)
        location_layout = QHBoxLayout(location_choices)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.setSpacing(8)
        self.inspection_label = QLabel('Inspect', location_choices)
        self.inspection_label.setProperty('stageFieldCaption', True)
        self.task = QComboBox(location_choices)
        self.task.setAccessibleName('Segmentation inspection task')
        for text, key in (('Browse fields', 'overview'),
                          ('Missing or incomplete nuclei', 'nuclei'),
                          ('Missing or incomplete cells', 'cells'),
                          ('Possible merged outlines', 'merged'),
                          ('Possible split or fragmented outlines', 'fragmented')):
            self.task.addItem(text, key)
        self.task.setMinimumContentsLength(18)
        self.task.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.task.currentIndexChanged.connect(self._task_changed)
        self.inspection_label.setBuddy(self.task)
        location_layout.addWidget(self.inspection_label)
        location_layout.addWidget(self.task, 1)
        location_choices.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        navigation.addWidget(location_choices, 1, Qt.AlignmentFlag.AlignLeft)
        self.survey = ContentSizedButton('Survey tissue', toolbar)
        self.survey.setToolTip('Compare staining coverage in fields distributed across the tissue. Each plotted point opens one field.')
        self.survey.clicked.connect(lambda: self._calculate_coverage(self.coverage.options(), survey=True))
        self.survey.hide()
        navigation.addWidget(self.survey, 0, Qt.AlignmentFlag.AlignRight)
        paging = QWidget(toolbar)
        paging_layout = QHBoxLayout(paging)
        paging_layout.setContentsMargins(0, 0, 0, 0)
        paging_layout.setSpacing(8)
        self.previous = ContentSizedButton('Previous field', paging)
        self.next = ContentSizedButton('Next field', paging)
        self.previous.clicked.connect(lambda: self._visit(-1))
        self.next.clicked.connect(lambda: self._visit(1))
        self.field_label = QLabel(paging)
        self.field_label.setAccessibleName('Inspection position')
        self.field_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.field_label.setWordWrap(True)
        self.field_label.setMinimumWidth(self.field_label.fontMetrics().horizontalAdvance('Cell 999 of 999')+4)
        self.field_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        paging_layout.addWidget(self.previous)
        paging_layout.addWidget(self.field_label)
        paging_layout.addWidget(self.next)
        paging.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        navigation.addWidget(paging, 0, Qt.AlignmentFlag.AlignRight)
        toolbar_body.addLayout(navigation)
        self.task_help = QLabel(toolbar)
        self.task_help.setWordWrap(True)
        self.task_help.setProperty('fieldHelp', True)
        self.task_help.hide()
        toolbar_body.addWidget(self.task_help)
        self._navigation = navigation
        self._location_choices = location_choices
        self._paging = paging
        self._toolbar = toolbar

        self.outlines.appearance.clicked.disconnect()
        self.outlines.appearance.clicked.connect(lambda: self.outlines.open_appearance(
            available_channels=self._bundle['metadata']['available_channels']) if self._bundle else None)
        body.addWidget(toolbar)
        self._field_host = QWidget(content)
        self._field_layout = QGridLayout(self._field_host)
        self._wide_inspection = None
        self._field_layout.setContentsMargins(0, 0, 0, 0)
        self._field_layout.setSpacing(12)
        self.coverage = NuclearCoverageReview(self._field_host)
        self.coverage.hide()
        self.coverage.method.currentIndexChanged.connect(self._inspection_changed)
        self.coverage.requested.connect(self._calculate_coverage)
        self.coverage.changed.connect(self._coverage_edited)
        self.coverage.field_selected.connect(self._select_coverage_field)
        self.coverage.overlay_changed.connect(self._show_coverage_overlay)
        self.coverage.result_changed.connect(self._coverage_exports)
        self.relationships = StainRelationshipReview(self.outlines, self._field_host)
        self.relationships.hide()
        self.cell_label = QLabel(content)
        self.cell_label.setObjectName('segmentation-cell-details')
        self.cell_label.setProperty('fieldHelp', True)
        self.cell_label.setWordWrap(True)
        self.cell_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cell_label.hide()
        self._display_controls = QWidget(self._field_host)
        display = QHBoxLayout(self._display_controls)
        display.setContentsMargins(0, 0, 0, 0)
        display.setSpacing(12)
        display.addWidget(self.cell_label, 1)
        display.addStretch()
        for control in (self.outlines.cells, self.outlines.nuclei):
            display.addWidget(control)
            control.show()
        self.correct_cell = QPushButton('+  Add correction', content)
        self.correct_cell.setAccessibleName('Add correction to the collection')
        self.correct_cell.setEnabled(False)
        self.correct_cell.setToolTip('Select a cell in the image, then choose the correction to add.')
        correction_menu = QMenu(self.correct_cell)
        self._correction_actions = {}
        for operation, label in (('cell_outline', 'Reshape cell outline…'),
                                 ('nucleus_outline', 'Reshape nuclear outline…'),
                                 ('split', 'Split selected outline…'), ('merge', 'Add selected cell to merge'),
                                 ('add_nucleus', 'Draw missing nuclear outline…'),
                                 ('add_cell', 'Draw a new cell…'), ('delete', 'Delete cell…')):
            self._correction_actions[operation] = correction_menu.addAction(label,
                lambda _checked=False, selected=operation: self._request_correction(selected))
        self.correct_cell.setMenu(correction_menu)
        self.corrections = CellCorrectionQueue(content, add_button=self.correct_cell)
        self.corrections.image_requested.connect(self._select_correction_image)
        self.corrections.changed.connect(lambda: QTimer.singleShot(0, self._fit_canvas_height))
        self.outlines.layout().setStretch(self.outlines.layout().indexOf(self.outlines.view), 1)
        self._arrange_field(True)
        body.addWidget(self._field_host)
        body.addWidget(self.corrections)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        settings_group = QWidget(self)
        settings_actions = QHBoxLayout(settings_group)
        settings_actions.setContentsMargins(0, 0, 0, 0)
        settings_actions.setSpacing(8)
        change = ContentSizedButton('Change segmentation settings…', self)
        change.clicked.connect(self.settings_requested)
        settings_actions.addWidget(change)
        settings_actions.addWidget(self.outlines.appearance)
        self.outlines.appearance.show()
        actions.addWidget(settings_group, 0, Qt.AlignmentFlag.AlignLeft)
        actions.addStretch()
        finish_group = QWidget(self)
        finish_actions = QHBoxLayout(finish_group)
        finish_actions.setContentsMargins(0, 0, 0, 0)
        finish_actions.setSpacing(8)
        export = QPushButton('Export…', self)
        export.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        menu = QMenu(export)
        self.export_field = menu.addAction('Tissue view…', lambda: self._export(False))
        self.export_data = menu.addAction('Tissue field data…', lambda: self._export(True))
        menu.addSeparator()
        self.export_coverage_plot = menu.addAction('Nuclear coverage plot…', self.coverage.export_plot)
        self.export_coverage_data = menu.addAction('Nuclear coverage table…', self.coverage.export_data)
        self._coverage_exports(False)
        self.export_relationship_plot = menu.addAction('Selected stain/outline overlap…', self.relationships.export_plot)
        self.export_relationship_data = menu.addAction('Field stain/outline overlaps…', self.relationships.export_data)
        self.relationships.result_changed.connect(self._relationship_exports)
        self._relationship_exports(False)
        export.setMenu(menu)
        finish_actions.addWidget(export)
        proceed = ContentSizedButton('Continue to measurements', self)
        self.proceed = proceed
        self._continue_destination = 'Continue to measurements'
        self.corrections.changed.connect(self.set_continue_destination)
        proceed.clicked.connect(self._proceed)
        proceed.setProperty('actionRole', 'primary')
        finish_actions.addWidget(proceed)
        actions.addWidget(finish_group, 0, Qt.AlignmentFlag.AlignRight)
        action_host = QWidget(self)
        action_host.setLayout(actions)
        action_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(action_host)
        self._actions = actions
        self._action_groups = (settings_group, finish_group)
        self.images.currentIndexChanged.connect(self._image_changed)
        self._scroll=scroll;self._body=body
        scroll.viewport().installEventFilter(self)
        self.outlines.view.height_changed.connect(lambda _height: self._browse_resize.start())
        self.outlines.view.height_changed.connect(self._schedule_coverage_fit)
        self._enabled(False)

    def _arrange_field(self, wide):
        if self._wide_inspection == wide:
            return
        self._wide_inspection = wide
        grid = self._field_layout
        for widget in (self.coverage, self.relationships, self.outlines, self._display_controls):
            grid.removeWidget(widget)
        image_row, image_column = (0, 1) if wide else (1, 0)
        for panel in (self.coverage, self.relationships):
            grid.addWidget(panel, 0, 0)
        grid.addWidget(self.outlines, image_row, image_column)
        grid.addWidget(self._display_controls, image_row + 1, image_column)
        grid.setColumnStretch(0, 0 if wide else 1)
        grid.setColumnStretch(1, 1 if wide else 0)

    def eventFilter(self,watched,event):
        if watched is self._scroll.viewport() and event.type()==QEvent.Type.Resize:self._fit_canvas_height()
        return super().eventFilter(watched,event)

    def _fit_canvas_height(self):
        """Keep the image prominent and wrap navigation groups at their natural width."""
        margins=self._body.contentsMargins()
        width=max(1,self._scroll.viewport().width()-margins.left()-margins.right())
        action_width=sum(group.sizeHint().width() for group in self._action_groups)+self._actions.spacing()*2
        horizontal=self.width()>=action_width
        self._actions.setDirection(QHBoxLayout.Direction.LeftToRight if horizontal else QHBoxLayout.Direction.TopToBottom)
        self._actions.setStretch(1, 1 if horizontal else 0)
        toolbar_margins=self._toolbar.layout().contentsMargins()
        available=width-toolbar_margins.left()-toolbar_margins.right()
        required=self._location_choices.sizeHint().width()+self._paging.sizeHint().width()+self._navigation.spacing()
        direction=(QHBoxLayout.Direction.LeftToRight if available>=required
                   else QHBoxLayout.Direction.TopToBottom)
        if self._navigation.direction()!=direction:
            self._navigation.setDirection(direction)
            self._navigation.setStretch(0, 1 if direction==QHBoxLayout.Direction.LeftToRight else 0)
        side_by_side = width >= 1000
        self._arrange_field(side_by_side)
        self.coverage.setMaximumWidth(350 if side_by_side else 16_777_215)
        self.relationships.setMaximumWidth(350 if side_by_side else 16_777_215)
        others=[self._body.itemAt(i) for i in range(self._body.count())
                if self._body.itemAt(i).widget() is not self._field_host and not self._body.itemAt(i).isEmpty()]
        used=sum(item.heightForWidth(width) if item.hasHeightForWidth() else item.sizeHint().height() for item in others)
        used+=margins.top()+margins.bottom()+self._body.spacing()*len(others)
        used+=self.outlines.layout().spacing()*(self.outlines.layout().count()-1)
        used += self._display_controls.sizeHint().height() + self._field_layout.verticalSpacing()
        inspector = next((panel for panel in (self.coverage,self.relationships) if not panel.isHidden()),self.coverage)
        if not inspector.isHidden() and not side_by_side:
            used+=inspector.heightForWidth(width) if inspector.hasHeightForWidth() else inspector.sizeHint().height()
            used+=self._field_layout.spacing()
        maximum = max(380, min(760, self._scroll.viewport().height() - used))
        image_view = self.outlines.view
        minimum = 380
        if side_by_side and not inspector.isHidden() and self._fit_anchor and self._fit_anchor.width() > 0:
            field_width = max(1, width - inspector.sizeHint().boundedTo(inspector.maximumSize()).width() - self._field_layout.spacing())
            field_height = round(field_width * self._fit_anchor.height() / self._fit_anchor.width())
            minimum = max(380, min(maximum, field_height))
        # In the two-column layout both widgets fill the same grid row. A tall
        # inspector can grow the row beyond the viewport's preferred height.
        image_view.setMaximumHeight(16_777_215 if side_by_side and not inspector.isHidden() else maximum)
        image_view.setMinimumHeight(minimum)

    def selection(self):
        stage=self.images.currentData()
        return (stage.artifact_ref_sha256,None) if stage is not None else None

    def set_continue_destination(self, text=None):
        if text is not None:self._continue_destination=text
        queue=self.corrections
        if queue.pending_count():
            self.proceed.setText('Review changes…' if queue.drafts_ready() else 'Edit outlines…')
            self.proceed.setEnabled(queue.batch_ready())
        else:
            self.proceed.setText(self._continue_destination)
            self.proceed.setEnabled(True)

    def _proceed(self):
        if self.corrections.pending_count():self.corrections.request_primary()
        else:self.continue_requested.emit()

    def set_status(self,text):
        self.status.setText(text);self.status.setVisible(bool(text))
        if self._coverage_loading and text != 'Calculating staining coverage…':
            self._coverage_loading=False
            self.coverage.set_busy(False)
            if text:
                self._coverage_request=None;self._coverage_target=None
                self.coverage.clear_result()
                self._clear_coverage_overlay()
                self.coverage.show_error(text)
            self._enabled(self._bundle is not None)

    def set_results(self,results,*,selected_artifact=None):
        previous=(selected_artifact,None) if selected_artifact is not None else self.selection()
        # This inventory contains only currently selected committed results.
        # Figure availability is independent of the saved segmentation itself.
        stages=[s for s in results.stages if s.stage_kind=='segmentation']
        with QSignalBlocker(self.images):
            self.images.clear()
            for stage in stages:self.images.addItem(', '.join(stage.sample_ids),stage)
            if previous:
                index=next((i for i,s in enumerate(stages) if s.artifact_ref_sha256==previous[0]),0)
                self.images.setCurrentIndex(index)
        self.images.setVisible(len(stages)>1);self.image_label.setVisible(len(stages)>1)
        self.previous_image.setVisible(len(stages)>1);self.next_image.setVisible(len(stages)>1)
        self._image_changed()

    def _select_correction_image(self,artifact):
        index=next((i for i in range(self.images.count())
            if self.images.itemData(i).artifact_ref_sha256==artifact),-1)
        if index>=0:self.images.setCurrentIndex(index)

    def _image_changed(self):
        self.previous_image.setEnabled(self.images.currentIndex()>0)
        self.next_image.setEnabled(0<=self.images.currentIndex()<self.images.count()-1)
        self.revision+=1;self._bundle=None;self._options={};self._viewport={};self._spatial=[];self._field_index=0;self._fit_next=True
        self._fit_anchor=None
        self._browse_preset = True
        self._browse_request_size = None
        self._missing_index=0;self._missing_count=None
        self._clear_coverage_overlay()
        self._coverage_request=None;self._coverage_target=None;self._coverage_loading=False
        self.coverage.reset();self.coverage.hide()
        self.relationships.invalidate_field();self.relationships.set_active(None)
        with QSignalBlocker(self.task):self.task.setCurrentIndex(0)
        with QSignalBlocker(self.coverage.method):self.coverage.method.setCurrentIndex(0)
        self.task_help.hide()
        self._updating=True
        try:
            self.outlines.clear()
        finally:self._updating=False
        self.cell_label.hide();self.correct_cell.setEnabled(False);self.field_label.clear();self._enabled(False)
        if self.selection():
            self.set_status('Loading saved cell outlines…');self.field_requested.emit()
        else:self.set_status('No completed cell outlines are available yet.')

    def field_options(self,appearance_path):
        """Request one field with the current valid coverage parameters when active."""
        self.image_appearance_path=Path(appearance_path)
        stage=self.images.currentData()
        if stage is None:return None
        mode=self._inspection_mode()
        options={**self._options,**self._viewport,
            'inspection_mode':'across_tissue' if self._coverage_active() else mode,
            'inspection_index':self._missing_index,
            'viewport_size':self._viewport_size()}
        if self._browse_preset and self.task.currentData() == 'overview':
            options['browse_field_index'] = self._field_index
            self._browse_request_size = options['viewport_size']
        options.pop('nuclear_coverage',None)
        if self._coverage_active() and self._coverage_request is not None:
            request=dict(self._coverage_request)
            if self._coverage_target is not None:
                request['field_index']=self._coverage_target
                options.pop('field_um',None)
            elif options.get('field_um') is not None:
                request['view_field_um']=list(options['field_um'])
            options['nuclear_coverage']=request
        return options

    def _viewport_size(self):
        viewport = self.outlines.view.viewport()
        width, height = max(1, viewport.width()), max(1, viewport.height())
        scale = min(1., 1024 / max(width, height))
        return [max(1, round(width * scale)), max(1, round(height * scale))]

    def _refresh_browse_width(self):
        if (not self.isVisible() or self._bundle is None or not self._browse_preset
                or self.task.currentData() != 'overview' or self._browse_request_size is None):
            return
        width, height = self._viewport_size()
        previous_width, previous_height = self._browse_request_size
        if abs((width / height) / (previous_width / previous_height) - 1) > 0.025:
            self.revision += 1
            self._fit_next = True
            self.field_requested.emit()

    def _enabled(self,enabled):
        if not enabled:
            self.relationships.invalidate_field()
        self.export_field.setEnabled(enabled);self.export_data.setEnabled(enabled)
        mode=self._inspection_mode()
        self.task.setEnabled(enabled or self._bundle is not None)
        count=(len(self.coverage.fields) if self._coverage_active() else
               self._missing_count if mode=='without_nuclear_outline' else len(self._spatial))
        self.previous.setEnabled(enabled and bool(count));self.next.setEnabled(enabled and bool(count))
        surveying = self._coverage_active()
        self.survey.setVisible(surveying and not self.coverage.fields)
        self.survey.setEnabled(enabled and self.coverage.options() is not None)
        self._paging.setVisible(not surveying or bool(self.coverage.fields))

    def set_field(self,bundle):
        result=bundle['metadata'].get('nuclear_coverage')
        if result is not None and (self._coverage_request is None or
                any(result.get(key)!=value for key,value in self._coverage_request.items())):
            return
        self._fit_canvas_height()
        self.correct_cell.setEnabled(False)
        self._bundle=bundle;self._spatial=bundle['metadata']['spatial_fields']
        metadata=bundle['metadata'];missing=metadata.get('inspection_mode')=='without_nuclear_outline'
        navigation_available=metadata.get('missing_nucleus_navigation_available',False)
        coverage_available=metadata.get('coverage_available',navigation_available)
        self.coverage.configure(metadata)
        self.task.model().item(1).setEnabled(coverage_available or navigation_available)
        self.task.model().item(2).setEnabled(metadata.get('cell_coverage_available', False))
        self.task.setItemData(1,metadata.get('coverage_unavailable_reason',''),Qt.ItemDataRole.ToolTipRole)
        self.task.setItemData(2,metadata.get('cell_coverage_unavailable_reason',''),Qt.ItemDataRole.ToolTipRole)
        nuclear_task = self.task.currentData() == 'nuclei'
        coverage_active=self._coverage_active()
        self.coverage.setVisible(nuclear_task or coverage_active)
        self.coverage.show_method(nuclear_task, metadata)
        self._missing_count=metadata.get('missing_nucleus_count')
        self._missing_index=metadata.get('inspection_index',0)
        self.previous.setText('Previous cell' if missing else 'Previous field')
        self.next.setText('Next cell' if missing else 'Next field')
        self.previous.full_name=self.previous.text();self.next.full_name=self.next.text()
        boundary = bundle['metadata'].get('cell_boundary_source')
        self._heading.setText({'learned_cell': 'Review Cellpose cell outlines',
            'nuclear_outline': 'Review StarDist nuclear outlines',
            'estimated_nuclear_expansion': 'Review nuclei and estimated cell regions'}.get(boundary, 'Review cell and nucleus outlines'))
        self._updating=True
        try:
            self.outlines.set_field_bundle(bundle)
            self._options=dict(self.outlines.options)
            if self._fit_next:
                left,right,bottom,top=map(float,bundle['extent_um'])
                self._fit_anchor=QRectF(left,top,right-left,bottom-top)
                self._fit_field(self._fit_anchor)
                self._fit_next=False
        finally:self._updating=False
        self.relationships.set_bundle(bundle)
        if result is not None:self._coverage_loading=False
        self._enabled(True);self.set_status('' if bundle['metadata']['outline_detail_available'] else 'Zoom in to inspect cell outlines.')
        if coverage_active:
            if result is not None and self.coverage.set_result(result):
                self._coverage_loading=False
                selected=result.get('selected_field_index')
                self._coverage_target=None
                self.field_label.setText(f'Field {selected+1} of {len(self.coverage.fields)}' if selected is not None else 'Current field')
                self._set_coverage_overlay(bundle)
            self._enabled(True)
        elif missing:
            if not self._viewport:
                self.field_label.setText(f'Cell {self._missing_index+1} of {self._missing_count}' if self._missing_count else 'No cells without nuclear outlines')
            selected=metadata.get('inspection_cell')
            if selected is not None:
                self.outlines._inspect_cell([self.outlines._cell_key(selected)])
        elif not self.field_label.text():self.field_label.setText(f'Field {self._field_index+1} of {len(self._spatial)}')
        selected = self.outlines.selected_cell_identity()
        row = next((item for item in metadata['visible_cells'] if self.outlines._cell_key(item)==selected), None)
        if row is not None:self._cell(row)
        else:
            self.cell_label.hide()
            self._update_correction_actions(None)
        self._fit_canvas_height()
        if self._fit_anchor is not None:
            self._coverage_fit_timer.start(0)

    def _schedule_coverage_fit(self, *_args):
        if self._fit_anchor is not None or self._coverage_active():
            self.outlines._field_timer.stop()
            if self._fit_anchor is not None:self._coverage_fit_timer.start(0)

    def _fit_coverage_selection(self):
        """Fit after responsive layout settles, without treating its scroll change as a pan."""
        if self._bundle is None or self._fit_anchor is None:return
        self._fit_field(self._fit_anchor)

    def _fit_field(self,rect):
        field=self.outlines;updating=field._updating;field._updating=True
        try:
            field.view._fit=False;field.view.fitInView(rect,Qt.AspectRatioMode.KeepAspectRatio);field.view.centerOn(rect.center())
            field._field_timer.stop();field._position_location()
        finally:field._updating=updating

    def _pan(self,options):
        if self._updating or self._bundle is None or self._fit_next:return
        self._fit_anchor=None
        self._browse_preset = False
        self._viewport=dict(options)
        if self._coverage_active():
            self._coverage_target=None
            self.coverage.select_field(None)
            self._clear_coverage_overlay()
        self.field_label.setText('Current field');self._enabled(False);self.field_requested.emit()

    def _visit(self,step):
        if self._coverage_active():
            fields=self.coverage.fields
            if not fields:return
            indices=[row['field_index'] for row in fields]
            selected=self.coverage._selected
            index=indices.index(selected) if selected in indices else -1
            self._select_coverage_field(indices[(index+step)%len(indices)])
            return
        if self._inspection_mode()=='without_nuclear_outline':
            if not self._missing_count:return
            self._missing_index=(self._missing_index+step)%self._missing_count
            self._viewport={};self._options.pop('field_um',None);self._fit_next=True
            self.field_label.setText(f'Cell {self._missing_index+1} of {self._missing_count}')
            self.cell_label.hide();self._enabled(False);self.field_requested.emit()
            return
        if not self._spatial:return
        self._field_index=(self._field_index+step)%len(self._spatial)
        self._browse_preset = self.task.currentData() == 'overview'
        self._viewport={'field_um':self._spatial[self._field_index]};self._fit_next=True
        self.field_label.setText(f'Field {self._field_index+1} of {len(self._spatial)}')
        self.cell_label.hide();self._enabled(False);self.field_requested.emit()

    def _inspection_changed(self):
        if self._bundle is None:return
        self.revision+=1
        coverage_active=self._coverage_active()
        mode=self._inspection_mode()
        self.previous.setText('Previous cell' if mode == 'without_nuclear_outline' else 'Previous field')
        self.next.setText('Next cell' if mode == 'without_nuclear_outline' else 'Next field')
        self.previous.full_name, self.next.full_name = self.previous.text(), self.next.text()
        nuclear_task = self.task.currentData() == 'nuclei'
        self.coverage.setVisible(nuclear_task or coverage_active)
        self.coverage.show_method(nuclear_task, self._bundle['metadata'])
        self._clear_coverage_overlay()
        self._coverage_target=None
        self._coverage_loading=False
        self.coverage.set_busy(False)
        self.coverage.set_active(coverage_active)
        if coverage_active:
            self.coverage.set_compartment('cell' if mode=='cell_coverage' else 'nucleus', self._bundle['metadata'])
            self._coverage_request=None
            self.coverage.clear_result()
            self.coverage.set_active(True)
            self.field_label.setText('Current field')
            self._enabled(True)
            self._fit_canvas_height()
            return
        self._coverage_request=None
        self._fit_canvas_height()
        if mode=='without_nuclear_outline':
            self._missing_index=0;self._viewport={};self._fit_next=True
            self._options.pop('field_um',None)
            self.cell_label.hide();self.field_label.clear()
            self.set_status('Finding cells without nuclear outlines…')
            self._enabled(False);self.field_requested.emit()
        else:
            self.field_label.setText('Current field')
            self._enabled(True)

    def _inspection_mode(self):
        task = self.task.currentData()
        if task == 'nuclei':
            return self.coverage.method.currentData()
        return 'cell_coverage' if task == 'cells' else 'across_tissue'

    def _coverage_active(self):
        return self._inspection_mode() in {'nuclear_coverage', 'cell_coverage'}

    def _task_changed(self):
        if self._bundle is None:return
        self._browse_preset = False
        view=self.outlines.view
        if self._fit_anchor is None:
            self._fit_anchor = view.mapToScene(view.viewport().rect()).boundingRect().intersected(self.outlines._full_rect)
        if self.task.currentData() == 'nuclei':
            metadata = self._bundle['metadata']
            navigation = metadata.get('missing_nucleus_navigation_available', False)
            coverage = metadata.get('coverage_available', navigation)
            if not coverage or not navigation:
                with QSignalBlocker(self.coverage.method):
                    self.coverage.method.setCurrentIndex(0 if coverage else 1)
        self._task_presentation()
        self._inspection_changed()
        self.outlines._field_timer.stop()
        self._coverage_fit_timer.start(0)

    def _task_presentation(self):
        task=self.task.currentData()
        hints={
            'cells':'Choose a stain showing the cell body or membrane. Inspect highlighted staining outside cell outlines in its tissue context.',
            'merged':'Find outlines containing several connected stained regions. Inspect the highlighted regions and any separating membrane.\nNote: One nucleus can contain several bright stained regions, and one cell can contain several nuclei.',
            'fragmented':'Find stained regions shared by several outlines. Inspect the boundaries dividing each region.\nNote: Staining from adjacent cells can touch even when their outlines are correct.',
        }
        self.task_help.setText(hints.get(task,''));self.task_help.setVisible(bool(hints.get(task)))
        self.relationships.set_active(task)
        self._relationship_exports(self.relationships._result is not None)
        self.previous.setText('Previous field');self.next.setText('Next field')
        self.previous.full_name=self.previous.text();self.next.full_name=self.next.text()

    def _coverage_exports(self, available):
        noun='Cell' if self.coverage.compartment=='cell' else 'Nuclear'
        self.export_coverage_plot.setText(f'{noun} coverage plot…')
        self.export_coverage_data.setText(f'{noun} coverage table…')
        self.export_coverage_plot.setEnabled(available)
        self.export_coverage_data.setEnabled(available)

    def _relationship_exports(self, available):
        available=available and not self.relationships.isHidden()
        self.export_relationship_plot.setEnabled(available and bool(self.relationships._records))
        self.export_relationship_data.setEnabled(available)

    def _coverage_edited(self):
        """Invalidate old numerical/overlay results and any read still in flight."""
        self.revision+=1
        self._coverage_request=None
        self._coverage_target=None
        self._coverage_loading=False
        self.set_status('')
        self._clear_coverage_overlay()
        if self._coverage_active():
            self.field_label.setText('Current field')
        self._enabled(self._bundle is not None)
        self._fit_canvas_height()

    def _calculate_coverage(self, request, *, survey=False):
        """Submit explicit parameters through the existing authenticated field request."""
        if request is None:return
        if not self._coverage_active():return
        self.revision+=1
        self._coverage_request=dict(request)
        self._coverage_target=0 if survey else None
        self._coverage_loading=True
        self.coverage.clear_result()
        self.coverage.set_busy(True)
        self._clear_coverage_overlay()
        view=self.outlines.view.mapToScene(self.outlines.view.viewport().rect()).boundingRect()
        self._viewport={} if survey else {'field_um':[view.left(),view.top(),view.right(),view.bottom()]}
        self._options.pop('field_um',None)
        self._options.update(channels=[request['channel']],channel_display='grayscale')
        self._fit_next=survey
        self.cell_label.hide()
        self.set_status('Calculating staining coverage…')
        self._enabled(False)
        self.field_requested.emit()

    def _select_coverage_field(self, index):
        """Open one returned physical field; selecting or panning never changes its areas."""
        if self._coverage_request is None or index not in [row['field_index'] for row in self.coverage.fields]:return
        self._coverage_target=int(index)
        self._viewport={}
        self._options.pop('field_um',None)
        self._fit_next=True
        self.cell_label.hide()
        self._clear_coverage_overlay()
        self._enabled(False)
        self.field_requested.emit()

    def _clear_coverage_overlay(self):
        if self._coverage_overlay is not None:
            try:
                scene=self._coverage_overlay.scene()
                if scene is not None:scene.removeItem(self._coverage_overlay)
            except RuntimeError:
                pass
            self._coverage_overlay=None

    def _set_coverage_overlay(self, bundle):
        """Place supplied categorical pixels in the same calibrated scene as the staining."""
        self._clear_coverage_overlay()
        if 'coverage_overlay' not in bundle:return
        rgba=coverage_rgba(bundle['coverage_overlay'])
        height,width=rgba.shape[:2]
        image=QImage(rgba.data,width,height,rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
        item=self.outlines.view.scene().addPixmap(QPixmap.fromImage(image))
        left,right,bottom,top=map(float,bundle['coverage_extent_um'])
        item.setPos(left,top)
        item.setTransform(QTransform.fromScale((right-left)/width,(bottom-top)/height))
        item.setTransformationMode(Qt.TransformationMode.FastTransformation)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        item.setZValue(4)
        self._coverage_overlay=item
        self._show_coverage_overlay()

    def _show_coverage_overlay(self, *_args):
        if self._coverage_overlay is not None:
            self._coverage_overlay.setVisible(self._coverage_active() and self.coverage.overlay.isChecked())

    def _display(self,options):
        if not self._updating:self._options=dict(options)

    def _load_channels(self,options):
        if self._updating:return
        self._options=dict(options);self._enabled(False);self.field_requested.emit()

    def _cell(self, row):
        """Show the selected outline's location below the field and retain its exact ID."""
        nucleus = (self._bundle or {}).get('metadata', {}).get('cell_boundary_source') == 'nuclear_outline'
        name = 'Selected nucleus' if nucleus else 'Selected cell'
        coordinates = f"X {row['x_um']:.2f} µm · Y {row['y_um']:.2f} µm"
        self.cell_label.setText(f'{name} · {coordinates}')
        self.cell_label.setToolTip(coordinates)
        self.cell_label.setAccessibleDescription(coordinates)
        self.cell_label.show()
        self._update_correction_actions(row)
        self._fit_canvas_height()

    def _update_correction_actions(self, row):
        metadata = (self._bundle or {}).get('metadata', {})
        available = metadata.get('cell_correction_available', False)
        rows = metadata.get('visible_cells', [])
        identity = self.outlines._cell_key(row) if row is not None else None
        index = next((i for i,item in enumerate(rows) if self.outlines._cell_key(item)==identity), None)
        present = {str(kind) for cell_index,kind in zip(self._bundle['outline_cell_indices'], self._bundle['outline_kinds'])
                   if int(cell_index)==index} if index is not None else set()
        add_available=available and all(metadata.get(key) for key in ('sample_id','project_entry_id','slide_id','section_id'))
        self.corrections.setVisible(available or bool(self.corrections.pending_count()))
        self.correct_cell.setEnabled(available and (bool(present) or add_available))
        self._correction_actions['cell_outline'].setEnabled(available and 'cell' in present
            and metadata.get('cell_boundary_source') != 'nuclear_outline')
        self._correction_actions['nucleus_outline'].setEnabled(available and 'nucleus' in present
            and metadata.get('nucleus_boundary_source') != 'not_segmented')
        self._correction_actions['delete'].setEnabled(available and bool(present))
        self._correction_actions['split'].setEnabled(available and bool(present))
        self._correction_actions['merge'].setEnabled(available and bool(present))
        self._correction_actions['add_nucleus'].setEnabled(available and 'cell' in present and 'nucleus' not in present
            and metadata.get('nucleus_boundary_source') != 'not_segmented')
        self._correction_actions['add_cell'].setEnabled(add_available)
        self._correction_actions['add_cell'].setText('Draw a new nucleus…' if metadata.get('cell_boundary_source')=='nuclear_outline' else 'Draw a new cell…')
        self.correct_cell.setToolTip('Choose a correction for the selected cell, or draw a missing outline.'
            if available else 'Finish the active processing or correction before editing outlines.')

    def _request_correction(self, operation):
        identity = self.outlines.selected_cell_identity()
        if operation=='add_cell' and self.correct_cell.isEnabled() and self._correction_actions[operation].isEnabled():
            from uuid import uuid4
            metadata=self._bundle['metadata']
            cell_id=str(uuid4())
            identity=tuple(metadata[key] for key in ('sample_id','project_entry_id','slide_id','section_id'))+(cell_id,)
            left,top,right,bottom=metadata['field_um']
            self.correction_requested.emit({'identity':identity,'operation':operation,
                'row':dict(cell_id=cell_id,x_um=(left+right)/2,y_um=(top+bottom)/2)})
            return
        if identity is not None and self.correct_cell.isEnabled() and self._correction_actions[operation].isEnabled():
            self.correction_requested.emit({'identity':identity, 'operation':operation})

    def _export(self,data_only):
        if self._bundle is None:return
        from .figure_export import _choose_path,_save_bytes,_DATA_FORMATS,export_figure
        from mif_pipeline.cell_analysis.measurement_spatial_plots import field_figure,field_npz,field_cells_csv
        field=self.outlines
        options={**field.options,'displayed_channel':field.channel.currentData(),'show_cell_outlines':field.cells.isChecked(),
                 'show_nuclear_outlines':field.nuclei.isChecked(),'selected_cell_identity':field.selected_cell_identity()}
        overlay_visible=(self._coverage_active() and self.coverage.overlay.isChecked()
                         and self._coverage_overlay is not None and 'coverage_overlay' in self._bundle)
        options['show_coverage_overlay']=overlay_visible
        relationships=self.relationships._result if not self.relationships.isHidden() else None
        if relationships is not None:
            options['stain_outline_relationships']={key:relationships[key] for key in ('channel','threshold','min_area_um2','compartment','rule')}
            options['stain_outline_relationships']['selected_region_ids']=self.relationships.selected_region_ids()
            options['stain_outline_relationships']['highlight']=self.relationships.highlight.isChecked()
        if not data_only:
            figure=field_figure(self._bundle,options)
            if relationships is not None and self.relationships.highlight.isChecked():
                figure.axes[0].imshow(self.relationships.highlight_rgba(),extent=relationships['extent_um'],interpolation='nearest',zorder=1)
            if overlay_visible:
                from matplotlib.patches import Patch
                from .segmentation_coverage_review import COVERED, UNCOVERED
                figure.axes[0].imshow(coverage_rgba(self._bundle['coverage_overlay']),
                    extent=self._bundle['coverage_extent_um'],interpolation='nearest',zorder=1)
                result=self._bundle['metadata']['nuclear_coverage']
                noun='cell' if result.get('compartment')=='cell' else 'nuclear'
                figure.axes[0].legend(handles=[Patch(color=COVERED,label=f'Inside {noun} outlines'),
                    Patch(color=UNCOVERED,label=f'Outside {noun} outlines')],
                    title=f"{result.get('channel_name','Channel '+str(result['channel']))} > {result['threshold']:g}",
                    loc='upper right',fontsize=9,title_fontsize=9,framealpha=.9)
            export_figure(self,figure,'segmentation-tissue-field.svg')
            return
        path=_choose_path(self,'segmentation-tissue-field.npz',_DATA_FORMATS)
        if path is not None:_save_bytes(field_npz(self._bundle,options) if path.suffix.lower()=='.npz' else field_cells_csv(self._bundle),path)
