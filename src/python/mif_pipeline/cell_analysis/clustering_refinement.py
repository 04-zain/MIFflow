"""Refit selected cells in the exact retained geometry of their parent fit."""
from dataclasses import replace

import numpy as np

from .clustering import (
    CellClusteringError, CellClusteringIdentity, ClusteringEvidence,
    _selected_feature_specs, _validate_graph_configuration, _labels_by_identity,
    _node_identity_sha256,
)
from .clustering_methods import validate_method_settings, partition_official


def refit_marker_matrix(features, *, identities, feature_specs, parent_evidence,
                       method, method_settings, k, resolution, seed):
    """Consume one owned subset matrix without estimating a new transformation.

    The caller selects exact parent memberships and reads the parent's accepted
    measurement sources. This numerical boundary requires the same ordered
    feature definitions and applies the recorded centers, divisors and weights.
    It returns only subset membership/evidence; the execution owner preserves
    unaffected cells and records parent lineage in the complete result.
    """
    selected = _selected_feature_specs(feature_specs, excluded_roles=())
    _validate_graph_configuration(k=k,resolution=resolution,seed=seed)
    settings = validate_method_settings(method, method_settings or {})
    if method not in {'phenograph','flowsom'}:
        raise CellClusteringError('Choose PhenoGraph or FlowSOM for the selected-cluster refit.')
    identities = tuple(identities)
    if (not isinstance(features,np.ndarray) or features.dtype != np.float64
            or not features.flags.writeable or not features.flags.owndata
            or len(identities)<2 or features.shape != (len(identities),len(selected))):
        raise CellClusteringError('Refitting requires at least two cells in one owned Float64 subset matrix.')
    if (any(not isinstance(identity,CellClusteringIdentity) for identity in identities)
            or any(identities[i-1]>=identities[i] for i in range(1,len(identities)))):
        raise CellClusteringError('Refit identities must be sorted and unique.')
    if (not isinstance(parent_evidence,ClusteringEvidence)
            or parent_evidence.feature_measurements != tuple(spec.measurement for spec in selected)
            or parent_evidence.canonical_biomarkers != tuple(spec.biomarker for spec in selected)
            or parent_evidence.feature_weights != tuple(spec.weight for spec in selected)):
        raise CellClusteringError('Refitting must retain the parent feature order, markers and weights.')
    centers = np.asarray(parent_evidence.robust_centers,dtype=np.float64)
    scales = np.asarray(parent_evidence.robust_scales,dtype=np.float64)
    weights = np.asarray(parent_evidence.feature_weights,dtype=np.float64)
    if (centers.shape != (len(selected),) or scales.shape != centers.shape
            or not np.isfinite(centers).all() or not np.isfinite(scales).all()
            or np.any(scales<=0)):
        raise CellClusteringError('The parent feature transformation is unavailable or invalid.')
    for start in range(0,len(identities),8192):
        if not np.isfinite(features[start:start+8192]).all():
            raise CellClusteringError('The selected parent measurements contain non-finite values.')
    np.subtract(features,centers,out=features)
    np.divide(features,scales,out=features)
    np.multiply(features,weights,out=features)
    memberships,evidence = partition_official(features,method,settings,k=k,resolution=resolution,seed=seed)
    labels = _labels_by_identity(identities,memberships)
    evidence = replace(evidence,
        feature_measurements=parent_evidence.feature_measurements,
        canonical_biomarkers=parent_evidence.canonical_biomarkers,
        robust_centers=parent_evidence.robust_centers,
        robust_scales=parent_evidence.robust_scales,
        feature_weights=parent_evidence.feature_weights,
        feature_transform_sha256=parent_evidence.feature_transform_sha256,
        node_identity_sha256=_node_identity_sha256(identities),resolution=float(resolution),seed=seed)
    if method=='flowsom':
        groups={int(group):labels[identity] for identity,group in zip(identities,memberships,strict=True)}
        evidence=replace(evidence,model_group_labels=tuple(sorted(groups.items())))
    return labels,evidence
