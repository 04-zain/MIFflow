"""

Applies the selected marker thresholds and combines the resulting states into cell phenotypes.

We read each marker from its exact QuPath measurement name, including the
selected cellular compartment and statistic. A confirmed threshold converts
finite intensity to a Boolean marker state. Missing or nonfinite measurements
retain an unknown state and cannot satisfy a positive or negative condition.
A finite measurement at or above the threshold is marker-positive.

Each phenotype then specifies positive, negative or ignore for the selected
markers. We combine its required states with logical AND, evaluate phenotypes
in ascending precedence order and assign each cell to its first matching
phenotype. Cells that match none receive the explicitly named residual
phenotype. We retain each rule's total matches and overlaps with earlier
assignments so the effect of changing the rule order can be inspected.

Threshold fitting and threshold acceptance occur before these operations.
This module applies the supplied cutoffs and combinations to already measured
cells; their original geometry and measurements remain associated with the
returned phenotype records.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from math import isfinite
from operator import attrgetter
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import numpy as np

from mif_pipeline.pipeline_core.models import CellRecord, ROIAnalysisError
from .thresholds import AcceptedThreshold

POSITIVE = "positive"
NEGATIVE = "negative"
UNKNOWN = "unknown"
IGNORE = "ignore"
_PREDICATE_STATES = frozenset({POSITIVE, NEGATIVE, IGNORE})


# ==============================================================================
#                                  Marker rules
# ==============================================================================


class PhenotypeRuleError(ROIAnalysisError):
    """

    Reports incomplete or inconsistent cell measurements, threshold settings or phenotype rules.
    """

@dataclass(frozen=True, slots=True)
class MarkerCellValues:
    """Our scientific marker inputs without an unused cell geometry copy."""

    cell_id: str
    slide_id: str
    section_id: str
    analysis_domain_id: str
    marker_measurements: Mapping[str, float | None]
    marker_states: Mapping[str, bool | None]
    primary_phenotype: str | None = None
    phenotype_components: tuple[str, ...] = ()
    phenotype_source: str | None = None


@dataclass(frozen=True, slots=True)
class MeasurementSemantic:
    """

    Associates a recorded intensity measurement with its channel, compartment and biomarker.

    The exact measurement name identifies both the measured channel and the
    selected statistic, such as "Nucleus: CD8: Mean". We compare the accompanying
    compartment and explicit biomarker with each threshold rule before retrieving
    cell values. A rule cannot use another measurement merely because its label
    includes the same marker text.

    Attributes:
        channel_id: Image channel associated with the recorded intensity values.
        measurement: Exact key in each cell's marker_measurements dictionary.
        compartment: Name of the cellular region used for this measurement.
        biomarker: Biological target assigned to the channel, or None when absent.
    """

    channel_id: str
    measurement: str
    compartment: str
    biomarker: str | None


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """

    Defines the confirmed intensity comparison used to assign one marker's positive state.

    The measurement and compartment identify which stored cell value enters
    the comparison. The cutoff remains in those measurement units. A positive
    result is True for finite values at or above the cutoff.

    Attributes:
        biomarker: Explicit marker name whose Boolean state is assigned.
        measurement: Exact measurement key supplying the intensity values.
        compartment: Cellular compartment recorded for that measurement.
        polarity: High-intensity positivity ("positive" or "above").
        cutoff: Finite threshold in measurement units, or None for a constant outcome.
        confirmed: True after this decision has been reviewed and accepted.
        outcome: "threshold", "all_positive" or "all_negative" for the higher-intensity
            side. Missing values
            remain unknown for every outcome.
        intensity_domain: "finite" for ordinary/manual gates; "nonnegative" for
            an unadjusted automatic Gamma decision. Values outside it are unknown.
    """

    biomarker: str
    measurement: str
    compartment: str
    polarity: str
    cutoff: float | None
    confirmed: bool
    outcome: str = "threshold"
    intensity_domain: str = "finite"


@dataclass(frozen=True, slots=True)
class CompositePhenotypeRule:
    """

    Defines one reviewed phenotype using the complete selected marker panel.

    Each selected marker receives one of three choices: positive requires its
    Boolean state to be True, negative requires False, and ignore removes that
    marker from this phenotype's conditions. Applying the rule requires every
    remaining condition to match. Precedence decides which phenotype receives
    a cell when more than one rule matches.

    Attributes:
        phenotype: Name recorded for a cell assigned to this rule.
        marker_states: One positive, negative or ignore entry per selected marker.
        precedence: Unique integer defining the order in which this rule is applied.
    """

    phenotype: str
    marker_states: Mapping[str, str]
    precedence: int

    def __post_init__(self) -> None:
        """

        Retains an independent read-only copy of the selected marker choices.

        The complete marker-panel and precedence checks occur when the rules are
        applied together, where their relationships can be compared.
        """

        object.__setattr__(self, "marker_states", MappingProxyType(dict(self.marker_states)))


@dataclass(frozen=True, slots=True)
class MarkerRuleDiagnostic:
    """

    Records how many cells match one phenotype and how many it actually assigns.

    A cell can satisfy several phenotype definitions. We therefore count all
    matches before precedence, then count only the matches not already assigned
    by an earlier rule. Their difference is the overlap with earlier assignments.
    A rule with matches but no new assignments is completely hidden by earlier
    rules for this particular cell population.

    Attributes:
        phenotype: Phenotype whose matches are being summarized.
        precedence: Its position in the ascending evaluation order.
        matched: Number of cells satisfying its positive/negative conditions.
        newly_assigned: Matching cells that were unassigned before this rule.
        overlap_with_prior: matched minus newly_assigned.
        wholly_shadowed: True when matched > 0 and newly_assigned == 0.
        warning: Saved partial- or complete-overlap warning, otherwise None.
    """

    phenotype: str
    precedence: int
    matched: int
    newly_assigned: int
    overlap_with_prior: int
    wholly_shadowed: bool
    warning: str | None


@dataclass(frozen=True, slots=True)
class MarkerRuleResult:
    """

    Retains the assigned cells and the threshold and rule choices used to classify them.

    Each returned cell has one final phenotype and the marker states used by
    this rule set. We also retain the counts needed to inspect overlapping
    definitions, the named residual, and the rows supplied to the QuPath display.
    The preview identifier is calculated from the ordered source cells, marker
    states, rule definitions and accepted threshold descriptions.

    Attributes:
        cells: Classified CellRecord values in the original input order.
        marker_order: Column order of the selected Boolean marker-state matrix.
        rules: CompositePhenotypeRule values sorted by precedence.
        diagnostics: Match, new-assignment and overlap counts for those ordered rules.
        residual_phenotype: Name assigned to cells that match no listed phenotype.
        residual_count: Number of cells receiving that residual name.
        import_rows: Cell/region identities and classifications supplied to QuPath.
        accepted_thresholds: Accepted marker cutoffs and section-specific overrides
            associated with the supplied Boolean states.
        selected_display_view: Classification view selected for the imported result.
        preview_id: Checksum identifying the source state matrix and rule review.
        reordered_from_preview_id: Previous result identifier when this result
            was compared with an earlier ordering, otherwise None.
    """

    cells: tuple[CellRecord, ...]
    marker_order: tuple[str, ...]
    rules: tuple[CompositePhenotypeRule, ...]
    diagnostics: tuple[MarkerRuleDiagnostic, ...]
    residual_phenotype: str
    residual_count: int
    import_rows: tuple[Mapping[str, object], ...]
    accepted_thresholds: tuple[AcceptedThreshold, ...]
    selected_display_view: str
    preview_id: str
    reordered_from_preview_id: str | None = None

    def __post_init__(self) -> None:
        """

        Copies the lists and nested mappings retained with the completed classification.

        We store cells, rules and diagnostics as tuples and validate each import
        row before retaining its read-only marker-state mapping. Section-specific
        threshold override sequences are also copied, keeping a later form edit
        separate from the result already calculated.

        Raises:
            PhenotypeRuleError: An import row has incomplete identities or inconsistent
                marker states and phenotype-component labels.
        """

        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "marker_order", tuple(self.marker_order))
        object.__setattr__(self, "rules", tuple(self.rules))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(
            self,
            "import_rows",
            tuple(_frozen_import_row(row) for row in self.import_rows),
        )
        object.__setattr__(
            self,
            "accepted_thresholds",
            tuple(
                replace(item, section_overrides=tuple(item.section_overrides))
                for item in self.accepted_thresholds
            ),
        )

    @property
    def by_cell_id(self) -> Mapping[str, CellRecord]:
        """

        Indexes the classified cells by their unique cell IDs.

        Returns:
            Read-only dictionary from each cell ID to its returned CellRecord.
        """

        return MappingProxyType(
            dict(zip(map(attrgetter("cell_id"), self.cells), self.cells, strict=True))
        )

    @property
    def unassigned_count(self) -> int:
        """

        Reports the number of cells without a final phenotype after ordered assignment.

        Returns:
            Zero. Every cell is assigned by its first matching rule or the named
            residual, whose separate count is retained in residual_count.
        """

        return 0

def apply_ordered_composites(
    cells: Iterable[CellRecord],
    rules: Iterable[CompositePhenotypeRule],
    *,
    residual: str,
    accepted_thresholds: Sequence[AcceptedThreshold] = (),
    selected_display_view: str = "marker_rules",
    prior_preview: MarkerRuleResult | None = None,
    state_matrix: np.ndarray | None = None,
    marker_order: Sequence[str] | None = None,
) -> MarkerRuleResult:
    """

    Assigns each cell to its first matching phenotype and calculates the effect of rule precedence.

    We arrange the selected Boolean marker states as a matrix with cells in
    rows and markers in columns. For a phenotype, positive selects the marker's
    column, negative selects its Boolean inverse, and ignore omits that condition.
    Applying logical AND across the remaining columns gives a match vector for
    all cells. An all-ignore rule matches every cell.

    We process phenotypes from the lowest precedence value to the highest.
    The new assignments for rule j are its match vector AND the current unassigned
    vector. We record all matches, new assignments and their difference, then
    remove the newly assigned cells from the unassigned vector. After the last
    rule, the remaining cells receive residual.

    For example, suppose 30 cells are CD3-positive and 12 of them are also
    CD8-positive. Placing the CD3-positive/CD8-positive rule first assigns those
    12 cells to the specific phenotype. A later CD3-positive rule matches all
    30 cells but assigns only the remaining 18; its recorded overlap is 12.
    Reversing the order assigns all 30 to the broad phenotype and leaves the
    specific rule with zero new assignments.

    The marker comparisons operate on whole Boolean columns. Python iterates
    over the rule definitions, while NumPy evaluates their conditions across
    cells. The final CellRecord values and import rows are constructed only
    after those numerical assignments and counts are available.

    Args:
        cells: Source CellRecord values in the required output order, with
            unique cell IDs and the selected marker states.
        rules: CompositePhenotypeRule values covering the same complete marker
            set, with unique phenotype names and precedence values.
        residual: Explicit phenotype name for unmatched cells, distinct from
            every listed phenotype.
        accepted_thresholds: Optional accepted cutoffs and section overrides
            associated with the supplied states. When supplied, they must cover
            every selected marker once and belong to one review population.
        selected_display_view: Classification view used when importing the result.
        prior_preview: Optional earlier MarkerRuleResult used to compare a new
            rule ordering with the same source cells and marker states.
        state_matrix: Optional array of shape (cells, selected markers). Boolean values
            are accepted; integer codes use -1 for unknown, 0 for negative and 1 for
            positive. Otherwise the states are read from the cell records.
        marker_order: Explicit state-matrix column names. If absent, use the
            first supplied rule's marker order.

    Returns:
        MarkerRuleResult with one phenotype per cell, per-rule overlap counts,
        the residual count and complete classification rows for QuPath.

    Raises:
        PhenotypeRuleError: Source IDs, rules, marker order, state matrix or
            threshold coverage are invalid, or prior_preview describes different
            cells, states, thresholds, residual name or display settings.
    """

    cell_records = tuple(cells)
    supplied_rules = tuple(rules)
    selected_markers = _validate_ordered_rules(supplied_rules, residual=residual)
    ordered_rules = tuple(sorted(supplied_rules, key=attrgetter("precedence")))
    display_view = _nonempty(selected_display_view, "selected_display_view")
    cell_ids = tuple(map(attrgetter("cell_id"), cell_records))
    if len(set(cell_ids)) != len(cell_ids):
        raise PhenotypeRuleError("stable cell IDs must be unique")

    if marker_order is None:
        matrix_order = selected_markers
    else:
        if isinstance(marker_order, (str, bytes)):
            raise PhenotypeRuleError("marker_order must contain marker names")
        try:
            matrix_order = tuple(marker_order)
        except TypeError as error:
            raise PhenotypeRuleError("marker_order must contain marker names") from error
    if any(not isinstance(marker, str) or not marker.strip() for marker in matrix_order):
        raise PhenotypeRuleError("marker_order values must be non-empty strings")
    if len(set(matrix_order)) != len(matrix_order) or set(matrix_order) != set(selected_markers):
        raise PhenotypeRuleError("marker_order must exactly match the composite marker order")
    matrix = (
        _matrix_from_cells(cell_records, matrix_order)
        if state_matrix is None
        else _validated_state_matrix(state_matrix, len(cell_records), len(matrix_order))
    )
    # Check the associated accepted cutoffs without recomputing marker
    # states while testing a new phenotype ordering.
    thresholds = _validated_threshold_provenance(accepted_thresholds, matrix_order)
    _validate_prior_preview(
        prior_preview,
        cell_ids,
        matrix_order,
        matrix,
        residual,
        thresholds,
        display_view,
    )

    # Begin with every cell available. Each rule assigns only its matches
    # that remain True here, giving precedence a direct Boolean calculation.
    unassigned = np.ones(len(cell_records), dtype=np.bool_)
    phenotype_codes = np.full(len(cell_records), -1, dtype=np.int64)
    diagnostics: list[MarkerRuleDiagnostic] = []
    marker_indices = MappingProxyType(
        dict(zip(matrix_order, range(len(matrix_order)), strict=True))
    )
    for code, rule in enumerate(ordered_rules):
        match = _rule_match(matrix, marker_indices, rule)
        # First match determines the phenotype. Preserve cells claimed by
        # an earlier rule while still counting their overlap with this rule.
        newly_assigned_mask = unassigned & match
        matched = int(np.count_nonzero(match))
        newly_assigned = int(np.count_nonzero(newly_assigned_mask))
        # Count matching cells already assigned by preceding rules, even
        # though their existing phenotype will remain in the final result.
        overlap = matched - newly_assigned
        phenotype_codes[newly_assigned_mask] = code
        unassigned &= ~newly_assigned_mask
        wholly_shadowed = matched > 0 and newly_assigned == 0
        warning = (
            "wholly_shadowed_by_prior_rules"
            if wholly_shadowed
            else "partly_shadowed_by_prior_rules" if overlap else None
        )
        diagnostics.append(
            MarkerRuleDiagnostic(
                phenotype=rule.phenotype,
                precedence=rule.precedence,
                matched=matched,
                newly_assigned=newly_assigned,
                overlap_with_prior=overlap,
                wholly_shadowed=wholly_shadowed,
                warning=warning,
            )
        )

    residual_count = int(np.count_nonzero(unassigned))
    # Give every remaining cell the explicitly selected residual phenotype.
    phenotype_codes[unassigned] = len(ordered_rules)
    phenotype_names = tuple(map(attrgetter("phenotype"), ordered_rules)) + (residual,)
    states_by_row = tuple(
        map(
            lambda row: MappingProxyType(dict(zip(matrix_order, map(_decoded_state, row), strict=True))),
            matrix,
        )
    )
    assigned = tuple(
        map(
            _assigned_cell,
            cell_records,
            map(phenotype_names.__getitem__, phenotype_codes.tolist()),
            states_by_row,
        )
    )
    import_rows = tuple(
        map(
            lambda cell: MappingProxyType(
                {
                    "cell_id": cell.cell_id,
                    "analysis_domain_id": cell.analysis_domain_id,
                    "slide_id": cell.slide_id,
                    "section_id": cell.section_id,
                    "primary_phenotype": cell.primary_phenotype,
                    "phenotype_source": cell.phenotype_source,
                    "phenotype_components": tuple(cell.phenotype_components),
                    "marker_states": MappingProxyType(
                        {
                            marker: _state_name(cell.marker_states[marker])
                            for marker in matrix_order
                        }
                    ),
                    "selected_display_view": display_view,
                }
            ),
            assigned,
        )
    )
    preview_id = _preview_id(
        cell_ids, matrix_order, matrix, ordered_rules, residual, thresholds, display_view
    )
    return MarkerRuleResult(
        cells=assigned,
        marker_order=matrix_order,
        rules=ordered_rules,
        diagnostics=tuple(diagnostics),
        residual_phenotype=residual,
        residual_count=residual_count,
        import_rows=import_rows,
        accepted_thresholds=thresholds,
        selected_display_view=display_view,
        preview_id=preview_id,
        reordered_from_preview_id=(prior_preview.preview_id if prior_preview is not None else None),
    )


def apply_threshold_rules(
    cells: Iterable[CellRecord],
    rules: Iterable[ThresholdRule],
    *,
    measurement_semantics: Iterable[MeasurementSemantic],
    batch_size: int = 65536,
) -> tuple[CellRecord, ...]:
    """

    Calculates each selected marker's Boolean state from its confirmed intensity threshold.

    We first match every rule's exact measurement key with its channel,
    compartment and biomarker description. The marker and compartment must agree,
    and only one threshold may assign the same marker in this call. We then read
    the selected measurement into bounded float64 batches and apply the requested
    high- or low-intensity-positive comparison directly to each batch. Model score
    evaluation and boundary search have already finished during fitting.

    The resulting columns are combined in rule order and added to each cell's
    existing marker-state dictionary. Other marker states and the source cell
    measurements are retained. This step supplies Boolean states for the later
    phenotype combinations; it does not apply those combinations itself.

    Args:
        cells: Source cells containing exact measurement-name keys and values.
        rules: Confirmed ThresholdRule values for the selected biomarkers.
        measurement_semantics: Descriptions linking each available measurement
            key with its channel, cellular compartment and explicit biomarker.
        batch_size: Maximum cells in each temporary marker-state matrix.

    Returns:
        Tuple of CellRecord values with the newly calculated marker states.
        An empty rule list returns the collected source records without new states.

    Raises:
        PhenotypeRuleError: A rule is unconfirmed, a marker is assigned twice,
            measurement descriptions disagree, or a required cell value is
            nonnumeric or Boolean. Missing and nonfinite values retain unknown states.
    """

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise PhenotypeRuleError("threshold batch_size must be a positive integer")
    cell_records = tuple(cells)
    semantics_by_measurement = _index_measurement_semantics(measurement_semantics)
    resolved_rules: list[tuple[ThresholdRule, str]] = []
    assigned_biomarkers: set[str] = set()
    for rule in tuple(rules):
        _validate_threshold_rule(rule)
        semantic = semantics_by_measurement.get(rule.measurement)
        if semantic is None:
            raise PhenotypeRuleError(f"measurement {rule.measurement!r} has no canonical semantics")
        if not semantic.biomarker:
            raise PhenotypeRuleError(f"measurement {rule.measurement!r} has an unknown biomarker")
        if rule.biomarker != semantic.biomarker:
            raise PhenotypeRuleError(
                f"biomarker mismatch for {rule.measurement!r}: rule {rule.biomarker!r}, "
                f"canonical {semantic.biomarker!r}"
            )
        if rule.compartment != semantic.compartment:
            raise PhenotypeRuleError(
                f"compartment mismatch for {rule.measurement!r}: rule {rule.compartment!r}, "
                f"canonical {semantic.compartment!r}"
            )
        if semantic.biomarker in assigned_biomarkers:
            raise PhenotypeRuleError(
                f"duplicate threshold assignment for biomarker {semantic.biomarker!r}"
            )
        assigned_biomarkers.add(semantic.biomarker)
        resolved_rules.append((rule, semantic.biomarker))

    if not resolved_rules:
        return cell_records
    marker_names = tuple(canonical_biomarker for _, canonical_biomarker in resolved_rules)
    result = []
    for start in range(0, len(cell_records), batch_size):
        batch = cell_records[start:start + batch_size]
        state_matrix = np.column_stack(tuple(
            threshold_states(_measurement_vector(batch, rule.measurement), rule)
            for rule, _canonical_biomarker in resolved_rules))
        result.extend(replace(cell, marker_states={
            **dict(cell.marker_states),
            **dict(zip(marker_names, map(_decoded_state, row), strict=True)),
        }) for cell, row in zip(batch, state_matrix, strict=True))
    return tuple(result)


def _assigned_cell(
    cell: CellRecord, phenotype: str, selected_states: Mapping[str, bool | None]
) -> CellRecord:
    """

    Adds the ordered-rule phenotype and selected Boolean states to one cell.

    We merge the selected states with any existing marker states and construct
    the phenotype's marker-positive/negative labels from the selected set in
    alphabetical marker order. The returned cell records marker_rules as the
    source of its phenotype assignment.

    Args:
        cell: Source cell with its existing measurements, geometry and marker states.
        phenotype: Name selected by rule precedence or the residual choice.
        selected_states: Boolean marker states used in this ordered-rule assignment.

    Returns:
        Updated CellRecord preserving the source cell ID and associated data.
    """

    states = dict(cell.marker_states)
    states.update(selected_states)
    components = tuple(
        f"{marker}{'?' if state is None else '+' if state else '-'}"
        for marker, state in sorted(selected_states.items())
    )
    return replace(
        cell,
        marker_states=states,
        primary_phenotype=phenotype,
        phenotype_components=components,
        phenotype_source="marker_rules",
    )


def _frozen_import_row(row: Mapping[str, object]) -> Mapping[str, object]:
    """

    Checks a completed cell-assignment row before retaining it with the phenotype result.

    We require the complete cell, image and region identifiers, a marker_rules
    assignment source and a selected display view. Marker states must be named
    positive, negative or unknown values. The sorted component labels describe
    those states exactly, such as ("CD3+", "CD8-", "PD1?"). This keeps the displayed
    phenotype description consistent with the marker values it represents.

    Args:
        row: Classification mapping created after ordered phenotype assignment.

    Returns:
        Read-only row containing copied component labels and marker-state values.

    Raises:
        PhenotypeRuleError: Required fields are missing, identities are blank,
            marker states are invalid or their component labels disagree.
    """

    if not isinstance(row, Mapping):
        raise PhenotypeRuleError("import rows must be mappings")
    expected = {
        "cell_id",
        "analysis_domain_id",
        "slide_id",
        "section_id",
        "primary_phenotype",
        "phenotype_source",
        "phenotype_components",
        "marker_states",
        "selected_display_view",
    }
    if set(row) != expected:
        raise PhenotypeRuleError("import rows must contain the complete typed assignment schema")
    scalar_fields = (
        "cell_id",
        "analysis_domain_id",
        "slide_id",
        "section_id",
        "primary_phenotype",
        "phenotype_source",
        "selected_display_view",
    )
    for field in scalar_fields:
        _nonempty(row[field], f"import row {field}")
    if row["phenotype_source"] != "marker_rules":
        raise PhenotypeRuleError("import row phenotype_source must be marker_rules")
    components = row["phenotype_components"]
    marker_states = row["marker_states"]
    if isinstance(components, (str, bytes)) or not isinstance(components, Sequence):
        raise PhenotypeRuleError("phenotype_components must be a sequence")
    if not isinstance(marker_states, Mapping):
        raise PhenotypeRuleError("marker_states must be a mapping")
    component_values = tuple(components)
    if any(not isinstance(item, str) or not item.strip() for item in component_values) or len(
        component_values
    ) != len(set(component_values)):
        raise PhenotypeRuleError("phenotype_components must contain unique non-empty strings")
    if not marker_states or any(
        not isinstance(marker, str)
        or not marker.strip()
        or not isinstance(state, str)
        or state not in {POSITIVE, NEGATIVE, UNKNOWN}
        for marker, state in marker_states.items()
    ):
        raise PhenotypeRuleError(
            "marker_states must map marker names to positive, negative or unknown"
        )
    expected_components = tuple(
        f"{marker}{'?' if state == UNKNOWN else '+' if state == POSITIVE else '-'}"
        for marker, state in sorted(marker_states.items())
    )
    if component_values != expected_components:
        raise PhenotypeRuleError("phenotype_components must exactly match marker_states")
    return MappingProxyType(
        {
            "cell_id": row["cell_id"],
            "analysis_domain_id": row["analysis_domain_id"],
            "slide_id": row["slide_id"],
            "section_id": row["section_id"],
            "primary_phenotype": row["primary_phenotype"],
            "phenotype_source": row["phenotype_source"],
            "phenotype_components": component_values,
            "marker_states": MappingProxyType(dict(marker_states)),
            "selected_display_view": row["selected_display_view"],
        }
    )


def _rule_match(
    matrix: np.ndarray,
    marker_indices: Mapping[str, int],
    rule: CompositePhenotypeRule,
) -> np.ndarray:
    """

    Calculates which cells satisfy one phenotype's selected marker conditions.

    We compare each positive marker column with code 1 and each negative
    marker column with code 0. Unknown code -1 satisfies neither condition.
    Ignored markers contribute no column. Logical AND across
    the selected columns is True only when that cell satisfies all conditions.
    With every marker ignored, we return True for all cells.

    Args:
        matrix: Boolean (cells, markers) array in the selected marker order.
        marker_indices: Mapping from each marker name to its matrix column.
        rule: Positive, negative and ignored states defining one phenotype.

    Returns:
        Boolean match vector of length number_of_cells. Precedence is applied
        afterward by the enclosing assignment loop.
    """

    predicates = tuple(
        (
            matrix[:, marker_indices[marker]] == 1
            if state == POSITIVE
            else matrix[:, marker_indices[marker]] == 0
        )
        for marker, state in rule.marker_states.items()
        if state != IGNORE
    )
    # An all-ignore rule places no marker condition on a cell.
    if not predicates:
        return np.ones(matrix.shape[0], dtype=np.bool_)
    return np.logical_and.reduce(np.column_stack(predicates), axis=1)


def _matrix_from_cells(cells: tuple[CellRecord, ...], marker_order: tuple[str, ...]) -> np.ndarray:
    """

    Reads nullable marker states into a compact cell-by-marker array.

    For each marker, we visit the cells in input order and require an explicit
    Boolean or explicit None state. NumPy constructs that column from the iterator;
    stacking the columns preserves the selected marker order without building
    an intermediate Python list for every cell-marker pair.

    Args:
        cells: Source cell tuple defining matrix row order.
        marker_order: Marker names defining matrix column order.

    Returns:
        Int8 array with shape (len(cells), len(marker_order)); unknown=-1, negative=0, positive=1.

    Raises:
        PhenotypeRuleError: A cell lacks a selected marker or its state is neither Boolean nor None.
    """

    columns = tuple(
        np.fromiter(
            map(lambda cell, key=marker: _cell_marker_state(cell, key), cells),
            dtype=np.int8,
            count=len(cells),
        )
        for marker in marker_order
    )
    return np.column_stack(columns) if columns else np.empty((len(cells), 0), dtype=np.int8)


def _cell_marker_state(cell: CellRecord, marker: str) -> int:
    """

    Encodes one nullable marker state while preserving missing evidence.

    Args:
        cell: Cell carrying the previously calculated marker states.
        marker: Exact marker name to read.

    Returns:
        The code -1 for None, 0 for False or 1 for True; NumPy Booleans are accepted.

    Raises:
        PhenotypeRuleError: The marker key is missing or contains an unsupported value.
    """

    if marker not in cell.marker_states:
        raise PhenotypeRuleError(f"cell {cell.cell_id} is missing marker state {marker!r}")
    value = cell.marker_states[marker]
    if value is None:
        return -1
    if not isinstance(value, (bool, np.bool_)):
        raise PhenotypeRuleError(f"cell {cell.cell_id} marker state {marker!r} must be Boolean or unknown")
    return int(value)


def _validated_state_matrix(matrix: np.ndarray, rows: int, columns: int) -> np.ndarray:
    """

    Checks the supplied state matrix against the current cell and marker counts.

    The array must be two-dimensional with the exact row and column counts,
    and contain Boolean values or integer -1/0/1 codes. Copying the checked array keeps
    subsequent caller changes separate from the assignment in progress.

    Args:
        matrix: Supplied marker-state array.
        rows: Number of source cells, in their retained order.
        columns: Number of selected marker names.

    Returns:
        Int8 NumPy copy with shape (rows, columns), retaining unknown code -1.

    Raises:
        PhenotypeRuleError: The shape or dtype is incompatible with the selected input.
    """

    values = np.asarray(matrix)
    if values.ndim != 2 or values.shape != (rows, columns):
        raise PhenotypeRuleError(
            f"state_matrix must have exact cell-by-marker shape {(rows, columns)}"
        )
    if values.dtype.kind not in "biu" or not np.all(np.isin(values, (-1, 0, 1))):
        raise PhenotypeRuleError("state_matrix must contain Boolean states or -1/0/1 codes")
    return values.astype(np.int8, copy=True)


def _validate_ordered_rules(
    rules: tuple[CompositePhenotypeRule, ...], *, residual: str
) -> tuple[str, ...]:
    """

    Requires complete marker choices and an unambiguous order for the phenotype definitions.

    Every rule must name a unique phenotype and use a unique integer precedence.
    The residual must have its own name. We obtain the selected marker panel
    from the first rule, then require every other rule to provide positive,
    negative or ignore for exactly that panel. A missing entry therefore remains
    a review error rather than becoming an unintended ignored marker.

    Args:
        rules: Nonempty tuple of CompositePhenotypeRule values for this review.
        residual: Phenotype assigned when no listed rule matches.

    Returns:
        Selected marker names in the first rule's order.

    Raises:
        PhenotypeRuleError: Rules are absent or invalid, phenotype/precedence
            values repeat, or a rule does not cover the same marker panel.
    """

    _nonempty(residual, "residual phenotype")
    if not rules:
        raise PhenotypeRuleError("at least one composite phenotype rule is required")
    if not all(isinstance(rule, CompositePhenotypeRule) for rule in rules):
        raise PhenotypeRuleError("ordered rules must be CompositePhenotypeRule values")
    for rule in rules:
        _nonempty(rule.phenotype, "composite phenotype")
        if isinstance(rule.precedence, bool) or not isinstance(rule.precedence, int):
            raise PhenotypeRuleError("composite rule precedence must be an integer")
        if not rule.marker_states:
            raise PhenotypeRuleError("composite rules must reference selected markers")
        for marker, state in rule.marker_states.items():
            if not isinstance(marker, str) or not marker.strip():
                raise PhenotypeRuleError("selected marker names must be unique non-empty strings")
            if not isinstance(state, str) or state not in _PREDICATE_STATES:
                raise PhenotypeRuleError(
                    f"marker {marker!r} requires exactly one positive, negative, or ignore state; "
                    f"got {state!r}"
                )
    phenotypes = tuple(map(attrgetter("phenotype"), rules))
    precedences = tuple(map(attrgetter("precedence"), rules))
    if len(set(phenotypes)) != len(phenotypes) or residual in phenotypes:
        raise PhenotypeRuleError("composite and residual phenotype names must be unique")
    if len(set(precedences)) != len(precedences):
        raise PhenotypeRuleError("composite rules require unique precedence values")
    marker_order = tuple(rules[0].marker_states)
    if len(set(marker_order)) != len(marker_order):
        raise PhenotypeRuleError("selected marker names must be unique non-empty strings")
    required = set(marker_order)
    for rule in rules:
        actual = set(rule.marker_states)
        if actual != required:
            missing = sorted(required - actual)
            extra = sorted(actual - required)
            marker = (missing or extra)[0]
            raise PhenotypeRuleError(
                f"every composite requires exactly one state for selected marker {marker!r}"
            )
    return marker_order


def _validated_threshold_provenance(
    accepted: Sequence[AcceptedThreshold], marker_order: tuple[str, ...]
) -> tuple[AcceptedThreshold, ...]:
    """

    Checks the accepted threshold descriptions associated with the selected marker states.

    When threshold records are supplied, each selected marker must appear once
    and all records must refer to the same review population. Their section
    override sequences are copied before being retained with the classification.
    Applying their numerical cutoffs occurs in the preceding threshold step.

    Args:
        accepted: Optional sequence of AcceptedThreshold values.
        marker_order: Complete marker set used by the phenotype rules.

    Returns:
        Tuple of accepted threshold records, or an empty tuple when this
        programmatic call supplies no threshold descriptions.

    Raises:
        PhenotypeRuleError: Record types, marker coverage or review-population
            identities are inconsistent.
    """

    values = tuple(accepted)
    if not values:
        return values
    if not all(isinstance(item, AcceptedThreshold) for item in values):
        raise PhenotypeRuleError("accepted_thresholds must contain AcceptedThreshold values")
    marker_ids = tuple(map(attrgetter("marker_id"), values))
    if len(set(marker_ids)) != len(marker_ids) or set(marker_ids) != set(marker_order):
        raise PhenotypeRuleError(
            "accepted threshold provenance must cover each selected marker exactly once"
        )
    domains = set(map(attrgetter("validation_domain"), values))
    if len(domains) != 1:
        raise PhenotypeRuleError("accepted thresholds must share one validation domain")
    return tuple(replace(item, section_overrides=tuple(item.section_overrides)) for item in values)


def _validate_prior_preview(
    prior: MarkerRuleResult | None,
    cell_ids: tuple[str, ...],
    marker_order: tuple[str, ...],
    state_matrix: np.ndarray,
    residual: str,
    accepted_thresholds: tuple[AcceptedThreshold, ...],
    display_view: str,
) -> None:
    """

    Checks that a comparison with an earlier rule result uses the same measured cells and marker states.

    We compare ordered cell IDs and the selected marker set, then reconstruct
    the earlier Boolean states in the current column order. Those values must
    equal the proposed matrix. The residual name, accepted threshold records
    and display view must also remain the same. Rule definitions and precedence
    are deliberately checked separately by the current ordered-rule validator.

    Args:
        prior: Earlier MarkerRuleResult, or None for a first calculation.
        cell_ids: Current source-cell IDs in matrix row order.
        marker_order: Current selected marker names in column order.
        state_matrix: Boolean values used by the proposed rule calculation.
        residual: Current name for unmatched cells.
        accepted_thresholds: Accepted thresholds and section overrides associated
            with those marker states.
        display_view: Current classification view selected for import.

    Raises:
        PhenotypeRuleError: The earlier result refers to different source cells,
            marker states, accepted thresholds, residual name or display view.
    """

    if prior is None:
        return
    if not isinstance(prior, MarkerRuleResult):
        raise PhenotypeRuleError("prior_preview must be a MarkerRuleResult")
    if tuple(map(attrgetter("cell_id"), prior.cells)) != cell_ids:
        raise PhenotypeRuleError("reordered preview must use the same stable cell IDs")
    if set(prior.marker_order) != set(marker_order):
        raise PhenotypeRuleError("reordered preview must use the same selected markers")
    prior_matrix = _matrix_from_cells(prior.cells, marker_order)
    if not np.array_equal(prior_matrix, state_matrix):
        raise PhenotypeRuleError("reordered preview must use the same marker-state matrix")
    if (
        prior.residual_phenotype != residual
        or prior.accepted_thresholds != accepted_thresholds
        or prior.selected_display_view != display_view
    ):
        raise PhenotypeRuleError("reordered preview must preserve provenance and display scope")


def _preview_id(
    cell_ids: tuple[str, ...],
    marker_order: tuple[str, ...],
    matrix: np.ndarray,
    rules: tuple[CompositePhenotypeRule, ...],
    residual: str,
    thresholds: tuple[AcceptedThreshold, ...],
    display_view: str,
) -> str:
    """

    Calculates a checksum identifying the input marker states and the complete rule review.

    We serialize the ordered cell IDs, marker columns, state matrix, phenotype
    definitions and precedence values. The same description includes the
    residual name, accepted thresholds, section overrides and selected display
    view. Marker states are encoded as -1/0/1 lists and dictionary keys are
    sorted before calculating the checksum.

    Args:
        cell_ids: Source IDs in matrix row order.
        marker_order: Marker names in matrix column order.
        matrix: Marker states encoded as -1 unknown, 0 negative and 1 positive.
        rules: Current phenotype rules in ascending precedence order.
        residual: Name assigned to unmatched cells.
        thresholds: Accepted marker thresholds and optional section overrides.
        display_view: Selected QuPath classification view.

    Returns:
        SHA-256 text identifying this exact set of classification inputs.
    """

    threshold_rows = tuple(
        {
            "marker_id": item.marker_id,
            "validation_domain": item.validation_domain,
            "threshold": item.threshold,
            "selection_source": item.selection_source,
            "reviewer_id": item.reviewer_id,
            "review_record_id": item.review_record_id,
            "outcome": item.outcome,
            "fit_scope": item.fit_scope,
            "score_provenance": dict(item.score_provenance),
            **{field: getattr(item, field) for field in (
                "proposed_threshold", "proposed_outcome", "proposed_score_cutoff", "score_cutoff", "adjustment_source")},
            "slide_thresholds": [
                {
                    "image_id": override.image_id,
                    **{field: getattr(override, field) for field in (
                        "proposed_threshold", "proposed_outcome", "proposed_score_cutoff", "score_cutoff", "adjustment_source")},
                    "threshold": override.threshold,
                    "outcome": override.outcome,
                    "selection_source": override.selection_source,
                    "reason": override.reason,
                    "reviewer_id": override.reviewer_id,
                    "review_record_id": override.review_record_id,
                }
                for override in item.slide_thresholds
            ],
            "section_overrides": [
                {
                    "section_id": override.section_id,
                    "threshold": override.threshold,
                    "outcome": override.outcome,
                    "selection_source": override.selection_source,
                    "reason": override.reason,
                    "reviewer_id": override.reviewer_id,
                    "review_record_id": override.review_record_id,
                }
                for override in item.section_overrides
            ],
        }
        for item in thresholds
    )
    payload = {
        "schema_version": "marker-rule-preview/v2",
        "cell_ids": cell_ids,
        "marker_order": marker_order,
        "state_matrix": matrix.astype(np.int8).tolist(),
        "rules": [
            {
                "phenotype": rule.phenotype,
                "precedence": rule.precedence,
                "marker_states": dict(rule.marker_states),
            }
            for rule in rules
        ],
        "residual": residual,
        "accepted_thresholds": threshold_rows,
        "selected_display_view": display_view,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_threshold_rule(rule: ThresholdRule) -> None:
    """

    Checks that a marker threshold has an explicit measurement, comparison direction and confirmation.

    We require nonempty marker, measurement and compartment names, a finite
    numerical cutoff and a supported comparison name. Confirmation must be
    exactly True before the rule can assign marker states to cells.

    Args:
        rule: ThresholdRule submitted for application to measured cells.

    Raises:
        PhenotypeRuleError: A required name is missing, polarity is unsupported,
            cutoff is nonfinite/nonnumeric or the threshold is unconfirmed.
    """

    for field in ("biomarker", "measurement", "compartment", "polarity"):
        value = getattr(rule, field)
        if not isinstance(value, str) or not value.strip():
            raise PhenotypeRuleError(f"threshold rule {field} must be a non-empty string")
    if rule.polarity.casefold() not in {"positive", "above"}:
        raise PhenotypeRuleError("Marker positivity requires an intensity at or above the threshold")
    if rule.outcome not in {"threshold", "all_positive", "all_negative"}:
        raise PhenotypeRuleError("Only a reviewed threshold or constant decision can be applied")
    if rule.intensity_domain not in {"finite", "nonnegative"}:
        raise PhenotypeRuleError("threshold intensity domain must be finite or nonnegative")
    if rule.outcome == "threshold":
        if isinstance(rule.cutoff, bool) or not isinstance(rule.cutoff, (int, float)):
            raise PhenotypeRuleError("threshold rule cutoff must be numeric")
        if not isfinite(float(rule.cutoff)):
            raise PhenotypeRuleError("threshold rule cutoff must be finite")
    elif rule.cutoff is not None:
        raise PhenotypeRuleError("A constant decision must have no numerical cutoff")
    if rule.confirmed is not True:
        raise PhenotypeRuleError(
            f"threshold rule for {rule.biomarker} must be explicitly confirmed"
        )


def _index_measurement_semantics(
    semantics: Iterable[MeasurementSemantic],
) -> dict[str, MeasurementSemantic]:
    """

    Indexes the available intensity measurements by their exact recorded names.

    Each description must identify its source channel, measurement and cellular
    compartment. A biomarker can be absent in the measurement description, but
    applying a marker threshold later requires an explicit biomarker assignment.
    Repeated measurement names are rejected so one value key cannot refer to
    two different channel or compartment descriptions.

    Args:
        semantics: MeasurementSemantic records supplied with the measured cells.

    Returns:
        Dictionary from exact measurement name to its recorded description.

    Raises:
        PhenotypeRuleError: Required fields are blank, an optional biomarker is
            invalid, or a measurement name occurs more than once.
    """

    indexed: dict[str, MeasurementSemantic] = {}
    for semantic in semantics:
        for field in ("channel_id", "measurement", "compartment"):
            value = getattr(semantic, field)
            if not isinstance(value, str) or not value.strip():
                raise PhenotypeRuleError(f"measurement semantic {field} must be a non-empty string")
        if semantic.biomarker is not None and (
            not isinstance(semantic.biomarker, str) or not semantic.biomarker.strip()
        ):
            raise PhenotypeRuleError(
                "measurement semantic biomarker must be a non-empty string or None"
            )
        if semantic.measurement in indexed:
            raise PhenotypeRuleError(
                f"duplicate canonical semantics for measurement {semantic.measurement!r}"
            )
        indexed[semantic.measurement] = semantic
    return indexed


def _measurement_vector(cells: tuple[CellRecord, ...], measurement: str) -> np.ndarray:
    """

    Reads one selected intensity value from every cell in input order.

    We require the exact measurement-name key in each cell and check that its
    value is numerical data or absent. Missing and nonfinite values become NaN
    in this working vector. NumPy fills it in one pass. Its positions then match the source cells used
    for vectorized threshold comparison.

    Args:
        cells: Cells whose marker measurements supply the numerical values.
        measurement: Exact recorded measurement name selected by the threshold.

    Returns:
        Float64 NumPy vector with one value per source cell.

    Raises:
        PhenotypeRuleError: A supplied measurement is Boolean or nonnumeric.
    """

    def finite_value(cell: CellRecord) -> float:
        """

        Retrieves and validates the enclosing function's selected measurement for one cell.

        Args:
            cell: Source CellRecord to inspect.

        Returns:
            Float intensity, or NaN for an absent/nonfinite measurement.

        Raises:
            PhenotypeRuleError: The supplied value is Boolean or nonnumeric.
        """

        value = cell.marker_measurements.get(measurement)
        if value is None:
            return np.nan
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PhenotypeRuleError(
                f"measurement {measurement!r} for cell {cell.cell_id} must be numeric"
            )
        number = float(value)
        return number if isfinite(number) else np.nan

    return np.fromiter(map(finite_value, cells), dtype=np.float64, count=len(cells))


def threshold_states(values: np.ndarray, rule: ThresholdRule) -> np.ndarray:
    """

    Apply a persisted decision to one numeric intensity array or column batch.

    For positive/above polarity, the marker state is True when value >= cutoff,
    including equality.
    NumPy evaluates the same comparison for every cell, preserving their order.

    Args:
        values: Numeric array; missing measurements must be represented by NaN.
        rule: Confirmed comparison direction and numerical threshold.

    Returns:
        Same-shaped Int8 array with positive=1, negative=0 and unknown=-1.
        Constants apply only inside the declared finite/nonnegative domain, with
        the finite-value mask applied afterward. The calculation uses direct comparisons.
    """

    _validate_threshold_rule(rule)
    values = np.asarray(values)
    if values.dtype.kind not in "fiu":
        raise PhenotypeRuleError("threshold values must be a numeric intensity array")
    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values)
    if rule.intensity_domain == "nonnegative":
        finite &= values >= 0
    states = np.full(values.shape, -1, dtype=np.int8)
    if rule.outcome == "threshold":
        positive = values[finite] >= float(rule.cutoff)
    else:
        positive = np.full(np.count_nonzero(finite), rule.outcome == "all_positive")
    states[finite] = positive.astype(np.int8)
    return states


def _decoded_state(value: int) -> bool | None:
    """Restore a nullable marker state from the compact computation code."""
    return None if value == -1 else bool(value)


def _state_name(value: bool | None) -> str:
    """Name a marker state for saved classifications and the native display."""
    return UNKNOWN if value is None else POSITIVE if value else NEGATIVE


def _nonempty(value: object, field: str) -> str:
    """

    Checks that a required rule or result field contains nonblank text.

    Args:
        value: Supplied text value.
        field: Field name included in an error.

    Returns:
        Original string when it contains a non-whitespace character.

    Raises:
        PhenotypeRuleError: The value is not text or is blank.
    """

    if not isinstance(value, str) or not value.strip():
        raise PhenotypeRuleError(f"{field} must be a non-empty string")
    return value


__all__ = [
    "IGNORE",
    "NEGATIVE",
    "POSITIVE",
    "CompositePhenotypeRule",
    "MarkerRuleDiagnostic",
    "MarkerRuleResult",
    "MeasurementSemantic",
    "PhenotypeRuleError",
    "ThresholdRule",
    "apply_ordered_composites",
    "apply_threshold_rules",
    "threshold_states",
]
