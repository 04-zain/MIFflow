"""Method-specific controls and official FlowSOM/PhenoGraph implementations."""
from collections.abc import Mapping
from types import MappingProxyType
from threading import Lock

_FLOWSOM_FIT_LOCK = Lock()


def validate_method_settings(method, settings):
    if method not in {'snn_leiden', 'phenograph', 'flowsom'}:
        raise ValueError('Choose a supported cell clustering method.')
    if not isinstance(settings, Mapping):
        raise ValueError('Clustering settings must contain named values.')
    if method != 'flowsom':
        if settings:
            raise ValueError('Map size and metacluster settings apply to FlowSOM.')
        return MappingProxyType({})
    values = {'grid_size': 10, 'n_clusters': 20, 'iterations': 10}
    if set(settings) - set(values):
        raise ValueError('The FlowSOM settings contain an unsupported option.')
    values.update(settings)
    if any(type(v) is not int for v in values.values()):
        raise ValueError('FlowSOM settings must be whole numbers.')
    if not 2 <= values['grid_size'] <= 30:
        raise ValueError('Choose a FlowSOM grid side between 2 and 30 nodes.')
    if not 2 <= values['n_clusters'] <= values['grid_size']**2:
        raise ValueError('The number of metaclusters must be between 2 and the number of map nodes.')
    if not 1 <= values['iterations'] <= 100:
        raise ValueError('Choose between 1 and 100 FlowSOM training iterations.')
    return MappingProxyType(values)


def validate_method_seed(method, seed):
    """Validate the official FlowSOM generator's unsigned 32-bit seed."""
    if method == 'flowsom' and (type(seed) is not int or not 0 <= seed < 2**32):
        raise ValueError('Choose a FlowSOM random seed between 0 and 4294967295.')
    return seed


def partition_official(features, method, settings, *, k, resolution, seed):
    """Keep official model mathematics and return the common cell memberships."""
    validate_method_seed(method, seed)
    if method == 'phenograph':
        return _phenograph(features, k, resolution, seed)
    if method == 'flowsom':
        return _flowsom(features, settings, seed)
    raise ValueError('Choose PhenoGraph or FlowSOM.')


def _phenograph(features, k, resolution, seed):
    import hashlib
    import importlib
    import numpy as np
    from scipy import sparse
    from scipy.sparse.csgraph import connected_components
    from phenograph.core import neighbor_graph, jaccard_kernel
    from sklearn.neighbors import NearestNeighbors
    from .clustering import ClusteringEvidence
    from mif_pipeline.pipeline_core.execution_resources import numerical_thread_limit
    with numerical_thread_limit():
        count = len(features)
        effective = min(k, count-1)
        if effective:
            # A training-set query excludes each cell by identity, including when
            # several cells have identical measurements. Dropping one shared
            # column of a query result can retain self-neighbours in tied rows.
            indices = NearestNeighbors(n_neighbors=effective, metric='euclidean', n_jobs=1).fit(features).kneighbors(return_distance=False)
            directed = neighbor_graph(jaccard_kernel, {'idx': indices})
            graph = sparse.tril((directed + directed.T).multiply(.5), -1).tocsr()
            graph.eliminate_zeros()
            graph.sort_indices()
        else:
            graph = sparse.csr_matrix((count, count), dtype=float)
        # The package's final size-sorting wrapper creates an unrestricted process
        # pool even with n_jobs=1. Use its unchanged graph and Leiden functions;
        # the application already gives all communities stable neutral names.
        if graph.nnz:
            module = importlib.import_module('phenograph.cluster')
            membership, _ = module.run_leiden(graph.tocoo(), False, None, resolution, -1, seed, True, {})
        else:
            membership = np.arange(count)
        undirected = graph + graph.T
        components = connected_components(undirected, directed=False, return_labels=False)
        digest = hashlib.sha256()
        for array in (np.asarray(graph.shape, dtype='<i8'), graph.indptr.astype('<i8'), graph.indices.astype('<i8'), graph.data.astype('<f8')):
            digest.update(array.tobytes())
        weights = graph.data
        evidence = ClusteringEvidence(backend='sklearn.neighbors.NearestNeighbors', pairwise_python_calls=0,
            tree_query_calls=int(bool(effective)), graph_algorithm='phenograph_leiden/v1',
            partitioner_backend='phenograph.cluster.run_leiden', requested_k=k,effective_k=effective,
            node_count=count,edge_count=graph.nnz,component_count=int(components),
            isolate_count=int(np.count_nonzero(np.diff(undirected.indptr)==0)),
            minimum_edge_weight=float(weights.min()) if weights.size else None,
            maximum_edge_weight=float(weights.max()) if weights.size else None,
            mean_edge_weight=float(weights.mean()) if weights.size else None,
            graph_sha256=digest.hexdigest(),resolution=resolution,seed=seed)
        return membership, evidence


def _flowsom(features, settings, seed):
    import hashlib
    import numpy as np
    from numba import get_num_threads, set_num_threads
    from flowsom.models import FlowSOMEstimator
    from .clustering import ClusteringEvidence
    from mif_pipeline.pipeline_core.execution_resources import current_operation_budget, numerical_thread_limit
    grid, clusters, iterations = (settings[key] for key in ('grid_size','n_clusters','iterations'))
    if len(features) < grid**2:
        raise ValueError(f'This FlowSOM grid requires at least {grid**2:,} cells. Select a smaller grid for {len(features):,} cells.')
    model = FlowSOMEstimator(xdim=grid,ydim=grid,rlen=iterations,n_clusters=clusters,seed=seed)
    # This pinned implementation seeds NumPy's legacy generator during fitting.
    # Serialize our fits and restore the caller's generator state afterwards.
    budget = current_operation_budget()
    previous_threads = get_num_threads()
    set_num_threads(min(previous_threads, budget.cpu_threads if budget else 1))
    try:
        with numerical_thread_limit():
            with _FLOWSOM_FIT_LOCK:
                state = np.random.get_state()
                try:
                    model.fit(features)
                finally:
                    np.random.set_state(state)
            labels = np.empty(len(features),dtype=np.int64)
            # predict temporarily allocates distances to every map node.
            block = max(1, min(8192, (16*1024**2)//(grid*grid*8)))
            for start in range(0,len(features),block):
                labels[start:start+block] = model.predict(features[start:start+block])
    finally:
        set_num_threads(previous_threads)
    codes = np.asarray(model.codes,dtype='<f8')
    model_clusters = np.asarray(model._y_codes,dtype='<i8')
    # Official metaclustering is deterministic average-linkage agglomeration.
    digest = hashlib.sha256(codes.tobytes()+model_clusters.tobytes()).hexdigest()
    evidence = ClusteringEvidence(backend='flowsom.models.FlowSOMEstimator',pairwise_python_calls=0,
        tree_query_calls=0,graph_algorithm='flowsom/v1',neighbor_mode='som',symmetrization='none',
        affinity='som_codebook',zero_edge_policy='not_applicable',pruning_policy='none',
        partitioner_backend='flowsom.models.ConsensusCluster',partition_quality_function='average_linkage',
        node_count=len(features),seed=seed,method_settings=tuple(sorted(settings.items())),
        model_codebook=tuple(tuple(float(x) for x in row) for row in codes),
        model_clusters=tuple(int(x) for x in model_clusters),model_sha256=digest)
    return labels,evidence
