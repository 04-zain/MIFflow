"""Shared learned-profile tree for interactive review and saved-result exports."""
from io import BytesIO

import numpy as np


def tree_geometry(codes):
    """Build the minimum spanning tree, retaining zero-length profile links."""
    from scipy.spatial.distance import pdist, squareform
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import minimum_spanning_tree
    import igraph

    codes = np.asarray(codes, dtype=float)
    distances = squareform(pdist(codes, metric='euclidean'))
    rows, columns = np.where(~np.eye(len(codes), dtype=bool))
    values = distances[rows, columns]
    matrix = csr_matrix((np.where(values == 0, np.nextafter(0., 1.), values),
                         (rows, columns)), shape=distances.shape)
    tree = minimum_spanning_tree(matrix).tocoo()
    edges = tuple(zip(tree.row.tolist(), tree.col.tolist()))
    if len(edges) != len(codes) - 1:
        raise ValueError('The learned-profile tree is incomplete.')
    lengths = tuple(float(distances[a, b]) for a, b in edges)
    graph = igraph.Graph(n=len(codes), edges=edges)
    root = max(range(len(codes)), key=lambda index: (graph.degree(index), -index))
    coordinates = np.asarray(graph.layout_reingold_tilford_circular(root=[root]).coords)
    return coordinates, edges, lengths


def draw_tree(axis, model, coordinates, edges):
    """Draw identical topology and group colours in the UI and exported figure."""
    from matplotlib.collections import LineCollection
    from .embedding_plots import _category_color

    labels = dict(model.get('model_group_labels', ()))
    colors = [_category_color(labels.get(group, f'Model group {group}'))
              for group in model['model_clusters']]
    axis.set_axis_off()
    axis.set_aspect('equal', adjustable='box')
    axis.add_collection(LineCollection([coordinates[[a, b]] for a, b in edges],
                                      colors='#b6cbce', linewidths=1., zorder=1))
    nodes = axis.scatter(*coordinates.T, c=colors, s=40, edgecolors='white',
                         linewidths=.5, zorder=3)
    axis.margins(.12)
    axis.autoscale_view()
    return nodes


def model_outputs(model):
    """Export the saved model and tree without fitting or reading cell measurements."""
    from matplotlib import rc_context
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from .embedding_plots import _category_color
    from .figure_style import apply_figure_font
    from .result_plots import _csv_output

    coordinates, edges, lengths = tree_geometry(model['model_codebook'])
    figure = Figure(figsize=(8, 6), layout='constrained')
    axis = figure.add_subplot()
    draw_tree(axis, model, coordinates, edges)
    labels = dict(model.get('model_group_labels', ()))
    groups = sorted(set(model['model_clusters']))
    if len(groups) <= 24:
        handles = [Line2D([], [], linestyle='none', marker='o', markersize=5,
                         color=_category_color(labels.get(group, f'Model group {group}')),
                         label=labels.get(group, str(group))) for group in groups]
        axis.legend(handles=handles, loc='center left', bbox_to_anchor=(1, .5),
                    frameon=False, fontsize=8, title='Cell groups' if labels else 'Model groups')
    apply_figure_font(figure)
    outputs = []
    with rc_context({'svg.fonttype': 'none', 'pdf.fonttype': 42}):
        for fmt in ('png', 'pdf', 'svg'):
            stream = BytesIO()
            figure.savefig(stream, format=fmt, dpi=300, bbox_inches='tight', pad_inches=.08)
            outputs.append(('flowsom-map/' + fmt, 'FlowSOM learned profiles', fmt, stream.getvalue()))
    outputs.append(_csv_output('flowsom-profiles', 'Learned profile data',
        ('profile', 'model_group', 'cell_cluster', 'measurement', 'scaled_value', 'tree_x', 'tree_y'),
        ((index + 1, group, labels.get(group), name, value, *coordinates[index])
         for index, (group, values) in enumerate(zip(model['model_clusters'], model['model_codebook'], strict=True))
         for name, value in zip(model['feature_measurements'], values, strict=True))))
    outputs.append(_csv_output('flowsom-edges', 'Profile connection data',
        ('source_profile', 'target_profile', 'distance'),
        ((a + 1, b + 1, length) for (a, b), length in zip(edges, lengths, strict=True))))
    return tuple(outputs)
