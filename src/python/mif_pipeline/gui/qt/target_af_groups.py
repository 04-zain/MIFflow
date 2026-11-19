"""Compose final AF references from retained control evidence."""
from copy import deepcopy
from collections.abc import Mapping
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHeaderView, QLayout, QMenu,
    QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton,
    QToolTip, QVBoxLayout, QWidget)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from .af_composition import (AfCompositionEditor, AfValidationMessage, LEFT, final_af_region_label,
                             region_curve_style, style_af_axes)
from .af_spectral_export import export_af_spectrum
from .reference_workspace import reference_table, text_label
from .researcher_widgets import configure_disclosure
from .spectral_review import SpectralLegend, show_spectral_hover
from mif_pipeline.preprocessing.unmixing.spectral_plots import label_spectral_axis, plot_spectral_profile, spectral_axis


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


class TargetAfGroupsPanel(QWidget):
    """Selections contain offered identities and decisions, never spectral data."""
    changed = Signal()
    library_export_requested = Signal()

    def __init__(self, inventory, parent=None):
        super().__init__(parent)
        self.setObjectName('target-af-groups-panel')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._inventory = inventory = _plain(inventory)
        self._names = tuple(inventory['measurement_names'])
        self._sources = {row['input_id']: row for row in inventory['sources']}
        self._source_ordinals = {identity: index + 1 for index, identity in enumerate(self._sources)}
        self._selection = deepcopy(inventory['selection'])
        self._group_names = list(dict.fromkeys(row['component_identity'] for row in
            self._selection['regions'] if row['component_identity']))
        if not self._group_names:
            self._group_names.append(self._next_group_name())
        self._updating = False
        self._hidden_curves = set()
        self._curve_keys = {}
        self._projection = inventory
        self.can_continue = False
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        regions = [dict(region,
            source_label=(f"Control {self._source_ordinals[region['input_id']]} · " if len(self._sources) > 1 else '') +
                self._sources[region['input_id']]['label'],
            source_description=self._role(self._sources[region['input_id']]) + '\n' +
                self._sources[region['input_id']]['filename'] + '\nInput: ' + region['input_id'] + '\n' +
                '; '.join(self._sources[region['input_id']].get('incompatibility_reasons', ())),
            usable=region['usable'] and self._sources[region['input_id']]['acquisition_compatible']) for region in inventory['regions']]
        self.composition = AfCompositionEditor(regions, {name: name for name in self._group_names},
            {row['roi_id']: row['component_identity'] if row['included'] else '' for row in self._selection['regions']},
            self, band_count=len(self._names), group_label='AF reference', rename=True,
            all_groups=True, separate_sources=True)
        self.group_selector, self.region_table = self.composition.selector, self.composition.table
        self.group_selector.setObjectName('target-af-group-filter')
        self.region_table.setObjectName('target-af-group-regions')
        self.region_table.setColumnHidden(6, True)  # No target angle is supplied by the service projection.
        self.region_table.setColumnHidden(3, True)  # Counts remain in each region's tooltip and exact details.
        self.region_table.setColumnHidden(7, len(self._sources) == 1)
        if len(self._sources) == 1:
            self.region_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.composition.table_height_limit = 216
        self.composition.legend_height_limit = 72
        self.group_editors = self.composition.editors
        layout.addWidget(self.composition)
        figure = Figure(figsize=(9, 3.3), layout='constrained')
        figure.set_facecolor('none')
        self._axes = figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(figure)
        self.canvas.setObjectName('target-af-group-spectral-canvas')
        self.canvas.setMinimumWidth(0)
        self.canvas.setFixedHeight(270)
        self.canvas.setStyleSheet('background: transparent;')
        self.composition.plot_layout.addWidget(self.canvas)
        self.legend = SpectralLegend(self.canvas, self, changed=self._curve_visibility)
        self.composition.set_legend(self.legend)
        self.group_summary = text_label('', self, supporting=True)
        self.group_summary.setObjectName('target-af-group-contributions')
        self.group_summary.hide()
        if len(self._sources) == 1:
            source = next(iter(self._sources.values()))
            source_label = text_label(source['label'], self, supporting=True)
            source_label.setToolTip(self._role(source) + '\n' + source['filename'] + '\nInput: ' + source['input_id'])
            layout.insertWidget(0, source_label)
        self.composition.changed.connect(self._composition_changed)
        self.composition.group_selected.connect(self._draw)
        self.composition.region_selected.connect(self._select_region)
        self.composition.new_requested.connect(self._new_group)
        self.composition.rename_requested.connect(self._rename_selected)
        self.composition.name_validation_requested.connect(self._validate_name_draft)
        self.composition.name_error_changed.connect(self._name_validation_changed)
        self.composition.name_draft_changed.connect(lambda: self._name_validation_changed(
            self.composition.name_error.text()))
        self.composition.merge_requested.connect(self.merge_groups)
        self._reviewed_sets = []
        for source_set in inventory.get('extraction_sets', ()):
            members = tuple(source_set['roi_ids'])
            identity = source_set.get('set_id')
            prior = next((row for row in self._reviewed_sets if
                (identity and (row.get('input_id'), row.get('set_id')) ==
                              (source_set.get('input_id'), identity)) or
                (not identity and not row.get('set_id') and set(row['roi_ids']) == set(members))), None)
            if prior is not None:
                continue
            self._reviewed_sets.append(dict(source_set,
                label=source_set.get('label') or f'Prepared set {len(self._reviewed_sets) + 1}',
                roi_ids=members))
        self.export_menu = QMenu(self.composition.export_button)
        self.spectrum_export_action = self.export_menu.addAction('Shown spectra…', self._export_spectrum)
        self.library_export_action = self.export_menu.addAction('Reference library…', self.library_export_requested.emit)
        self.library_export_action.setVisible(False)
        self.composition.export_button.setText('Export')
        self.composition.export_button.setMenu(self.export_menu)
        self.status = AfValidationMessage(self)
        self.status.setObjectName('target-af-group-validation')
        layout.addWidget(self.status)
        self.details_toggle = QToolButton(self)
        self.details_toggle.setObjectName('target-af-group-details')
        self.details_toggle.setText('Band values and calculation details')
        self.details_toggle.setCheckable(True)
        configure_disclosure(self.details_toggle)
        layout.addWidget(self.details_toggle)
        details = QWidget(self)
        body = QVBoxLayout(details)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        body.addWidget(text_label('Each final AF group averages the signed, background-corrected regional means equally, '
            'then clips negative values and normalizes the result to sum to one (L1). Prepared extraction sets do not '
            'determine these final groups.', details, supporting=True))
        self.details_selector = QComboBox(details)
        self.details_selector.setAccessibleName('Region whose exact measurements are shown')
        for region in inventory['regions']:
            self.details_selector.addItem(self._sources[region['input_id']]['label'] + ' · ' + region['label'], region['roi_id'])
        body.addWidget(self.details_selector)
        self.details_description = text_label('', details, supporting=True)
        body.addWidget(self.details_description)
        self.band_table = QTableWidget(len(self._names), 4, details)
        self.band_table.setHorizontalHeaderLabels(('Band', 'After subtraction', 'After clipping', 'L1-normalized'))
        reference_table(self.band_table)
        self.band_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.band_table.horizontalHeader().setDefaultAlignment(LEFT)
        self.band_table.setFixedHeight(min(270, 44 + 40 * len(self._names)))
        body.addWidget(self.band_table)
        layout.addWidget(details)
        details.hide()
        self.details_toggle.toggled.connect(details.setVisible)
        self.details_selector.currentIndexChanged.connect(self._show_details)
        self.canvas.mpl_connect('motion_notify_event', self._hover)
        self.canvas.mpl_connect('figure_leave_event', lambda _: QToolTip.hideText())
        self.set_value(self._selection)

    def configure_library_export(self, available, enabled):
        """The enclosing review owns accepted-library availability and routing."""
        self.library_export_action.setVisible(available)
        self.library_export_action.setEnabled(available and enabled)

    @staticmethod
    def _role(source):
        role = source['origin_role']
        if role in {'single_stain_negative_control', 'negative', 'negative_control'}:
            dyes = ', '.join(source.get('fluorophores', ()))
            return 'AF control for ' + dyes if dyes else 'Separate AF control'
        return {'shared_unstained_control': 'Shared AF control', 'unstained_control': 'Shared AF control',
                'target_unstained_control': 'Control for mixed image', 'af': 'AF control'}.get(role, role.replace('_', ' '))

    def value(self, *, validate=True):
        if validate and not self.can_continue:
            message = self.composition.name_error.text()
            if not message and self.composition.has_pending_name():
                message = 'Apply or cancel the reference name edit.'
            raise ValueError(message or self.status.text()
                             or 'Choose usable AF regions for the final reference.')
        return deepcopy(self._selection)

    def set_value(self, selection):
        self._selection = _plain(selection)
        for row in self._selection['regions']:
            if row['component_identity'] and row['component_identity'] not in self._group_names:
                self._group_names.append(row['component_identity'])
        self._sync_editors()
        self._recompute()

    def _sync_editors(self):
        self._updating = True
        self.composition.set_memberships({name: name for name in self._group_names},
            {row['roi_id']: row['component_identity'] if row['included'] else '' for row in self._selection['regions']})
        self._updating = False

    def _composition_changed(self):
        if self._updating:
            return
        memberships = self.composition.memberships()
        for row in self._selection['regions']:
            group = memberships[row['roi_id']]
            row['included'] = bool(group)
            if group:
                row['component_identity'] = group
        self._sync_editors()
        self._recompute()

    def assign_set(self, index, group):
        if group not in self._group_names:
            raise ValueError('Choose an existing AF group.')
        members = set(self._reviewed_sets[index]['roi_ids'])
        if any(not row['usable'] or not self._sources[row['input_id']]['acquisition_compatible']
               for row in self._inventory['regions'] if row['roi_id'] in members):
            raise ValueError('This set contains unusable regions. Choose usable members individually.')
        for row in self._selection['regions']:
            if row['roi_id'] in members:
                row.update(included=True, component_identity=group)
        self._sync_editors()
        self.group_selector.setCurrentIndex(self.group_selector.findData(group))
        self._recompute()

    def _next_group_name(self):
        index = 1
        reserved = {name.casefold() for name in (*self._group_names, *self._inventory.get('fluorophore_identities', ()))}
        while f'AF {index}'.casefold() in reserved:
            index += 1
        return f'AF {index}'

    def _validate_name(self, name, *, old=None):
        name = name.strip()
        reserved = {item.casefold() for item in self._inventory.get('fluorophore_identities', ())}
        existing = {item.casefold() for item in self._group_names if item != old}
        if not name or name.casefold() in reserved or name.casefold() in existing:
            raise ValueError('Use a unique AF reference name, different from the fluorophore names.')
        return name

    def _new_group(self):
        name = self._next_group_name()
        self._group_names.append(name)
        self._sync_editors()
        self.group_selector.setCurrentIndex(self.group_selector.findData(name))
        return name

    def rename_group(self, old, new):
        new = self._validate_name(new, old=old)
        self._group_names[self._group_names.index(old)] = new
        for row in self._selection['regions']:
            if row['component_identity'] == old:
                row['component_identity'] = new
        self._sync_editors()
        self.group_selector.setCurrentIndex(self.group_selector.findData(new))
        self._recompute()

    def _rename_selected(self, old, new):
        selected = self.group_selector.currentData()
        try:
            self.rename_group(old, new)
        except ValueError as exc:
            self.composition.set_name_error(str(exc))
        else:
            if selected != old and self.group_selector.findData(selected) >= 0:
                self.group_selector.setCurrentIndex(self.group_selector.findData(selected))
            self.composition.set_name_error('')

    def _validate_name_draft(self, old, new):
        try:
            self._validate_name(new, old=old)
        except ValueError as exc:
            self.composition.set_name_validation(str(exc))
        else:
            self.composition.set_name_validation('')

    def _name_validation_changed(self, message):
        self.can_continue = (bool(self._projection['can_apply']) and not message
                             and not self.composition.has_pending_name())
        self.changed.emit()

    def merge_groups(self, source, target):
        if source == target or source not in self._group_names or target not in self._group_names:
            raise ValueError('Choose two different AF groups to merge.')
        for row in self._selection['regions']:
            if row['component_identity'] == source:
                row['component_identity'] = target
        self._group_names.remove(source)
        self._sync_editors()
        self.group_selector.setCurrentIndex(self.group_selector.findData(target))
        self._recompute()

    def _recompute(self):
        from mif_pipeline.preprocessing.unmixing.reference_service_af_groups import project_target_af_groups_selection
        # Selecting regions for a final AF reference is one explicit target-use
        # decision. Encode that decision in the legacy source fields only for
        # sources represented in this selection; extraction history is irrelevant.
        chosen = {row['roi_id'] for row in self._selection['regions']
                  if row['included'] and row['component_identity']}
        used_sources = {row['input_id'] for row in self._inventory['regions'] if row['roi_id'] in chosen}
        for source in self._selection['sources']:
            disposition = 'accepted' if source['input_id'] in used_sources else 'unreviewed'
            source.update(preparation_disposition=disposition, tissue_context_disposition=disposition)
        try:
            self._projection = project_target_af_groups_selection(self._inventory, self._selection)
        except (ValueError, TypeError) as exc:
            self._projection = {**self._inventory, 'components': [], 'can_apply': False, 'issues': [str(exc)]}
        self.can_continue = (bool(self._projection['can_apply']) and not self.composition.name_error.text()
                             and not self.composition.has_pending_name())
        issues = self._projection.get('issues', ())
        self.status.setText('\n'.join(self._display_issues(issues)))
        self.status.setToolTip('\n'.join(str(issue) for issue in issues))
        self.status.setVisible(bool(issues))
        weights = {roi_id: weight for group in self._projection.get('components', ())
                   for roi_id, weight in zip(group['roi_ids'], group['roi_weights'], strict=True)}
        self.composition.set_evidence(self._inventory['regions'], weights=weights)
        self._draw()
        self._show_details()
        self.changed.emit()

    @staticmethod
    def _display_issues(issues):
        """A named separation failure already explains the generic QC summary."""
        messages = [('Assign at least one usable region to an AF reference.'
                     if str(issue) == 'Include at least one usable region and assign an AF group.'
                     else str(issue)) for issue in issues]
        if not any('are too similar to use as separate references.' in text for text in messages):
            return messages
        prefix = 'The AF groups fail spectral quality checks: '
        return [text for text in messages if not (text.startswith(prefix) and
            set(text.removeprefix(prefix).removesuffix('.').split(', ')) <=
            {'unstable separation', 'groups cannot be separated'})]

    def _curve_visibility(self, label, visible):
        key = self._curve_keys.get(label)
        if key is None:
            return
        if visible:
            self._hidden_curves.discard(key)
        else:
            self._hidden_curves.add(key)
        self._describe_legend()

    def _export_spectrum(self):
        description = ['Final autofluorescence reference']
        visible_labels = {line.get_label() for line in self._axes.lines if line.get_visible()}
        contributors = set()
        for group in self._projection.get('components', ()):
            if group['component'] + ' · combined' in visible_labels:
                contributors.update(group['roi_ids'])
                description.append(group['component'] + ': ' + ', '.join(
                    f'{roi_id} (weight {weight:.8g})' for roi_id, weight in
                    zip(group['roi_ids'], group['roi_weights'], strict=True)))
        for index, line in self._region_lines.items():
            region = self._inventory['regions'][index]
            if line.get_visible() or region['roi_id'] in contributors:
                description.append(f"{line.get_label()}: {region['filename']} · {region['label']}; "
                                   f"input {region['input_id']}; ROI {region['roi_id']}")
        return export_af_spectrum(self, self._axes, review=self._inventory, default_name='final-af-reference',
            description='\n'.join(description),
            region_indices={line.get_label(): index for index, line in self._region_lines.items()})

    def _describe_legend(self):
        descriptions = {line.get_label(): label for line, label, *_ in self._hover_profiles}
        for button, line, *_ in self.legend._entries:
            label = descriptions[line.get_label()]
            button.setToolTip(('Hide ' if line.get_visible() else 'Show ') + label + ' (display only)')
            button.setAccessibleDescription(label)

    def _draw(self, *_):
        if self._updating:
            return
        selected = self.group_selector.currentData() or ''
        self._axes.clear()
        self._hover_profiles, self._region_lines = [], {}
        self._curve_keys = {}
        x, optical_groups, unit = spectral_axis(self._names,
            wavelengths=self._inventory.get('measurement_wavelengths'),
            wavelength_unit=self._inventory.get('wavelength_unit'),
            optical_settings=self._inventory.get('measurement_optical_settings'))
        decisions = {row['roi_id']: row for row in self._selection['regions']}
        colours = ('#155e75', '#9c4b14', '#6256a3', '#32704a', '#a14162')
        summaries = []
        for group in self._projection.get('components', ()):
            if selected and selected != group['component']:
                continue
            values = group.get('values')
            if values is not None and len(values):
                colour = colours[self._group_names.index(group['component']) % len(colours)]
                label = group['component'] + ' · combined'
                self._curve_keys[label] = ('component', group['component'])
                line = plot_spectral_profile(self._axes, x, values, optical_groups, ordinal=True,
                    label=label, color=colour, linewidth=2.8, zorder=5)
                self._hover_profiles.append((line, label, values, values))
            contributions = group.get('source_contributions', ())
            total = len(group['roi_ids'])
            summary = f"{group['component']} · " + '; '.join(
                f"{self._sources[c['input_id']]['label']}: {c['region_count']} of {total} regions ({c['weight']:.0%})"
                for c in contributions)
            if len(contributions) > 1:
                summaries.append(summary)
        for index, region in enumerate(self._inventory['regions']):
            choice = decisions[region['roi_id']]
            if selected == '__excluded__' and choice['included']:
                continue
            if selected and selected != '__excluded__' and (not choice['included'] or choice['component_identity'] != selected):
                continue
            values = region.get('values')
            if values is None or not len(values):
                continue
            included = choice['included']
            group = choice['component_identity']
            label = final_af_region_label(region['label'])
            if len(self._sources) > 1:
                label = f"Control {self._source_ordinals[region['input_id']]} · {label}"
            elif label.startswith('Region '):
                label = 'AF region ' + label.removeprefix('Region ')
            if not included:
                label += ' · excluded'
            self._curve_keys[label] = ('region', region['input_id'], region['roi_id'])
            line = plot_spectral_profile(self._axes, x, values, optical_groups, ordinal=True,
                label=label, linewidth=1.3, alpha=.65 if included else .4,
                **region_curve_style(index))
            self._region_lines[index] = line
            description = self._sources[region['input_id']]['label'] + ' · ' + region['label']
            if not included:
                description += ' · excluded'
            self._hover_profiles.append((line, description, values, values))
        label_spectral_axis(self._axes, self._names, x, optical_groups, unit if optical_groups else None, ordinal=True)
        self._axes.set_ylabel('Fraction of total\nreference signal')
        if unit and not optical_groups:
            self._axes.set_xlabel(f'Acquired band (detection wavelengths: {min(x):g}–{max(x):g} {unit})')
        self._axes.set_ylim(bottom=0)
        if not self._hover_profiles:
            self._axes.text(.5, .5, 'No usable AF spectrum in this view', ha='center', va='center',
                            transform=self._axes.transAxes, color='#536878')
        style_af_axes(self._axes, self)
        for line, *_ in self._hover_profiles:
            line.set_visible(self._curve_keys[line.get_label()] not in self._hidden_curves)
        self.legend.set_lines([entry[0] for entry in self._hover_profiles])
        self._describe_legend()
        self.composition.fit_legend()
        self.group_summary.setText('\n'.join(summaries))
        self.composition.summary.setToolTip('\n'.join(summaries))
        self._emphasize(self.region_table.currentRow())
        self.canvas.draw_idle()

    def _emphasize(self, row):
        for index, line in self._region_lines.items():
            line.set_linewidth(2.3 if index == row else 1.3)
            line.set_alpha(1 if index == row else .45)
        self.canvas.draw_idle()

    def _select_region(self, roi_id):
        row = next(index for index, region in enumerate(self._inventory['regions']) if region['roi_id'] == roi_id)
        self._emphasize(row)
        self.details_selector.setCurrentIndex(row)

    def _show_details(self, *_):
        index = self.details_selector.currentIndex()
        if index < 0:
            return
        region = self._inventory['regions'][index]
        signal = region.get('corrected_l2')
        self.details_description.setText(f"{region['filename']}\n"
            f"Accepted pixels: {region.get('accepted_pixel_count', '—')} · "
            f"Signal (L2): {signal:.6g} · " if signal is not None else region['filename'] + '\n')
        self.details_description.setText(self.details_description.text() +
            f"Clipped bands: {region.get('clipped_count', '—')} / {len(self._names)}")
        arrays = [region.get(key) if region.get(key) is not None else [None] * len(self._names)
                  for key in ('signed_values', 'corrected_values', 'values')]
        for row, entries in enumerate(zip(self._names, *arrays, strict=True)):
            for column, value in enumerate(entries):
                item = QTableWidgetItem(str(value) if column == 0 else '—' if value is None else f'{value:.8g}')
                item.setToolTip(str(value))
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.band_table.setItem(row, column, item)

    def _hover(self, event):
        show_spectral_hover(self.canvas, self._axes, event, self._names,
                            np.arange(1, len(self._names) + 1), self._hover_profiles)
        if event.inaxes is not self._axes:
            return
        nearest = None
        for row, line in self._region_lines.items():
            if not line.get_visible():
                continue
            values = self._inventory['regions'][row]['values']
            points = self._axes.transData.transform(np.column_stack((np.arange(1, len(self._names) + 1), values)))
            distance = float(np.min(np.sum((points - [event.x, event.y]) ** 2, axis=1)))
            if distance <= 81 and (nearest is None or distance < nearest[0]):
                nearest = distance, row
        if nearest is not None and self.region_table.currentRow() != nearest[1]:
            self.region_table.selectRow(nearest[1])
