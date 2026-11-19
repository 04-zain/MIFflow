"""

Groups cells by their measured protein-marker profiles using a shared-neighbour graph and Leiden.

We first centre each selected marker at its population median and divide
by its interquartile range. The selected feature weight multiplies that
scaled value. Euclidean distances between these complete marker vectors
determine the other cells selected as each cell's nearest neighbours.

We connect a candidate pair when either cell selected the other as a
neighbour. Their shared neighbour IDs determine its Jaccard weight. We
average the two directed contributions to form the undirected graph, then
apply weighted Leiden with the selected resolution and random seed.
The reference at docs/stages/phenotyping/unsupervised.md gives the objective
and its relationship to these measured-marker distances.

Before calculation, cells are ordered by (sample_id, project_entry_id, cell_id).
This same order resolves equal-distance choices and determines neutral
Cluster_NNN names after partitioning. We return assignments in that stable order,
retaining image and cell identities for subsequent tissue review.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from math import isfinite
import sys
from typing import Callable, Hashable, Iterable, Sequence

import numpy as np
from scipy.spatial import cKDTree

from mif_pipeline.pipeline_core.models import ROIAnalysisError

_MAX_K = 64


# ==============================================================================
#                                   Clustering
# ==============================================================================


class CellClusteringError(ROIAnalysisError):
    """

    Reports incompatible cell identities, measurement values, graph settings or returned group assignments.
    """

class ClusteringDependencyError(ImportError):
    """

    Reports that an igraph or leidenalg backend required for a nonempty graph is unavailable.
    """

class FeatureRole(str, Enum):
    """

    Identifies how a measurement is used when selecting clustering features.

    EXPRESSION represents a measured protein-marker intensity. MORPHOLOGY and CONTEXTUAL
    identify geometric or surrounding-tissue measurements. NUCLEAR, DAPI and
    AUTOFLUORESCENCE identify channels excluded by the default clustering selection.
    The public value "expression" is the stored name for protein-marker intensity.
    """

    EXPRESSION = "expression"
    MORPHOLOGY = "morphology"
    NUCLEAR = "nuclear"
    AUTOFLUORESCENCE = "autofluorescence"
    CONTEXTUAL = "contextual"
    DAPI = "dapi"


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """

    Describes one selected cell measurement and its contribution to feature-space distance.

    The measurement name identifies the exact QuPath value. Its feature ID and channel,
    marker and cellular-compartment fields associate that value with the accepted
    measurement plan. The weight multiplies the robustly scaled feature before distance
    calculation, so its contribution to squared distance is multiplied by weight squared.

    Attributes:
        measurement: Exact QuPath measurement name, including cellular compartment and statistic.
        role: Measurement category used during clustering selection.
        channel_id: Image-channel identity for an intensity measurement, when applicable.
        feature_id: Recorded identity linking this choice to the measurement plan.
        transform: Supported transformation, "robust_scale".
        weight: Positive multiplier applied after centering and interquartile scaling.
        kind: Measurement category recorded by the producing measurement step.
        biomarker: Explicit biological marker associated with the image channel.
        compartment: Cellular region measured, such as nucleus or cytoplasm.
        units: Unit label supplied with the measurement.
        producer: Program or operation that calculated the measurement.
        producer_version: Version of that measurement-producing program.
        missing_rate: Fraction of cells lacking the measurement, when supplied.
        non_finite_rate: Fraction of cells with nonfinite values, when supplied.
        opt_in: Whether explicit selection is required in the measurement interface.
    """

    measurement: str
    role: FeatureRole
    channel_id: str | None = None
    feature_id: str | None = None
    transform: str = "robust_scale"
    weight: float = 1.0
    # Measurement metadata preserves the channel, cellular compartment and
    # producing program used to interpret the selected numerical values.
    kind: str | None = None
    biomarker: str | None = None
    compartment: str | None = None
    units: str | None = None
    producer: str | None = None
    producer_version: str | None = None
    missing_rate: float | None = None
    non_finite_rate: float | None = None
    opt_in: bool = False

    def __post_init__(self) -> None:
        """

        Checks that a selected measurement has a supported transformation and positive finite weight.

        Optional descriptive fields must contain nonblank text when supplied. Missing and
        nonfinite rates are proportions between zero and one. Weights and rates are
        converted to ordinary Python floats for later serialization.

        Raises:
            CellClusteringError: A required name, role, transformation, rate or weight is invalid.
        """

        if not isinstance(self.measurement, str) or not self.measurement.strip():
            raise CellClusteringError("feature measurements must be non-empty strings")
        if not isinstance(self.role, FeatureRole):
            raise CellClusteringError("feature role must be a FeatureRole")
        if self.feature_id is not None and (
            not isinstance(self.feature_id, str) or not self.feature_id.strip()
        ):
            raise CellClusteringError("feature_id must be non-empty when provided")
        for field in ("kind", "biomarker", "compartment", "units", "producer", "producer_version"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise CellClusteringError(f"feature {field} must be a non-empty string or None")
        for field in ("missing_rate", "non_finite_rate"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise CellClusteringError(
                    f"feature {field} must be finite and between zero and one"
                )
            if value is not None:
                object.__setattr__(self, field, float(value))
        if not isinstance(self.opt_in, bool):
            raise CellClusteringError("feature opt_in must be boolean")
        if self.transform != "robust_scale":
            raise CellClusteringError("feature transform must be robust_scale")
        if (
            isinstance(self.weight, bool)
            or not isinstance(self.weight, (int, float))
            or not isfinite(float(self.weight))
            or float(self.weight) <= 0
        ):
            raise CellClusteringError("feature weight must be positive and finite")
        object.__setattr__(self, "weight", float(self.weight))

    @property
    def measurement_name(self) -> str:
        """

        Returns the exact QuPath key used to retrieve this feature from a cell.

        Returns:
            Complete measurement name, including the selected cellular compartment,
            image channel and statistic where applicable.
        """

        return self.measurement


@dataclass(frozen=True, order=True, slots=True)
class CellClusteringIdentity:
    """

    Identifies a cell by its sample, QuPath image entry and local cell ID.

    A local cell ID can recur in another image. Keeping all three fields together
    allows pooled clustering results to return to the correct original cell. The
    lexicographic order of these fields is used for deterministic row and tie ordering.

    Attributes:
        sample_id: Sample containing the cell.
        project_entry_id: QuPath image entry containing the cell.
        cell_id: Cell identity within that image entry.
    """

    sample_id: str
    project_entry_id: str
    cell_id: str

    def __post_init__(self) -> None:
        """

        Requires nonblank text for the sample, image-entry and cell identities.

        Raises:
            CellClusteringError: An identity field is missing or is not text.
        """

        for field in ("sample_id", "project_entry_id", "cell_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise CellClusteringError(f"clustering identity {field} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class WeightedEdge:
    """

    Stores one positive undirected cell-graph connection.

    Attributes:
        source: Lower node index in the stable ordered cell list.
        target: Higher node index; requiring source < target stores each pair once.
        weight: Symmetrized shared-neighbour similarity in the interval (0, 1].
    """

    source: int
    target: int
    weight: float

    def __post_init__(self) -> None:
        """

        Checks ordered nonnegative node indices and a finite positive similarity weight.

        Raises:
            CellClusteringError: Indices repeat, are reversed or invalid, or the weight
                lies outside (0, 1].
        """

        if (
            isinstance(self.source, bool)
            or not isinstance(self.source, int)
            or isinstance(self.target, bool)
            or not isinstance(self.target, int)
            or self.source < 0
            or self.target <= self.source
        ):
            raise CellClusteringError(
                "weighted edges require integer endpoints with 0 <= source < target"
            )
        if (
            isinstance(self.weight, bool)
            or not isinstance(self.weight, (int, float))
            or not isfinite(float(self.weight))
            or not 0.0 < float(self.weight) <= 1.0
        ):
            raise CellClusteringError("weighted edge weight must be finite and in (0, 1]")
        object.__setattr__(self, "weight", float(self.weight))


GraphPartitioner = Callable[
    [np.ndarray, tuple[WeightedEdge, ...], float, int],
    Sequence[Hashable],
]


@dataclass(frozen=True, slots=True)
class ClusteringEvidence:
    """

    Records the fitted feature transformation, graph construction and partition settings.

    These fields explain how a particular cell list became the graph that Leiden
    received. Checksums identify the ordered cell list, weighted edges and feature
    transformation; they permit later results to be associated with that same run.

    Attributes:
        backend: Nearest-neighbour implementation, currently "scipy.spatial.cKDTree".
        pairwise_python_calls: Number of scalar Python distance calls; this implementation uses zero.
        tree_query_calls: Number of compiled batched tree queries, normally zero or one.
        graph_algorithm: Versioned name of the exact SNN/Jaccard construction.
        neighbor_mode: "exact" for the selected nearest-neighbour method.
        distance_metric: "euclidean" on weighted, robustly scaled marker features.
        symmetrization: "directed_weight_average", corresponding to (A + A.T) / 2.
        affinity: Jaccard overlap of the original directed neighbour sets.
        zero_edge_policy: "remove", because zero similarities supply no graph connection.
        pruning_policy: "none"; positive weights are passed to the partitioner.
        partitioner_backend: Native Leiden function or supplied custom partitioner name.
        partition_quality_function: "RBConfigurationVertexPartition" for the default backend.
        resolution: Positive resolution parameter supplied to the partition objective.
        seed: Random seed supplied to the partitioner.
        requested_k: Selected number of other cells in each neighbour list.
        effective_k: min(requested_k, number_of_cells - 1), or zero for an empty/singleton input.
        node_count: Number of cells represented by nodes.
        edge_count: Number of positive undirected weighted edges.
        component_count: Number of connected graph components, including isolated cells.
        isolate_count: Cells with no positive edge.
        minimum_edge_weight: Minimum positive edge weight, or None for an edgeless graph.
        maximum_edge_weight: Maximum positive edge weight, or None when no edges exist.
        mean_edge_weight: Mean positive edge weight, or None when no edges exist.
        feature_measurements: Selected measurement names in feature-column order.
        canonical_biomarkers: Marker identities aligned with those measurement columns.
        robust_centers: Population medians subtracted from the selected features.
        robust_scales: Interquartile ranges used as divisors; zero ranges use 1.0.
        feature_weights: Multipliers applied after robust scaling.
        feature_transform_sha256: Checksum of names, markers, centers, scales and weights.
        node_identity_sha256: Checksum of the ordered composite cell identities.
        graph_sha256: Checksum of node count and ordered weighted edges.
        retained_candidate_slots: External neighbour positions retained during exact selection.
        maximum_candidate_working_set: Largest temporary candidate comparison block, in pairs.
        exact_distance_evaluations: Distance calculations used to resolve external neighbour order.
        fallback_group_count: Feature-vector groups requiring the boundary-tie scan.
    """

    backend: str
    pairwise_python_calls: int
    tree_query_calls: int
    graph_algorithm: str = "exact_snn_jaccard_leiden/v1"
    neighbor_mode: str = "exact"
    distance_metric: str = "euclidean"
    symmetrization: str = "directed_weight_average"
    affinity: str = "jaccard_directed_neighbor_sets"
    zero_edge_policy: str = "remove"
    pruning_policy: str = "none"
    partitioner_backend: str = "leidenalg.find_partition"
    partition_quality_function: str = "RBConfigurationVertexPartition"
    resolution: float = 1.0
    seed: int = 0
    requested_k: int = 0
    effective_k: int = 0
    node_count: int = 0
    edge_count: int = 0
    component_count: int = 0
    isolate_count: int = 0
    minimum_edge_weight: float | None = None
    maximum_edge_weight: float | None = None
    mean_edge_weight: float | None = None
    feature_measurements: tuple[str, ...] = ()
    canonical_biomarkers: tuple[str | None, ...] = ()
    robust_centers: tuple[float, ...] = ()
    robust_scales: tuple[float, ...] = ()
    feature_weights: tuple[float, ...] = ()
    feature_transform_sha256: str = ""
    node_identity_sha256: str = ""
    graph_sha256: str = ""
    retained_candidate_slots: int = 0
    maximum_candidate_working_set: int = 0
    exact_distance_evaluations: int = 0
    fallback_group_count: int = 0
    method_settings: tuple[tuple[str, int], ...] = ()
    model_codebook: tuple[tuple[float, ...], ...] = ()
    model_clusters: tuple[int, ...] = ()
    model_sha256: str = ''
    model_group_labels: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _CandidateSelectionEvidence:
    """

    Counts the work and retained storage used to select external neighbours.

    Attributes:
        retained_candidate_slots: Sum of required external neighbours across source groups.
        maximum_candidate_working_set: Largest candidate block considered at once.
        exact_distance_evaluations: Number of vectorized exact-distance comparisons.
        fallback_group_count: Groups whose nearest-neighbour cutoff contained a distance tie.
    """

    retained_candidate_slots: int
    maximum_candidate_working_set: int
    exact_distance_evaluations: int
    fallback_group_count: int


@dataclass(frozen=True, slots=True)
class _ExternalMemberSelection:
    """

    Returns selected external cells and the work required to resolve their distance order.

    Attributes:
        members: Integer array with one row per source feature group. Valid positions
            precede any -1 padding; the caller knows the required count for each row.
        evidence: Counts of retained candidates, temporary comparisons and tie scans.
    """

    members: np.ndarray
    evidence: _CandidateSelectionEvidence


@dataclass(frozen=True, slots=True)
class _GraphBuildResult:
    """

    Returns the positive cell-graph edges together with construction statistics.

    Attributes:
        edges: Undirected positive-weight pairs in node-index order.
        evidence: Graph sizes, settings, identities and neighbour-search counters.
        candidate_selection: Counters for the strict-cutoff and tied-cutoff selection routes.
    """

    edges: tuple[WeightedEdge, ...]
    evidence: ClusteringEvidence
    candidate_selection: _CandidateSelectionEvidence


@dataclass(frozen=True, slots=True)
class MarkerIntensityClusterAssignment:
    """

    Associates a neutral cluster label with its original cell and analysis region.

    Attributes:
        identity: Composite sample, image-entry and cell identity.
        analysis_domain_id: Region recorded for that cell in the measurement table.
        cluster_id: Neutral label such as "Cluster_003", awaiting biological review.
    """

    identity: CellClusteringIdentity
    analysis_domain_id: str
    cluster_id: str

    def __post_init__(self) -> None:
        """

        Checks the composite cell identity and nonempty region/cluster labels.

        Raises:
            CellClusteringError: Identity has the wrong type or a required label is blank or padded.
        """

        if not isinstance(self.identity, CellClusteringIdentity):
            raise CellClusteringError(
                "marker cluster assignment identity must be a CellClusteringIdentity"
            )
        for field in ("analysis_domain_id", "cluster_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise CellClusteringError(
                    f"marker cluster assignment {field} must be canonical text"
                )


@dataclass(frozen=True, slots=True)
class MarkerIntensityClusteringResult:
    """

    Returns compact cell-to-cluster assignments and their fitted graph settings.

    Attributes:
        assignments: Ordered per-cell assignments retaining the source analysis-domain labels.
        evidence: Scaling, graph, population and partition records for the completed clustering.
    """

    assignments: tuple[MarkerIntensityClusterAssignment, ...]
    evidence: ClusteringEvidence

    def __post_init__(self) -> None:
        """

        Checks that the result contains typed assignments and the associated clustering record.

        Raises:
            CellClusteringError: Assignment collection or computation record has an invalid type.
        """

        if type(self.assignments) is not tuple or not all(
            isinstance(item, MarkerIntensityClusterAssignment) for item in self.assignments
        ):
            raise CellClusteringError(
                "marker clustering assignments must be an exact assignment tuple"
            )
        if not isinstance(self.evidence, ClusteringEvidence):
            raise CellClusteringError("marker clustering evidence must be ClusteringEvidence")


def cluster_marker_matrix(
    features: np.ndarray,
    *,
    identities: tuple[CellClusteringIdentity, ...],
    domain_codes: np.ndarray,
    domain_labels: tuple[str, ...],
    feature_specs: tuple[FeatureSpec, ...],
    k: int,
    resolution: float,
    seed: int,
    partitioner: GraphPartitioner | None = None,
    method: str = 'snn_leiden',
    method_settings=None,
) -> MarkerIntensityClusteringResult:
    """Cluster our one owned Float64 matrix in stable composite-ID order.

    i) We check the requested feature order, current identities and compact
    Int32 domain codes. ii) We consume the private matrix for the unchanged
    median/IQR transform and weighted graph coordinates. iii) We return exact
    source identities/domain labels and the existing scientific graph evidence.
    This operation mutates its private feature buffer and never loads geometry.
    """
    selected = _selected_feature_specs(feature_specs, excluded_roles=())
    _validate_graph_configuration(k=k, resolution=resolution, seed=seed)
    if (not isinstance(features, np.ndarray) or features.dtype != np.float64 or
            not len(identities) or features.shape != (len(identities), len(selected)) or not features.flags.writeable):
        raise CellClusteringError("Clustering requires its one owned writable Float64 matrix")
    if any(not isinstance(identity, CellClusteringIdentity) for identity in identities) or any(identities[i-1] >= identities[i] for i in range(1,len(identities))):
        raise CellClusteringError("Clustering identities must be sorted and unique")
    if (not isinstance(domain_codes,np.ndarray) or domain_codes.dtype != np.int32 or
            domain_codes.shape != (len(identities),) or
            np.any(domain_codes < 0) or np.any(domain_codes >= len(domain_labels))):
        raise CellClusteringError("Clustering domains require declared Int32 codes")
    for start in range(0,len(identities),8192):
        block = features[start:start+8192]
        invalid = ~np.isfinite(block)
        invalid_count = int(np.count_nonzero(invalid))
        if invalid_count:
            examples = []
            for row in np.flatnonzero(invalid.any(axis=1)):
                identity = identities[start + int(row)]
                for column in np.flatnonzero(invalid[row])[:5 - len(examples)]:
                    examples.append(
                        f"sample {identity.sample_id!r}, image entry {identity.project_entry_id!r}, "
                        f"cell {identity.cell_id!r}, {selected[int(column)].measurement!r}: {block[row, column]}"
                    )
                if len(examples) == 5:
                    break
            raise CellClusteringError(
                f"Selected clustering measurements contain {invalid_count:,} non-finite values "
                f"in this batch of {len(block):,} {'cell' if len(block) == 1 else 'cells'}. Examples: " + "; ".join(examples) + ". "
                "Review these cells and their measurement channels and compartments. "
                "Choose finite measurement columns in Clustering setup, or correct the "
                "underlying cell measurements before fitting again."
            )
    mapping, evidence = _cluster_prepared_features(
        ordered_identities=identities,features=features,selected=selected,
        k=k,resolution=float(resolution),seed=seed,partitioner=partitioner,
        method=method,method_settings=method_settings,
    )
    return MarkerIntensityClusteringResult(
        assignments=tuple(MarkerIntensityClusterAssignment(identity,domain_labels[int(domain_codes[i])],mapping[identity]) for i,identity in enumerate(identities)),
        evidence=evidence,
    )


def _validate_graph_configuration(*, k: int, resolution: float, seed: int) -> None:
    """

    Checks graph settings before allocating neighbour lists.

    Args:
        k: Requested neighbour count; must be an integer from 1 through 64.
        resolution: Finite positive partition resolution.
        seed: Integer random seed. Boolean values are rejected for all numerical fields.

    Raises:
        CellClusteringError: A setting is outside its supported type or numerical range.
    """

    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise CellClusteringError("k must be a positive integer")
    if k > _MAX_K:
        raise CellClusteringError(f"k must not exceed {_MAX_K}")
    if (
        isinstance(resolution, bool)
        or not isinstance(resolution, (int, float))
        or not isfinite(float(resolution))
        or float(resolution) <= 0
    ):
        raise CellClusteringError("resolution must be positive and finite")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise CellClusteringError("seed must be an integer")


def _cluster_prepared_features(
    *,
    ordered_identities: tuple[CellClusteringIdentity, ...],
    features: np.ndarray,
    selected: tuple[FeatureSpec, ...],
    k: int,
    resolution: float,
    seed: int,
    partitioner: GraphPartitioner | None,
    method: str = 'snn_leiden',
    method_settings=None,
) -> tuple[dict[CellClusteringIdentity, str], ClusteringEvidence]:
    """

    Scales the ordered marker matrix, partitions its graph and records the fitted transformation.

    For feature j, use center[j] = median(values[:, j]) and scale[j] = Q75 - Q25.
    A zero interquartile range uses scale 1.0. The final graph coordinate is:

        coordinate[i, j] = weight[j] * (value[i, j] - center[j]) / scale[j]

    The same transformed rows supply nearest-neighbour selection and the optional
    partitioner interface. Save the centers, scales, weights and ordered identities
    alongside graph statistics so later review can trace the resulting groups.

    Args:
        ordered_identities: Unique composite cell identities already sorted lexicographically.
        features: Finite float64 matrix shaped (cells, selected_features), in that cell order.
        selected: Feature definitions aligned with matrix columns.
        k: Checked requested neighbour count.
        resolution: Checked positive Leiden resolution.
        seed: Recorded partition seed.
        partitioner: Optional custom partition function; None selects the native backend.

    Returns:
        A composite-identity-to-neutral-label mapping and ClusteringEvidence containing
        the fitted feature transformation and graph settings.
    """

    # i) We compute the exact population statistics with column-sized workspaces.
    centers = np.array([np.median(features[:,j]) for j in range(features.shape[1])],dtype=np.float64)
    interquartile_ranges = np.array([np.diff(np.percentile(features[:,j],(25.0,75.0)))[0] for j in range(features.shape[1])],dtype=np.float64)
    scales = np.where(interquartile_ranges > 0.0,interquartile_ranges,1.0)
    # ii) Our private fitting allocation becomes the weighted graph coordinates.
    np.subtract(features,centers,out=features)
    np.divide(features,scales,out=features)
    np.multiply(features,np.array([spec.weight for spec in selected],dtype=np.float64),out=features)
    scaled = features
    from .clustering_methods import validate_method_settings, partition_official
    settings = validate_method_settings(method, method_settings or {})
    if method == 'snn_leiden':
        backend = partitioner if partitioner is not None else _leiden_partitioner
        memberships, evidence = _partition_cells(scaled, k, resolution, seed, backend)
    else:
        if partitioner is not None:
            raise CellClusteringError('A custom partitioner cannot replace the selected clustering method.')
        memberships, evidence = partition_official(scaled, method, settings, k=k, resolution=resolution, seed=seed)
    weights = tuple(spec.weight for spec in selected)
    evidence = replace(
        evidence,
        feature_measurements=tuple(spec.measurement for spec in selected),
        canonical_biomarkers=tuple(spec.biomarker for spec in selected),
        robust_centers=tuple(float(value) for value in centers),
        robust_scales=tuple(float(value) for value in scales),
        feature_weights=weights,
        feature_transform_sha256=_feature_transform_sha256(
            selected, centers=centers, scales=scales
        ),
        node_identity_sha256=_node_identity_sha256(ordered_identities),
        partitioner_backend=_partitioner_backend(partitioner) if method == 'snn_leiden' else evidence.partitioner_backend,
        partition_quality_function=(
            ("RBConfigurationVertexPartition" if partitioner is None else "custom") if method == 'snn_leiden' else evidence.partition_quality_function
        ),
        resolution=resolution,
        seed=seed,
    )
    labels = _labels_by_identity(ordered_identities, memberships)
    if method == 'flowsom':
        # Record occupied metaclusters while the actual backend memberships are
        # available. A map profile never implies a retained per-cell SOM node.
        groups = {int(group): labels[identity]
                  for identity, group in zip(ordered_identities, memberships, strict=True)}
        evidence = replace(evidence, model_group_labels=tuple(sorted(groups.items())))
    return labels, evidence


def _labels_by_identity(
    identities: Sequence[CellClusteringIdentity],
    memberships: Sequence[Hashable],
) -> dict[CellClusteringIdentity, str]:
    """

    Converts backend group numbers to stable neutral names using complete cell identities.

    Group cells by backend membership, then order those groups by their smallest
    (sample_id, project_entry_id, cell_id) value. Backend numbers therefore do not
    become biological labels or determine display order.

    Args:
        identities: Composite identities in the order presented to the partitioner.
        memberships: One hashable group value per identity.

    Returns:
        Mapping from each composite identity to its Cluster_NNN name.

    Raises:
        CellClusteringError: Membership length differs or a group value is unhashable.
    """

    if len(memberships) != len(identities):
        raise CellClusteringError(
            "partitioner returned a membership count that does not match cells"
        )
    communities: dict[Hashable, list[CellClusteringIdentity]] = {}
    for identity, community in zip(identities, memberships):
        try:
            communities.setdefault(community, []).append(identity)
        except TypeError as exc:
            raise CellClusteringError("partitioner memberships must be hashable") from exc
    ordered_communities = sorted(communities.values(), key=lambda members: min(members))
    normalized: dict[CellClusteringIdentity, str] = {}
    for number, members in enumerate(ordered_communities, start=1):
        label = f"Cluster_{number:03d}"
        for identity in members:
            normalized[identity] = label
    return normalized


def _selected_feature_specs(
    feature_specs: Iterable[FeatureSpec],
    *,
    excluded_roles: Iterable[FeatureRole],
) -> tuple[FeatureSpec, ...]:
    """

    Selects the protein-marker measurements permitted to define clustering distance.

    Check feature names for duplicates before excluding the configured roles. Every
    retained feature must have the protein-marker intensity role. The current graph
    therefore compares measured marker profiles; its rows can still retain geometry
    and other measurements for later interpretation.

    Args:
        feature_specs: Ordered candidate measurement definitions.
        excluded_roles: Unique typed roles to remove, including the default nuclear/DAPI/AF roles.

    Returns:
        Ordered tuple of remaining marker-intensity features.

    Raises:
        CellClusteringError: Roles or names are invalid, names repeat, an unsupported
            retained feature is requested, or the selection leaves no marker features.
    """

    exclusions = tuple(excluded_roles)
    if any(not isinstance(item, FeatureRole) for item in exclusions):
        raise CellClusteringError("excluded feature roles must be FeatureRole values")
    if len(set(exclusions)) != len(exclusions):
        raise CellClusteringError("excluded feature roles must be unique")
    selected: list[FeatureSpec] = []
    seen: set[str] = set()
    for spec in feature_specs:
        if not isinstance(spec, FeatureSpec):
            raise CellClusteringError("clustering features must be FeatureSpec values")
        if not isinstance(spec.measurement, str) or not spec.measurement.strip():
            raise CellClusteringError("feature measurements must be non-empty strings")
        if not isinstance(spec.role, FeatureRole):
            raise CellClusteringError("feature role must be a FeatureRole")
        if spec.channel_id is not None and (
            not isinstance(spec.channel_id, str) or not spec.channel_id.strip()
        ):
            raise CellClusteringError("feature channel_id must be a non-empty string or None")
        if spec.measurement in seen:
            raise CellClusteringError(f"duplicate feature measurement {spec.measurement!r}")
        seen.add(spec.measurement)
        if spec.role in set(exclusions):
            continue
        if spec.role is not FeatureRole.EXPRESSION:
            raise CellClusteringError(
                "prototype clustering graph dimensions must be protein-marker "
                "intensity measurements"
            )
        selected.append(spec)
    if not selected:
        raise CellClusteringError("no clustering features remain after excluding AF/DAPI")
    return tuple(selected)


def _feature_transform_sha256(
    feature_specs: Sequence[FeatureSpec],
    *,
    centers: np.ndarray,
    scales: np.ndarray,
) -> str:
    """

    Records an exact identity for the selected measurements and their fitted scaling.

    Serialize feature names, markers, centers, scales and weights using sorted-key
    compact JSON, then hash those bytes. This identifies the transformation fitted
    on this population for later association with its clustering result.

    Args:
        feature_specs: Features in matrix-column order.
        centers: Fitted median values aligned with those columns.
        scales: Positive divisors used after centering.

    Returns:
        Lowercase SHA-256 text for the complete ordered transformation record.
    """

    material = {
        "algorithm": "robust_scale/v1",
        "features": [
            {
                "measurement": spec.measurement,
                "biomarker": spec.biomarker,
                "center": float(center),
                "scale": float(scale),
                "weight": spec.weight,
            }
            for spec, center, scale in zip(feature_specs, centers, scales)
        ],
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _node_identity_sha256(
    identities: Sequence[CellClusteringIdentity],
) -> str:
    """

    Hashes the ordered sample, image-entry and cell IDs defining the graph's node positions.

    Args:
        identities: Composite cell identities in graph-row order.

    Returns:
        SHA-256 text used to associate graph indices with the same ordered cells.
    """

    digest = hashlib.sha256(b"[")
    for index, identity in enumerate(identities):
        if index:
            digest.update(b",")
        digest.update(json.dumps([identity.sample_id, identity.project_entry_id, identity.cell_id], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    digest.update(b"]")
    return digest.hexdigest()



def _graph_sha256(*, row_count: int, edges: Sequence[WeightedEdge]) -> str:
    """

    Hashes node count and the ordered weighted edges for a completed graph.

    Each float edge weight is serialized with float.hex(), preserving its exact
    binary value. Node identities are recorded separately by _node_identity_sha256().

    Args:
        row_count: Total nodes, including isolates.
        edges: Positive undirected pairs with source < target.

    Returns:
        SHA-256 text identifying the graph topology and numerical weights.
    """

    return _canonical_sha256(
        {
            "node_count": row_count,
            "edges": [[edge.source, edge.target, edge.weight.hex()] for edge in edges],
        }
    )


def _canonical_sha256(material: object) -> str:
    """

    Serializes a finite JSON-compatible record with sorted keys and hashes its UTF-8 bytes.

    Args:
        material: Record describing a graph, node order or another saved numerical choice.

    Returns:
        Lowercase SHA-256 text. Nonfinite or non-JSON values fail during serialization.
    """

    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _partitioner_backend(partitioner: GraphPartitioner | None) -> str:
    """

    Returns the callable name recorded for the partition implementation.

    Args:
        partitioner: Custom function, or None for the standard Leiden partitioner.

    Returns:
        Qualified callable name, using module and function/class name where available.
    """

    if partitioner is None:
        return "leidenalg.find_partition"
    module = getattr(partitioner, "__module__", "unknown")
    qualified_name = getattr(
        partitioner,
        "__qualname__",
        getattr(partitioner, "__name__", type(partitioner).__qualname__),
    )
    return f"{module}.{qualified_name}"


def _compiled_snn_jaccard_graph(features: np.ndarray, k: int) -> _GraphBuildResult:
    """

    Builds exact nearest-neighbour sets, calculates their shared-neighbour similarities and forms undirected edges.

    Set effective_k = min(k, cell_count - 1), excluding each cell from its own set.
    Group identical feature vectors so the tree is queried once per distinct vector.
    Stable identity order selects zero-distance neighbours within a group. Groups
    needing additional neighbours use a compiled tree query and a lookahead distance
    to determine whether the kth cutoff has a tie.

    A strict cutoff uses its small tree candidate list. A tied cutoff scans bounded
    NumPy blocks and sorts by (distance, stable_row_index), retaining only the needed
    neighbours. This preserves exact tie handling without retaining a dense distance matrix.

    For directed neighbour pair i -> j, calculate:

        J[i, j] = |N(i) intersect N(j)| / |N(i) union N(j)|

    Combine directions as (A[i, j] + A[j, i]) / 2, where A contains J only for selected
    directed neighbour pairs. A reciprocal pair keeps its Jaccard weight; a one-way
    pair receives half that weight. Zero-weight pairs are removed.

    Args:
        features: Weighted float64 feature matrix in stable composite-cell order.
        k: Checked requested nearest-neighbour count.

    Returns:
        Positive undirected edges and records of graph size, neighbor-search work and ties.

    Raises:
        CellClusteringError: An exact neighbour selection fails to supply the required count.
    """

    row_count = features.shape[0]
    neighbor_count = min(k, row_count - 1)
    if neighbor_count <= 0:
        return _GraphBuildResult(
            edges=(),
            evidence=_graph_evidence(
                row_count=row_count,
                requested_k=k,
                effective_k=0,
                edges=(),
                tree_query_calls=0,
            ),
            candidate_selection=_CandidateSelectionEvidence(0, 0, 0, 0),
        )

    unique_features, members_by_group, group_by_row = _feature_vector_groups(features)
    neighbors = np.empty((row_count, neighbor_count), dtype=np.intp)
    assigned = np.zeros(row_count, dtype=bool)
    active_groups: list[int] = []
    for group, members in enumerate(members_by_group):
        zero_neighbor_count = len(members) - 1
        if zero_neighbor_count >= neighbor_count:
            for position, source in enumerate(members):
                neighbors[int(source)] = _stable_group_members(members, position, neighbor_count)
                assigned[int(source)] = True
        else:
            active_groups.append(group)

    query_calls = 0
    candidate_selection = _CandidateSelectionEvidence(0, 0, 0, 0)
    if active_groups:
        active = np.asarray(active_groups, dtype=np.intp)
        tree = cKDTree(unique_features)
        # Self occupies one tree result.  One additional external result lets
        # us distinguish a strict kth boundary from an exact boundary tie.
        query_k = min(neighbor_count + 2, len(unique_features))
        distances, candidate_groups = tree.query(unique_features if len(active) == len(unique_features) else unique_features[active], k=query_k, workers=1)
        distances = np.asarray(distances, dtype=np.float64).reshape(len(active), query_k)
        candidate_groups = np.asarray(candidate_groups, dtype=np.intp).reshape(len(active), query_k)
        group_sizes = np.asarray([len(members) for members in members_by_group])
        needed = np.asarray(
            [neighbor_count - (len(members_by_group[group]) - 1) for group in active],
            dtype=np.intp,
        )
        (
            boundary_distances,
            lookahead_distances,
            has_lookahead,
        ) = _neighbor_boundaries_by_group_multiplicity(
            source_groups=active,
            distances=distances,
            candidate_groups=candidate_groups,
            group_sizes=group_sizes,
            required=needed,
        )
        # Equal kth and next-cell distances leave the selected identity
        # unresolved by distance alone. Route these groups to the stable tie scan.
        fallback_mask = has_lookahead & (boundary_distances == lookahead_distances)
        fallback_rows = np.flatnonzero(fallback_mask)
        fast_rows = np.flatnonzero(~fallback_mask)
        external_members = np.full((len(active), int(np.max(needed))), -1, dtype=np.intp)
        selections: list[_ExternalMemberSelection] = []
        if len(fast_rows):
            fast_selection = _select_tree_external_members(
                features,
                unique_features=unique_features,
                source_groups=active[fast_rows],
                candidate_groups=candidate_groups[fast_rows],
                candidate_distances=distances[fast_rows],
                members_by_group=members_by_group,
                required=needed[fast_rows],
                boundary_distances=boundary_distances[fast_rows],
            )
            external_members[fast_rows, : fast_selection.members.shape[1]] = fast_selection.members
            selections.append(fast_selection)
        if len(fallback_rows):
            fallback_selection = _select_exact_external_members(
                features,
                unique_features=unique_features,
                group_by_row=group_by_row,
                source_groups=active[fallback_rows],
                required=needed[fallback_rows],
                boundary_distances=boundary_distances[fallback_rows],
            )
            external_members[fallback_rows, : fallback_selection.members.shape[1]] = (
                fallback_selection.members
            )
            selections.append(fallback_selection)
        query_calls = 1
        candidate_selection = _combine_candidate_selection_evidence(selections)
        for row, group in enumerate(active):
            members = members_by_group[int(group)]
            external = external_members[row, : int(needed[row])]
            for position, source in enumerate(members):
                zero_members = _stable_group_members(members, position, len(members) - 1)
                neighbors[int(source), :len(zero_members)] = zero_members
                neighbors[int(source), len(zero_members):] = external
                assigned[int(source)] = True

    if not assigned.all():
        raise CellClusteringError("compiled neighbor construction left a cell unassigned")
    sources = np.repeat(np.arange(row_count, dtype=np.intp), neighbor_count)
    targets = neighbors.reshape(-1)
    directed_weights = _directed_jaccard_weights(neighbors, sources, targets)
    undirected_pairs, inverse = np.unique(
        np.sort(np.column_stack((sources, targets)), axis=1),
        axis=0,
        return_inverse=True,
    )
    # This is exactly (A + A.T) / 2 over the directed candidate matrix:
    # reciprocal links retain their Jaccard value and one-way links receive
    # half. Jaccard itself is calculated from the original directed sets.
    undirected_weights = np.bincount(
        inverse,
        weights=directed_weights * 0.5,
        minlength=len(undirected_pairs),
    )
    edges = tuple(
        WeightedEdge(int(pair[0]), int(pair[1]), float(weight))
        for pair, weight in zip(undirected_pairs, undirected_weights)
        if weight > 0.0
    )
    return _GraphBuildResult(
        edges=edges,
        evidence=replace(
            _graph_evidence(
                row_count=row_count,
                requested_k=k,
                effective_k=neighbor_count,
                edges=edges,
                tree_query_calls=query_calls,
            ),
            retained_candidate_slots=(candidate_selection.retained_candidate_slots),
            maximum_candidate_working_set=(candidate_selection.maximum_candidate_working_set),
            exact_distance_evaluations=(candidate_selection.exact_distance_evaluations),
            fallback_group_count=candidate_selection.fallback_group_count,
        ),
        candidate_selection=candidate_selection,
    )


def _graph_evidence(
    *,
    row_count: int,
    requested_k: int,
    effective_k: int,
    edges: Sequence[WeightedEdge],
    tree_query_calls: int,
) -> ClusteringEvidence:
    """

    Counts connected groups, isolated cells and edge-weight summaries for the completed graph.

    We begin with every node in its own connected group. For each positive
    edge, we increment the endpoint degrees and follow their parent indices
    to their current group representatives. If those representatives differ,
    we join the groups and decrease the connected-group count by one.

    Nodes whose final degree is zero are isolated and each still contributes
    one connected group. These connectivity counts describe the input graph;
    the subsequent Leiden partition can divide a connected group further.

    Args:
        row_count: Total cell nodes, including cells with no retained edges.
        requested_k: Neighbour count selected before considering population size.
        effective_k: Actual neighbour count used for every cell.
        edges: Positive undirected weighted pairs, one record per pair.
        tree_query_calls: Number of compiled nearest-neighbour query calls.

    Returns:
        ClusteringEvidence with graph counts, minimum/mean/maximum edge weight
        and the checksum identifying these graph nodes and edges.
    """

    degrees = np.zeros(row_count, dtype=np.int64)
    parents = np.arange(row_count, dtype=np.intp)

    def root(index: int) -> int:
        """

        Finds a node's current connected-group representative and shortens its parent chain.

        While a node points to another parent, we replace that parent with the
        parent's own parent and continue upward. Repeated connectivity checks can
        then reach the shared representative using fewer intermediate indices.

        Args:
            index: Node index whose connected group is needed by the edge loop.

        Returns:
            Representative node index for its current connected group.
        """

        while int(parents[index]) != index:
            parents[index] = parents[int(parents[index])]
            index = int(parents[index])
        return index

    component_count = row_count
    for edge in edges:
        degrees[edge.source] += 1
        degrees[edge.target] += 1
        source_root = root(edge.source)
        target_root = root(edge.target)
        if source_root != target_root:
            parents[target_root] = source_root
            component_count -= 1
    weights = tuple(edge.weight for edge in edges)
    return ClusteringEvidence(
        backend="scipy.spatial.cKDTree",
        pairwise_python_calls=0,
        tree_query_calls=tree_query_calls,
        requested_k=requested_k,
        effective_k=effective_k,
        node_count=row_count,
        edge_count=len(edges),
        component_count=component_count,
        isolate_count=int(np.count_nonzero(degrees == 0)),
        minimum_edge_weight=min(weights, default=None),
        maximum_edge_weight=max(weights, default=None),
        mean_edge_weight=(None if not weights else float(np.mean(weights))),
        graph_sha256=_graph_sha256(row_count=row_count, edges=edges),
    )


def _directed_jaccard_weights(
    neighbors: np.ndarray,
    sources: np.ndarray,
    targets: np.ndarray,
) -> np.ndarray:
    """

    Calculates the shared-neighbour similarity for each selected directed cell pair.

    For cell i and its candidate neighbour j, we compare every ID in N(i) with
    every ID in N(j). Because both lists contain k unique cells, the number of
    equal-ID comparisons is their intersection size c. Their union size is
    2*k-c, giving Jaccard similarity c/(2*k-c).

    We process pair batches with at most 262,144 Boolean comparisons in the
    temporary (pairs, k, k) array. For k=16, a batch contains at most 1024
    directed pairs. The resulting weights remain in source/target order so
    the next step can combine the two possible directions of each cell pair.

    Args:
        neighbors: Integer array of shape (cells, effective_k), with each cell's
            unique selected neighbours in its row.
        sources: Source node indices of the directed pairs being compared.
        targets: Target node indices in the same directed-pair order.

    Returns:
        Float64 Jaccard weights for the supplied pairs, including zero when
        their neighbour lists share no cell IDs.

    Raises:
        CellClusteringError: The neighbour array has the wrong number of axes
            or the source and target lists have different lengths.
    """

    if neighbors.ndim != 2:
        raise CellClusteringError("neighbor matrix must be two-dimensional")
    if len(sources) != len(targets):
        raise CellClusteringError("directed graph endpoints must have equal length")
    neighbor_count = neighbors.shape[1]
    if neighbor_count == 0 or len(sources) == 0:
        return np.empty(0, dtype=np.float64)
    # A link comparison contains k by k neighbour-ID pairs. Choose a link
    # batch small enough to keep this temporary Boolean array within the
    # selected maximum number of comparison entries.
    comparison_cap = 262_144
    block_size = max(1, comparison_cap // (neighbor_count * neighbor_count))
    weights = np.empty(len(sources), dtype=np.float64)
    for start in range(0, len(sources), block_size):
        stop = min(start + block_size, len(sources))
        left = neighbors[sources[start:stop], :, np.newaxis]
        right = neighbors[targets[start:stop], np.newaxis, :]
        intersections = np.count_nonzero(left == right, axis=(1, 2))
        unions = 2 * neighbor_count - intersections
        weights[start:stop] = intersections / unions
    return weights


def _feature_vector_groups(
    features: np.ndarray,
) -> tuple[np.ndarray, Sequence[np.ndarray], np.ndarray]:
    """

    Groups cells with exactly equal marker vectors for the nearest-neighbour query.

    i) We sort primitive row indices by feature-column views and compare
    adjacent vectors in bounded blocks. ii) When every vector is distinct, the
    tree reuses our existing numeric matrix. Repeated vectors use one compact
    representative matrix. iii) Primitive member indices and offsets preserve
    stable cell-ID order within each group without per-cell array objects.

    The tree stores one vector for each group, reducing repeated identical
    queries. All original cells remain separate graph nodes; their group lists
    supply the zero-distance neighbour choices and the number of cells represented
    by each tree result.

    Args:
        features: Scaled and weighted cell-by-feature array in composite-ID order.

    Returns:
        Distinct feature vectors, views of ordered primitive cell indices for those
        vectors, and the group index associated with each original cell row.
    """

    order = np.lexsort(tuple(features[:, column] for column in range(features.shape[1]-1,-1,-1)))
    starts = np.ones(len(features), dtype=bool)
    for begin in range(1,len(features),8192):
        end = min(begin+8192,len(features))
        starts[begin:end] = np.any(features[order[begin:end]] != features[order[begin-1:end-1]],axis=1)
    if starts.all():
        # Each cell is already a distinct tree point: our numeric matrix is
        # sufficient. Singleton groups retain primitive views of stable IDs.
        rows = np.arange(len(features),dtype=np.intp)
        return features, _VectorMembers(rows,np.arange(len(features)+1,dtype=np.intp)), rows
    ordered_groups = np.cumsum(starts,dtype=np.intp)-1
    group_by_row = np.empty(len(features),dtype=np.intp)
    group_by_row[order] = ordered_groups
    representatives = order[starts]
    grouped_rows = np.argsort(group_by_row,kind='stable')
    offsets = np.concatenate((np.flatnonzero(starts),[len(features)])).astype(np.intp,copy=False)
    return features[representatives], _VectorMembers(grouped_rows,offsets), group_by_row


class _VectorMembers(Sequence[np.ndarray]):
    """Expose our feature groups through one primitive index and offset array.

    We retain stable within-group row order without allocating an array object
    for every cell. Requested group members are views of that shared index.
    """

    def __init__(self, rows, offsets):
        self.rows, self.offsets = rows, offsets

    def __len__(self):
        return len(self.offsets)-1

    def __getitem__(self,index):
        if not isinstance(index,(int,np.integer)) or index >= len(self) or index < -len(self):
            raise IndexError(index)
        if index < 0:
            index += len(self)
        return self.rows[self.offsets[index]:self.offsets[index+1]]


def _stable_group_members(
    members: np.ndarray,
    position: int,
    count: int,
) -> np.ndarray:
    """

    Selects the first requested cells from an equal-feature group, excluding the source cell.

    Args:
        members: Group's cell indices in stable row order.
        position: Source cell's position within members.
        count: Number of other group members to return, already bounded by the caller.

    Returns:
        Ordered neighbour indices. These cells have zero feature-space distance from the source.
    """

    before = min(position, count)
    after = count - before
    return np.concatenate((members[:before], members[position + 1 : position + 1 + after]))


def _neighbor_boundaries_by_group_multiplicity(
    *,
    source_groups: np.ndarray,
    distances: np.ndarray,
    candidate_groups: np.ndarray,
    group_sizes: np.ndarray,
    required: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """

    Finds the distance of the last required external neighbour and the next available cell.

    A tree result represents one feature vector, which can belong to several
    cells. We set the source group's contribution to zero, then accumulate the
    sizes of the other groups in distance order. The first cumulative count
    reaching required determines the last required cell's distance. We repeat
    that calculation for required+1 when another external cell exists.

    For example, suppose three external neighbours are needed. A group of two
    cells at distance 1 supplies the first two; another group of two at distance
    2 contains both the third and fourth cells. The two boundary distances are
    equal, so the selected third cell must be determined using stable cell-ID
    order among the tied candidates. The next calculation performs that selection.

    Args:
        source_groups: Distinct feature-vector groups whose additional neighbours
            are being selected.
        distances: Tree-query distances for those source groups, ordered by distance.
        candidate_groups: Group ID associated with each returned distance.
        group_sizes: Number of original cells represented by each distinct vector.
        required: Number of external cells still needed by each source group
            after its zero-distance neighbours have been included.

    Returns:
        Required-cell distances, next-cell distances and a Boolean vector
        identifying sources with an available next cell. Missing next-cell
        distances are recorded as infinity.

    Raises:
        CellClusteringError: The tree results do not contain enough grouped cells
            to locate a required boundary position.
    """

    non_self = candidate_groups != source_groups[:, np.newaxis]
    multiplicities = np.where(non_self, group_sizes[candidate_groups], 0)
    cumulative = np.cumsum(multiplicities, axis=1)
    kth_reached = cumulative >= required[:, np.newaxis]
    if not bool(np.all(np.any(kth_reached, axis=1))):
        raise CellClusteringError("compiled neighbor query did not find enough groups")
    rows = np.arange(len(source_groups))
    kth_columns = np.argmax(kth_reached, axis=1)
    boundary_distances = distances[rows, kth_columns]

    external_totals = int(np.sum(group_sizes)) - group_sizes[source_groups]
    has_lookahead = required < external_totals
    lookahead_reached = cumulative >= (required + 1)[:, np.newaxis]
    if not bool(np.all(np.any(lookahead_reached[has_lookahead], axis=1))):
        raise CellClusteringError("compiled neighbor query did not find the boundary lookahead")
    lookahead_columns = np.argmax(lookahead_reached, axis=1)
    lookahead_distances = np.full(len(source_groups), np.inf, dtype=np.float64)
    lookahead_distances[has_lookahead] = distances[
        rows[has_lookahead], lookahead_columns[has_lookahead]
    ]
    return boundary_distances, lookahead_distances, has_lookahead


def _select_tree_external_members(
    features: np.ndarray,
    *,
    unique_features: np.ndarray,
    source_groups: np.ndarray,
    candidate_groups: np.ndarray,
    candidate_distances: np.ndarray,
    members_by_group: Sequence[np.ndarray],
    required: np.ndarray,
    boundary_distances: np.ndarray,
) -> _ExternalMemberSelection:
    """

    Selects the external cells when the required neighbour distance has no unresolved boundary tie.

    We expand tree-returned groups up to the required distance into their
    original cell indices. Because the next cell is farther away, the number
    of expanded candidates must equal the requested count. We recalculate their
    distances from the selected feature vectors and sort by distance, followed
    by the stable cell-row index for any internal tie.

    Args:
        features: Complete scaled/weighted marker matrix in stable cell order.
        unique_features: One vector per group of equal measured profiles.
        source_groups: Groups whose last required distance has no boundary tie.
        candidate_groups: Their tree-returned candidate group IDs.
        candidate_distances: Distances associated with those candidate groups.
        members_by_group: Original stable cell-index arrays for the feature groups.
        required: Additional cell count required by each source group.
        boundary_distances: Last required external distance, calculated using
            the number of cells represented by each tree result.

    Returns:
        Selected original cell indices for each source group, followed in its
        result record by the retained-candidate and exact-distance counts.

    Raises:
        CellClusteringError: Expanding the selected tree groups produces too
            few or too many cells for the established distance boundary.
    """

    maximum_required = int(np.max(required))
    selected_members = np.full((len(source_groups), maximum_required), -1, dtype=np.intp)
    maximum_candidate_working_set = 0
    exact_distance_evaluations = 0

    for row, source_group in enumerate(source_groups):
        required_count = int(required[row])
        boundary = boundary_distances[row]
        within_boundary = (candidate_groups[row] != source_group) & (
            candidate_distances[row] <= boundary
        )
        boundary_groups = candidate_groups[row, within_boundary]
        candidate_members = np.empty(required_count, dtype=np.intp)
        cursor = 0
        for group in boundary_groups:
            group_members = members_by_group[int(group)]
            stop = cursor + len(group_members)
            if stop > required_count:
                raise CellClusteringError(
                    "strict tree boundary contained too many candidate members"
                )
            candidate_members[cursor:stop] = group_members
            cursor = stop
        if cursor != required_count:
            raise CellClusteringError(
                "strict tree boundary did not contain enough candidate members"
            )
        exact_distances = np.linalg.norm(
            features[candidate_members] - unique_features[int(source_group)],
            axis=1,
        )
        order = np.lexsort((candidate_members, exact_distances))
        selected_members[row, :required_count] = candidate_members[order]
        maximum_candidate_working_set = max(maximum_candidate_working_set, required_count)
        exact_distance_evaluations += required_count

    return _ExternalMemberSelection(
        members=selected_members,
        evidence=_CandidateSelectionEvidence(
            retained_candidate_slots=int(np.sum(required)),
            maximum_candidate_working_set=maximum_candidate_working_set,
            exact_distance_evaluations=exact_distance_evaluations,
            fallback_group_count=0,
        ),
    )


def _select_exact_external_members(
    features: np.ndarray,
    *,
    unique_features: np.ndarray,
    group_by_row: np.ndarray,
    source_groups: np.ndarray,
    required: np.ndarray,
    boundary_distances: np.ndarray,
) -> _ExternalMemberSelection:
    """

    Resolves equal-distance neighbour choices by scanning bounded groups of cells in stable order.

    We process a small block of source vectors, then read candidate feature rows
    in blocks chosen from the source count and feature count. For each source/
    candidate pair, NumPy calculates Euclidean distance. Members of the source's
    own equal-vector group are excluded because their zero-distance neighbours
    were selected separately.

    Candidates within the tree's required distance, plus a small floating-point
    allowance, are merged with the best cells retained so far. Sorting by exact
    distance and stable row index selects at most the required number per source.
    We repeat this update across all candidate blocks, then check that each
    source has enough finite-distance selections.

    This bounds the temporary difference arrays and avoids storing every
    cell-pair distance simultaneously. A population with many tied vectors can
    still require distances to most other cells; the recorded comparison count
    makes that additional calculation visible.

    Args:
        features: Complete scaled/weighted matrix in composite cell-ID order.
        unique_features: Distinct marker vectors used by the tree query.
        group_by_row: Equal-vector group associated with each original cell.
        source_groups: Groups whose required and next-cell distances were equal.
        required: Number of external cell indices needed for each source group.
        boundary_distances: Corresponding required-neighbour distances from the tree.

    Returns:
        Selected original cell indices with the retained-position, distance-
        comparison and tied-group counts. Per-row required identifies how many
        entries of the returned padded index array belong to that source.

    Raises:
        CellClusteringError: The full scan cannot supply the required count of
            valid finite-distance neighbours.
    """

    active_count = len(source_groups)
    maximum_required = int(np.max(required))
    selected_members = np.full((active_count, maximum_required), -1, dtype=np.intp)
    selected_distances = np.full((active_count, maximum_required), np.inf, dtype=np.float64)
    row_count, feature_count = features.shape
    source_block_size = max(1, min(active_count, maximum_required))
    maximum_candidate_working_set = 0
    exact_distance_evaluations = 0
    # Bound the temporary B x C x feature_count difference array while keeping
    # candidate-pair capacity no larger than row_count * k.
    working_value_cap = 262_144

    for source_start in range(0, active_count, source_block_size):
        source_stop = min(source_start + source_block_size, active_count)
        block_groups = source_groups[source_start:source_stop]
        block_size = len(block_groups)
        candidate_block_size = max(
            1,
            min(
                row_count,
                working_value_cap // max(1, block_size * feature_count),
            ),
        )
        best_members = selected_members[source_start:source_stop]
        best_distances = selected_distances[source_start:source_stop]
        boundary_upper = (
            boundary_distances[source_start:source_stop]
            + np.finfo(np.float64).eps
            * np.maximum(
                1.0,
                np.abs(boundary_distances[source_start:source_stop]),
            )
            * 8.0
        )

        for candidate_start in range(0, row_count, candidate_block_size):
            candidate_stop = min(candidate_start + candidate_block_size, row_count)
            candidate_members = np.arange(candidate_start, candidate_stop, dtype=np.intp)
            differences = (
                features[np.newaxis, candidate_start:candidate_stop, :]
                - unique_features[block_groups, np.newaxis, :]
            )
            candidate_distances = np.linalg.norm(differences, axis=2)
            candidate_groups = group_by_row[candidate_start:candidate_stop]
            eligible = (candidate_groups[np.newaxis, :] != block_groups[:, np.newaxis]) & (
                candidate_distances <= boundary_upper[:, np.newaxis]
            )
            candidate_distances = np.where(eligible, candidate_distances, np.inf)
            member_keys = np.broadcast_to(candidate_members, candidate_distances.shape)
            working_members = np.concatenate((best_members, member_keys), axis=1)
            working_distances = np.concatenate((best_distances, candidate_distances), axis=1)
            order = np.lexsort((working_members, working_distances), axis=1)
            retained = order[:, :maximum_required]
            best_members[:] = np.take_along_axis(working_members, retained, axis=1)
            best_distances[:] = np.take_along_axis(working_distances, retained, axis=1)
            candidate_pairs = block_size * len(candidate_members)
            maximum_candidate_working_set = max(maximum_candidate_working_set, candidate_pairs)
            exact_distance_evaluations += candidate_pairs

        required_mask = (
            np.arange(maximum_required)[np.newaxis, :]
            < required[source_start:source_stop, np.newaxis]
        )
        if not bool(np.all(np.isfinite(best_distances[required_mask]))):
            raise CellClusteringError(
                "exact boundary selection did not find enough stable neighbors"
            )

    return _ExternalMemberSelection(
        members=selected_members,
        evidence=_CandidateSelectionEvidence(
            retained_candidate_slots=int(np.sum(required)),
            maximum_candidate_working_set=maximum_candidate_working_set,
            exact_distance_evaluations=exact_distance_evaluations,
            fallback_group_count=len(source_groups),
        ),
    )


def _combine_candidate_selection_evidence(
    selections: Sequence[_ExternalMemberSelection],
) -> _CandidateSelectionEvidence:
    """

    Combines work counts from the strict-cutoff and tied-cutoff selection routes.

    Args:
        selections: Results for disjoint sets of source groups.

    Returns:
        Summed retained positions, distance evaluations and tie groups, with the
        largest temporary working-set count across the routes.
    """

    return _CandidateSelectionEvidence(
        retained_candidate_slots=sum(
            selection.evidence.retained_candidate_slots for selection in selections
        ),
        maximum_candidate_working_set=max(
            (selection.evidence.maximum_candidate_working_set for selection in selections),
            default=0,
        ),
        exact_distance_evaluations=sum(
            selection.evidence.exact_distance_evaluations for selection in selections
        ),
        fallback_group_count=sum(
            selection.evidence.fallback_group_count for selection in selections
        ),
    )


def _partition_cells(
    features: np.ndarray,
    k: int,
    resolution: float,
    seed: int,
    partitioner: GraphPartitioner,
) -> tuple[tuple[Hashable, ...], ClusteringEvidence]:
    """

    Builds the weighted cell graph and checks that partitioning assigns one group value per cell.

    Args:
        features: Scaled and weighted marker matrix in graph-node order.
        k: Checked requested neighbour count.
        resolution: Positive partition resolution.
        seed: Recorded random seed.
        partitioner: Callable receiving immutable feature rows, weighted edges, resolution and seed.

    Returns:
        Tuple of group membership values and the graph-construction record.

    Raises:
        CellClusteringError: The partitioner returns a different number of memberships from cells.
    """

    graph = _compiled_snn_jaccard_graph(features, k)
    partitioner_features = features.view()
    partitioner_features.flags.writeable = False
    memberships = tuple(partitioner(partitioner_features, graph.edges, resolution, seed))
    if len(memberships) != len(features):
        raise CellClusteringError(
            "partitioner returned a membership count that does not match cells"
        )
    return memberships, graph.evidence


def _leiden_partitioner(
    features: np.ndarray,
    edges: tuple[WeightedEdge, ...],
    resolution: float,
    seed: int,
) -> Sequence[Hashable]:
    """

    Divides the positive weighted cell graph using Leiden's configuration-model objective.

    We create one igraph vertex per cell and attach the selected undirected
    edges and their Jaccard-derived weights. The RBConfigurationVertexPartition
    objective compares each within-cluster weight with its expected value from
    the connected nodes' total weighted degrees. With s_i=sum_j W_ij and total
    undirected edge weight m, the objective is proportional to:

        Q = sum_ij((W_ij - resolution*s_i*s_j/(2*m)) * same_cluster(i, j))

    Increasing resolution raises the expected-weight penalty for combining
    nodes. The installed Leiden implementation applies its local moves,
    refinement and graph-aggregation steps using the supplied random seed.
    We retain its normal iteration setting. If the graph has no positive
    edges, each cell is assigned a separate singleton group directly.

    Args:
        features: Scaled feature rows; their count determines the graph's nodes.
        edges: Positive undirected pairs and their numerical weights.
        resolution: Positive multiplier of the expected-weight term above.
        seed: Random seed passed to the Leiden calculation.

    Returns:
        One membership value per cell node. The enclosing calculation converts
        those values into neutral names ordered by the smallest cell ID in each group.

    Raises:
        ClusteringDependencyError: The igraph or leidenalg package is
            unavailable when a nonempty graph needs partitioning.
    """

    if not edges:
        # Without a positive edge, retain one singleton group per cell.
        return tuple(range(len(features)))
    try:
        import igraph  # type: ignore[import-not-found]
        import leidenalg  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError) as exc:
        recovery = (
            "Repair or replace the portable application with a complete distribution."
            if getattr(sys, "frozen", False)
            else "From the source folder, install the desktop with `python -m pip install '.[desktop]'` "
            "or a headless worker with `python -m pip install .`."
        )
        raise ClusteringDependencyError(
            "Leiden clustering could not load its igraph/leidenalg backends. " + recovery
        ) from exc

    graph = igraph.Graph(
        n=len(features),
        edges=[(edge.source, edge.target) for edge in edges],
        directed=False,
    )
    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights=[edge.weight for edge in edges],
        resolution_parameter=resolution,
        seed=seed,
    )
    return tuple(partition.membership)


__all__ = [
    "CellClusteringError",
    "CellClusteringIdentity",
    "ClusteringDependencyError",
    "ClusteringEvidence",
    "FeatureRole",
    "FeatureSpec",
    "GraphPartitioner",
    "MarkerIntensityClusterAssignment",
    "MarkerIntensityClusteringResult",
    "WeightedEdge",
    "cluster_marker_matrix",
]
