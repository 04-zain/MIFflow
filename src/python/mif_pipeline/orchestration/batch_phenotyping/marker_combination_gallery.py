"""Image examples from the exact bounded identities retained by a draft preview."""
import json

from ._common import digest
from .marker_combinations import validate_combination_response
from .marker_combination_preview import resolve_preview_inputs
from .marker_review_gallery import measurement_cells
from ..saved_cell_gallery import load_cell_crops, display_options, gallery_resources, MAX_CHANNELS


def _retained_pattern(provider, pattern_id):
    from pathlib import Path
    from .marker_combination_preview import pattern_from_cache
    return pattern_from_cache(Path(provider._marker_combination_population[1].name) / 'patterns.sqlite', pattern_id)


def _default_channels(channels, nuclear, definition, options, provider):
    if options['category']=='pattern':
        pattern = _retained_pattern(provider,options['pattern_id'])
        states = dict(zip(provider._marker_gate_examples[1]['observed_patterns']['markers'],pattern['states']))
        relevant = [i for i,c in enumerate(channels) if c['biomarker'] in states]
        relevant.sort(key=lambda i: states[channels[i]['biomarker']]!='positive')
    else:
        states = {} if options['category']=='unmatched' else definition['composite_rules'][options['precedence']]['marker_states']
        relevant = [i for i,c in enumerate(channels) if states.get(c['biomarker']) in ('positive','negative')]
    return list(dict.fromkeys((*([nuclear] if nuclear is not None else []),*relevant)))[:MAX_CHANNELS] or [0]


def retained_examples(head, target, definition, options, provider):
    """Require the current server preview, never accept arbitrary client cell IDs."""
    expected = ('marker-combinations',head.sha256,target.interaction_context_sha256,digest(definition))
    retained = provider._marker_gate_examples
    if retained is None or retained[0]!=expected:
        raise ValueError('Update assignment preview before opening these cell examples.')
    population = provider._marker_combination_population
    if population is None or population[0] != (head.sha256, target.interaction_request_id, target.interaction_context_sha256):
        raise ValueError('Update assignment preview before opening these cell examples.')
    preview = retained[1]
    category, precedence = options.get('category'), options.get('precedence')
    if category=='pattern' and precedence is None and type(options.get('pattern_id')) is str:
        examples, label = (), None
    elif category=='unmatched' and precedence is None:
        examples, label = preview['unmatched_examples'], preview['residual_phenotype']
    elif category in ('assigned','overlap','unknown') and type(precedence) is int:
        row = next((r for r in preview['rows'] if r['precedence']==precedence),None)
        if row is None:
            raise ValueError('Choose a rule from the current assignment preview.')
        examples, label = row['examples'][category], row['phenotype']
    else:
        raise ValueError('Choose an available assignment example category.')
    if 'pattern_id' in options:
        patterns = preview['observed_patterns']
        pattern = _retained_pattern(provider,options['pattern_id'])
        winner = pattern['assignments'][0]['phenotype']
        permitted = category=='pattern' or category=='unmatched' and winner==preview['residual_phenotype']
        if category in ('assigned','overlap','unknown'):
            import numpy as np
            from .marker_application_execution import _composites
            from mif_pipeline.cell_analysis.marker_rules import _rule_match
            rules, _residual, _ = _composites(definition, tuple(patterns['markers']))
            rule = next(r for r in rules if r.precedence==precedence)
            indices = {marker:i for i,marker in enumerate(patterns['markers'])}
            matrix = np.array([[{'positive':1,'negative':0,'unknown':-1}[state] for state in pattern['states']]], dtype=np.int8)
            match = bool(_rule_match(matrix,indices,rule)[0])
            unknown = any(matrix[0,indices[m]]==-1 for m,state in rule.marker_states.items() if state!='ignore')
            permitted = {'assigned':match and winner==rule.phenotype,
                         'overlap':match and winner!=rule.phenotype,'unknown':unknown}[category]
        if not permitted:
            raise ValueError('The observed pattern is outside the selected assignment category.')
        examples = pattern['examples']
        if category=='pattern':
            label = winner
    image = options.get('image_identity')
    if not isinstance(image,(list,tuple)) or len(image)!=3 or any(type(v) is not str or not v for v in image):
        raise ValueError('Choose the image belonging to these cell examples.')
    identities = tuple(tuple(i) for i in examples if tuple(i[:3])==tuple(image))
    if 'cell_identity' in options:
        identity = options['cell_identity']
        if not isinstance(identity,(list,tuple)) or tuple(identity) not in identities:
            raise ValueError('Choose a cell retained by the current assignment preview.')
        identities = (tuple(identity),)
    if not identities:
        raise ValueError('No retained examples are available for this rule and image.')
    return identities, label, category


