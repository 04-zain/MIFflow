"""Display saved classifier inputs without changing the reproducible recipe."""

import json


def recipe_label(field):
    names = {"model.resolution_um_per_pixel": "Training pixel size (µm)",
        "model.normalization": "Feature scaling", "model.pca_retained_variance": "PCA variance target",
        "model.pca_normalize": "Scale PCA features", "model.maximum_training_samples": "Maximum fitting rows",
        "model.boundary_strategy": "Reference boundaries", "model.boundary_thickness_px": "Boundary width (training pixels)",
        "model.reweight_samples": "Reweight classes", "model.image_features.include_intensity": "Channel intensities",
        "model.image_features.scales": "Filters by scale",
        "random_seed": "Random seed", "eligible_parent_classes": "Detection area"}
    if field.startswith('channels.channel_roles.'):
        return field.removeprefix('channels.channel_roles.').replace('_', ' ').capitalize() + ' channel'
    return names.get(field, field.removeprefix('model.').replace('_', ' ').replace('.', ' · ').capitalize())


def recipe_value(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list) and value and all(
            isinstance(row, dict) and set(row) == {"sigma_um", "filters"} for row in value):
        from mif_pipeline.gui.pixel_image_features import image_feature_summary
        return "; ".join(image_feature_summary(dict(include_intensity=False, scales=value)).splitlines())
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else "None"
    return str(value).replace('_', ' ')


def recipe_sections(recipes, names, units):
    """Return scientific inputs and graph settings; raw rows stay in the disclosure.

    Channel IDs and marker names share encoder order. Never infer a marker from
    an acquisition ID, or interpret the saved nuclear index as a channel number.
    Unrecognized fields remain available in the complete saved recipe.
    """
    inputs, graph = [], []
    by_classifier = {}
    for item in recipes:
        by_classifier.setdefault(item['classifier'], {})[item['setting']] = json.loads(item['value_json'])
    for key, values in by_classifier.items():
        name = names.get(key, key)
        def add(label, value):
            inputs.append((name, label, value))
        schema = values.get('model.configuration_schema', '')
        patch_kronos = schema.startswith('qupath-kronos-')
        encoder = 'model.encoder.' if patch_kronos else 'model.feature_source.encoder.'
        markers = values.get(encoder + 'marker_names')
        channels = values.get(encoder + 'channel_ids')
        if markers is not None and channels is not None and len(markers) == len(channels):
            add('Encoder markers', '; '.join(markers))
            nuclear = values.get(encoder + 'nuclear_channel')
            if type(nuclear) is int and 0 <= nuclear < len(markers):
                add('Nuclear marker', markers[nuclear])
            size = values.get(encoder + 'pixel_size_um')
            if isinstance(size, list) and len(size) == 2:
                add('Input pixel size (µm)', f'X {size[0]:.6g} · Y {size[1]:.6g}')
            patch = values.get(encoder + 'patch_size')
            if patch is not None:
                add('Patch size' if patch_kronos else 'Cell crop size', f'{patch} × {patch} pixels')
        features = values.get('features.feature_schema')
        if features:
            if all(row.get('semantic_role') == 'kronos_cell_embedding' for row in features):
                add('Input features', f'{len(features):,} KRONOS cell embedding features')
            else:
                add('Input measurements', '\n'.join(row['measurement_name'] for row in features))
        if patch_kronos:
            # Native pixel defaults are constrained by QuPathKronosConfiguration;
            # they are not additional pixel filters or patch sampling controls.
            add('Fitting samples', 'Unique complete labelled patches')
            prefix = 'model.native_configuration.'
        else:
            prefix = 'model.'
            for field in ('channels.channel_roles.epithelial', 'channels.channel_roles.nuclear'):
                if values.get(field) is not None:
                    add(recipe_label(field), recipe_value(values[field]))
            for field, label in (('model.training_annotations', 'Reference annotations'),
                                 ('features.point_selection_radius_level0', 'Point matching radius')):
                if values.get(field) is not None:
                    value = values[field]
                    add(label, 'Points' if value == 'points' else f'{value:g} full-resolution pixels' if isinstance(value, (float, int)) else recipe_value(value))
        fields = ('normalization',) if patch_kronos else (
            'resolution_um_per_pixel', 'normalization', 'pca_retained_variance', 'pca_normalize',
            'image_features.include_intensity', 'image_features.scales', 'maximum_training_samples',
            'boundary_strategy', 'boundary_thickness_px', 'reweight_samples')
        for suffix in fields:
            value = values.get(prefix + suffix)
            if (value is None or (suffix == 'boundary_thickness_px' and value == 0)
                    or (suffix == 'pca_normalize' and values.get(prefix + 'pca_retained_variance') == 0)):
                continue
            label, rendered = recipe_label('model.' + suffix), recipe_value(value)
            if suffix == 'normalization':
                if patch_kronos:
                    label = 'Classifier feature normalization'
                rendered = {'none': 'None', 'mean_variance': 'Mean and variance', 'min_max': 'Minimum and maximum'}.get(value, rendered)
            elif suffix == 'pca_retained_variance' and value == 0:
                rendered = 'Off'
            elif suffix == 'boundary_strategy' and value == 'skip':
                rendered = 'Use reference interiors'
            elif suffix == 'maximum_training_samples':
                unit = units.get(key, 'samples')
                label = 'Maximum ' + unit + ' for fitting'
                rendered = 'All extracted ' + unit if value == 0 else rendered
            add(label, rendered)
        if values.get('eligible_parent_classes') is not None:
            add('Detection area', ', '.join('Analysis region' if p == 'AnalysisDomain' else p for p in values['eligible_parent_classes']))
        for suffix, label in (('hidden_width', 'Learned features per patch'), ('epochs', 'Training passes'),
                              ('batch_size', 'Labelled patches per update'), ('learning_rate', 'Learning rate'),
                              ('weight_decay', 'Weight decay')):
            if values.get('model.graph.' + suffix) is not None:
                graph.append((name, label, recipe_value(values['model.graph.' + suffix])))
    return inputs, graph
