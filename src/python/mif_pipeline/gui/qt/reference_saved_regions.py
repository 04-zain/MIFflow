"""Local editing of accepted positive choices; no historical response authority."""
from dataclasses import replace
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea
from mif_pipeline.gui.interactions import FormModel, FormControl, ControlKind, decode_form_values
from mif_pipeline.gui.reference_settings import reference_settings
from mif_pipeline.preprocessing.unmixing.reference_sampling import CompleteVectorQcPolicy, ProcessingChunkShape
from .forms import FormRenderer


class SavedReferenceRegionsEditor(QWidget):
    returned = Signal()
    changed = Signal(object, bool)

    def __init__(self, baseline, parent=None):
        super().__init__(parent)
        self.baseline = baseline
        layout = QVBoxLayout(self)
        settings, _paths, _groups, _constants = reference_settings(
            'advance_reference_purification', 'positive_reference_rois', 0)
        controls = (FormControl('selected_roi_ids', 'Positive regions to use', ControlKind.OBJECT_SEQUENCE,
            choices=tuple(row['roi_id'] for row in baseline['roi_choices']),
            value=tuple(baseline['selected_roi_ids'])),) + tuple(
            replace(control, value=value) for control, value in zip(settings, baseline['sampling_values'], strict=True))
        self.form_model = FormModel('saved_positive_regions', 'Review positive regions',
            'Review the saved region choices and pixel-selection settings.', (), controls=controls,
            context={'choice_labels': {'selected_roi_ids': {r['roi_id']: r['label'] for r in baseline['roi_choices']}}})
        scroll = QScrollArea(self); scroll.setWidgetResizable(True)
        self.form = FormRenderer(parent=scroll); self.form.set_form(self.form_model)
        # This local editor never invokes the renderer's scientific submission path.
        self.form.cancelled.connect(self.returned)
        for button in self.form.findChildren(QPushButton):
            if button.text() == 'Cancel': button.hide()
        scroll.setWidget(self.form); layout.addWidget(scroll, 1)
        self.notice = QLabel('Saving changed choices repeats this reference’s positive sampling and its later control, background and spectrum reviews. Returning without saving keeps the current review.', self)
        self.notice.setWordWrap(True); layout.addWidget(self.notice)
        self.error = QLabel(self); self.error.setWordWrap(True); layout.addWidget(self.error)
        self.edit = QPushButton('Edit regions in QuPath', self)
        self.edit.setObjectName('saved-reference-edit-outlines')
        self.edit.setToolTip('Return to positive-region editing and repeat this reference’s dependent sampling.')
        self.edit.clicked.connect(lambda: self.save(edit=True)); layout.addWidget(self.edit)
        back = QPushButton('Return to current review', self); back.setObjectName('saved-reference-return')
        back.clicked.connect(self.returned); layout.addWidget(back)
        save = QPushButton('Save and next', self); save.setObjectName('saved-reference-save')
        save.setProperty('actionRole', 'primary'); save.clicked.connect(lambda: self.save()); layout.addWidget(save)

    def values(self):
        # Decode by the shared controls, then enforce the exact sampling policy.
        model = replace(self.form_model, allowed_actions=('accept',))
        values = decode_form_values(model, 'accept', self.form._raw_values())
        ids = tuple(values['selected_roi_ids'])
        choices = tuple(row['roi_id'] for row in self.baseline['roi_choices'])
        if not ids or len(set(ids)) != len(ids) or any(value not in choices for value in ids):
            raise ValueError('Select at least one of the saved positive regions, without duplicates.')
        # The immutable export defines the scientific selection order.
        values['selected_roi_ids'] = tuple(value for value in choices if value in ids)
        CompleteVectorQcPolicy(ProcessingChunkShape(values['reference_setting_0'], values['reference_setting_1']),
                               values['reference_setting_2'], values['reference_setting_3'])
        return values

    def save(self, *, edit=False):
        try:
            values = self.values()
        except (ValueError, TypeError, KeyError) as error:
            self.error.setText(str(error)); return
        self.error.clear()
        baseline = {'selected_roi_ids': tuple(self.baseline['selected_roi_ids']),
                    **{f'reference_setting_{i}': value for i, value in enumerate(self.baseline['sampling_values'])}}
        if not edit and values == baseline:
            self.returned.emit()
        else:
            self.changed.emit(values, edit)
