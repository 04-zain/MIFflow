"""Read final secondary decisions and memberships from the accepted cell handoff."""
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
import json

from .batch_service_models import BatchSavedStageFigure
from .saved_cell_figures import _accepted_handoff
from .region_review_geometry import geometry_measurements, _plain
from mif_pipeline.cell_analysis.spatial_handoff import FinalizedSecondaryDomainEvidence


def _domains(handoff):
    return {key: result for key, result in handoff.domain_results.items()
            if isinstance(result.finalization, FinalizedSecondaryDomainEvidence)}


def secondary_choices(authority):
    with _accepted_handoff(authority) as handoff:
        if not _domains(handoff):
            return ()
        return (
            BatchSavedStageFigure('secondary-structure-tables', 'Secondary structures · Outcomes and memberships',
                'Final decisions, criterion values, count limits and included-cell memberships.', data_formats=('csv',)),
            BatchSavedStageFigure('secondary-outlines', 'Secondary-structure outlines',
                'Saved structure boundaries, with final inclusion and calibrated areas.', data_formats=('csv',)),
        )


def _states(handoff):
    return tuple(state for state in handoff.final_roi_states.values()
                 if state.pipeline_stage == 'secondary_classifier')


def _dispositions(handoff):
    return {item.roi_id: item for result in _domains(handoff).values()
            for item in result.finalization.dispositions}


def _value(value):
    if isinstance(value, (Mapping, tuple, list)):
        return json.dumps(_plain(value), sort_keys=True, separators=(',', ':'))
    return value


def _membership_rows(handoff, counts):
    """One narrow pass; dictionary codes belong to the selected collection."""
    dataset = handoff.cell_dataset
    dictionary = dataset.dictionaries['secondary_roi_ids']
    labels = dictionary.label_ids or dictionary.labels
    scope = dataset.description['scope']
    domains = _domains(handoff)
    states = handoff.final_roi_states
    active = {key for key, item in _dispositions(handoff).items()
              if item.disposition == 'active_included'
              and domains[states[key].record.analysis_domain_id].domain_status == 'active'}
    columns = ('cell_id', 'slide_id', 'section_id', 'analysis_domain_id',
               'membership_run_id', 'membership_generation_id', 'secondary_roi_ids')
    for batch in dataset.iter_batches(columns=columns):
        for row in batch.to_pylist():
            codes = row['secondary_roi_ids']
            if not codes:
                continue
            if any(type(code) is not int or not 0 <= code < len(labels) for code in codes):
                raise ValueError('A final secondary membership has an invalid dictionary code.')
            ids = tuple(labels[code] for code in codes)
            if len(set(ids)) != len(ids) or not set(ids) <= active:
                raise ValueError('A final membership names a rejected, capped or unavailable structure.')
            domain = domains.get(row['analysis_domain_id'])
            if domain is None or (row['slide_id'], row['section_id']) != (domain.member.slide_id, domain.member.section_id):
                raise ValueError('A final secondary membership belongs to a different domain.')
            receipt = domain.finalization.finalization_receipt
            if (row['membership_run_id'], row['membership_generation_id']) != (receipt['pipeline_run_id'], receipt['tissue_generation_id']):
                raise ValueError('A final secondary membership belongs to a different accepted generation.')
            for roi_id in ids:
                record = states[roi_id].record
                if record.analysis_domain_id != row['analysis_domain_id']:
                    raise ValueError('A final secondary membership crosses analysis domains.')
                counts[roi_id] += 1
                yield (scope['sample_id'], scope['image_entry_id'], domain.segmentation_population_id,
                       row['slide_id'], row['section_id'], row['analysis_domain_id'], row['cell_id'],
                       roi_id, record.semantic_class_id, row['membership_run_id'], row['membership_generation_id'])


