"""

Calculates the marker profiles, cell examples and tissue locations used to review cell clusters.

We group the labelled cells by their fitted cluster ID and arrange the
selected measurements as a cell-by-feature matrix. For each cluster, we
calculate the arithmetic mean and median of every measurement and retain
its sorted values for distribution plots. Section and compartment counts
show where those cluster members occur in the tissue.

The review also selects actual cells near the cluster's median feature
vector and near each feature's quartiles. Their saved full-resolution
centroids allow the image viewer to locate the corresponding cell masks.
These summaries use the original measurement values, including their
selected cellular compartments and units.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Iterable, Mapping

import numpy as np

from mif_pipeline.pipeline_core.models import CellRecord, ROIAnalysisError

from .clustering import FeatureSpec

# ==============================================================================
#                                 Cluster review
# ==============================================================================


class ClusterReviewError(ROIAnalysisError):
    """

    Reports incomplete cluster assignments or invalid measurements among the cells being reviewed.
    """

@dataclass(frozen=True, slots=True)
class ClusterProfile:
    """

    Retains the numerical profile and tissue distribution of one fitted cluster.

    Attributes:
        cluster_id: Cluster ID assigned by the completed clustering calculation.
        cell_count: Number of cells belonging to this cluster.
        feature_means: Arithmetic means keyed by exact measurement name.
        feature_medians: Median values keyed by the same measurement names.
        section_counts: Cluster-cell count in each tissue section.
        compartment_counts: Cluster-cell count in each primary compartment;
            an empty key records cells without a primary-compartment assignment.
        representative_cell_ids: Ordered, unique example-cell IDs selected near
            the median feature vector and individual feature quartiles.
    """

    cluster_id: str
    cell_count: int
    feature_means: Mapping[str, float]
    feature_medians: Mapping[str, float]
    section_counts: Mapping[str, int]
    compartment_counts: Mapping[str, int]
    representative_cell_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """

        Copies the profile values into read-only mappings before presenting the review.

        Mean and median values are retained as floats, category counts as integers
        and example IDs as a tuple. The completed profile then remains separate
        from edits to the dictionaries originally supplied by its calculation.
        """

        object.__setattr__(self, "feature_means", _freeze_float_mapping(self.feature_means))
        object.__setattr__(self, "feature_medians", _freeze_float_mapping(self.feature_medians))
        object.__setattr__(self, "section_counts", _freeze_count_mapping(self.section_counts))
        object.__setattr__(
            self, "compartment_counts", _freeze_count_mapping(self.compartment_counts)
        )
        object.__setattr__(self, "representative_cell_ids", tuple(self.representative_cell_ids))


@dataclass(frozen=True, slots=True)
class ClusterHeatmapRow:
    """

    Provides one cluster's selected mean and median measurements for a heatmap row.

    Attributes:
        cluster_id: Cluster represented by this row.
        feature_means: Means in the original measurement units.
        feature_medians: Medians in those units, using the same feature names.
    """

    cluster_id: str
    feature_means: Mapping[str, float]
    feature_medians: Mapping[str, float]

    def __post_init__(self) -> None:
        """

        Retains independent read-only copies of the row's means and medians.
        """

        object.__setattr__(self, "feature_means", _freeze_float_mapping(self.feature_means))
        object.__setattr__(self, "feature_medians", _freeze_float_mapping(self.feature_medians))


@dataclass(frozen=True, slots=True)
class SpatialOverlayRow:
    """

    Connects a cluster-assigned cell to its location in the original tissue image.

    Attributes:
        cell_id: ID used to find the corresponding cell object.
        slide_id: Slide containing the cell.
        section_id: Tissue section containing the cell.
        x_level0: Cell-centroid X coordinate in full-resolution image pixels.
        y_level0: Cell-centroid Y coordinate in full-resolution image pixels.
        cluster_id: Fitted cluster assignment displayed at that location.
        primary_compartment: Accepted primary tissue class, or None when unassigned.
    """

    cell_id: str
    slide_id: str
    section_id: str
    x_level0: float
    y_level0: float
    cluster_id: str
    primary_compartment: str | None


@dataclass(frozen=True, slots=True)
class ClusterReview:
    """

    Collects the cluster summaries, complete distributions and image locations for review.

    We retain the fitted memberships while presenting the measurements needed
    to interpret them. Phenotype naming is applied after that review using the
    separate cluster-to-name operation.

    Attributes:
        profile_rows: Cluster profiles ordered by cluster ID.
        heatmap_rows: Corresponding mean/median measurement rows.
        feature_distributions: Sorted measured values for every cluster/feature
            pair, including every cell used in that cluster's review.
        spatial_overlay_rows: Cell locations and cluster IDs, sorted by cell ID.
    """

    profile_rows: tuple[ClusterProfile, ...]
    heatmap_rows: tuple[ClusterHeatmapRow, ...]
    feature_distributions: Mapping[str, Mapping[str, tuple[float, ...]]]
    spatial_overlay_rows: tuple[SpatialOverlayRow, ...]

    def __post_init__(self) -> None:
        """

        Copies the review collections and nested distributions into read-only containers.

        Each per-feature list becomes a tuple and each cluster's feature dictionary
        becomes read-only. The recorded distributions therefore retain the values
        used to calculate and display this review.
        """

        object.__setattr__(self, "profile_rows", tuple(self.profile_rows))
        object.__setattr__(self, "heatmap_rows", tuple(self.heatmap_rows))
        object.__setattr__(self, "spatial_overlay_rows", tuple(self.spatial_overlay_rows))
        frozen_distributions: dict[str, Mapping[str, tuple[float, ...]]] = {}
        for cluster_id, by_feature in self.feature_distributions.items():
            frozen_distributions[cluster_id] = MappingProxyType(
                {feature: tuple(values) for feature, values in by_feature.items()}
            )
        object.__setattr__(self, "feature_distributions", MappingProxyType(frozen_distributions))

    @property
    def representative_cell_ids(self) -> tuple[str, ...]:
        """

        Collects the selected example IDs across clusters while retaining each cell once.

        Returns:
            Tuple in cluster-profile order, followed by each profile's example order.
        """

        return tuple(
            dict.fromkeys(
                cell_id
                for profile in self.profile_rows
                for cell_id in profile.representative_cell_ids
            )
        )

    @property
    def distributions(self) -> Mapping[str, Mapping[str, tuple[float, ...]]]:
        """

        Provides the complete per-cluster measurement values retained for distribution plots.

        Returns:
            Read-only cluster-to-feature mapping of sorted measurement tuples.
        """

        return self.feature_distributions


def build_cluster_review(
    clustered_cells: Iterable[CellRecord], feature_specs: Iterable[FeatureSpec]
) -> ClusterReview:
    """

    Calculates the measured profile and image locations of every fitted cell cluster.

    We first check the selected feature definitions, unique cell IDs, nonempty
    cluster assignments and finite measurement values. After grouping by cluster
    ID, the cells within each group are sorted by ID and arranged in a float64
    matrix with one column per selected measurement.

    For column j, the mean is the sum of its values divided by cluster size;
    the median and quartiles use the ordered column values. We retain both the
    summary values and complete sorted distribution, then select example cells
    using _representative_ids(). Section and primary-compartment counts are
    calculated from the same cluster members. Finally, the original centroids
    and cluster IDs are recorded for image navigation.

    Args:
        clustered_cells: Cells with completed cluster assignments and the
            selected measurement values.
        feature_specs: Ordered FeatureSpecs naming the measurements to summarize.

    Returns:
        ClusterReview containing the profiles, heatmap values, complete
        distributions, example IDs and full-resolution cell positions.

    Raises:
        ClusterReviewError: Features are empty or repeated, cell IDs repeat,
            a cluster assignment is missing or a selected measurement is nonfinite.
    """

    cells = tuple(clustered_cells)
    features = _validated_features(feature_specs)
    _validate_cells(cells, features)
    grouped: dict[str, list[CellRecord]] = {}
    for cell in cells:
        assert cell.cluster_id is not None
        grouped.setdefault(cell.cluster_id, []).append(cell)

    profiles: list[ClusterProfile] = []
    heatmap_rows: list[ClusterHeatmapRow] = []
    distributions: dict[str, Mapping[str, tuple[float, ...]]] = {}
    for cluster_id in sorted(grouped):
        members = tuple(sorted(grouped[cluster_id], key=lambda cell: cell.cell_id))
        matrix = np.asarray(
            [[_measurement(cell, spec.measurement) for spec in features] for cell in members],
            dtype=np.float64,
        )
        means = dict(
            zip((spec.measurement for spec in features), matrix.mean(axis=0).tolist(), strict=True)
        )
        medians = dict(
            zip(
                (spec.measurement for spec in features),
                np.median(matrix, axis=0).tolist(),
                strict=True,
            )
        )
        # Keep original measurement units in summaries and distributions.
        # The fitted graph retains its own medians, scales and feature weights.
        distributions[cluster_id] = MappingProxyType(
            {
                spec.measurement: tuple(sorted(matrix[:, index].tolist()))
                for index, spec in enumerate(features)
            }
        )
        profiles.append(
            ClusterProfile(
                cluster_id=cluster_id,
                cell_count=len(members),
                feature_means=means,
                feature_medians=medians,
                section_counts=_counts(cell.section_id for cell in members),
                compartment_counts=_counts(cell.primary_compartment or "" for cell in members),
                representative_cell_ids=_representative_ids(members, matrix),
            )
        )
        heatmap_rows.append(ClusterHeatmapRow(cluster_id, means, medians))

    overlays = tuple(
        SpatialOverlayRow(
            cell_id=cell.cell_id,
            slide_id=cell.slide_id,
            section_id=cell.section_id,
            x_level0=cell.x_level0,
            y_level0=cell.y_level0,
            cluster_id=cell.cluster_id or "",
            primary_compartment=cell.primary_compartment,
        )
        for cell in sorted(cells, key=lambda cell: cell.cell_id)
    )
    return ClusterReview(tuple(profiles), tuple(heatmap_rows), distributions, overlays)


def _representative_ids(cells: tuple[CellRecord, ...], matrix: np.ndarray) -> tuple[str, ...]:
    """

    Selects observed cells near the cluster's median feature vector and each feature's quartiles.

    For the cell-by-feature matrix X, we calculate the median of each column
    to obtain vector m. The first example minimizes the summed squared distance:

        distance_i = sum_j((X[i, j] - m[j]) ** 2)

    We then calculate each feature's 25th, 50th and 75th percentiles and select
    the cell minimizing its absolute difference from that percentile. Repeated
    selections are kept once. Exact ties use the lowest cell ID, which makes
    the example list reproducible after the input cells have been reordered.

    The first distance uses the original measurement scales. A feature with
    a larger numerical spread can therefore have greater influence on that
    example. The separate per-feature quartile examples provide cells spanning
    each selected measurement even when it contributes little to that summed distance.

    Args:
        cells: Cluster members aligned with the matrix rows.
        matrix: Finite raw-measurement array with shape (cells, features).

    Returns:
        Unique example IDs, starting with the nearest-to-median-vector cell
        followed by the selected quartile cells in feature order.
    """

    # Compare each observed cell with the vector of feature-wise medians,
    # then add examples near the individual feature quartiles.
    center = np.median(matrix, axis=0)
    distances = np.sum((matrix - center) ** 2, axis=1)
    selected = [_stable_argmin(distances, cells)]
    for column in range(matrix.shape[1]):
        for quantile in (0.25, 0.5, 0.75):
            target = float(np.quantile(matrix[:, column], quantile))
            selected.append(_stable_argmin(np.abs(matrix[:, column] - target), cells))
    return tuple(dict.fromkeys(selected))


def _stable_argmin(values: np.ndarray, cells: tuple[CellRecord, ...]) -> str:
    """

    Selects the cell with the smallest supplied distance, resolving equal minima by cell ID.

    Args:
        values: Distance or absolute-deviation values aligned with the cells.
        cells: CellRecords corresponding to the numerical values.

    Returns:
        Lowest cell ID among values exactly equal to the minimum.
    """

    minimum = float(np.min(values))
    return min(
        cell.cell_id for cell, value in zip(cells, values, strict=True) if float(value) == minimum
    )


def _validated_features(feature_specs: Iterable[FeatureSpec]) -> tuple[FeatureSpec, ...]:
    """

    Checks that the cluster review selects distinct measurement columns.

    Args:
        feature_specs: Iterable of FeatureSpecs requested for the review.

    Returns:
        Nonempty feature tuple in the selected order.

    Raises:
        ClusterReviewError: The selection is empty, contains another record type
            or repeats an exact measurement name.
    """

    features = tuple(feature_specs)
    if not features:
        raise ClusterReviewError("cluster review requires at least one selected feature")
    if any(not isinstance(spec, FeatureSpec) for spec in features):
        raise ClusterReviewError("cluster review features must be FeatureSpec values")
    names = tuple(spec.measurement for spec in features)
    if len(set(names)) != len(names):
        raise ClusterReviewError("cluster review feature measurements must be unique")
    return features


def _validate_cells(cells: tuple[CellRecord, ...], features: tuple[FeatureSpec, ...]) -> None:
    """

    Checks the cluster assignment and selected measurements for every reviewed cell.

    We require unique cell IDs and nonblank cluster IDs, then read each selected
    measurement through _measurement(). This catches an absent or invalid value
    before the review constructs a numerical matrix for any cluster.

    Args:
        cells: Cells included in this review.
        features: Selected exact measurement definitions.

    Raises:
        ClusterReviewError: IDs repeat, a cluster assignment is absent or a
            required measurement is not a finite number.
    """

    ids = tuple(cell.cell_id for cell in cells)
    if len(set(ids)) != len(ids):
        raise ClusterReviewError("cluster review stable cell IDs must be unique")
    for cell in cells:
        if not isinstance(cell.cluster_id, str) or not cell.cluster_id.strip():
            raise ClusterReviewError(f"cell {cell.cell_id} does not have a non-empty cluster_id")
        for spec in features:
            _measurement(cell, spec.measurement)


def _measurement(cell: CellRecord, name: str) -> float:
    """

    Reads one selected cell measurement as a finite numerical value.

    Args:
        cell: CellRecord containing its marker_measurements dictionary.
        name: Exact measurement key from the selected feature definition.

    Returns:
        Float in the measurement's recorded units.

    Raises:
        ClusterReviewError: The value is absent, boolean, nonnumeric, NaN or infinite.
    """

    value = cell.marker_measurements.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ClusterReviewError(
            f"cell {cell.cell_id} lacks a finite value for selected feature {name!r}"
        )
    return float(value)


def _counts(values: Iterable[str]) -> Mapping[str, int]:
    """

    Counts how many cluster cells carry each section or compartment label.

    Args:
        values: Category labels from the cluster's cells.

    Returns:
        Read-only label-to-count dictionary ordered by label.
    """

    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return MappingProxyType(dict(sorted(result.items())))


def _freeze_float_mapping(values: Mapping[str, float]) -> Mapping[str, float]:
    """

    Copies calculated profile values into a read-only float dictionary.

    Args:
        values: Measurement-name mapping of means or medians.

    Returns:
        Read-only copy with Python float values.
    """

    return MappingProxyType({key: float(value) for key, value in values.items()})


def _freeze_count_mapping(values: Mapping[str, int]) -> Mapping[str, int]:
    """

    Copies the section or compartment counts into a read-only integer dictionary.

    Args:
        values: Category counts calculated from the reviewed cells.

    Returns:
        Read-only copy with Python integer values.
    """

    return MappingProxyType({key: int(value) for key, value in values.items()})


__all__ = [
    "ClusterHeatmapRow",
    "ClusterProfile",
    "ClusterReview",
    "ClusterReviewError",
    "SpatialOverlayRow",
    "build_cluster_review",
]