def _load_examples(identities, labels, measurements, image, crop_options):
    """Look up only chosen geometries in the accepted measured components."""
    wanted = {i[:4] for i in identities}
    by_scope = {(m.sample_id,m.project_entry_id,m.slide_id,m.section_id):m for m in measurements}
    if not wanted <= by_scope.keys():
        raise ValueError('The example belongs to an unavailable measured section.')
    ids = tuple(i[-1] for i in identities)
    if len(set(ids))!=len(ids):
        raise ValueError('Cell examples must have distinct identities within this image.')
    rows, scope = {}, None
    for key in wanted:
        measurement = by_scope[key]
        if measurement.canonical_image_sha256!=image.sha256:
            raise ValueError('The example image differs from its measured cell image.')
        selected = tuple(i[-1] for i in identities if i[:4]==key)
        with measurement_cells(measurement) as (view, _component, current_scope):
            scope = current_scope
            for batch in view.lookup_ids(selected,columns=('cell_id','x_level0','y_level0','cell_geometry','nucleus_geometry')):
                for row in batch.to_pylist():
                    if row['cell_id'] in rows:
                        raise ValueError('Cell examples repeat an identity across measured sections.')
                    rows[row['cell_id']]=row
    if set(rows)!=set(ids):
        raise ValueError('An example is no longer in the measured cell geometry.')
    return load_cell_crops(image.locator,rows,ids,labels,crop_options,
        image_id=by_scope[identities[0][:4]].canonical_image_id,image_sha256=image.sha256,
        pixel_size_um=(scope['x_um_per_px'],scope['y_um_per_px']))


def render_combination_gallery(snapshot, target, options, provider):
    """Reuse current examples and crop/display rendering without rescanning scores."""
    from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    definition = validate_combination_response(target.stage_result,
        {'phenotype_definition':options.get('phenotype_definition')})['phenotype_definition']
    identities, phenotype, category = retained_examples(snapshot.head,target,definition,options,provider)
    _gate, measurements, authorities = resolve_preview_inputs(snapshot,target.stage_result.resume_context)
    image_identity = tuple(options['image_identity'])
    measurement = next(m for m in measurements if (m.sample_id,m.project_entry_id,m.slide_id,m.section_id)==identities[0][:4])
    images = [a.artifact_ref for a in snapshot.committed_artifact_authorities
              if a.artifact_ref.artifact_kind=='processed_image'
              and a.artifact_ref.scope.sample_id==image_identity[0]
              and a.artifact_ref.sha256==measurement.canonical_image_sha256]
    if len(images)!=1:
        raise ValueError('The measured image is no longer available for these examples.')
    image = images[0]
    status = {'assigned':'assigned here','overlap':'assigned by an earlier rule',
              'unknown':'required marker state unknown','unmatched':'unmatched','pattern':'observed marker pattern'}[category]
    assignments = {tuple(row['cell_identity']): row['assigned_phenotype']
                   for row in provider._marker_gate_examples[1]['example_assignments']}
    if 'pattern_id' in options:
        winner = _retained_pattern(provider, options['pattern_id'])['assignments'][0]['phenotype']
        assignments.update((identity, winner) for identity in identities)
    labels = tuple(assignments[identity] if category=='pattern' else f'{assignments[identity]}\n{status}' for identity in identities)
    caption = ('Observed marker pattern' if category=='pattern' else f'Rule {phenotype}: {status}')
    caption += f'; {len(identities)} example {"cell" if len(identities)==1 else "cells"}.'
    crop_options = {key:options[key] for key in ('channels','crop_um') if key in options}
    if 'channels' not in crop_options:
        from ome_types import from_xml
        from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
        metadata = json.loads((image.locator/'zarr.json').read_text())
        inventory = resolve_component_identity_inventory(from_xml(metadata['attributes']['mif_pipeline']['ome_xml']),0)
        nuclear = inventory.nuclear_component_index if inventory else None
        crop_options['channels'] = _default_channels(measurement.canonical_channels,nuclear,definition,options,
                                                    provider)
    selection_key = ('marker-combinations',snapshot.head.sha256,target.interaction_context_sha256,
                     digest(definition),category,options.get('precedence'),identities)
    crop_key = (selection_key,json.dumps(crop_options,sort_keys=True))
    with gallery_resources(snapshot,authorities[0]):
        previous = provider._cell_gallery
        if previous is None or previous[0]!=crop_key:
            provider._cell_gallery = None
            gallery = _load_examples(identities,labels,measurements,image,crop_options)
            gallery.selection.update(cell_identities=[list(i) for i in identities],category=category,
                                     phenotype=phenotype,definition_sha256=digest(definition))
            provider._cell_gallery = (crop_key,gallery)
        else:
            gallery = previous[1]
        resolved = display_options(gallery,{**options,**crop_options,'cell_ids':list(gallery.cell_ids)})
        resolved.update(available_channels=[{'index':i,'label':c['name']} for i,c in enumerate(measurement.canonical_channels)],
            label_field='review_label',include_pixels=True,
            image_label=' · '.join(dict.fromkeys((image_identity[0],image_identity[2]))),
            cell_identities=[list(i) for i in identities],review_caption=caption,export_basename='phenotype-cell-examples')
        outputs = render_cell_gallery(gallery,resolved)
        table = _csv_output('phenotype-cell-examples','Phenotype assignment examples',
            ('sample_id','project_entry_id','slide_id','section_id','cell_id','phenotype','assigned_phenotype','category','pattern_id','definition_sha256'),
            ((*identity,phenotype,assignments[identity],category,options.get('pattern_id'),digest(definition)) for identity in identities))
        outputs = (*outputs,table)
    return outputs,resolved
