"""Transient navigation into the ordinary marker-method form."""
from mif_pipeline.pipeline_core import StageResult, InteractionReason
from mif_pipeline.orchestration.batch_phenotyping._common import digest
from mif_pipeline.orchestration.batch_phenotyping.marker_preparation import preparation_request
from mif_pipeline.gui.marker_setup import build_marker_setup_form
from .researcher_widgets import ContentSizedButton
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout


def interrupted_marker_draft_key(form):
    if form.context.get('adapter') != 'marker_setup':
        return None
    context = form.context['stage_result']['resume_context']
    workflow = context.get('marker_workflow')
    if not workflow:
        return None
    return ('marker-preparation',context['validation_domain_id'],workflow['current_marker_id'],
            digest(context['measurement_authority_bindings']))


class MarkerPreparationVisit:
    def __init__(self, renderer, marker=None):
        from .forms import FormRenderer
        self.renderer = renderer
        self.request = StageResult.from_mapping(renderer._form.context['stage_result'])
        self.projected = preparation_request(self.request, marker)
        self.marker = self.projected.resume_context['marker_workflow']['current_marker_id']
        self.baseline = self.projected.resume_context['initial_marker_settings']
        source = self.request.to_mapping()['resume_context']
        self.full_settings = source.get('initial_marker_settings') or source['marker_workflow']['settings']
        self.hidden = []
        for index in range(renderer.layout().count()):
            widget = renderer.layout().itemAt(index).widget()
            if widget is not None and not widget.isHidden():
                self.hidden.append(widget); widget.hide()
        for widget in (*renderer._marker_buttons.values(), renderer.navigation_widget):
            if widget is not None and not widget.isHidden() and widget not in self.hidden:
                self.hidden.append(widget); widget.hide()
        panel = getattr(renderer, '_marker_review_panel_widget', None)
        if panel is not None and not panel.isHidden():
            self.hidden.append(panel); panel.hide()
        self.editor = FormRenderer(parent=renderer)
        self.editor.set_form(build_marker_setup_form(self.projected))
        self.draft_key = (source['validation_domain_id'], digest(source['measurement_authority_bindings']),
                          self.marker, digest(self.baseline))
        drafts = getattr(renderer, '_marker_preparation_drafts', {})
        if self.draft_key in drafts:
            self.editor.restore_values(drafts[self.draft_key])
        self.editor._marker_workbench.revisit_button.hide()
        self.editor._marker_buttons['abort'].hide()
        self.editor._marker_buttons['define_marker_composite_rules'].setText('Continue')
        self.back = ContentSizedButton('Back', self.editor)
        self.back.setObjectName('marker-preparation-return')
        self.back.clicked.connect(self.close)
        self.footer = getattr(renderer, '_stage_action_footer', None)
        self.navigation = None
        if self.footer is not None:
            self.navigation = QWidget(self.footer)
            row = QHBoxLayout(self.navigation); row.setContentsMargins(0,8,0,8)
            row.addWidget(self.back); row.addStretch(1)
            row.addWidget(self.editor._marker_buttons['define_marker_composite_rules'])
            self.footer.layout().addWidget(self.navigation,1)
        else:
            self.editor.layout().insertWidget(0,self.back,0,Qt.AlignmentFlag.AlignLeft)
        renderer.layout().addWidget(self.editor)
        self.editor.submission_ready.connect(self.submit)
        renderer.page_changed.emit()

    def close(self):
        if not hasattr(self.renderer, '_marker_preparation_drafts'):
            self.renderer._marker_preparation_drafts = {}
        self.renderer._marker_preparation_drafts[self.draft_key] = self.editor.capture_draft_values()
        if self.navigation is not None:
            self.footer.layout().removeWidget(self.navigation)
            self.navigation.hide(); self.navigation.setParent(None); self.navigation.deleteLater()
        self.renderer.layout().removeWidget(self.editor)
        self.editor.hide(); self.editor.setParent(None); self.editor.deleteLater()
        for widget in self.hidden: widget.show()
        self.renderer._marker_preparation_visit = None
        self.renderer.page_changed.emit()

    def submit(self, action, response):
        if action == 'abort':
            self.close(); return
        settings = response['marker_settings']
        if digest(settings) == digest(self.baseline):
            self.close(); return
        settings = {**self.full_settings,'marker_rules':[settings['marker_rules'][0]
            if rule['marker_id'] == self.marker else rule for rule in self.full_settings['marker_rules']]}
        if self.request.interaction_reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
            raw = {'revised_marker_settings': settings}
            action = 'review_marker_thresholds'
        else:
            raw = {'revisit_marker_id': self.marker, 'marker_settings': settings}
            action = 'define_marker_composite_rules'
        # The original live renderer/batch binder supplies the current request fence.
        self.renderer._marker_preparation_response = raw
        try:
            self.renderer._submit(action)
        finally:
            self.renderer._marker_preparation_response = None


def open_marker_preparation(renderer, marker=None):
    if getattr(renderer, '_marker_preparation_visit', None) is None:
        renderer._marker_preparation_visit = MarkerPreparationVisit(renderer,marker)
