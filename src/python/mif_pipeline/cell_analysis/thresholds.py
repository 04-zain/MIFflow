"""Build marker review distributions, constrained fits and explicit decisions.

We preserve the selected measurement scale. Mixtures use a reproducible fitting
subset, followed by the increasing score correction of positive-component
responsibilities for both Gaussian and Gamma fits. Linear interpolation with clipped endpoints defines the
monotone score curve; score >= cutoff defines its gate or an explicit constant
outcome. These scores describe fitted component membership. Biological probability
calibration requires separate reference labels.
Histograms and final counts use the complete finite population. Missing values,
failed fits and marker-negative decisions remain different states.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations


from mif_pipeline._defaults import default_section

_STARTING_VALUES = default_section("phenotyping.thresholds")

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import json
from math import isfinite
from types import MappingProxyType

import numpy as np
from mif_pipeline.cell_analysis.marker_models import (
    NumericalFitError, fit_gaussian, fit_gamma, resolve_location_bounds,
)

from mif_pipeline.pipeline_core.errors import ROIAnalysisError

# ==============================================================================
#                                   Thresholds
# ==============================================================================


class ThresholdServiceError(ROIAnalysisError):
    """

    Reports invalid marker measurements, fitting settings or threshold-review choices.
    """

@dataclass(frozen=True, slots=True)
class GaussianFitPolicy:
    """Select shared/unshared variance and deterministic Gaussian fitting starts.

    The variance floor is relative to the squared numerical fitting scale; we
    record its original-unit value with the result. It stabilizes likelihood
    evaluation.
    """

    variance_mode: str = "shared"
    starts: int = 6
    variance_floor_relative: float = 1e-8

    def __post_init__(self) -> None:
        if self.variance_mode not in {"shared", "unshared"}:
            raise ThresholdServiceError("Gaussian variance mode must be shared or unshared")
        if isinstance(self.starts, bool) or not isinstance(self.starts, int) or self.starts < 1:
            raise ThresholdServiceError("Gaussian starts must be a positive integer")
        if not isfinite(self.variance_floor_relative) or self.variance_floor_relative <= 0:
            raise ThresholdServiceError("Gaussian variance floor must be finite and positive")


@dataclass(frozen=True, slots=True)
class ThresholdFitPolicy:
    """Define histogram, sample, convergence and score-decision settings.

    Gaussian variance behavior is nested under ``gaussian``. Both mixtures use
    deterministic multistart fitting; ``score_cutoff`` selects the monotone-score boundary.
    Histograms and classification counts use all finite values. The fitted
    likelihood and declared score cutoff determine the proposed decision.
    """

    histogram_bins: int = _STARTING_VALUES["histogram_bins"]
    gmm_max_iterations: int = _STARTING_VALUES["gmm_max_iterations"]
    gmm_tolerance: float = _STARTING_VALUES["gmm_tolerance"]
    gamma_max_iterations: int = _STARTING_VALUES["gamma_max_iterations"]
    gamma_tolerance: float = _STARTING_VALUES["gamma_tolerance"]
    fitting_sample_cap: int = _STARTING_VALUES["fitting_sample_cap"]
    seed: int = _STARTING_VALUES["seed"]
    count_batch_size: int = _STARTING_VALUES["count_batch_size"]
    gaussian: GaussianFitPolicy = field(default_factory=lambda: GaussianFitPolicy(**_STARTING_VALUES["gaussian"]))
    gamma_starts: int = _STARTING_VALUES["gamma_starts"]
    score_cutoff: float = _STARTING_VALUES["score_cutoff"]


    def __post_init__(self) -> None:
        """

        Check active fitting settings before allocating histogram or mixture arrays.

        Bin, iteration, sample and count limits are positive integers, with at least
        four histogram bins. Active convergence tolerances are finite and positive.

        Raises:
            ThresholdServiceError: A policy value has an invalid type or numerical range.
        """

        if isinstance(self.gaussian, Mapping):
            object.__setattr__(self, "gaussian", GaussianFitPolicy(**self.gaussian))
        if not isinstance(self.gaussian, GaussianFitPolicy):
            raise ThresholdServiceError("gaussian must contain Gaussian fitting settings")
        if not isfinite(self.score_cutoff) or not 0 < self.score_cutoff < 1:
            raise ThresholdServiceError("score_cutoff must lie strictly between zero and one")
        integer_fields = (
            "gamma_starts",
            "histogram_bins",
            "gmm_max_iterations",
            "gamma_max_iterations",
            "fitting_sample_cap",
            "count_batch_size",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ThresholdServiceError(f"{name} must be a positive integer")
        if self.histogram_bins < 4:
            raise ThresholdServiceError("histogram_bins must be at least four")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ThresholdServiceError("seed must be an integer")
        for name in (
            "gmm_tolerance",
            "gamma_tolerance",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ThresholdServiceError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class Histogram:
    """

    Stores histogram counts and their intensity-bin boundaries.

    Attributes:
        counts: Number of cell values in each bin, in ascending intensity order.
        bin_edges: Intensity boundaries, with one more entry than counts.
    """

    counts: tuple[int, ...]
    bin_edges: tuple[float, ...]

    def __post_init__(self) -> None:
        """

        Checks that nonnegative counts correspond to finite, strictly increasing bin edges.

        Raises:
            ThresholdServiceError: Counts are empty or negative, the edge count differs,
                or an edge is nonfinite or out of order.
        """

        if not self.counts or len(self.bin_edges) != len(self.counts) + 1:
            raise ThresholdServiceError("histogram edges must have one more item than counts")
        if any(item < 0 for item in self.counts):
            raise ThresholdServiceError("histogram counts cannot be negative")
        edges = np.asarray(self.bin_edges, dtype=np.float64)
        if not np.all(np.isfinite(edges)) or not np.all(np.diff(edges) > 0):
            raise ThresholdServiceError("histogram edges must be finite and increasing")

    @property
    def centers(self) -> tuple[float, ...]:
        """

        Calculates the representative intensity at the center of every histogram bin.

        Each center is left_edge / 2 + right_edge / 2. Splitting the sum avoids overflow
        from adding two large finite edge values before dividing by two.

        Returns:
            Bin-center intensities in the same order as the histogram counts.
        """

        edges = np.asarray(self.bin_edges)
        centers = _finite_midpoints(edges[:-1], edges[1:], context="histogram centers")
        return tuple(centers.tolist())


@dataclass(frozen=True, slots=True)
class ThresholdCounts:
    """

    Stores the numbers of cells below and at or above a threshold.

    Attributes:
        negative: Number of measurements strictly below the threshold.
        positive: Number of measurements greater than or equal to the threshold.
    """

    negative: int
    positive: int

    @property
    def total(self) -> int:
        """

        Returns the total number of negative and positive cells.
        """

        return self.negative + self.positive

    @property
    def positive_fraction(self) -> float:
        """

        Calculates positive / (negative + positive), returning zero for an empty count.
        """

        return self.positive / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class ThresholdCandidate:
    """

    Stores a proposed threshold together with the calculations supporting its review.

    An available candidate contains a finite threshold, an explicit constant
    outcome, or a direct image-score decision. Unknown and failed candidates retain
    their reasons without thresholds. Mixture support and monotone scores preserve the
    full observed fitting support. Plotting and serialization preserve the exact
    decision boundary and fitted-component interpretation.

    Attributes:
        method: Method identifier, such as "manual", "triangle", "gmm" or "gamma".
        status: "available" for a valid decision, otherwise "unavailable".
        outcome: threshold, all_positive, all_negative, direct_scores, unknown or failed.
        threshold: Proposed value in the selected measurement's original units.
        reason: Specific result or rejection reason.
        iterations: Number of completed mixture updates; zero for noniterative methods.
        converged: Mixture convergence result, or None when convergence does not apply.
        parameters: Fitted parameters and numerical checks saved with this candidate.
        density_x: Intensities at which the component-density curves were evaluated.
        component_densities: Weighted density values for each plotted mixture component.
        posterior_x: Distinct fitting intensities supporting the monotone score curve.
        positive_posterior: Monotone positive-component scores at those intensities.
        counts: Complete-population negative/positive counts, once calculated.
        interpretation: Description of the source of the candidate, such as a
            distribution-derived suggestion or a manual review value.
    """

    method: str
    status: str
    threshold: float | None
    reason: str
    iterations: int = 0
    converged: bool | None = None
    parameters: Mapping[str, object] = field(default_factory=dict)
    density_x: tuple[float, ...] = ()
    component_densities: tuple[tuple[float, ...], ...] = ()
    posterior_x: tuple[float, ...] = ()
    positive_posterior: tuple[float, ...] = ()
    counts: ThresholdCounts | None = None
    interpretation: str = "distribution_based_suggestion"
    outcome: str = "threshold"

    def __post_init__(self) -> None:
        """

        Checks threshold availability and retains an independent read-only parameter mapping.

        Raises:
            ThresholdServiceError: Status/outcome disagree, a finite gate is missing,
                or a constant, direct or unavailable result contains a threshold.
        """

        if self.status not in {"available", "unavailable"}:
            raise ThresholdServiceError("candidate status is unsupported")
        if self.status == "available":
            _validate_decision(self.outcome, self.threshold, allow_direct=True)
        elif self.threshold is not None or self.outcome not in {"unknown", "failed"}:
            raise ThresholdServiceError("unavailable candidates require unknown/failed and no threshold")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @property
    def posterior_threshold(self) -> float | None:
        """

        Returns the stored intensity threshold associated with this proposal.

        For a mixture proposal, this is the intensity where the monotone score
        reaches its configured cutoff. Constants and direct image-score decisions
        have no intensity threshold. Manual and Triangle return their stored gate.

        Returns:
            Threshold in the measurement's units, or None for an unavailable proposal.
        """

        return self.threshold


@dataclass(frozen=True, slots=True)
class ThresholdFacet:
    """

    Stores a section/compartment histogram using the complete review's bin edges.

    Attributes:
        section: Tissue-section label for the selected cells.
        compartment: Tissue-compartment label within that section.
        histogram: Counts for this group, on the common intensity-bin boundaries.
    """

    section: str
    compartment: str
    histogram: Histogram


@dataclass(frozen=True, slots=True)
class ThresholdReview:
    """

    Collects threshold candidates and the cell selections needed to inspect them.

    Attributes:
        histogram: Histogram of every eligible measurement.
        manual: Entered manual threshold and counts, or an unavailable manual entry.
        candidates: Only the explicitly requested methods, in requested order.
        facets: Histograms for the observed section/compartment groups.
        fit_cell_ids: Stable IDs of cells included in mixture fitting.
        example_cell_ids: Stable IDs of low, near-threshold and high-intensity examples.
        eligible_count: Finite cell population used for the histogram and final counts.
        unknown_count: Rows with nonfinite intensity, excluded from those denominators.
    """

    histogram: Histogram
    manual: ThresholdCandidate
    candidates: tuple[ThresholdCandidate, ...]
    facets: tuple[ThresholdFacet, ...]
    fit_cell_ids: tuple[str, ...]
    example_cell_ids: tuple[str, ...]
    eligible_count: int
    unknown_count: int = 0

    def candidate(self, method: str) -> ThresholdCandidate:
        """

        Finds the reviewed threshold entry associated with a selected method.

        Args:
            method: "manual" or a method name present in candidates.

        Returns:
            The corresponding threshold candidate, including its availability and diagnostics.

        Raises:
            ThresholdServiceError: The requested method is absent from this review.
        """

        if method == "manual":
            return self.manual
        for item in self.candidates:
            if item.method == method:
                return item
        raise ThresholdServiceError(f"unknown threshold method {method!r}")


@dataclass(frozen=True, slots=True)
class SectionThresholdOverride:
    """

    Records a section-specific threshold and our explanation for replacing the shared value.

    We identify the affected section and retain the same measurement units as
    the shared marker threshold. The saved reason and review identify why the
    section requires a different value and who accepted that choice.

    Attributes:
        section_id: Tissue section receiving the replacement value.
        threshold: Finite replacement intensity, or None for a constant outcome.
        selection_source: Method or review action used to choose the value.
        reason: Explanation of the section-specific change.
        reviewer_id: Person associated with the accepted change.
        review_record_id: Saved review containing that choice.
    """

    section_id: str
    threshold: float | None
    selection_source: str
    reason: str
    reviewer_id: str
    review_record_id: str
    outcome: str = "threshold"

    def __post_init__(self) -> None:
        """

        Require a valid decision and an explicit section, reason and reviewer record.

        Raises:
            ThresholdServiceError: A required text field is empty or the threshold is nonfinite.
        """

        for name in ("section_id", "selection_source", "reason", "reviewer_id", "review_record_id"):
            _nonempty(getattr(self, name), name)
        _validate_decision(self.outcome, self.threshold)


def _validate_adjustment_provenance(value) -> None:
    """Retain the original proposal separately from a finite reviewed adjustment."""
    for name in ("proposed_threshold", "proposed_score_cutoff", "score_cutoff"):
        number = getattr(value, name)
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not isfinite(number)):
            raise ThresholdServiceError(f"{name} must be finite numeric or null")
    if value.score_cutoff is not None:
        if value.outcome != "direct_scores" or not 0 <= value.score_cutoff <= 1:
            raise ThresholdServiceError("only direct scores have a cutoff between zero and one")
    if value.adjustment_source is None:
        if any(item is not None for item in (value.proposed_threshold, value.proposed_outcome, value.proposed_score_cutoff)):
            raise ThresholdServiceError("proposal adjustment metadata requires its input source")
        return
    if value.adjustment_source not in {"drag", "numeric"}:
        raise ThresholdServiceError("adjustment source must be drag or numeric")
    _validate_decision(value.proposed_outcome, value.proposed_threshold, allow_direct=True)
    if value.proposed_outcome == "direct_scores":
        if value.proposed_score_cutoff is None or not 0 <= value.proposed_score_cutoff <= 1 or value.score_cutoff is None:
            raise ThresholdServiceError("direct score adjustment must retain the proposed and final cutoffs")
    elif value.proposed_score_cutoff is not None or value.outcome != "threshold":
        raise ThresholdServiceError("an adjusted intensity proposal requires a final finite threshold")


@dataclass(frozen=True, slots=True)
class SlideThresholdOverride:
    """Bind a reviewed gate to a canonical JSON [sample, project entry, slide] key.

    Identically named slides from different samples or entries retain separate
    decisions. Constant outcomes have no finite threshold; they remain explicit
    when a saved rule is reapplied to its bound image population.
    """

    image_id: str
    threshold: float | None
    selection_source: str
    reason: str
    reviewer_id: str
    review_record_id: str
    outcome: str = "threshold"
    proposed_threshold: float | None = None
    proposed_outcome: str | None = None
    proposed_score_cutoff: float | None = None
    score_cutoff: float | None = None
    adjustment_source: str | None = None

    def __post_init__(self) -> None:
        for name in ("image_id", "selection_source", "reason", "reviewer_id", "review_record_id"):
            _nonempty(getattr(self, name), name)
        try:
            key = json.loads(self.image_id)
        except (ValueError, TypeError) as error:
            raise ThresholdServiceError("slide image_id must be a JSON image identity") from error
        if not isinstance(key, list) or len(key) != 3 or any(not isinstance(v, str) or not v for v in key):
            raise ThresholdServiceError("slide image_id requires sample, project entry and slide")
        _validate_decision(self.outcome, self.threshold, allow_direct=True)
        _validate_adjustment_provenance(self)


@dataclass(frozen=True, slots=True)
class AcceptedThreshold:
    """

    Stores the chosen marker threshold for the population in which it was reviewed.

    Attributes:
        marker_id: Marker assigned by this threshold.
        validation_domain: Named analysis group whose threshold review supplied the choice.
        threshold: Shared intensity gate, or None for a constant/direct/per-slide decision.
        outcome: threshold, all_positive, all_negative, direct_scores or per_slide.
        fit_scope: pooled or per_slide, selected before numerical fitting.
        slide_thresholds: Image-bound decisions for per-slide application.
        score_provenance: Frozen direct-image-score source and cutoff information.
        selection_source: Accepted method, including "manual" when entered directly.
        reviewer_id: Person associated with acceptance.
        review_record_id: Saved record of the acceptance.
        section_overrides: Explicit section-specific replacements and their review reasons.
    """

    marker_id: str
    validation_domain: str
    threshold: float | None
    selection_source: str
    reviewer_id: str
    review_record_id: str
    section_overrides: tuple[SectionThresholdOverride, ...] = ()
    outcome: str = "threshold"
    fit_scope: str = "pooled"
    slide_thresholds: tuple[SlideThresholdOverride, ...] = ()
    score_provenance: Mapping[str, object] = field(default_factory=dict)
    proposed_threshold: float | None = None
    proposed_outcome: str | None = None
    proposed_score_cutoff: float | None = None
    score_cutoff: float | None = None
    adjustment_source: str | None = None

    def __post_init__(self) -> None:
        """Keep pooled, image-specific and direct-score decisions unambiguous."""
        for name in ("marker_id", "validation_domain", "selection_source", "reviewer_id", "review_record_id"):
            _nonempty(getattr(self, name), name)
        if self.fit_scope not in {"pooled", "per_slide"}:
            raise ThresholdServiceError("fit_scope must be pooled or per_slide")
        if self.fit_scope == "per_slide":
            if self.outcome != "per_slide" or self.threshold is not None or not self.slide_thresholds:
                raise ThresholdServiceError("per-slide acceptance requires explicit slide decisions")
        else:
            _validate_decision(self.outcome, self.threshold, allow_direct=True)
        keys = [item.image_id for item in self.slide_thresholds]
        if len(keys) != len(set(keys)):
            raise ThresholdServiceError("slide decisions must have unique image identities")
        _validate_adjustment_provenance(self)
        object.__setattr__(self, "score_provenance", MappingProxyType(dict(self.score_provenance)))


def resolve_accepted_threshold(
    accepted: AcceptedThreshold, *, image_id: str, section_id: str,
) -> tuple[AcceptedThreshold | SlideThresholdOverride | SectionThresholdOverride, str]:
    """Select the persisted pooled/image decision and any section replacement.

    Selection reuses the recorded decisions. An image-scoped acceptance must
    cover the exact physical image even when the section has its own override.
    """
    selected, scope = accepted, "domain_default"
    if accepted.fit_scope == "per_slide":
        matches = tuple(item for item in accepted.slide_thresholds if item.image_id == image_id)
        if len(matches) != 1:
            raise ThresholdServiceError("accepted per-slide gate lacks this physical image")
        selected, scope = matches[0], "per_slide"
    overrides = tuple(item for item in accepted.section_overrides if item.section_id == section_id)
    if len(overrides) > 1:
        raise ThresholdServiceError("marker gate has duplicate section overrides")
    if overrides:
        selected, scope = overrides[0], "section_override"
    return selected, scope


def threshold_intensity_domain(
    selected: AcceptedThreshold | SlideThresholdOverride | SectionThresholdOverride,
) -> str:
    """Keep the automatic Gamma domain; reviewed numerical gates use finite values."""
    return ("nonnegative" if selected.selection_source == "gamma"
            and getattr(selected, "adjustment_source", None) is None else "finite")


def deterministic_stratified_indices(
    cell_ids: Sequence[object] | np.ndarray,
    sections: Sequence[object] | np.ndarray,
    compartments: Sequence[object] | np.ndarray,
    *,
    cap: int,
    seed: int = 0,
) -> np.ndarray:
    """

    Selects a reproducible fitting sample while retaining representation across tissue sections and compartments.

    Group cells by their (section, compartment) labels. When the cap permits, assign
    one place to each group, then distribute the remaining places in proportion to
    its remaining cell count. Round quotas down and assign unused places to the
    largest fractional remainders.

    Within each group, sort cells by stable ID and select evenly spaced positions,
    with a cyclic offset derived from the seed and group name. Sorting the selected
    IDs again gives the same sample when the input rows are reordered. These groups
    use section and compartment labels; XY coordinates do not enter the selection.

    Args:
        cell_ids: Unique stable cell IDs, aligned with sections and compartments.
        sections: Tissue-section label for each cell.
        compartments: Tissue-compartment label for each cell.
        cap: Maximum number of fitting cells. A cap below the number of groups can
            leave some groups without a selected cell.
        seed: Integer used to determine the within-group selection offset.

    Returns:
        Integer positions of the selected cells, ordered by stable cell ID.

    Raises:
        ThresholdServiceError: The vectors differ in length, IDs repeat, labels are
            invalid or the sample cap is not a positive integer.
    """

    ids = _string_vector(cell_ids, "cell_ids")
    section_values = _string_vector(sections, "sections")
    compartment_values = _string_vector(compartments, "compartments")
    if not (len(ids) == len(section_values) == len(compartment_values)):
        raise ThresholdServiceError("cell IDs and labels must have equal lengths")
    if len(np.unique(ids)) != len(ids):
        raise ThresholdServiceError("stable cell IDs must be unique")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ThresholdServiceError("cap must be a positive integer")
    if cap >= len(ids):
        return np.argsort(ids, kind="stable")

    separator = "\x1f"
    if np.any(np.char.find(section_values, separator) >= 0) or np.any(
        np.char.find(compartment_values, separator) >= 0
    ):
        raise ThresholdServiceError("section and compartment labels contain a reserved separator")
    strata = np.char.add(np.char.add(section_values, separator), compartment_values)
    unique, inverse, sizes = np.unique(strata, return_inverse=True, return_counts=True)
    quotas = _stratum_quotas(sizes, cap)
    selected: list[np.ndarray] = []
    for stratum_index, (stratum, quota) in enumerate(zip(unique, quotas, strict=True)):
        if quota == 0:
            continue
        members = np.flatnonzero(inverse == stratum_index)
        members = members[np.argsort(ids[members], kind="stable")]
        digest = hashlib.blake2b(f"{seed}\0{stratum}".encode("utf-8"), digest_size=8).digest()
        offset = int.from_bytes(digest, "big") % len(members)
        positions = np.floor((np.arange(quota) + 0.5) * len(members) / quota).astype(int)
        selected.append(members[(positions + offset) % len(members)])
    result = np.concatenate(selected)
    return result[np.argsort(ids[result], kind="stable")]


def fit_triangle(histogram: Histogram, policy: ThresholdFitPolicy) -> ThresholdCandidate:
    """

    Calculates the Triangle threshold from the histogram peak and its longer tail.

    Find the first and last occupied bins and the bin with the largest count.
    Select the side spanning more bins from the peak to the occupied endpoint; a
    tie selects the right side. For each bin j on that side, calculate the absolute
    line-distance numerator between its count and the peak-to-endpoint line:

        distance[j] = abs((tail_count - peak_count) * j
                          - (tail_index - peak_index) * count[j]
                          + tail_index * peak_count - tail_count * peak_index)

    The common perpendicular-distance denominator does not affect which bin has the
    largest distance. The center of the first maximizing bin becomes the threshold.

    Args:
        histogram: Counts and intensity boundaries for the complete marker population.
        policy: Fitting policy accepted by this interface. The direct Triangle
            calculation uses the supplied histogram; bin sensitivity is recorded separately.

    Returns:
        A Triangle candidate, or an unavailable result when there are fewer than
        three occupied bins, equal occupied counts or insufficient peak-to-tail geometry.
    """

    del policy
    counts = np.asarray(histogram.counts, dtype=np.float64)
    nonzero = np.flatnonzero(counts > 0)
    if len(nonzero) < 3 or np.max(counts) == np.min(counts[nonzero]):
        return _unavailable("triangle", "degenerate_histogram")
    start, end = int(nonzero[0]), int(nonzero[-1])
    peak = int(np.argmax(counts))
    use_left = peak - start > end - peak
    if use_left:
        indices = np.arange(start, peak + 1)
        tail = start
    else:
        indices = np.arange(peak, end + 1)
        tail = end
    if peak == tail or len(indices) < 3:
        return _unavailable("triangle", "dominant_peak_tail_geometry_absent")
    x1, y1 = float(peak), counts[peak]
    x2, y2 = float(tail), counts[tail]
    distances = np.abs((y2 - y1) * indices - (x2 - x1) * counts[indices] + x2 * y1 - y2 * x1)
    threshold_bin = int(indices[int(np.argmax(distances))])
    return ThresholdCandidate(
        method="triangle",
        status="available",
        threshold=histogram.centers[threshold_bin],
        reason="maximum_histogram_distance",
        iterations=0,
        converged=None,
        parameters={"bin_index": threshold_bin, "histogram_bins": len(counts)},
    )


def build_threshold_review(
    values: Sequence[float] | np.ndarray,
    cell_ids: Sequence[object] | np.ndarray,
    sections: Sequence[object] | np.ndarray,
    compartments: Sequence[object] | np.ndarray,
    policy: ThresholdFitPolicy,
    *,
    manual_threshold: float | None = None,
    methods: Sequence[str] | None = None,
    constraints: Mapping[str, object] | None = None,
    nimbus_scores: Sequence[float] | np.ndarray | None = None,
    nimbus_score_cutoff: float = 0.5,
) -> ThresholdReview:
    """Build full-scope distributions and only the requested numerical candidates.

    ``methods=None`` retains the Triangle/Gaussian/Gamma comparison API; an empty
    sequence builds distribution/manual review without any model fitting. The
    orchestration owner supplies either pooled cells or one image's cells.
    ``constraints`` is this marker's shared method configuration: ``gmm`` contains
    ``mean_bounds`` and ``gamma`` contains ``mode_bounds``, each two [lower, upper]
    pairs. Endpoints are numbers, None, or {"quantile": q}; quantiles resolve on
    all finite scope values before the reproducible fitting sample is selected.

    Both mixture families use the same positive-component increasing score correction.
    The selected score cutoff determines a finite gate or an explicit constant
    decision. ``nimbus`` here is the explicitly requested intensity distillation
    of already validated image scores, using exact hard-label disagreement.
    Direct Nimbus inference and artifact validation belong to orchestration.

    Nonfinite measurements are unknown and excluded from fitted values, counts
    and histograms. Empty/all-missing scopes return unknown candidates. Identity
    vectors must remain aligned and stable IDs unique, including missing rows.
    Invalid settings or constraints raise ThresholdServiceError; numerical fit
    failures are retained as failed candidates for review.
    """
    try:
        data = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ThresholdServiceError("marker values must be numeric") from error
    if data.ndim != 1:
        raise ThresholdServiceError("marker values must be a vector")
    vectors = [np.asarray(v) for v in (cell_ids, sections, compartments)]
    if any(v.ndim != 1 or len(v) != len(data) for v in vectors):
        raise ThresholdServiceError("values, cell IDs, and labels must have equal lengths")
    if len(data):
        vectors = [_string_vector(v, name) for v, name in
                   zip(vectors, ("cell_ids", "sections", "compartments"), strict=True)]
    ids, section_values, compartment_values = vectors
    if len(np.unique(ids)) != len(ids):
        raise ThresholdServiceError("stable cell IDs must be unique")
    if manual_threshold is not None and not isfinite(manual_threshold):
        raise ThresholdServiceError("manual threshold must be finite")
    selected_methods = tuple(("triangle", "gmm", "gamma") if methods is None else methods)
    if len(set(selected_methods)) != len(selected_methods) or any(
            method not in {"triangle", "gmm", "gamma", "nimbus", "manual"}
            for method in selected_methods):
        raise ThresholdServiceError("threshold methods must be unique supported method names")
    if constraints is not None and not isinstance(constraints, Mapping):
        raise ThresholdServiceError("constraints must be a method mapping")
    constraints = constraints or {}
    if set(constraints) - {"gmm", "gamma"}:
        raise ThresholdServiceError("location constraints apply only to gmm and gamma")

    # i) The complete finite population owns plots, numeric percentile bounds and
    # final counts. Its reproducible subset owns mixture fitting and score support.
    finite = np.isfinite(data)
    unknown_count = int(np.count_nonzero(~finite))
    teacher = None
    if nimbus_scores is not None:
        teacher = np.asarray(nimbus_scores, dtype=float)
        if teacher.shape != data.shape:
            raise ThresholdServiceError("Nimbus scores must align with the complete input rows")
        if np.any(np.isfinite(teacher) & ((teacher < 0) | (teacher > 1))):
            raise ThresholdServiceError("Nimbus scores must lie between zero and one")
        teacher = teacher[finite]
    data, ids, section_values, compartment_values = (
        v[finite] for v in (data, ids, section_values, compartment_values))
    counts, edges = np.histogram(data, bins=policy.histogram_bins)
    histogram = Histogram(tuple(int(v) for v in counts), tuple(float(v) for v in edges))
    fit_indices = (deterministic_stratified_indices(
        ids, section_values, compartment_values,
        cap=min(policy.fitting_sample_cap, len(data)), seed=policy.seed)
        if len(data) and any(m in {"gmm", "gamma"} for m in selected_methods)
        else np.array([], dtype=int))
    fit_values, centers = data[fit_indices], np.asarray(histogram.centers)
    candidates = []
    for method in selected_methods:
        if method == "manual":
            continue
        if not len(data):
            candidate = _unavailable(method, "no_finite_measurements")
        elif method == "triangle":
            candidate = _fit_triangle_with_sensitivity(data, histogram, policy)
        elif method == "nimbus":
            candidate = _distill_nimbus(data, teacher, nimbus_score_cutoff)
        else:
            specification = constraints.get(method, {})
            key = "mean_bounds" if method == "gmm" else "mode_bounds"
            if not isinstance(specification, Mapping) or set(specification) - {key}:
                raise ThresholdServiceError(f"{method} constraints require {key}")
            try:
                bounds = resolve_location_bounds(specification.get(key), data, gamma=method == "gamma")
            except ValueError as error:
                raise ThresholdServiceError(str(error)) from error
            candidate = _fit_mixture(method, fit_values, centers, policy, bounds,
                                     specification, complete_values=data)
        count_values = data
        if method == "nimbus" and teacher is not None:
            count_values = data[np.isfinite(teacher)]
        candidates.append(_with_counts(candidate, count_values, policy.count_batch_size))

    # ii) Manual gates are independent review decisions; recording them never
    # repeats fitting or changes the supplied measurement values.
    manual = (_unavailable("manual", "not_provided", interpretation="manual_review_value")
              if manual_threshold is None else _with_counts(ThresholdCandidate(
                  method="manual", status="available", threshold=float(manual_threshold),
                  reason="provided_by_reviewer", interpretation="manual_review_value"),
                  data, policy.count_batch_size))
    facets = _facet_histograms(data, section_values, compartment_values, histogram.bin_edges)
    example_gate = manual.threshold
    if example_gate is None:
        example_gate = next((item.threshold for item in candidates if item.threshold is not None),
                            float(np.median(data)) if len(data) else 0.)
    example_ids = (_example_ids(data, ids, section_values, compartment_values, float(example_gate))
                   if len(data) else ())
    return ThresholdReview(histogram, manual, tuple(candidates), facets,
                           tuple(str(v) for v in ids[fit_indices]), example_ids,
                           len(data), unknown_count=unknown_count)


def accept_threshold(
    review: ThresholdReview,
    *,
    marker_id: str,
    validation_domain: str,
    method: str,
    reviewer_id: str,
    review_record_id: str,
    section_overrides: Sequence[SectionThresholdOverride] = (),
    fit_scope: str = "pooled",
    slide_thresholds: Sequence[SlideThresholdOverride] = (),
) -> AcceptedThreshold:
    """

    Records the threshold we selected for a marker and its reviewed cell population.

    We retrieve the selected method's existing available proposal, including a
    valid constant or direct-image-score decision. Section-specific choices retain
    the shared value; each section can have at most one replacement. The fit
    is already complete, so this operation records the decision without changing
    the proposed intensity or re-estimating mixture parameters.

    Args:
        review: Previously calculated proposals, distributions and cell counts.
        marker_id: Marker whose positive/negative states use this threshold.
        validation_domain: Named sample group whose cells supplied the review.
        method: Selected manual, Triangle, Gaussian or gamma proposal.
        reviewer_id: Person accepting the threshold.
        review_record_id: Saved review recording the decision.
        section_overrides: Optional accepted thresholds for individual tissue sections.

    Returns:
        AcceptedThreshold containing the shared value and section-specific choices.

    Raises:
        ThresholdServiceError: A required identifier is missing, the selected
            proposal is unavailable or section-specific choices repeat a section.
    """

    for name, value in (
        ("marker_id", marker_id),
        ("validation_domain", validation_domain),
        ("reviewer_id", reviewer_id),
        ("review_record_id", review_record_id),
    ):
        _nonempty(value, name)
    candidate = review.candidate(method)
    if candidate.status != "available":
        raise ThresholdServiceError("only an available decision can be accepted")
    overrides = tuple(section_overrides)
    section_ids = tuple(item.section_id for item in overrides)
    if len(set(section_ids)) != len(section_ids):
        raise ThresholdServiceError("section threshold overrides must be unique")
    return AcceptedThreshold(
        marker_id=marker_id,
        validation_domain=validation_domain,
        threshold=candidate.threshold,
        selection_source=method,
        reviewer_id=reviewer_id,
        review_record_id=review_record_id,
        section_overrides=overrides,
        outcome=candidate.outcome,
        fit_scope=fit_scope,
        slide_thresholds=tuple(slide_thresholds),
        score_provenance=candidate.parameters.get("score_provenance", {}),
    )


def index_accepted_thresholds(
    acceptances: Sequence[AcceptedThreshold],
) -> Mapping[tuple[str, str], AcceptedThreshold]:
    """

    Indexes accepted values by marker and review population before applying cell rules.

    Args:
        acceptances: AcceptedThreshold records to make available to the application step.

    Returns:
        Read-only mapping from (marker_id, validation_domain) to the corresponding acceptance.

    Raises:
        ThresholdServiceError: Two records specify the same marker/population pair.
    """

    indexed: dict[tuple[str, str], AcceptedThreshold] = {}
    for item in acceptances:
        key = (item.marker_id, item.validation_domain)
        if key in indexed:
            raise ThresholdServiceError(
                "one accepted threshold is permitted per marker/validation domain"
            )
        indexed[key] = item
    return MappingProxyType(indexed)


def _fit_mixture(method, values, density_x, policy, bounds, constraints, *, complete_values):
    """Adapt a converged numerical fit into a persisted review decision.

    Bounds refer to the complete fitting scope, whereas responsibilities and the
    monotone correction use actual sampled observations. Numerical failure remains a
    failed result; a valid curve with no cutoff crossing is a selectable constant.
    """
    serialized_bounds = tuple(tuple(float(v) if isfinite(v) else None for v in pair)
                              for pair in bounds)
    common = {"constraints": dict(constraints), "resolved_location_bounds": serialized_bounds,
              "quantile_method": "linear", "analysis_scale": "supplied_measurement",
              "score_cutoff": policy.score_cutoff, "positive_rule": "greater_than_or_equal",
              "score_interpretation": "monotone_component_membership",
              "score_interpolation": "linear", "score_extrapolation": "endpoint_clip",
              "monotone_method": "increasing_positive_score",
              "monotone_support": ("unique_fitting_intensities" if method == "gmm"
                                   else "unique_positive_fitting_intensities")}
    if method == "gamma" and np.any(complete_values < 0):
        return _unavailable(method, "gamma_requires_nonnegative_intensities", parameters=common)
    try:
        if method == "gmm":
            fitted = fit_gaussian(values, density_x, bounds,
                variance_mode=policy.gaussian.variance_mode, starts=policy.gaussian.starts,
                max_iterations=policy.gmm_max_iterations, tolerance=policy.gmm_tolerance,
                variance_floor_relative=policy.gaussian.variance_floor_relative, seed=policy.seed)
        else:
            fitted = fit_gamma(values, density_x, bounds, starts=policy.gamma_starts,
                max_iterations=policy.gamma_max_iterations, tolerance=policy.gamma_tolerance, seed=policy.seed)
        curve = fitted.curve
        outcome, threshold = curve.decision(policy.score_cutoff)
        parameters = {**fitted.parameters, **common}
        if method == "gamma":
            parameters["scope_zero_count"] = int(np.count_nonzero(complete_values == 0))
        return ThresholdCandidate(method=method, status="available", threshold=threshold,
            reason="monotone_score_crossing" if outcome == "threshold" else "monotone_score_no_crossing",
            iterations=fitted.iterations, converged=fitted.converged, parameters=parameters,
            density_x=tuple(density_x.tolist()),
            component_densities=tuple(tuple(v.tolist()) for v in fitted.densities),
            posterior_x=tuple(curve.x.tolist()), positive_posterior=tuple(curve.scores.tolist()),
            outcome=outcome)
    except (NumericalFitError, FloatingPointError, OverflowError) as error:
        return ThresholdCandidate(method=method, status="unavailable", threshold=None,
            reason=str(error), converged=False, parameters=common, outcome="failed")


def _fit_triangle_with_sensitivity(values, histogram, policy):
    """Retain neighboring-bin sensitivity as a diagnostic without rejecting a gate.

    The Triangle decision itself is the recorded longer-tail geometry. Neighboring
    histograms illustrate bin sensitivity; they do not provide a confidence test.
    """
    candidate = fit_triangle(histogram, policy)
    adjacent = []
    for bins in (max(4, policy.histogram_bins - 1), policy.histogram_bins + 1):
        counts, edges = np.histogram(values, bins=bins)
        result = fit_triangle(Histogram(tuple(int(v) for v in counts),
                                      tuple(float(v) for v in edges)), policy)
        adjacent.append(result.threshold)
    return replace(candidate, parameters={**candidate.parameters,
        "adjacent_bin_thresholds": tuple(adjacent), "sensitivity_is_rejection_rule": False})


def _distill_nimbus(values, scores, cutoff):
    """Find an exact minimum-disagreement intensity rule for image-score labels.

    We compare every attainable ordered split of the distinct observed intensities
    plus both constant rules. Cumulative class counts avoid an observations-by-
    thresholds matrix. Equal objective values choose the highest threshold, with
    all-negative considered above the maximum; no score regression changes this
    hard-label objective. Missing image scores remain outside the comparison.
    """
    if not isfinite(cutoff) or not 0 <= cutoff <= 1:
        raise ThresholdServiceError("Nimbus score cutoff must lie between zero and one, inclusive")
    if scores is None:
        return _unavailable("nimbus", "nimbus_scores_not_provided",
                            interpretation="nimbus_intensity_distillation")
    valid = np.isfinite(scores)
    x, labels = values[valid], scores[valid] >= cutoff
    if not len(x):
        return _unavailable("nimbus", "no_finite_nimbus_scores",
                            interpretation="nimbus_intensity_distillation")
    unique, inverse, counts = np.unique(x, return_inverse=True, return_counts=True)
    positives = np.bincount(inverse, weights=labels.astype(int)).astype(np.int64)
    negatives = counts - positives
    # Split j puts the first j unique values below the threshold. j=0 and j=J
    # are explicit all-positive/all-negative decisions, not finite sentinels.
    false_negatives = np.r_[0, np.cumsum(positives)]
    false_positives = negatives.sum() - np.r_[0, np.cumsum(negatives)]
    errors = false_negatives + false_positives
    chosen = int(np.flatnonzero(errors == errors.min())[-1])
    outcome = "all_positive" if chosen == 0 else "all_negative" if chosen == len(unique) else "threshold"
    threshold = float(unique[chosen]) if outcome == "threshold" else None
    return ThresholdCandidate(method="nimbus", status="available", threshold=threshold,
        outcome=outcome, reason="minimum_hard_label_disagreement",
        interpretation="nimbus_intensity_distillation", parameters={
            "objective": "hard_label_disagreement", "disagreements": int(errors[chosen]),
            "evaluated_count": len(x), "unknown_score_count": int(np.count_nonzero(~valid)),
            "score_positive_count": int(positives.sum()),
            "score_positive_intensity_negative": int(false_negatives[chosen]),
            "score_negative_intensity_positive": int(false_positives[chosen]),
            "nimbus_score_cutoff": float(cutoff), "tie_break": "highest_threshold",
            "positive_rule": "greater_than_or_equal"})


def _finite_midpoints(
    left: np.ndarray | np.float64 | float,
    right: np.ndarray | np.float64 | float,
    *,
    context: str,
) -> np.ndarray | np.float64:
    """

    Calculates midpoints as left/2 + right/2 to avoid overflowing their intermediate sum.

    Args:
        left: Lower value or array of values.
        right: Corresponding upper value or array.
        context: Calculation name used when reporting a nonfinite result.

    Returns:
        Finite float64 midpoint values with the broadcast shape of left and right.

    Raises:
        ThresholdServiceError: A calculated midpoint is nonfinite.
    """

    result = np.asarray(left, dtype=np.float64) * 0.5 + np.asarray(right, dtype=np.float64) * 0.5
    if not np.all(np.isfinite(result)):
        raise ThresholdServiceError(f"{context} must be finite")
    return result


def _with_counts(
    candidate: ThresholdCandidate, values: np.ndarray, batch_size: int
) -> ThresholdCandidate:
    """

    Counts all eligible cells at or above an available candidate's threshold.

    Process one measurement batch at a time using value >= threshold, then obtain
    negative_count as population_size - positive_count. The fitting-sample size does
    not replace the population size used for these counts.

    Args:
        candidate: Finite-gate or constant decision to add counts to.
        values: Complete eligible measurement vector.
        batch_size: Number of comparisons to perform in each count batch.

    Returns:
        Candidate with complete negative/positive counts, or the unavailable candidate
        when it has no threshold to apply.
    """

    if candidate.status != "available":
        return candidate
    if candidate.outcome == "all_positive":
        return replace(candidate, counts=ThresholdCounts(0, len(values)))
    if candidate.outcome == "all_negative":
        return replace(candidate, counts=ThresholdCounts(len(values), 0))
    if candidate.threshold is None:
        return candidate
    positive = 0
    for start in range(0, len(values), batch_size):
        positive += int(np.count_nonzero(values[start : start + batch_size] >= candidate.threshold))
    return replace(candidate, counts=ThresholdCounts(len(values) - positive, positive))


def _facet_histograms(
    values: np.ndarray,
    sections: np.ndarray,
    compartments: np.ndarray,
    edges: tuple[float, ...],
) -> tuple[ThresholdFacet, ...]:
    """

    Calculates section/compartment histograms on the same intensity boundaries as the complete review.

    Args:
        values: Complete eligible measurements.
        sections: Aligned tissue-section labels.
        compartments: Aligned tissue-compartment labels.
        edges: Shared bin boundaries from the complete-population histogram.

    Returns:
        ThresholdFacet records for each observed section/compartment pair.
    """

    facets: list[ThresholdFacet] = []
    for section in np.unique(sections):
        for compartment in np.unique(compartments[sections == section]):
            mask = (sections == section) & (compartments == compartment)
            counts, _ = np.histogram(values[mask], bins=np.asarray(edges))
            facets.append(
                ThresholdFacet(
                    section=str(section),
                    compartment=str(compartment),
                    histogram=Histogram(
                        tuple(int(item) for item in counts),
                        edges,
                    ),
                )
            )
    return tuple(facets)


def _example_ids(
    values: np.ndarray,
    cell_ids: np.ndarray,
    sections: np.ndarray,
    compartments: np.ndarray,
    threshold: float,
) -> tuple[str, ...]:
    """

    Selects low, near-threshold and high-intensity example cells from each section/compartment.

    For each group, select its minimum value below the threshold, its value closest
    to the threshold, and its maximum value at or above the threshold when those
    categories exist. Stable cell ID breaks ties. Repeated selections are included once.

    Args:
        values: Complete eligible marker measurements.
        cell_ids: Stable IDs aligned with values.
        sections: Aligned tissue-section labels.
        compartments: Aligned tissue-compartment labels.
        threshold: Manual or available candidate threshold used to choose examples.

    Returns:
        Ordered unique example cell IDs for image review.
    """

    selected: list[str] = []
    strata = np.char.add(np.char.add(sections, "\x1f"), compartments)
    for stratum in np.unique(strata):
        members = np.flatnonzero(strata == stratum)
        below = members[values[members] < threshold]
        above = members[values[members] >= threshold]
        choices: list[int] = []
        if len(below):
            choices.append(_stable_argmin(below, values[below], cell_ids))
        choices.append(_stable_argmin(members, np.abs(values[members] - threshold), cell_ids))
        if len(above):
            choices.append(_stable_argmin(above, -values[above], cell_ids))
        selected.extend(str(cell_ids[index]) for index in choices)
    return tuple(dict.fromkeys(selected))


def _stable_argmin(indices: np.ndarray, scores: np.ndarray, cell_ids: np.ndarray) -> int:
    """

    Finds the smallest score and uses stable cell ID to resolve an exact tie.

    Args:
        indices: Candidate row positions in the complete cell vectors.
        scores: One score for each candidate position.
        cell_ids: Complete stable-cell-ID vector indexed by indices.

    Returns:
        Original row position of the selected cell.
    """

    minimum = np.min(scores)
    tied = indices[scores == minimum]
    tied_ids = cell_ids[tied]
    return int(tied[np.argmin(tied_ids)])


def _stratum_quotas(sizes: np.ndarray, cap: int) -> np.ndarray:
    """

    Distributes the fitting-sample cap across section/compartment groups.

    When the cap permits, reserve one observation for every group first. Allocate
    the remaining places in proportion to the remaining group sizes, floor the
    allocations, then distribute unused places by descending fractional remainder.
    A cap smaller than the number of groups starts all quotas at zero.

    Args:
        sizes: Positive cell counts for the sorted section/compartment groups.
        cap: Number of fitting observations to allocate, no greater than total cells.

    Returns:
        Integer fitting quota per group, summing to cap.
    """

    if cap >= len(sizes):
        quotas = np.ones(len(sizes), dtype=np.int64)
        remaining_sizes = sizes - 1
        remaining_cap = cap - len(sizes)
    else:
        quotas = np.zeros(len(sizes), dtype=np.int64)
        remaining_sizes = sizes
        remaining_cap = cap
    if remaining_cap == 0:
        return quotas
    ideal = remaining_sizes * remaining_cap / np.sum(remaining_sizes)
    additions = np.floor(ideal).astype(np.int64)
    additions = np.minimum(additions, remaining_sizes)
    quotas += additions
    unassigned = remaining_cap - int(np.sum(additions))
    remainder_order = np.argsort(-(ideal - additions), kind="stable")
    for index in remainder_order:
        if unassigned == 0:
            break
        if quotas[index] < sizes[index]:
            quotas[index] += 1
            unassigned -= 1
    return quotas


def _string_vector(values: Sequence[object] | np.ndarray, name: str) -> np.ndarray:
    """

    Checks an aligned cell-identity or group-label vector before sampling and review.

    Args:
        values: One-dimensional sequence of nonempty strings.
        name: Vector name included in errors.

    Returns:
        Nonempty NumPy string vector.

    Raises:
        ThresholdServiceError: Shape or string values are invalid.
    """

    result = np.asarray(values)
    if result.ndim != 1 or len(result) == 0:
        raise ThresholdServiceError(f"{name} must be a non-empty vector")
    if any(not isinstance(item, str) or not item.strip() for item in result.tolist()):
        raise ThresholdServiceError(f"{name} must contain non-empty strings")
    return result.astype(str, copy=False)


def _nonempty(value: object, name: str) -> str:
    """

    Requires a text identifier or review description containing at least one nonspace character.

    Args:
        value: Text to check.
        name: Field name included in errors.

    Returns:
        The checked text.

    Raises:
        ThresholdServiceError: The value is blank or is not a string.
    """

    if not isinstance(value, str) or not value.strip():
        raise ThresholdServiceError(f"{name} must be a non-empty string")
    return value


def _validate_decision(outcome: str, threshold: float | None, *, allow_direct=False) -> None:
    """Require a finite gate or an explicit constant/direct-score decision."""
    constants = {"all_positive", "all_negative"}
    if allow_direct:
        constants.add("direct_scores")
    if outcome == "threshold":
        if threshold is None or not isfinite(threshold):
            raise ThresholdServiceError("threshold outcome requires a finite intensity threshold")
    elif outcome not in constants or threshold is not None:
        raise ThresholdServiceError("constant or direct decisions require their outcome and no threshold")


def _unavailable(
    method: str,
    reason: str,
    *,
    iterations: int = 0,
    converged: bool | None = None,
    parameters: Mapping[str, object] | None = None,
    interpretation: str = "distribution_based_suggestion",
) -> ThresholdCandidate:
    """

    Creates a threshold result explaining why a method cannot supply a value.

    Args:
        method: Method that was attempted or left unselected.
        reason: Specific condition preventing an available threshold.
        iterations: Completed fitting iterations, when applicable.
        converged: Recorded convergence outcome, when available.
        parameters: Completed fitting diagnostics and settings.
        interpretation: Description of the source of this result.

    Returns:
        ThresholdCandidate with status="unavailable" and threshold=None.
    """

    return ThresholdCandidate(
        method=method,
        status="unavailable",
        threshold=None,
        outcome="unknown",
        reason=reason,
        iterations=iterations,
        converged=converged,
        parameters=parameters or {},
        interpretation=interpretation,
    )


__all__ = [
    "AcceptedThreshold",
    "GaussianFitPolicy",
    "SlideThresholdOverride",
    "Histogram",
    "SectionThresholdOverride",
    "ThresholdCandidate",
    "ThresholdCounts",
    "ThresholdFacet",
    "ThresholdFitPolicy",
    "ThresholdReview",
    "ThresholdServiceError",
    "accept_threshold",
    "build_threshold_review",
    "deterministic_stratified_indices",
    "fit_triangle",
    "index_accepted_thresholds",
]
