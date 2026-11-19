"""Inspect exact retained held-out cell predictions using accepted image crops."""


def selected_reference(votes, options):
    """A displayed cell must belong to this classifier's recorded held-out result."""
    if options.get('split') not in {'validation', 'test'}:
        raise ValueError('Cell images require retained validation or independent-test predictions.')
    key = tuple(options.get(name) for name in ('split', 'project_entry_id', 'cell_id'))
    row = votes.get(options.get('classifier'), {}).get(key)
    if row is None:
        raise ValueError('This cell is not in the selected classifier’s held-out evaluation.')
    return row


def qupath_cell_request(data, row):
    """Locate the evaluated cell using its measured geometry, without refitting."""
    import numpy as np
    pixel_size = np.asarray([data.frame['level0_pixel_size_x_um'], data.frame['level0_pixel_size_y_um']])
    centre = np.asarray(data.centers_um[0])
    left, right, bottom, top = data.extents[0]
    return dict(project_entry_id=row['project_entry_id'], cell_id=row['cell_id'],
        split=row['split'], reference_class=row['reference_class'], predicted_class=row['predicted_class'],
        roi_bounds=dict(x=float((centre[0]+left)/pixel_size[0]), y=float((centre[1]+top)/pixel_size[1]),
                        width=float((right-left)/pixel_size[0]), height=float((bottom-top)/pixel_size[1]), z=0, t=0),
        cell_outlines=[dict(kind=kind, rings=[((ring+centre)/pixel_size).tolist() for ring in rings])
                       for kind, rings in zip(('cell', 'nucleus'), data.outlines[0])])


def render_supervised_gallery(snapshot, record_set, plan_set, votes, options):
    from ..batch_measurement_artifacts import decode_committed_measurements_result
    from ..batch_phenotyping.marker_review_gallery import measurement_cells
    from ..saved_cell_gallery import gallery_resources, load_cell_crops, display_options
    from ..saved_cell_figures import _MeasuredCells
    from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery

    row = selected_reference(votes, options)
    record = next(record for record in record_set.classifier_records
                  if record.classifier_id == options['classifier'])
    if record.classifier_kind.value != 'supervised_phenotype':
        raise ValueError('Choose a supervised cell classifier evaluation.')
    plan = next(plan for plan in plan_set.plans if plan.request.classifier_id == record.classifier_id)
    matches = [authority for authority in snapshot.committed_artifact_authorities
               if authority.artifact_ref.artifact_kind == 'measurements_result'
               and all(authority.artifact_ref.owning_identity.get(key) == row[key]
                       for key in ('project_entry_id', 'segmentation_generation_id', 'measurement_generation_id'))]
    if len(matches) != 1:
        raise ValueError('The evaluated cell’s accepted measurement geometry is no longer current.')
    authority = matches[0]
    measurement = decode_committed_measurements_result(authority)
    image = next((authority.artifact_ref for authority in snapshot.committed_artifact_authorities
                  if authority.artifact_ref.artifact_kind == 'processed_image'
                  and authority.artifact_ref.sha256 == measurement.canonical_image_sha256
                  and authority.artifact_ref.scope.sample_id == measurement.sample_id), None)
    if image is None:
        raise ValueError('The evaluated cell’s measured image is unavailable.')
    selection = {name: options[name] for name in ('classifier', 'split', 'project_entry_id', 'cell_id')}
    settings = {**options, 'cell_ids': [row['cell_id']]}
    if 'channels' not in settings:
        names = {feature['measurement_name'] for feature in plan.request.feature_requirements['feature_schema']}
        channel_ids = {feature.channel_id for feature in measurement.measurement_plan.features
                       if feature.measurement in names and feature.channel_id is not None}
        settings['channels'] = [i for i, channel in enumerate(measurement.canonical_channels)
                                if channel['channel_id'] in channel_ids][:6]
        if not settings['channels']:
            settings['channels'] = list(range(min(3, len(measurement.canonical_channels))))
    with gallery_resources(snapshot, authority):
        with measurement_cells(measurement) as (view, component, scope):
            columns = ('cell_id', 'x_level0', 'y_level0', 'cell_geometry', 'nucleus_geometry')
            cells = _MeasuredCells(view, component, measurement, scope)
            rows = {item['cell_id']: item for batch in cells.lookup_ids(settings['cell_ids'], columns=columns)
                    for item in batch.to_pylist()}
            if row['cell_id'] not in rows:
                raise ValueError('The evaluated cell is absent from its recorded measurement geometry.')
            label = f"Reference: {row['reference_class']}\nPrediction: {row['predicted_class']}"
            data = load_cell_crops(image.locator, rows, tuple(settings['cell_ids']), (label,), settings,
                image_id=measurement.canonical_image_id, image_sha256=measurement.canonical_image_sha256,
                pixel_size_um=(scope['x_um_per_px'], scope['y_um_per_px']))
        resolved = display_options(data, settings)
        split_label = 'Independent test' if row['split'] == 'test' else 'Validation'
        resolved.update(qupath_review=qupath_cell_request(data, row), supervised_selection=selection, supervised_request=dict(options), label_field='review_label', include_pixels=True,
            available_channels=[{'index':i,'label':channel['name']} for i,channel in enumerate(measurement.canonical_channels)],
            image_label=measurement.sample_id + ' · ' + measurement.section_id,
            review_caption=f"{split_label} · Subject {row['subject_id']} · {label.replace(chr(10), ' · ')}",
            export_basename='supervised-evaluated-cell')
        outputs = render_cell_gallery(data, resolved)
    return tuple(('supervised-cell-gallery/' + identity.split('/', 1)[-1], title, fmt, payload)
                 for identity,title,fmt,payload in outputs), resolved
