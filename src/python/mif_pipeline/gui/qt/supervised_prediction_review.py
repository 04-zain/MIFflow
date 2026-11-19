"""Linked assignment coverage, complete paged score inspection and exact cells."""
from collections import OrderedDict
import json
import math

from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QCheckBox, QPushButton, QComboBox, QHeaderView

from .classifier_review_data import review_table
from .researcher_widgets import ContentSizedButton
from .marker_cell_examples import MarkerCellExamples
from .supervised_score_view import PredictionPageModel, ScoreCoverageView


class SupervisedPredictionReview(QWidget):
    image_requested = Signal()
    page_requested = Signal(object)
    assign_requested = Signal(object)

    def __init__(self, context, editor, parent=None):
        super().__init__(parent)
        self.setObjectName('supervised-prediction-review')
        self.editor = editor
        self.summary = None
        self.selected_class = self.selected_cell = None
        self._range = (0., 1.)
        self._revision = self._image_revision = 0
        self._pending = self._active_page = None
        self._pending_pages = OrderedDict()
        self._viewer = None
        self._cohort_images = []
        self._navigation_images = OrderedDict()
        self._processing_groups = []
        self._has_multiple_images = False
        self.scoped_assignment_available = False
        self.selected_prediction = None
        self.binary_svm = context['score_kind'] == 'native_svm_pairwise_vote_fraction' and context['class_count'] == 2
        self.score_label = {'native_tree_vote_fraction':'Forest vote fraction',
            'native_logistic_ovr_normalized_score':'Normalized logistic score',
            'native_svm_pairwise_vote_fraction':'SVM vote agreement'}[context['score_kind']]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel('Review cell assignments', self)
        title.setStyleSheet('font-size: 21px; font-weight: 600; color: #173d47;')
        layout.addWidget(title)
        explanation = {
            'native_tree_vote_fraction':'The score is the fraction of trees voting for the predicted class.',
            'native_logistic_ovr_normalized_score':'The score is the predicted class’s logistic response divided by the sum across classes.',
            'native_svm_pairwise_vote_fraction':'The score is the fraction of pairwise comparisons won by the predicted class, among comparisons involving that class.',
        }[context['score_kind']]
        self._score_explanation = explanation
        self.explanation = QLabel(explanation+' Cells below the cutoff are left Unclassified.', self)
        if self.binary_svm:
            self.explanation.setText('With two classes, SVM gives every prediction a vote-agreement score of 1. Use held-out evaluation and cell images to assess errors.')
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        self.scope_bar = QWidget(self)
        scope_layout = QHBoxLayout(self.scope_bar)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        self.previous_scope = ContentSizedButton('←', self.scope_bar)
        self.previous_scope.setAccessibleName('Previous image or processing group')
        self.previous_scope.clicked.connect(lambda: self._move_scope(-1))
        scope_layout.addWidget(self.previous_scope)
        self.scope = QComboBox(self.scope_bar)
        self.scope.addItem('Selected image', 'image')
        self.scope.setAccessibleName('Image or processing group to review and assign')
        self.scope.currentIndexChanged.connect(self._scope_changed)
        scope_layout.addWidget(self.scope, 1)
        self.next_scope = ContentSizedButton('→', self.scope_bar)
        self.next_scope.setAccessibleName('Next image or processing group')
        self.next_scope.clicked.connect(lambda: self._move_scope(1))
        scope_layout.addWidget(self.next_scope)
        self.scope_bar.hide()
        layout.addWidget(self.scope_bar)
        row = QHBoxLayout()
        layout.addLayout(row)
        label = QLabel(self.score_label+' cutoff', self)
        self.cutoff_label = label
        row.addWidget(label)
        self.cutoff = editor if editor is not None else QLineEdit('0', self)
        self.cutoff.setMaximumWidth(100)
        self.cutoff.setAccessibleName(self.score_label+' cutoff')
        row.addWidget(self.cutoff)
        self.cutoff.show()
        self.assign = QPushButton('Assign', self)
        self.assign.clicked.connect(self._assign)
        row.addWidget(self.assign)
        if self.binary_svm:
            label.hide(); self.cutoff.hide()
        self.refresh = QPushButton('Retry', self)
        row.addWidget(self.refresh)
        self.refresh.hide()
        row.addStretch()
        self.classes = QComboBox(self)
        self.classes.setAccessibleName('Predicted class')
        self.classes.addItem('All predicted classes', None)
        self.classes.currentIndexChanged.connect(self._select_class)
        plot_choices = QHBoxLayout()
        plot_choices.addWidget(self.classes,1)
        layout.addLayout(plot_choices)
        self.counts = QLabel('Loading cell predictions…', self)
        self.counts.setWordWrap(True)
        layout.addWidget(self.counts)
        self.curve = ScoreCoverageView(self.score_label, self)
        self.canvas = self.curve
        self.curve.setVisible(not self.binary_svm)
        self.curve.cutoff_changed.connect(lambda value: self.cutoff.setText(format(value, '.12g')))
        self.curve.range_selected.connect(self._select_range)
        layout.addWidget(self.curve)
        hint = QLabel('Drag the amber cutoff handle to change assignments. Drag across the plot to inspect a score range.', self)
        self.hint = hint
        hint.setWordWrap(True)
        hint.setProperty('choiceHelp', True)
        hint.setVisible(not self.binary_svm)
        layout.addWidget(hint)
        filters = QHBoxLayout()
        layout.addLayout(filters)
        self.matches = QLabel(self)
        filters.addWidget(self.matches)
        filters.addStretch()
        self.clear_range = QPushButton('Clear score selection', self)
        self.clear_range.clicked.connect(lambda: self._select_range(0., 1.))
        self.clear_range.hide()
        filters.addWidget(self.clear_range)
        self.rejected = QCheckBox('Unclassified only', self)
        filters.addWidget(self.rejected)
        from .cohort_image_navigation import CohortImageNavigation
        self.image_navigation = CohortImageNavigation(self)
        self.image_navigation.image_changed.connect(self._image_changed)
        layout.addWidget(self.image_navigation)
        self.owner_label = QLabel(self);self.owner_label.setWordWrap(True);self.owner_label.hide()
        layout.addWidget(self.owner_label)
        self.examples = review_table('supervised-prediction-cells', self)
        self.examples.setAccessibleName('All matching predicted cells')
        self.model = PredictionPageModel(self.examples)
        self.examples.setModel(self.model)
        header = self.examples.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.examples.setColumnWidth(0, 76)
        self.examples.setColumnWidth(2, 90)
        self.examples.setColumnWidth(3, 132)
        self.examples.setFixedHeight(160)
        self.examples.selectionModel().currentRowChanged.connect(self._select_cell)
        self.model.page_requested.connect(self._queue_page)
        layout.addWidget(self.examples)
        from .supervised_assignment_field import SupervisedAssignmentField
        self.field = SupervisedAssignmentField(self)
        layout.addWidget(self.field)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._request)
        self._image_resize_timer = QTimer(self)
        self._image_resize_timer.setSingleShot(True)
        self._image_resize_timer.setInterval(200)
        self._image_resize_timer.timeout.connect(self._refresh_image_extent)
        self.cutoff.textChanged.connect(self._changed)
        self.rejected.toggled.connect(self._changed)
        self.refresh.clicked.connect(lambda: self.field._schedule() if self._cohort_images else
                                     self._request_image() if self.selected_cell else self._request())

    def _cutoff(self):
        value = 0. if self.binary_svm else float(self.cutoff.text())
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('Enter a score cutoff between zero and one.')
        return value

    def set_processing_groups(self, groups):
        """Offer the declared processing groups without constructing new cohorts."""
        previous = self.scope.currentData()
        self._processing_groups = list(groups)
        with QSignalBlocker(self.scope):
            self.scope.clear()
            if self.scoped_assignment_available:
                for group in groups:
                    self.scope.addItem('Processing group · '+group['label'], 'group:'+group['id'])
                    item = self.scope.model().item(self.scope.count()-1)
                    item.setEnabled(group.get('assignment_available', True))
                    if not item.isEnabled():
                        item.setToolTip('These images use different fitted classifiers. Review their cutoffs separately.')
            for key, image in self._navigation_images.items():
                self.scope.addItem('Image · '+image['label'], 'image:'+json.dumps(key, separators=(',', ':')))
            if not self._navigation_images:
                self.scope.addItem('Selected image', 'image')
            index = self.scope.findData(previous)
            if index < 0 or not self.scope.model().item(index).isEnabled():
                image_key = self.image_navigation.current_key()
                index = self.scope.findData('image:'+json.dumps(image_key, separators=(',', ':')))
                if index < 0:
                    index = self.scope.findData('image')
            self.scope.setCurrentIndex(max(0, index))
        self._scope_buttons()

    def _scope_buttons(self):
        index = self.scope.currentIndex()
        enabled = [i for i in range(self.scope.count()) if self.scope.model().item(i).isEnabled()]
        self.previous_scope.setEnabled(any(i < index for i in enabled))
        self.next_scope.setEnabled(any(i > index for i in enabled))

    def _move_scope(self, direction):
        index = self.scope.currentIndex()+direction
        while 0 <= index < self.scope.count():
            if self.scope.model().item(index).isEnabled():
                self.scope.setCurrentIndex(index)
                return
            index += direction

    def _selected_image_key(self):
        choice = self.scope.currentData() or 'image'
        return json.loads(choice[6:]) if choice.startswith('image:') else self.image_navigation.current_key()

    def _scope_changed(self, *_args):
        self._scope_buttons()
        if self._scope_settings()['plot_scope'] == 'image':
            with QSignalBlocker(self.image_navigation.images):
                index = self.image_navigation.images.findData(self._selected_image_key())
                if index >= 0:
                    self.image_navigation.images.setCurrentIndex(index)
            self.image_navigation._buttons()
        self.field.cell_view.clear()
        self._changed()

    def _scope_settings(self):
        choice = (self.scope.currentData() or 'image') if self._cohort_images or self._processing_groups else 'image'
        if choice.startswith('group:'):
            return dict(plot_scope='group', processing_group_id=choice[6:])
        return dict(plot_scope='image')

    def assignment_options(self):
        scope = self._scope_settings()
        if scope['plot_scope'] == 'group' and not self._group_assignable(scope['processing_group_id']):
            raise ValueError('This processing group contains predictions from different classifiers. Select an image to apply its cutoff.')
        options = dict(scope=scope.pop('plot_scope'), cutoff=self._cutoff(), **scope)
        if options['scope'] == 'image' and self._cohort_images:
            options['image_identity'] = self._selected_image_key()
        return options

    def _group_assignable(self, group_id):
        return any(group['id'] == group_id and group.get('assignment_available', True)
                   for group in self._processing_groups)

    def _cutoff_editable(self):
        scope = self._scope_settings()
        if not self.scoped_assignment_available and self._cohort_images:
            active = next((image for image in self._cohort_images
                           if image['key'] == self.image_navigation.current_key()), None)
            return scope['plot_scope'] == 'image' and active is not None and any(
                owner['pending'] for owner in active['owners'])
        return (scope['plot_scope'] == 'image' or
                scope['plot_scope'] == 'group' and self._group_assignable(scope['processing_group_id']))

    def _assign(self):
        try:
            options = self.assignment_options()
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.assign_requested.emit(options)

    def _settings(self):
        lower, upper = self._range
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (lower, upper)) or lower > upper:
            raise ValueError('Enter an inclusive inspection range between zero and one, from lower to higher.')
        settings = dict(cutoff=self._cutoff(), class_id=self.selected_class, lower=lower, upper=upper,
                        unclassified_only=self.rejected.isChecked(), revision=self._revision)
        if self._cohort_images:
            settings.update(image_identity=self._selected_image_key())
        if self._cohort_images or self._processing_groups:
            settings.update(self._scope_settings())
        return settings

    def _hide_cell(self):
        self.selected_cell = None
        self._image_resize_timer.stop()
        self._pending = None
        if self._viewer is not None:
            self._viewer.cell_view.alignment_context_changed.emit(None)
            self._viewer.hide()

    def _changed(self, *_args):
        self._hide_cell()
        if self._cohort_images:
            self.field.invalidate()
        self.clear_range.setVisible(not self.binary_svm and self._range != (0., 1.))
        self._revision += 1
        self._pending_pages.clear()
        self.model.reset()
        self.matches.clear()
        try:
            settings = self._settings()
        except ValueError as error:
            self.assign.setEnabled(False)
            self._timer.stop(); self.status.setText(str(error)); return
        self.assign.setEnabled(self._cutoff_editable())
        self.curve.cutoff = settings['cutoff']
        self.curve.inspection = (settings['lower'], settings['upper'])
        self.curve.update()
        self.status.setText('Updating matching cells…')
        self._timer.start()

    def _image_changed(self,*_args):
        self.field.cell_view.clear()
        self._changed()

    def _select_range(self, lower, upper):
        self._range = (float(format(lower, '.12g')), float(format(upper, '.12g')))
        self._changed()

    def _select_class(self, _index):
        self.selected_class = self.classes.currentData()
        self._changed()

    def _select_cell(self, current, _previous):
        row = self.model.row(current.row()) if current.isValid() else None
        if row is not None:
            self.selected_cell = row['cell_id']
            self._image_revision += 1
            if self._cohort_images:
                self.selected_prediction = row['prediction_sha256']
                self.field.focus_cell(row)
            else:
                self._request_image()

    def image_options(self):
        settings = self._settings()
        settings['image_revision'] = self._image_revision
        if self.selected_cell is not None:
            settings['cell_id'] = self.selected_cell
            if self.selected_prediction is not None:
                settings['prediction_sha256'] = self.selected_prediction
            settings.update(self._viewer.options() if self._viewer is not None else
                {'channel_display':'colour', 'merged_panel':True,
                 'field_aspect':max(.5, min(4., self.width()/320.)), 'pyramid_level':0})
        self._pending = settings
        return {'prediction_review_settings':settings}

    def _request_image(self):
        try:
            self.image_options()
        except ValueError as error:
            self.set_error(str(error)); return
        self.status.setText('Loading selected cell…')
        self.image_requested.emit()

    def _request(self):
        self._timer.stop()
        self._queue_page(0)

    def _queue_page(self, offset):
        try:
            options = dict(self._settings(), offset=offset, page_size=self.model.page_size)
        except ValueError as error:
            self.set_error(str(error)); return
        if options == self._active_page:
            return
        self._pending_pages[offset] = options
        self._pending_pages.move_to_end(offset)
        while len(self._pending_pages) > self.model.cache_pages:
            dropped, _ = self._pending_pages.popitem(last=False)
            self.model._requested.discard(dropped)
        self._next_page()

    def _next_page(self):
        if self._active_page is not None or not self._pending_pages:
            return
        _, self._active_page = self._pending_pages.popitem(last=False)
        self.page_requested.emit(self._active_page)

    def set_page(self, result):
        """Accept only the requested query generation; older reads release the queue."""
        request = result.get('request')
        if request != self._active_page:
            return False
        self._active_page = None
        if request['revision'] == self._revision:
            self.summary = result['summary']
            summary = self.summary
            if 'cohort_images' in result:
                self._cohort_images = result['cohort_images']
                for image in self._cohort_images:
                    self._navigation_images[tuple(image['key'])] = image
                self.image_navigation.set_images(self._cohort_images)
                if 'processing_groups' in result:
                    self.set_processing_groups(result['processing_groups'])
                self._has_multiple_images = (self._has_multiple_images or len(self._cohort_images) > 1
                                             or len(self._processing_groups) > 1)
                self.scope_bar.setVisible(self._has_multiple_images)
                self.image_navigation.setVisible(self._scope_settings()['plot_scope'] == 'group'
                    and len(self._cohort_images) > 1)
                self.cutoff_label.setText(self.score_label+' cutoff')
                if not self.binary_svm:
                    self.explanation.setText(self._score_explanation+' Cells below the cutoff are left Unclassified.')
                active = next(row for row in self._cohort_images if row['key'] == result['active_image'])
                editable = self._cutoff_editable()
                self.cutoff.setEnabled(editable);self.assign.setEnabled(editable)
                self.curve.inspection_only = not editable
                self.curve.setToolTip('Drag the cutoff handle to change assignments. Drag across the plot to inspect a score range.' if editable else
                    'Drag across the plot to inspect a score range across images.')
                self.hint.setText('Drag the amber cutoff handle, then select Assign. Drag across the plot to inspect a score range.' if editable else
                    'Select a score range to inspect cells. Choose a processing group or image to apply a cutoff.')
                if not editable and not self.scoped_assignment_available:
                    self.hint.setText('Select the image awaiting review to apply its cutoff. Drag across the plot to inspect a score range.')
                if not editable and self._scope_settings()['plot_scope'] == 'group':
                    self.hint.setText('This processing group contains predictions from different classifiers. Select an image to apply its cutoff.')
                self.counts.setToolTip('\n'.join(
                    f"Model {owner['classifier_record_sha256']} · validation domain {owner['validation_domain_id']}"
                    for owner in active['owners']))
                self.owner_label.hide()
                self.field.set_query(dict(self._settings(),image_identity=result['active_image']))
            self.counts.setText(f"{summary['sample_id']} · {summary['scope_total']:,} predicted · {summary['scope_assigned']:,} assigned · {summary['scope_unclassified']:,} Unclassified")
            self.matches.setText(f"{result['matching_total']:,} matching cells")
            self.curve.set_summary(summary, (result['lower'], result['upper']))
            with QSignalBlocker(self.classes):
                self.classes.clear(); self.classes.addItem('All predicted classes', None)
                for row in summary['classes']:
                    self.classes.addItem(row['class_id'], row['class_id'])
                self.classes.setCurrentIndex(max(0, self.classes.findData(self.selected_class)))
            self.model.put_page(result['offset'], result['rows'], result['matching_total'])
            current = self.examples.currentIndex()
            if (self.selected_cell is None and current.isValid()
                    and self.examples.selectionModel().hasSelection()
                    and result['offset'] <= current.row() < result['offset']+len(result['rows'])):
                self._select_cell(current, current)
            self.refresh.hide()
            self.status.setText('' if result['matching_total'] else 'No cells match this inspection range.')
        self._next_page()
        return True

    def page_failed(self, request, message):
        if request != self._active_page:
            return
        self._active_page = None
        if request['revision'] == self._revision:
            self.model._requested.discard(request['offset'])
            self.set_error(message)
        self._next_page()

    def set_error(self, message):
        self.refresh.show()
        self.status.setText(message)

    def set_plots(self, plots):
        if not plots:
            return
        options = plots[0].display_options
        if options.get('prediction_request') != self._pending or self.selected_cell is None:
            return
        if 'selected_cell_ids' not in options:
            return
        if self._viewer is None:
            sample = options['prediction_review']['sample_id']
            self._viewer = MarkerCellExamples([dict(sample_id=sample, project_entry_id='', slide_id=sample)],
                self, title='Predicted cell', embedded=True)
            self._viewer.refresh.hide()
            self.layout().addWidget(self._viewer)
            self._viewer.requested.connect(self._request_image)
        self._viewer.set_plots(plots)
        self._viewer.show()
        self.status.clear()
        self._image_resize_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.selected_cell is not None:
            self._image_resize_timer.start()

    def _refresh_image_extent(self):
        """Read a matching rectangular source field after the viewport settles."""
        if self.selected_cell is None or self._viewer is None or self._pending is None:
            return
        previous = self._pending.get('field_aspect')
        current = self._viewer.options().get('field_aspect')
        if previous is not None and current is not None and abs(current-previous) > .02*previous:
            self._request_image()
