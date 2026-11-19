"""Local source review, with changes committed only through a new live prompt."""
from dataclasses import replace
from pathlib import Path
import csv
import io
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QFrame
from mif_pipeline.gui.interactions import FormModel, FormControl, ControlKind, decode_form_values
from mif_pipeline.gui.reference_copy import SOURCE_CHOICES, FIELD_COPY, SPECTRAL_CHOICE_LABELS
from mif_pipeline.preprocessing.unmixing.curated_library import CuratedSpectralLibrary, parse_spectral_csv
from mif_pipeline.preprocessing.unmixing.library_exchange import read_library_payload, load_library_exchange
from .forms import FormRenderer


class SavedSpectralSourceEditor(QWidget):
    returned = Signal()
    changed = Signal(object)

    def __init__(self, baseline, parent=None):
        super().__init__(parent)
        self.baseline = baseline
        saved = baseline.get('library')
        self.library = None if saved is None else CuratedSpectralLibrary(**{
            **saved, 'band_names': tuple(saved['band_names']),
            'component_names': tuple(saved['component_names']),
            'values': tuple(tuple(row) for row in saved['values'])})
        conventions = {
            'full': ('acquisition_normalized', 'target_raw_acquisition'),
            'exposure_only': ('exposure_normalized', 'target_raw_acquisition'),
            'as_stored': ('stored_calibrated',),
        }[baseline['correction_mode']]
        choices = ('curated_reference_library', 'phenoimager_motif_profile', 'construct_reference_library')
        controls = (
            FormControl('option_value', 'Spectral references', ControlKind.CHOICE,
                        value=baseline['source'], choices=choices),
            FormControl('library_locator', 'Spectral library', ControlKind.LOCAL_FILE, required=False),
            FormControl('spectral_csv', 'Spectral values', ControlKind.TEXT, required=False),
            FormControl('normalization', FIELD_COPY['normalization'][0], ControlKind.CHOICE,
                        choices=('l1', 'l2'), value=None if saved is None else saved['normalization'], required=False,
                        help_text=FIELD_COPY['normalization'][1]),
            FormControl('measurement_convention', FIELD_COPY['measurement_convention'][0], ControlKind.CHOICE,
                        choices=conventions, value=None if saved is None else saved['measurement_convention'], required=False,
                        help_text=FIELD_COPY['measurement_convention'][1]),
        )
        self.form_model = FormModel('saved_spectral_source', 'Review spectral references',
            'Review the accepted source. Keep it, edit the saved numerical table, or choose a replacement.',
            (), controls=controls, context={
                'spectral_input': baseline,
                'choice_labels': {'option_value': {key: SOURCE_CHOICES[key][0] for key in choices},
                    **SPECTRAL_CHOICE_LABELS}})
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        scroll = QScrollArea(self); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form = FormRenderer(parent=scroll); self.form.set_form(self.form_model)
        self.form.cancelled.connect(self.returned)
        for button in self.form.findChildren(QPushButton):
            if button.text() == 'Cancel': button.hide()
        scroll.setWidget(self.form); layout.addWidget(scroll, 1)
        if self.library is not None:
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(('measurement', *self.library.component_names))
            writer.writerows((name, *row) for name, row in zip(self.library.band_names, self.library.values))
            self.form.restore_values({'spectral_csv': output.getvalue()})
        self.notice = QLabel('Saving a changed source replaces the current spectral basis and repeats its dependent review. Returning or saving unchanged choices keeps the current review.', self)
        self.notice.setWordWrap(True)
        self.notice.setProperty("choiceHelp", True)
        self.notice.setContentsMargins(16, 0, 16, 0)
        layout.addWidget(self.notice)
        self.error = QLabel(self); self.error.setWordWrap(True)
        self.error.setProperty("validationMessage", True)
        self.error.setContentsMargins(16, 0, 16, 0)
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.setContentsMargins(16, 0, 16, 16)
        actions.setSpacing(12)
        back = QPushButton('Return to current review', self); back.setObjectName('saved-source-return')
        back.clicked.connect(self.returned); actions.addWidget(back)
        actions.addStretch(1)
        save = QPushButton('Save and next', self); save.setObjectName('saved-source-save')
        save.setProperty('actionRole', 'primary'); save.clicked.connect(self.save); actions.addWidget(save)
        layout.addLayout(actions)
        self._save_button = save
        self.form.page_changed.connect(self._sync_save_button)
        self.form._editors['option_value'].currentIndexChanged.connect(self._show_source_fields)
        self._show_source_fields()

    def _sync_save_button(self):
        curated = self.form._editors['option_value'].currentData() == 'curated_reference_library'
        self._save_button.setEnabled(not curated or self.form._spectral_step == 1)

    def _show_source_fields(self):
        numerical = self.form._editors['option_value'].currentData() == 'curated_reference_library'
        # The library field contains the shared importer and numerical table.
        self.form._spectral_choices.parentWidget().setVisible(numerical)
        self.form._apply_library_declarations()
        for name in ('normalization', 'measurement_convention'):
            label, field = self.form._library_declaration_rows[name]
            label.setVisible(numerical and self.form._spectral_step == 1)
            field.setVisible(numerical and self.form._spectral_step == 1)
        self.form._spectral_declarations.setVisible(numerical and self.form._spectral_step == 1)
        self.form._spectral_back.setVisible(numerical and self.form._spectral_step == 1)
        self.form._spectral_return.setVisible(numerical and self.form._spectral_step == 0 and self.form._spectral_opened)
        self._sync_save_button()

    def selection(self):
        choice = self.form._editors['option_value'].currentData()
        if choice in ('phenoimager_motif_profile', 'construct_reference_library'):
            return {'option_value': choice}, choice == self.baseline['source']
        values = decode_form_values(replace(self.form_model, allowed_actions=('accept',)),
                                    'accept', self.form._raw_values())
        path = values.get('library_locator')
        declarations = {name: values.get(name) for name in ('normalization', 'measurement_convention')}
        if values.get('spectral_csv'):
            library = parse_spectral_csv(values['spectral_csv'],
                band_names=tuple(self.baseline['band_names']),
                component_names=tuple(self.baseline['component_names']),
                allow_cycle_subsets=bool(self.baseline.get('shared_cycle_reference')), **declarations)
            return values, self.library is not None and library == self.library
        if not path:
            raise ValueError('Import a library file or enter the reference values in the table.')
        path = Path(path)
        if not path.is_absolute():
            raise ValueError('Choose an absolute library-file path.')
        payload = read_library_payload(path)
        if path.suffix.casefold() == '.csv':
            library = parse_spectral_csv(payload.decode('utf-8-sig'),
                band_names=tuple(self.baseline['band_names']),
                component_names=tuple(self.baseline['component_names']),
                allow_cycle_subsets=bool(self.baseline.get('shared_cycle_reference')), **declarations)
            unchanged = self.library is not None and library == self.library
        else:
            if not (path.name.casefold().endswith('.miflib.zip') or path.suffix.casefold() == '.json'):
                raise ValueError('Choose a .miflib.zip package, reviewed library JSON or spectral CSV.')
            library = load_library_exchange(payload)
            from mif_pipeline.preprocessing.unmixing.curated_library import _spectral_label_order
            from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
            _spectral_label_order(library.axis.parent_band_ids, tuple(self.baseline['band_names']))
            _spectral_label_order(tuple(item.fluorophore_identity for item in library.spectra),
                tuple(name for name in self.baseline['component_names']
                      if not is_autofluorescence_component(name)), components=True)
            unchanged = False
        return values, unchanged

    def save(self):
        try:
            values, unchanged = self.selection()
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.error.setText(str(error)); return
        self.error.clear()
        if unchanged:
            self.returned.emit()
        else:
            self.changed.emit(values)