def secondary_tables(authority):
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    from .semantic_execution import authenticate_roi_semantic_result_artifact

    with _accepted_handoff(authority) as handoff:
        domains = _domains(handoff)
        if not domains:
            raise ValueError('This accepted result has no secondary-structure finalization.')
        counts = Counter()
        memberships = _csv_output('secondary-memberships', 'Final secondary cell memberships',
            ('sample_id', 'image_entry_id', 'population_id', 'slide_id', 'section_id', 'analysis_domain_id',
             'cell_id', 'secondary_roi_id', 'subtype', 'membership_run_id', 'membership_generation_id'),
            _membership_rows(handoff, counts))
        dispositions = _dispositions(handoff)
        frame = handoff.spatial_frame
        outcomes = []
        for number, state in enumerate(_states(handoff), 1):
            record = state.record
            disposition = dispositions.get(record.roi_id)
            outcome = (disposition.disposition if disposition else
                       'classifier_other' if state.candidate_status == 'other' else
                       'review_rejected' if record.review_status.value == 'rejected' else 'not_accepted')
            area = geometry_measurements(record.geometry, frame.x_um_per_px, frame.y_um_per_px)['area_um2']
            outcomes.append((number, record.roi_id, record.section_id, record.analysis_domain_id,
                state.structure_id, record.roi_class, record.semantic_class_id, record.semantic_status.value,
                record.review_status.value, outcome, domains[record.analysis_domain_id].domain_status,
                counts[record.roi_id], area, record.review_rationale, state.path_object_id, state.geometry_sha256))
        criteria, decisions = [], []
        for domain_id, domain in domains.items():
            final = domain.finalization
            artifact = final.semantic_result_artifact
            rows = authenticate_roi_semantic_result_artifact(result_rows=None,
                result_set_sha256=artifact.semantic_result_set_sha256, result_artifact=artifact,
                result_artifact_bytes=Path(artifact.path).read_bytes(), analysis_domain_id=domain_id,
                section_id=domain.member.section_id, tissue_generation_id=domain.member.tissue_generation_id,
                pipeline_run_id=final.finalization_receipt['pipeline_run_id'])
            for row in rows:
                for item in row.get('measurement_results', ()):
                    criteria.append((row['roi_id'], domain_id, item['criterion_id'], item['kind'],
                        item.get('criterion_scope'), _value(item.get('selector')), _value(item.get('value')),
                        item.get('unit'), item.get('outcome'), item.get('comparator'), _value(item.get('threshold')),
                        item.get('required'), item.get('unavailable_reason'), _value(item.get('calculation_inputs'))))
            for output in final.count_decision['outputs']:
                lineage = output.get('selection_lineage') or {}
                decisions.append((domain_id, domain.member.section_id, output['output_id'],
                    output['observed_count'], output['minimum'], output['maximum'],
                    len(output['included_content_keys']), output['status'], domain.domain_status,
                    lineage.get('algorithm'), _value(lineage), final.decision_sha256))
        return (
            _csv_output('secondary-outcomes', 'Final structure outcomes',
                ('region_number', 'roi_id', 'section_id', 'analysis_domain_id', 'structure_id', 'classifier_class',
                 'subtype', 'semantic_status', 'review_status', 'final_disposition', 'domain_status',
                 'final_member_cells', 'area_um2', 'observation', 'path_object_id', 'geometry_sha256'), outcomes),
            _csv_output('secondary-criteria', 'Recorded criterion measurements',
                ('roi_id', 'analysis_domain_id', 'criterion_id', 'kind', 'criterion_scope', 'selector', 'value',
                 'unit', 'outcome', 'comparator', 'threshold', 'required', 'unavailable_reason', 'calculation_inputs'), criteria),
            _csv_output('secondary-count-decisions', 'Final structure count decisions',
                ('analysis_domain_id', 'section_id', 'structure_id', 'observed_count', 'minimum', 'maximum',
                 'included_count', 'status', 'domain_status', 'selection_method', 'selection_lineage', 'decision_sha256'), decisions),
            memberships,
        )


def secondary_outlines(authority, *, palette='teal'):
    from .region_review_figures import region_overview_figure, region_measurements_csv
    from mif_pipeline.cell_analysis.result_plots import _exports

    with _accepted_handoff(authority) as handoff:
        frame, dispositions, domains = handoff.spatial_frame, _dispositions(handoff), _domains(handoff)
        regions = []
        for number, state in enumerate(_states(handoff), 1):
            if state.candidate_status == 'other':
                continue
            record = state.record
            geometry = _plain(record.geometry)
            disposition = dispositions.get(record.roi_id)
            included = (disposition is not None and disposition.disposition == 'active_included'
                        and domains[record.analysis_domain_id].domain_status == 'active')
            regions.append(dict(region_id=record.roi_id, section_id=record.section_id,
                parent_region_id=record.parent_roi_id, display_number=number,
                display_label=record.semantic_class_id or record.roi_class, geometry=geometry,
                layer='secondary', edited=record.original_predicted_geometry is not None, included=included,
                **geometry_measurements(geometry, frame.x_um_per_px, frame.y_um_per_px)))
        review=dict(kind='secondary', slide_id=next(iter(domains.values())).member.slide_id, x_um_per_px=frame.x_um_per_px,
                    y_um_per_px=frame.y_um_per_px, regions=regions)
        figure=region_overview_figure(review, palette=palette)
        from matplotlib.patches import Patch
        from mif_pipeline.cell_analysis.figure_style import outline_palette
        colors=outline_palette(palette)
        figure.axes[0].legend(handles=[
            Patch(facecolor=colors['region_fill'], edgecolor=colors['region_edge'], label='Included'),
            Patch(facecolor=colors['tissue_fill'], edgecolor=colors['tissue_edge'], linestyle='--', label='Excluded')],
            loc='upper center', bbox_to_anchor=(.5, 1.12), ncol=2, frameon=False, fontsize=8)
        data=region_measurements_csv(review)
        return (*_exports(figure, 'secondary-outlines', 'Secondary-structure outlines'),
                ('secondary-outline-areas/csv', 'Secondary-region areas and dimensions', 'csv', data[3]))
