"""Reuse saved cell-gallery arrays for display and export without reading images."""
from __future__ import annotations

import json
from types import SimpleNamespace


def gallery_data_from_bundle(bundle, indices=None, *, labels=None, channels=None):
    """Reconstruct selected gallery rows, preserving image coordinates and values."""
    metadata = json.loads(str(bundle['metadata_json']))
    options = metadata['display_options']
    indices = tuple(range(len(bundle['cell_ids']))) if indices is None else tuple(indices)
    available = tuple(map(int, bundle['channel_indices']))
    channels = available if channels is None else tuple(map(int, channels))
    columns = [available.index(channel) for channel in channels]
    outlines = []
    for index in indices:
        rings = [[], []]
        for ring, cell in enumerate(bundle['outline_cell_indices']):
            if int(cell) == index:
                start, stop = bundle['outline_offsets'][ring:ring+2]
                rings[int(bundle['outline_kinds'][ring] == 'nucleus')].append(bundle['outline_vertices_um'][start:stop])
        outlines.append(tuple(tuple(group) for group in rings))
    saved_labels = metadata.get('review_labels', metadata.get('phenotypes', options.get('selected_labels', [])))
    return SimpleNamespace(
        cell_ids=tuple(str(bundle['cell_ids'][i]) for i in indices),
        labels=tuple(labels) if labels is not None else tuple(saved_labels[i] for i in indices),
        crops=tuple(bundle['pixels'][i][columns] for i in indices),
        channels=channels, channel_labels=tuple(str(bundle['channel_names'][i]) for i in columns),
        outlines=tuple(outlines), extents=tuple(tuple(map(float, bundle['extents_um'][i])) for i in indices),
        crop_um=float(options.get('crop_um', 40)), limits=tuple(options['limits'][str(c)] for c in channels),
        selection=options, frame=metadata['source_frame'], pyramid_level=metadata['pyramid_level'],
        pixel_size_um=tuple(metadata['pixel_size_um']),
        centers_um=tuple(tuple(map(float, bundle['centers_um'][i])) for i in indices),
        channel_colors=tuple(options.get('channel_colors', {}).get(str(c), '#ffffff') for c in channels))
