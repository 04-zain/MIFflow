"""Source-review behavior for the existing AF spectrum panel."""
import numpy as np
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QHeaderView, QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton, QToolTip, QVBoxLayout, QWidget
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from .af_composition import AfCompositionEditor, AfValidationMessage, LEFT, region_curve_style, style_af_axes
from .af_spectral_export import export_af_spectrum
from .reference_workspace import INTENSITY_UNITS, reference_table, text_label
from .researcher_widgets import ContentSizedButton, configure_disclosure
from .spectral_review import SpectralLegend, show_spectral_hover
from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis, plot_spectral_profile, label_spectral_axis


class ControlSourceComposition:
    """Concrete source composition; scientific estimates remain in the service."""

    def _init_source_composition(self, review, *, editable=False):
        self._review = review
        self._source_review, self._target, self._matched_negative = True, False, False
        self._names = tuple(review['measurement_names'])
        self._sets = tuple(review['sets'])
        self._regions = tuple(review['regions'])
        self._source_by_id = {row['roi_id']: row for row in self._regions}
        self._original_sets = [list(row['roi_ids']) for row in self._sets]
        self._source_projection = review
        self._source_group_sets = {row['set_id']: row for row in self._sets}
        self._selected_roi = None
        self._source_hidden_curves = set()
        self._revision_option = None
        self._combined_label = 'Combined AF spectrum'
        self.can_continue = bool(review.get('can_accept', True))
        self.composition_error = ''
        groups = {row['set_id']: row['label'] for row in self._sets}
        memberships = {row['roi_id']: next((group['set_id'] for group in self._sets
            if row['roi_id'] in group['roi_ids']), '') for row in self._regions}
        self._overlapping_ids = {row['roi_id'] for row in self._regions if
            sum(row['roi_id'] in group['roi_ids'] for group in self._sets) > 1}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        identity = QHBoxLayout()
        self.source = text_label(review['control_filename'], self, supporting=True)
        role = ('AF control for ' + review.get('source_link_id', '')
                if review.get('source_role') in {'negative_control', 'single_stain_negative_control'}
                else 'Shared AF control')
        self.source.setToolTip(role + '\n' + review['control_filename'])
        self.source_role = text_label(role + ' ·', self, supporting=True)
        self.source_role.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        identity.addWidget(self.source_role)
        identity.addWidget(self.source, 1)
        self.edit_regions_button = ContentSizedButton('Back: regions', self)
        self.edit_regions_button.setObjectName('reference-source-regions-back')
        self.edit_regions_button.setProperty('actionRole', 'secondary')
        self.edit_regions_button.clicked.connect(lambda: self.revision_requested.emit(self._revision_option)
            if self._revision_option else None)
        self.edit_regions_button.hide()
        layout.addLayout(identity)
        self.composition = AfCompositionEditor(self._regions, groups, memberships, self,
            band_count=len(self._names), editable=editable, all_groups=True)
        self.table, self.set_selector = self.composition.table, self.composition.selector
        if groups:
            self.set_selector.setCurrentIndex(self.set_selector.findData(next(iter(groups))))
        for identity in self._overlapping_ids:
            editor = self.composition.editors[identity]
            with QSignalBlocker(editor):
                editor.addItem('Multiple sets', '__legacy_overlap__')
                editor.setCurrentIndex(editor.count() - 1)
            editor.setEnabled(False)
            editor.setToolTip(', '.join(group['label'] for group in self._sets if identity in group['roi_ids']))
        self.table.setObjectName('af-control-regional-evidence')
        self.set_selector.setObjectName('af-spectrum-control-set')
        layout.addWidget(self.composition)
        figure = Figure(figsize=(9, 3.1), layout='constrained')
        figure.set_facecolor('none')
        self._axes = figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setMinimumWidth(0)
        self.canvas.setFixedHeight(270)
        self.canvas.setStyleSheet('background: transparent;')
        self.composition.plot_layout.addWidget(self.canvas)
        self.legend = SpectralLegend(self.canvas, self, changed=self._source_curve_visibility)
        self.composition.set_legend(self.legend)
        self.composition.export_button.clicked.connect(self._source_export_spectrum)
        self.status = AfValidationMessage(self)
        layout.addWidget(self.status)
        self.details_toggle = QToolButton(self)
        self.details_toggle.setText('Band values and calculation details')
        self.details_toggle.setObjectName('af-control-band-values')
        self.details_toggle.setCheckable(True)
        configure_disclosure(self.details_toggle)
        layout.addWidget(self.details_toggle)
        details = QWidget(self)
        body = QVBoxLayout(details)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        self.method = text_label('Each regional curve shows background subtraction, zero clipping and unit L2 '
            'normalization. The combined curve averages the signed regional means equally, then clips negative '
            'values and normalizes to unit L2 length.', details, supporting=True)
        body.addWidget(self.method)
        controls = QHBoxLayout()
        self.region_selector = QComboBox(details)
        self.region_selector.setAccessibleName('Spectrum whose exact band values are shown')
        self.region_selector.addItem(self._combined_label, -1)
        for index, row in enumerate(self._regions):
            self.region_selector.addItem(row['label'].replace('Autofluorescence region', 'AF region'), index)
        controls.addWidget(self.region_selector, 1)
        self.plot_view = QComboBox(details)
        self.plot_view.addItem('L2-normalized spectrum', 'shape')
        self.plot_view.addItem('Background-corrected signal', 'signal')
        self.plot_view.setAccessibleName('AF spectrum display')
        controls.addWidget(self.plot_view)
        self.combined_button = ContentSizedButton('Combined spectrum', details)
        self.combined_button.clicked.connect(self._source_show_combined)
        controls.addWidget(self.combined_button)
        body.addLayout(controls)
        self.details_source = text_label('', details, supporting=True)
        body.addWidget(self.details_source)
        self.band_table = QTableWidget(len(self._names), 4, details)
        self.band_table.setHorizontalHeaderLabels(('Acquired band', 'After subtraction', 'After clipping', 'Normalized'))
        reference_table(self.band_table)
        self.band_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.band_table.horizontalHeader().setDefaultAlignment(LEFT)
        self.band_table.setFixedHeight(min(270, 44 + 40 * len(self._names)))
        body.addWidget(self.band_table)
        layout.addWidget(details)
        layout.addWidget(self.edit_regions_button, alignment=Qt.AlignmentFlag.AlignLeft)
        details.hide()
        self.details_toggle.toggled.connect(details.setVisible)
        self.composition.new_requested.connect(self._source_new_set)
        self.composition.merge_requested.connect(self.composition.merge_groups)
        self.composition.changed.connect(self._source_membership_changed)
        self.composition.group_selected.connect(self._source_choose_group)
        self.composition.region_selected.connect(self._source_select_region)
        self.region_selector.currentIndexChanged.connect(self._source_details_choice)
        self.plot_view.currentIndexChanged.connect(self._source_draw)
        self.canvas.mpl_connect('motion_notify_event', lambda event: show_spectral_hover(
            self.canvas, self._axes, event, self._names, np.arange(1, len(self._names) + 1), self._hover_profiles))
        self.canvas.mpl_connect('figure_leave_event', lambda _: QToolTip.hideText())
        self._source_refresh_evidence()
        self._source_choose_group(self.set_selector.currentData())

    @property
    def composition_changed(self):
        return self.control_sets() != self._original_sets

    def _source_curve_visibility(self, label, visible):
        key = self._source_curve_keys[label]
        if visible:
            self._source_hidden_curves.discard(key)
        else:
            self._source_hidden_curves.add(key)

    def _source_export_spectrum(self):
        description = [self._review['control_filename'] + ' · ' + self.set_selector.currentText()]
        if self._review.get('input_id'):
            description.append('Source input: ' + self._review['input_id'])
        indices = {line.get_label(): index for index, line in self._region_lines.items() if line is not None}
        contributors = set()
        for line in self._axes.lines:
            if not line.get_visible():
                continue
            context = self._source_curve_context[line.get_label()]
            description.append(line.get_label())
            if context.get('set_id'):
                description.append('AF set: ' + context['set_id'])
            roi_ids = context.get('roi_ids', (context.get('roi_id'),))
            contributors.update(roi_id for roi_id in roi_ids if roi_id)
            if context.get('roi_weights'):
                description.extend(f'{roi_id} (weight {weight:.8g})' for roi_id, weight in
                                   zip(roi_ids, context['roi_weights'], strict=True))
        description.extend(f"{row['label']}; ROI {row['roi_id']}" for row in self._regions
                           if row['roi_id'] in contributors)
        return export_af_spectrum(self, self._axes, review=self._review, default_name='af-control-spectrum',
                                  description='\n'.join(description), region_indices=indices)

    def control_sets(self):
        return self.composition.grouped_roi_ids()

    def set_control_sets(self, groups):
        from mif_pipeline.preprocessing.unmixing.reference_service_control_preparation import project_control_preparation_sets
        preview = project_control_preparation_sets(self._review, groups)
        mapping = {row['set_id']: row['label'] for row in preview['sets']}
        memberships = {row['roi_id']: next((group['set_id'] for group in preview['sets']
            if row['roi_id'] in group['roi_ids']), '') for row in self._regions}
        self.composition.set_memberships(mapping, memberships)
        self._source_membership_changed()

    def _source_new_set(self):
        groups = dict(self.composition._groups)
        index = 1
        while f'local-set-{index}' in groups:
            index += 1
        identity = f'local-set-{index}'
        groups[identity] = f'AF set {len(groups) + 1}'
        self.composition.set_memberships(groups, self.composition.memberships())
        self.set_selector.setCurrentIndex(self.set_selector.findData(identity))

    def _source_membership_changed(self):
        from mif_pipeline.preprocessing.unmixing.reference_service_control_preparation import project_control_preparation_sets
        try:
            self._source_projection = project_control_preparation_sets(self._review, self.control_sets())
            self.composition_error = ''
        except (TypeError, ValueError) as exc:
            self.composition_error = str(exc)
            self._source_projection = {**self._review, 'sets': (), 'can_accept': False}
        keys = [key for key in self.composition._groups if key in self.composition.memberships().values()]
        self._source_group_sets = dict(zip(keys, self._source_projection['sets']))
        self._sets = tuple(self._source_projection['sets'])
        # Keep empty draft sets available while the researcher assigns regions.
        # Only nonempty groups enter the existing scientific projection and form value.
        self.composition.set_memberships({key: f'AF set {index + 1}'
            for index, key in enumerate(self.composition._groups)}, self.composition.memberships())
        self.can_continue = bool(self._sets) and all(row.get('status') == 'pass' for row in self._sets)
        self._source_refresh_evidence()
        self._source_draw()
        self.changed.emit()

    def _source_refresh_evidence(self):
        weights, angles = {}, {}
        for group in self._source_projection.get('sets', ()):
            weights.update(zip(group['roi_ids'], group['roi_weights'], strict=True))
            angles.update({row['roi_id']: row.get('angle_to_combined_degrees') for row in group['regions']})
        for identity in self._overlapping_ids:
            weights.pop(identity, None)
            angles[identity] = None
        self.composition.set_evidence(self._regions, weights=weights, angles=angles)
        failures = [row.get('diagnostic') or 'Insufficient AF signal' for row in self._source_projection.get('sets', ())
                    if row.get('status') == 'failed']
        self.status.setText(self.composition_error or '\n'.join(dict.fromkeys(failures)))
        self.status.setVisible(bool(self.status.text()))

    def _source_choose_group(self, _identity):
        self._selected_roi = None
        self.region_selector.blockSignals(True)
        self.region_selector.setCurrentIndex(0)
        self.region_selector.blockSignals(False)
        self._source_draw()

    def _source_select_region(self, identity):
        self._selected_roi = identity
        self.region_selector.blockSignals(True)
        self.region_selector.setCurrentIndex(tuple(self._source_by_id).index(identity) + 1)
        self.region_selector.blockSignals(False)
        self._source_draw()

    def _source_details_choice(self, _index):
        index = self.region_selector.currentData()
        if index in (None, -1):
            self._source_show_combined()
        else:
            self.table.setCurrentCell(index, 0)
            self._source_select_region(self._regions[index]['roi_id'])

    def _source_show_combined(self):
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self._source_choose_group(self.set_selector.currentData())

    def _source_draw(self, *_):
        selected_group = self.set_selector.currentData()
        comparing = selected_group == '' and len(self.composition._groups) > 1
        group = self._source_group_sets.get(selected_group)
        self._current = group or {'values': None, 'signed_values': None, 'corrected_values': None, 'regions': ()}
        chosen = self._source_by_id.get(self._selected_roi, self._current)
        self._axes.clear()
        self._hover_profiles, self._region_lines = [], {}
        self._source_curve_context = {}
        self._source_curve_keys = {}
        self.plot_view.setEnabled(not comparing)
        self.plot_view.setVisible(not comparing)
        self.combined_button.setEnabled(not comparing and self._selected_roi is not None)
        self.plot_view.setToolTip('All sets compares their L2-normalized combined spectra.' if comparing else '')
        x, optical, unit = spectral_axis(self._names, wavelengths=self._review.get('measurement_wavelengths'),
            wavelength_unit=self._review.get('wavelength_unit'), optical_settings=self._review.get('measurement_optical_settings'))
        def add(values, label, context, key, **style):
            if values is None:
                return None
            line = plot_spectral_profile(self._axes, x, values, optical, ordinal=True, label=label, **style)
            line.set_visible(key not in self._source_hidden_curves)
            self._source_curve_context[label] = context
            self._source_curve_keys[label] = key
            self._hover_profiles.append((line, label, values, values))
            return line
        if comparing:
            colours = ('#155e75', '#9c4b14', '#6256a3', '#32704a', '#a14162')
            for index, (identity, projected) in enumerate(self._source_group_sets.items()):
                add(projected['values'], self.composition._groups[identity] + ' · combined', projected,
                    ('group', identity),
                    color=colours[index % len(colours)], linewidth=2.6, zorder=5)
            self._axes.set_ylabel('AF signal\n(L2-normalized)')
            self._axes.set_ylim(bottom=0)
        elif self.plot_view.currentData() == 'signal':
            add(chosen['signed_values'], 'After subtraction (signed)', chosen,
                ('signed', self._selected_roi or selected_group),
                color='#5b7c8c', linewidth=2, linestyle='--')
            add(chosen['corrected_values'], 'After clipping', chosen,
                ('clipped', self._selected_roi or selected_group), color='#006b70', linewidth=2.4)
            self._axes.set_ylabel(INTENSITY_UNITS.get(self._review.get('correction_mode'), 'Corrected intensity'))
        else:
            if group:
                add(group['values'], self._combined_label, group, ('group', selected_group),
                    color='#164f5c', linewidth=2.6, zorder=5)
            members = set(group['roi_ids']) if group else {rid for rid, value in self.composition.memberships().items()
                if not value} if self.set_selector.currentData() == '__excluded__' else set()
            for index, region in enumerate(self._regions):
                if region['roi_id'] not in members:
                    continue
                selected = region['roi_id'] == self._selected_roi
                self._region_lines[index] = add(region['values'], region['label'].replace('Autofluorescence region', 'AF region'),
                    region, ('region', region['roi_id']), linewidth=2.3 if selected else 1.3,
                    alpha=1 if selected else .3 if self._selected_roi else .65, **region_curve_style(index))
            self._axes.set_ylabel('AF signal\n(L2-normalized)')
            self._axes.set_ylim(bottom=0)
        label_spectral_axis(self._axes, self._names, x, optical, unit if optical else None, ordinal=True)
        if unit and not optical:
            self._axes.set_xlabel(f'Acquired band (detection wavelengths: {min(x):g}–{max(x):g} {unit})')
        if not self._hover_profiles:
            self._axes.text(.5, .5, 'No usable AF spectrum in this selection', ha='center', va='center',
                            transform=self._axes.transAxes, color='#536878')
        style_af_axes(self._axes, self)
        self.legend.set_lines([row[0] for row in self._hover_profiles])
        self.composition.fit_legend()
        self.canvas.draw_idle()
        self._source_band_values(chosen)
        if comparing:
            self.details_source.setText('Select a set or region to inspect its exact band values.')

    def _source_band_values(self, region):
        units = INTENSITY_UNITS.get(self._review.get('correction_mode'), 'Corrected intensity')
        self.details_source.setText(units + '. Signed values are after image-background subtraction.')
        arrays = [region.get(key) if region.get(key) is not None else [None] * len(self._names)
                  for key in ('signed_values', 'corrected_values', 'values')]
        for row, values in enumerate(zip(self._names, *arrays, strict=True)):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value) if column == 0 else '—' if value is None else f'{value:.8g}')
                item.setTextAlignment(LEFT)
                item.setToolTip(repr(value))
                self.band_table.setItem(row, column, item)
