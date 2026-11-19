"""

Calculates a two-dimensional UMAP view of the selected cell measurements.

We receive selected measurements as a cell-by-feature matrix with aligned
cell identities and existing cluster labels. Each column is centred on its median and divided
by its interquartile range, then multiplied by its feature weight. These
statistics use the full supplied cell population before we select the
rows for the UMAP calculation.

When the population exceeds the selected maximum, we group cells by cluster,
tissue section and primary compartment and retain a reproducible sample
from every observed group. UMAP receives those transformed rows and fits
two display coordinates. We retain their cell IDs, original measurements
and existing cluster labels so the plotted cells can be inspected in the
tissue and coloured by their measured values.

The maximum selected cell count limits the UMAP fit. Preparing and checking
the raw and transformed measurement matrices still uses all supplied cells.
Missing dependencies, an infeasible sample or an unsuccessful fit are
recorded with the settings and an explanation, without partially drawn points.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from math import isfinite
import sys
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import numpy as np

from mif_pipeline.pipeline_core.models import ROIAnalysisError

from .clustering import FeatureSpec

# ==============================================================================
#                                   Embeddings
# ==============================================================================


class EmbeddingError(ROIAnalysisError):
    """

    Reports invalid cell-feature, sampling or coordinate values used by a UMAP view.
    """

@dataclass(frozen=True, slots=True)
class EmbeddingPolicy:
    """

    Selects the cell population and parameters used for a two-dimensional UMAP fit.

    We first choose whether to fit every supplied cell or a reproducible sample.
    The requested neighbour count is reduced when necessary to remain smaller
    than the fitted population. The same saved seed controls cell sampling and
    the random state supplied to UMAP.

    Attributes:
        max_cells: Maximum number of cells permitted in the fit; initially 50,000.
        neighbors: Requested UMAP neighbour count; initially 15. Its effective
            value is at most the selected cell count minus one.
        min_dist: Value passed to UMAP to control how tightly nearby observations
            can be arranged in the display; initially 0.1, permitted from 0 to 1.
        metric: Distance used between transformed feature rows; initially Euclidean.
        seed: Integer used for sample ranking and the UMAP random state.
        full_cell: Whether all supplied cells are required. True reports an
            unavailable view when their count exceeds max_cells.
    """

    max_cells: int = 50_000
    neighbors: int = 15
    min_dist: float = 0.1
    metric: str = "euclidean"
    seed: int = 0
    full_cell: bool = False

    def __post_init__(self) -> None:
        """

        Checks the sampling limit, neighbour count and UMAP parameter values.

        We require a positive integer maximum, at least two requested neighbours,
        a finite min_dist in [0, 1], a nonblank distance name, an integer seed and
        a boolean full-population choice. A valid min_dist is retained as a float.

        Raises:
            EmbeddingError: A parameter has an unsupported type or numerical value.
        """

        if (
            isinstance(self.max_cells, bool)
            or not isinstance(self.max_cells, int)
            or self.max_cells < 1
        ):
            raise EmbeddingError("max_cells must be a positive integer")
        if (
            isinstance(self.neighbors, bool)
            or not isinstance(self.neighbors, int)
            or self.neighbors < 2
        ):
            raise EmbeddingError("neighbors must be an integer of at least two")
        if (
            isinstance(self.min_dist, bool)
            or not isinstance(self.min_dist, (int, float))
            or not isfinite(float(self.min_dist))
            or not 0.0 <= float(self.min_dist) <= 1.0
        ):
            raise EmbeddingError("min_dist must be finite and between zero and one")
        if not isinstance(self.metric, str) or not self.metric.strip():
            raise EmbeddingError("metric must be a non-empty string")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise EmbeddingError("seed must be an integer")
        if not isinstance(self.full_cell, bool):
            raise EmbeddingError("full_cell must be boolean")
        object.__setattr__(self, "min_dist", float(self.min_dist))


@dataclass(frozen=True, slots=True)
class EmbeddingArtifact:
    """

    Retains a completed cell-coordinate view or the reason its calculation was unavailable.

    For a completed view, the cell IDs, coordinates, original feature values
    and category labels share one order. We retain the selected parameters and
    UMAP version with that data. A checksum of the complete record identifies
    the view when its plotting settings are later changed.

    Attributes:
        status: "available", "unavailable" or "failed".
        reason: Recorded reason for an unavailable or failed calculation,
            otherwise None.
        message: Explanation of the unmet requirement or failed operation.
        cell_ids: Unique IDs of the cells included in the UMAP fit.
        coordinates: Finite two-dimensional positions aligned with cell_ids.
        feature_specs: Measurement definitions and weights used to prepare the inputs.
        feature_values: Original measured values for colouring these same cells.
        categorical_values: Cluster ID, section, primary compartment and selected
            display label for each fitted cell.
        parameters: Selected sampling, scaling, feature-weight and UMAP values,
            including the effective neighbour and fitted-cell counts.
        backend_version: UMAP version when the dependency was loaded.
        content_hash: Checksum calculated from the complete result description.
        artifact_id: Saved view ID, formed from "cell-embedding:" and content_hash.
    """

    status: str
    reason: str | None
    message: str | None
    cell_ids: tuple[str, ...]
    coordinates: tuple[tuple[float, float], ...]
    feature_specs: tuple[FeatureSpec, ...]
    feature_values: Mapping[str, tuple[float, ...]]
    categorical_values: Mapping[str, tuple[str, ...]]
    parameters: Mapping[str, object]
    backend_version: str | None
    content_hash: str = field(init=False)
    artifact_id: str = field(init=False)

    def __post_init__(self) -> None:
        """

        Checks the result's coordinates and cell alignment before calculating its identity.

        An available result requires nonempty unique cell IDs, the same number of
        finite coordinate pairs, a recorded UMAP version and no failure reason.
        Every feature-value and category sequence must cover that same cell order.
        Unavailable and failed results contain no partial point population.

        We copy feature/category values to tuple-valued read-only mappings and
        calculate the checksum after these checks. That identifier therefore covers
        the coordinates, displayed measurements, labels and selected settings together.

        Raises:
            EmbeddingError: Status, coordinate shape, cell IDs or associated
                feature/category lengths are invalid.
        """

        if self.status not in {"available", "unavailable", "failed"}:
            raise EmbeddingError("embedding status must be available, unavailable, or failed")
        if len(set(self.cell_ids)) != len(self.cell_ids):
            raise EmbeddingError("embedding cell IDs must be unique")
        if len(self.coordinates) != len(self.cell_ids):
            raise EmbeddingError("embedding coordinates must align with cell IDs")
        if self.status == "available":
            if not self.cell_ids or self.reason is not None or self.backend_version is None:
                raise EmbeddingError("available embedding must have points and backend evidence")
        elif self.cell_ids or self.coordinates:
            raise EmbeddingError("non-available embedding must not expose partial points")
        for point in self.coordinates:
            if len(point) != 2 or not all(isfinite(float(value)) for value in point):
                raise EmbeddingError("embedding coordinates must be finite two-dimensional points")
        object.__setattr__(self, "feature_specs", tuple(self.feature_specs))
        object.__setattr__(
            self,
            "feature_values",
            MappingProxyType(
                {
                    key: tuple(float(value) for value in values)
                    for key, values in self.feature_values.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "categorical_values",
            MappingProxyType(
                {
                    key: tuple(str(value) for value in values)
                    for key, values in self.categorical_values.items()
                }
            ),
        )
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        for values in (*self.feature_values.values(), *self.categorical_values.values()):
            if len(values) != len(self.cell_ids):
                raise EmbeddingError("embedding shading values must align with cell IDs")
        content_hash = _content_hash(self._content_mapping())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "artifact_id", f"cell-embedding:{content_hash}")

    @property
    def is_available(self) -> bool:
        """

        Reports whether the UMAP calculation produced a complete coordinate view.

        Returns:
            True when status is "available".
        """

        return self.status == "available"

    def to_export_mapping(self) -> dict[str, object]:
        """

        Converts the complete coordinate view to JSON-compatible containers.

        Returns:
            Dictionary containing the saved coordinates, cell/feature descriptions,
            parameters, status, checksum and view ID.
        """

        return _export(
            {
                **self._content_mapping(),
                "content_hash": self.content_hash,
                "artifact_id": self.artifact_id,
            }
        )

    def _content_mapping(self) -> Mapping[str, object]:
        """

        Collects the result fields used for saving and identifying a coordinate view.

        Returns:
            Mapping containing the current format, status, ordered cells/coordinates,
            feature and category values, settings and UMAP version. Its calculated
            checksum and view ID are added by the calling export method.
        """

        return {
            "schema_version": "cell-embedding/v1",
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "cell_ids": self.cell_ids,
            "coordinates": self.coordinates,
            "feature_specs": tuple(_feature_mapping(spec) for spec in self.feature_specs),
            "feature_values": self.feature_values,
            "categorical_values": self.categorical_values,
            "parameters": self.parameters,
            "backend_version": self.backend_version,
        }


def build_umap_artifact(
    values: np.ndarray,
    feature_specs: Iterable[FeatureSpec],
    *,
    cell_ids: Sequence[str],
    categories: Mapping[str, Sequence[str]],
    policy: EmbeddingPolicy = EmbeddingPolicy(),
    display_labels: Mapping[str, str] | None = None,
) -> EmbeddingArtifact:
    """

    Prepares selected cell features, chooses the fitting population and calculates two UMAP coordinates.

    We validate every supplied cell and selected measurement before calculating
    the column medians and interquartile ranges. The transformed input is:

        Z[i, j] = weight[j] * (X[i, j] - median[j]) / scale[j]

    where scale is Q75 - Q25, or 1 when that difference is zero. With more cells
    than permitted, we retain every observed cluster/section/compartment group
    and allocate its sample as described by _sample_indices(). A request to
    fit all cells instead reports that its maximum must be raised.

    UMAP receives the selected Z rows, n_components=2, the effective neighbour
    count, min_dist, distance metric and saved seed. We check its returned shape
    and finite coordinates before associating them with the selected cell IDs.
    Original X values, cluster IDs and display labels are retained for plotting.

    Args:
        values: Complete Float64 matrix in the accepted clustering row order.
        cell_ids: Unique composite cell identities aligned with matrix rows.
        categories: Aligned cluster_id, section_id and primary_compartment labels.
        feature_specs: Ordered measurement definitions and numerical feature weights.
        policy: Sampling and UMAP settings for this view.
        display_labels: Optional phenotype/display names for cluster IDs already
            present among the cells.

    Returns:
        Complete coordinate-view record. An invalid input, insufficient population,
        unavailable dependency or infeasible sample returns an unavailable result.
        An exception or invalid coordinates during UMAP fitting returns a failed
        result containing its parameters and error explanation.

    Raises:
        EmbeddingError: policy is not an EmbeddingPolicy.
    """

    if not isinstance(policy, EmbeddingPolicy):
        raise EmbeddingError("policy must be an EmbeddingPolicy")
    selected_specs = tuple(feature_specs)
    try:
        ordered, categories, features, raw_rows, scaled_rows, feature_ids, labels = _preflight(
            values, cell_ids, categories, selected_specs, display_labels
        )
    except EmbeddingError as exc:
        return _unavailable("invalid_input", str(exc), selected_specs, policy)

    parameters = _parameters(policy, feature_ids, tuple(spec.weight for spec in features))
    if len(ordered) < 3:
        return _unavailable(
            "insufficient_cells",
            "UMAP requires at least three valid cells",
            features,
            policy,
            parameters,
        )
    if policy.full_cell and len(ordered) > policy.max_cells:
        return _unavailable(
            "full_cell_preflight_exceeds_max_cells",
            "full-cell UMAP exceeds the configured max_cells resource preflight",
            features,
            policy,
            parameters,
        )
    selected_indices = _sample_indices(ordered, categories, policy)
    if selected_indices is None:
        return _unavailable(
            "sampling_budget_infeasible",
            "max_cells cannot retain every cluster/section/compartment stratum",
            features,
            policy,
            parameters,
        )
    # Scaling used all supplied cells. Only these selected rows enter the
    # optional UMAP fit, while colour values retain their original units.
    sample_rows = scaled_rows[selected_indices]
    parameters = dict(parameters)
    parameters["sample_mode"] = "full" if len(selected_indices) == len(ordered) else "stratified"
    parameters["sampled_cell_count"] = len(selected_indices)
    parameters["effective_neighbors"] = min(policy.neighbors, len(selected_indices) - 1)

    try:
        # Load UMAP only after the display population and sample are ready.
        import umap  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError) as exc:
        recovery = (
            "Repair or replace the portable application with a complete distribution."
            if getattr(sys, "frozen", False)
            else "From the source folder, install the desktop with `python -m pip install '.[desktop]'` "
            "or a headless worker with `python -m pip install .`."
        )
        return _unavailable(
            "dependency_unavailable",
            f"UMAP could not load its backend ({exc}). {recovery}",
            features, policy, parameters,
        )

    try:
        reducer = umap.UMAP(
            n_components=2,
            n_neighbors=parameters["effective_neighbors"],
            min_dist=policy.min_dist,
            metric=policy.metric,
            random_state=policy.seed,
            n_jobs=1,
        )
        import numba
        previous_threads = numba.get_num_threads()
        try:
            numba.set_num_threads(1)
            coordinates = np.asarray(reducer.fit_transform(sample_rows), dtype=np.float64)
        finally:
            numba.set_num_threads(previous_threads)
        if coordinates.shape != (len(selected_indices), 2) or not np.all(np.isfinite(coordinates)):
            raise EmbeddingError(
                "UMAP backend returned non-finite or non-two-dimensional coordinates"
            )
    except Exception as exc:  # Preserve the optional view failure with its settings.
        return _failed(
            "backend_failure",
            str(exc),
            features,
            policy,
            parameters,
            backend_version=str(getattr(umap, "__version__", "unknown")),
        )

    return EmbeddingArtifact(
        status="available",
        reason=None,
        message=None,
        cell_ids=tuple(ordered[index] for index in selected_indices),
        coordinates=tuple((float(row[0]), float(row[1])) for row in coordinates),
        feature_specs=features,
        feature_values={
            feature_id: tuple(float(value) for value in raw_rows[selected_indices, column])
            for column, feature_id in enumerate(feature_ids)
        },
        categorical_values={
            **{name: tuple(items[index] for index in selected_indices)
               for name, items in categories.items()},
            "display_label": tuple(labels[categories["cluster_id"][index]]
                                   for index in selected_indices),
        },
        parameters=parameters,
        backend_version=str(getattr(umap, "__version__", "unknown")),
    )


def _preflight(values, cell_ids, categories, feature_specs, display_labels):
    """Check aligned numerical rows and calculate full-population feature scaling.

    The source reader establishes cell order. We preserve it, require a unique
    identity for every row and retain only the three categories used by sampling.
    No cell geometry or reconstructed CellRecord is needed for this calculation.
    """
    identities = tuple(cell_ids)
    features = tuple(feature_specs)
    rows = np.asarray(values, dtype=np.float64)
    if not identities or any(not isinstance(item, str) or not item for item in identities):
        raise EmbeddingError("UMAP requires nonempty cell identities")
    if len(set(identities)) != len(identities):
        raise EmbeddingError("UMAP cell identities must be unique")
    if not features or any(not isinstance(spec, FeatureSpec) for spec in features):
        raise EmbeddingError("UMAP requires selected FeatureSpec values")
    feature_ids = tuple(spec.feature_id or spec.measurement for spec in features)
    if len(set(feature_ids)) != len(feature_ids):
        raise EmbeddingError("Selected feature identities must be unique")
    if rows.shape != (len(identities), len(features)) or not np.all(np.isfinite(rows)):
        raise EmbeddingError("UMAP requires a complete numerical row for each cell")
    category_rows = {}
    for name in ("cluster_id", "section_id", "primary_compartment"):
        items = tuple(categories.get(name, ()))
        if len(items) != len(identities) or any(not isinstance(item, str) or not item for item in items):
            raise EmbeddingError(f"UMAP {name} values must align with cell identities")
        category_rows[name] = items
    clusters = set(category_rows["cluster_id"])
    supplied = {} if display_labels is None else display_labels
    if not isinstance(supplied, Mapping) or set(supplied) - clusters:
        raise EmbeddingError("Display names must refer to the supplied clusters")
    labels = {cluster: supplied.get(cluster, cluster) for cluster in clusters}
    if any(not isinstance(label, str) or not label.strip() for label in labels.values()):
        raise EmbeddingError("Display names must contain text")
    scaled = _robust_scale(rows)
    scaled *= np.asarray([spec.weight for spec in features])
    return identities, category_rows, features, rows, scaled, feature_ids, labels


def _sample_indices(identities, categories, policy):
    """Retain a seeded sample from every cluster, image-section and compartment.

    Proportional quotas preserve each observed group. Stable identity hashes
    select its cells, and sorted indices preserve the accepted matrix order.
    """
    if policy.full_cell or len(identities) <= policy.max_cells:
        return np.arange(len(identities), dtype=np.intp)
    strata = {}
    groups = zip(categories["cluster_id"], categories["section_id"], categories["primary_compartment"])
    for index, group in enumerate(groups):
        strata.setdefault(group, []).append(index)
    if len(strata) > policy.max_cells:
        return None
    quotas = _stratified_quotas({key: len(indices) for key, indices in strata.items()}, policy.max_cells)
    selected = []
    for key in sorted(strata):
        ranked = sorted(strata[key], key=lambda index: _sample_rank(policy.seed, identities[index]))
        selected.extend(ranked[:quotas[key]])
    return np.asarray(sorted(selected), dtype=np.intp)


def _stratified_quotas(
    sizes: Mapping[tuple[str, str, str], int], cap: int
) -> Mapping[tuple[str, str, str], int]:
    """

    Divides the permitted UMAP sample across groups after reserving one cell from each.

    We give every group one place. Let r be the unassigned places and a_g the
    unselected cells still available in group g. Its next proposed addition is:

        proposed_g = r * a_g / sum_h(a_h)

    We allocate the integer part of each proposal, update the remaining cells
    and places, and repeat. When every integer part is zero, one place goes to
    the largest proposal, with group-key order resolving ties. No allocation
    can exceed the group's available cells.

    For group sizes (90, 9, 1) and ten places, this calculation retains (8, 1, 1).
    The smallest group therefore remains visible, but plotted group proportions
    need not equal the proportions in the full cell population.

    Args:
        sizes: Positive group counts keyed by cluster, section and compartment.
        cap: Total number of selected cells, already sufficient for one per group.

    Returns:
        Read-only mapping containing the number of cells to select per group.
    """

    # Retain every observed group, then divide the remaining sample places
    # according to the number of cells still available in each group.
    quotas = {key: 1 for key in sizes}
    remaining = cap - len(quotas)
    availability = {key: size - 1 for key, size in sizes.items()}
    while remaining and any(value > 0 for value in availability.values()):
        total = sum(availability.values())
        ideal = {key: remaining * availability[key] / total for key in availability}
        additions = {key: min(availability[key], int(ideal[key])) for key in availability}
        allocated = sum(additions.values())
        if not allocated:
            key = min(
                (key for key, value in availability.items() if value > 0),
                key=lambda item: (-ideal[item], item),
            )
            additions[key] = 1
            allocated = 1
        for key, count in additions.items():
            quotas[key] += count
            availability[key] -= count
        remaining -= allocated
    return MappingProxyType(quotas)


def _sample_rank(seed: int, cell_id: str) -> bytes:
    """

    Calculates a repeatable ordering value from the sample seed and cell ID.

    Args:
        seed: Recorded integer seed for the fitting sample.
        cell_id: Unique cell identifier within that sample's population.

    Returns:
        Sixteen-byte BLAKE2b value used to order the cells within their group.
    """

    return hashlib.blake2b(f"{seed}\0{cell_id}".encode("utf-8"), digest_size=16).digest()


def _robust_scale(rows: np.ndarray) -> np.ndarray:
    """

    Centres each feature on its median and scales it by its interquartile range.

    For column j, we calculate median[j], Q25[j] and Q75[j] over all supplied
    cells. The transformed value is (X - median) / (Q75 - Q25). When the
    interquartile range is zero, we use divisor 1 and retain median centring.
    The caller multiplies the resulting columns by their selected feature weights.

    Args:
        rows: Finite float64 matrix with cells in rows and selected features in columns.

    Returns:
        Scaled matrix with the same cell and feature order.
    """

    centers = np.median(rows, axis=0)
    quartiles = np.percentile(rows, (25.0, 75.0), axis=0)
    scales = quartiles[1] - quartiles[0]
    return (rows - centers) / np.where(scales > 0.0, scales, 1.0)


def _parameters(
    policy: EmbeddingPolicy,
    feature_ids: tuple[str, ...],
    feature_weights: tuple[float, ...] = (),
) -> Mapping[str, object]:
    """

    Records the selected sampling and coordinate-fitting settings with the feature order.

    Args:
        policy: Checked UMAP and cell-selection settings.
        feature_ids: Measurement identities in matrix-column order.
        feature_weights: Multipliers applied after median/IQR scaling.

    Returns:
        Read-only mapping describing the requested settings, two-coordinate
        output and robust_scale_then_feature_weight calculation.
    """

    return MappingProxyType(
        {
            "max_cells": policy.max_cells,
            "neighbors": policy.neighbors,
            "min_dist": policy.min_dist,
            "metric": policy.metric,
            "seed": policy.seed,
            "full_cell": policy.full_cell,
            "feature_ids": feature_ids,
            "feature_weights": feature_weights,
            "scaling": "robust_scale_then_feature_weight",
            "n_components": 2,
        }
    )


def _unavailable(
    reason: str,
    message: str,
    features: Iterable[FeatureSpec],
    policy: EmbeddingPolicy,
    parameters: Mapping[str, object] | None = None,
) -> EmbeddingArtifact:
    """

    Records why a requested coordinate view could not proceed to fitting.

    Args:
        reason: Category identifying the unmet requirement.
        message: Explanation of the input, sample-size or dependency problem.
        features: Selected measurement definitions to retain.
        policy: Requested UMAP settings.
        parameters: Resolved settings when available, otherwise None.

    Returns:
        Unavailable result with no fitted coordinates and the retained explanation.
    """

    return _non_available("unavailable", reason, message, features, policy, parameters)


def _failed(
    reason: str,
    message: str,
    features: Iterable[FeatureSpec],
    policy: EmbeddingPolicy,
    parameters: Mapping[str, object] | None = None,
    *,
    backend_version: str | None = None,
) -> EmbeddingArtifact:
    """

    Records the failure of a UMAP fit after its implementation was loaded.

    Args:
        reason: Recorded fitting-failure category.
        message: Exception or coordinate-validation explanation.
        features: Measurement definitions supplied to the attempted fit.
        policy: Requested UMAP settings.
        parameters: Resolved fitting settings when available.
        backend_version: Version of the UMAP implementation that attempted the fit.

    Returns:
        Failed result with the selected settings and no partial coordinate array.
    """

    return _non_available(
        "failed",
        reason,
        message,
        features,
        policy,
        parameters,
        backend_version=backend_version,
    )


def _non_available(
    status: str,
    reason: str,
    message: str,
    features: Iterable[FeatureSpec],
    policy: EmbeddingPolicy,
    parameters: Mapping[str, object] | None,
    *,
    backend_version: str | None = None,
) -> EmbeddingArtifact:
    """

    Creates an unavailable or failed view while retaining its valid measurement descriptions.

    We keep only typed FeatureSpecs, construct their ordered IDs and associate
    empty coordinate/colour sequences with the selected settings. The result
    can then report why its display is absent alongside a completed clustering
    analysis.

    Args:
        status: "unavailable" or "failed".
        reason: Input, dependency, sample or fitting reason.
        message: Explanation accompanying the reason.
        features: Selected measurement descriptions from the attempted view.
        policy: Requested UMAP settings.
        parameters: Previously resolved settings, or None to record the policy values.
        backend_version: UMAP version when it was loaded, otherwise None.

    Returns:
        Complete status record with empty point data and the available settings.
    """

    specs = tuple(spec for spec in features if isinstance(spec, FeatureSpec))
    feature_ids = tuple(spec.feature_id or spec.measurement for spec in specs)
    return EmbeddingArtifact(
        status=status,
        reason=reason,
        message=message,
        cell_ids=(),
        coordinates=(),
        feature_specs=specs,
        feature_values={feature_id: () for feature_id in feature_ids},
        categorical_values={
            "cluster_id": (),
            "section_id": (),
            "primary_compartment": (),
            "display_label": (),
        },
        parameters=parameters
        or _parameters(policy, feature_ids, tuple(spec.weight for spec in specs)),
        backend_version=backend_version,
    )


def _feature_mapping(spec: FeatureSpec) -> Mapping[str, object]:
    """

    Collects the input feature description retained with the coordinate view.

    Args:
        spec: FeatureSpec supplied to the display calculation.

    Returns:
        Dictionary containing the measurement identity, weight and descriptive
        fields. The view's parameters separately record its actual median/IQR
        scaling calculation.
    """

    return {
        "measurement": spec.measurement,
        "role": spec.role.value,
        "channel_id": spec.channel_id,
        "feature_id": spec.feature_id,
        "transform": spec.transform,
        "weight": spec.weight,
        "kind": spec.kind,
        "biomarker": spec.biomarker,
        "compartment": spec.compartment,
        "units": spec.units,
        "producer": spec.producer,
        "producer_version": spec.producer_version,
        "missing_rate": spec.missing_rate,
        "non_finite_rate": spec.non_finite_rate,
        "opt_in": spec.opt_in,
    }


def _content_hash(value: Mapping[str, object]) -> str:
    """

    Calculates the identity of the saved coordinates, feature values and fitting settings.

    Args:
        value: Complete result mapping before its checksum is added.

    Returns:
        SHA-256 of finite, sorted-key JSON. Cell and feature sequence order
        remains part of the encoded result.
    """

    payload = json.dumps(
        value, default=_json_default, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_default(value: object) -> object:
    """

    Converts a supported read-only container for JSON encoding.

    Args:
        value: Mapping or tuple encountered by the encoder.

    Returns:
        Ordinary dictionary or list containing its values.

    Raises:
        TypeError: The value has an unsupported type.
    """

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported embedding artifact value {type(value).__name__}")


def _export(value: object) -> object:
    """

    Recursively converts the saved coordinate-view fields into dictionaries and lists.

    Args:
        value: Nested mappings, tuples and scalar values from the result.

    Returns:
        JSON-compatible data preserving the point and feature order.
    """

    if isinstance(value, Mapping):
        return {str(key): _export(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export(item) for item in value]
    return value


__all__ = ["EmbeddingArtifact", "EmbeddingError", "EmbeddingPolicy", "build_umap_artifact"]
