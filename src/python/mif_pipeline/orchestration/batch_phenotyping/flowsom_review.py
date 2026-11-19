"""Project a retained FlowSOM model without reopening cell measurements."""

from ._common import BatchPhenotypingError
from .codec import decode_committed_batch_clustering_result


def model_view(snapshot, review):
    """Read the review's current clustering artifact once and return its codebooks."""
    authorities = {item.artifact_ref.artifact_ref_sha256: item
                   for item in snapshot.committed_artifact_authorities}
    authority = authorities[review.clustering_result_ref_sha256]
    if ((review.refit_summary is not None and review.refit_summary['method']!='flowsom')
            or (review.refit_summary is None and authority.artifact_ref.owning_identity['graph_algorithm'] != 'flowsom/v1')):
        return None
    sources = tuple(authorities[key] for key in review.source_authority_sha256s)
    result = decode_committed_batch_clustering_result(authority, source_authorities=sources,
        maximum_byte_count=authority.artifact_ref.byte_count)
    if result.result_sha256 != review.clustering_result_sha256:
        raise BatchPhenotypingError('The learned model differs from the current cluster review.')
    model = result.refinement.evidence if result.refinement is not None else result.evidence
    if model.graph_algorithm != 'flowsom/v1':
        return None
    counts = {cluster.cluster_id: cluster.cell_count for cluster in review.clusters}
    groups=model.model_group_labels
    if result.refinement is not None:
        groups=tuple((node,result.refinement.child_labels[label]) for node,label in groups)
        counts={key:count for key,count in counts.items() if key in result.refinement.child_labels.values()}
    if groups and {label for _, label in groups} != set(counts):
        raise BatchPhenotypingError('The learned model groups differ from the current cell clusters.')
    return {name: getattr(model, name) for name in (
        'model_codebook', 'model_clusters', 'model_group_labels', 'model_sha256', 'feature_measurements')
    } | {'cluster_counts': counts,'model_group_labels':groups,
         'scope_note':('Learned profiles for the selected-cell refit. Unchanged clusters are outside this model.'
                      if result.refinement is not None else '')}
