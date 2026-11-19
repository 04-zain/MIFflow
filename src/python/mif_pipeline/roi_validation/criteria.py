"""

Evaluates explicit biological and geometric conditions for a candidate tissue region.

A criterion names one recorded quantity, its selector, a comparison and a
threshold. Counts, phenotype fractions, cell densities, marker states and region
geometry are supplied by the accepted candidate's measurement record. The rule
evaluator compares those values; it does not estimate missing measurements.

Criteria can be combined with all_of, any_of or at_least. Four outcomes preserve
why a decision is incomplete: pass, fail, unavailable input, or a required manual
review. Optional criteria are reported but omitted from the combined required
outcome. Manual decisions apply only to the same profile, candidate geometry
and measured population on which that decision was made.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence
import json
import math
from .evidence import SemanticEvidenceSnapshot, SemanticRegionDecision

# ==============================================================================
#                                    Criteria
# ==============================================================================


class RuleOutcome(str, Enum):
    """

    Identifies a satisfied criterion, a failed criterion, unavailable measurements or a pending manual decision.
    """

    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"
    NEEDS_REVIEW = "needs_review"
    AVAILABLE = "available"


_LEAVES = frozenset(
    {
        "segmented_cell_count",
        "segmented_nucleus_count",
        "phenotype_count",
        "phenotype_fraction",
        "marker_state_count",
        "marker_state_fraction",
        "population_marker_fraction",
        "cell_density",
        "nucleus_density",
        "phenotype_density",
        "cell_measurement_aggregate",
        "distance_to_primary",
        "overlap_fraction_with_primary",
        "candidate_area",
        "geometry_component_count",
        "classifier_confidence",
        "manual_region_decision",
    }
)
_COMPARATORS = frozenset({"gt", "ge", "lt", "le", "eq"})
_FRACTION_DENOMINATORS = frozenset({"segmented_cells"})
_OVERLAP_DENOMINATORS = frozenset({"candidate_area", "primary_area"})
_MARKER_STATES = frozenset({"positive", "negative"})
_MEASUREMENT_STATISTICS = frozenset({"mean", "median"})
_MEASUREMENT_MISSING_POLICIES = frozenset({"unavailable", "exclude"})


@dataclass(frozen=True, slots=True)
class SemanticCriterionResult:
    """

    Records the value and decision produced by one criterion or combined rule.

    Attributes:
        criterion_id: Declared leaf ID, or the composition name for a combined result.
        outcome: PASS, FAIL, UNAVAILABLE or NEEDS_REVIEW.
        required: Whether the result contributes to its enclosing composition.
        observed_value: Compared numerical value when available.
        unit: Declared unit label copied from the criterion.
        comparator: Selected gt/ge/lt/le/eq operation.
        threshold: Configured numerical cutoff.
        evidence_digest: Identity of the candidate's measured evidence record.
        unavailable_evidence: Requested quantity or manual-criterion ID still unresolved.
    """

    criterion_id: str
    outcome: RuleOutcome
    required: bool = True
    observed_value: object | None = None
    unit: str | None = None
    comparator: str | None = None
    threshold: object | None = None
    evidence_digest: str | None = None
    unavailable_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Criterion:
    """

    Represents a numerical/manual condition or a composition of child conditions.

    Attributes:
        kind: Registered quantity name or all_of, any_of, at_least composition.
        criterion_id: Unique profile-wide identity for a leaf condition.
        comparator: Numerical comparison name, absent for compositions and manual decisions.
        threshold: Finite numerical cutoff; count criteria require integer thresholds.
        required: Whether this leaf contributes to the combined acceptance condition.
        unit: Optional unit label retained with the comparison.
        selector: Exact phenotype, marker, measurement or primary-class selection for this quantity.
        children: Ordered subcriteria for a composition.
        k: Required number of passing children for at_least.
        prompt: Researcher-facing question for a manual criterion.
    """

    kind: str
    criterion_id: str | None = None
    comparator: str | None = None
    threshold: int | float | None = None
    required: bool = True
    unit: str | None = None
    selector: Mapping[str, str] | None = None
    children: tuple["Criterion", ...] = ()
    k: int | None = None
    prompt: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        """

        Serializes the criterion using its supported leaf or composition representation.

        Returns:
            Mapping with child criteria for a composition, a manual prompt, or an exact
            registered numerical type, selector, comparator and threshold.
        """

        if self.kind == "all_of":
            return {"all_of": [child.to_mapping() for child in self.children]}
        if self.kind == "any_of":
            return {"any_of": [child.to_mapping() for child in self.children]}
        if self.kind == "at_least":
            return {
                "at_least": {
                    "k": self.k,
                    "criteria": [child.to_mapping() for child in self.children],
                }
            }
        result = {"criterion_id": self.criterion_id, "type": self.kind}
        if self.kind == "manual_region_decision":
            result["prompt"] = self.prompt
        elif self.comparator is not None:
            result.update({"comparator": self.comparator, "threshold": self.threshold})
        if not self.required:
            result["required"] = False
        if self.unit is not None:
            result["unit"] = self.unit
        if self.selector is not None:
            result["selector"] = dict(self.selector)
        return result


def AllOf(children: Sequence[Criterion]) -> Criterion:
    """

    Combines the supplied child conditions so every required child must pass.

    Args:
        children: Ordered Criterion objects to evaluate together.

    Returns:
        Criterion with kind "all_of" and the supplied children.
    """

    return Criterion("all_of", children=tuple(children))


def AnyOf(children: Sequence[Criterion]) -> Criterion:
    """

    Combines child conditions so one passing required child satisfies the composition.

    Args:
        children: Ordered Criterion objects representing acceptable alternatives.

    Returns:
        Criterion with kind "any_of".
    """

    return Criterion("any_of", children=tuple(children))


def AtLeast(k: int, children: Sequence[Criterion]) -> Criterion:
    """

    Combines child conditions with a required number of passes.

    Args:
        k: Positive integer no larger than the supplied child count.
        children: Ordered conditions contributing to the requirement.

    Returns:
        Criterion with kind "at_least", the children and k.

    Raises:
        ValueError: k is Boolean, noninteger or outside the possible child count.
    """

    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= len(children):
        raise ValueError("at_least k must be within criteria length")
    return Criterion("at_least", children=tuple(children), k=k)


def parse_criterion(raw: object, seen: set[str] | None = None, *, allow_observations: bool = False) -> Criterion:
    """

    Parses a profile's condition tree and checks every leaf selector and numerical comparison.

    A composition contains exactly one operator and a nonempty child list. Recurse
    with one shared seen-ID set so leaf identities are unique throughout the profile.
    For a numerical leaf, require a registered quantity, finite threshold and a
    supported comparison. Count thresholds are integers. Each quantity has an exact
    selector schema, such as phenotype_id plus denominator for a phenotype fraction.

    Manual conditions contain a prompt instead of a numerical comparator. Optional
    unit text is retained with a numerical condition; conversion to that unit is
    performed by the quantity producer, not this parser.

    Args:
        raw: Mapping decoded from the profile's YAML/JSON criterion section.
        seen: Optional shared set of criterion IDs already used by related rules.

    Returns:
        Criterion tree ready to evaluate against a candidate's recorded measurements.

    Raises:
        ValueError: A type, ID, field set, selector, threshold or composition is invalid.
    """

    seen = set() if seen is None else seen
    if not isinstance(raw, Mapping):
        raise ValueError("criterion must be a mapping")
    keys = set(raw)
    compositions = keys & {"all_of", "any_of", "at_least"}
    if compositions:
        if len(compositions) != 1 or keys != compositions:
            raise ValueError("composition criterion must contain exactly one composition")
        name = next(iter(compositions))
        value = raw[name]
        if name == "at_least":
            if (
                not isinstance(value, Mapping)
                or set(value) != {"k", "criteria"}
                or not isinstance(value["k"], int)
                or isinstance(value["k"], bool)
            ):
                raise ValueError("at_least requires integer k and criteria")
            children_raw = value["criteria"]
            k = value["k"]
        else:
            children_raw = value
            k = None
        if (
            not isinstance(children_raw, Sequence)
            or isinstance(children_raw, (str, bytes))
            or not children_raw
        ):
            raise ValueError("composition criteria must be a non-empty array")
        children = tuple(parse_criterion(item, seen, allow_observations=allow_observations) for item in children_raw)
        if name == "at_least" and not 1 <= k <= len(children):
            raise ValueError("at_least k must be within criteria length")
        return Criterion(name, children=children, k=k)
    allowed = {"criterion_id", "type", "comparator", "threshold", "required", "unit", "selector"}
    unknown = keys - allowed - {"prompt"}
    if unknown:
        raise ValueError(f"unknown criterion fields: {', '.join(sorted(unknown))}")
    kind = raw.get("type")
    ident = raw.get("criterion_id")
    if kind not in _LEAVES:
        raise ValueError("criterion type is not registered")
    if not isinstance(ident, str) or not ident or ident.startswith("_") or ident in seen:
        raise ValueError("criterion_id must be unique and not reserved")
    seen.add(ident)
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ValueError("criterion required must be boolean")
    if kind == "manual_region_decision":
        if (
            set(raw) - {"criterion_id", "type", "prompt", "required"}
            or not isinstance(raw.get("prompt"), str)
            or not raw["prompt"].strip()
        ):
            raise ValueError("manual_region_decision requires only a prompt")
        return Criterion(kind, ident, required=required, prompt=raw["prompt"])
    observation = allow_observations and not keys & {"comparator", "threshold"}
    comparator = threshold = None
    if not observation:
        if not {"criterion_id", "type", "comparator", "threshold"} <= keys:
            raise ValueError("criterion requires both comparator and threshold")
        comparator, threshold = raw["comparator"], raw["threshold"]
        if comparator not in _COMPARATORS:
            raise ValueError("criterion comparator is invalid")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("criterion threshold must be numeric")
        if isinstance(threshold, float) and not math.isfinite(threshold):
            raise ValueError("criterion threshold must be finite")
        if kind.endswith("_count") or kind == "geometry_component_count":
            if not isinstance(threshold, int):
                raise ValueError("count criterion threshold must be an integer")
    unit = raw.get("unit")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        raise ValueError("criterion unit must be a non-empty string")
    selector = raw.get("selector", {})
    if not isinstance(selector, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in selector.items()
    ):
        raise ValueError("criterion selector must be a string mapping")
    required_selector = {
        "phenotype_count": {"phenotype_id"},
        "phenotype_fraction": {"phenotype_id", "denominator"},
        "marker_state_count": {"marker_id", "marker_state_id"},
        "marker_state_fraction": {"marker_id", "marker_state_id", "denominator"},
        "population_marker_fraction": {
            "phenotype_ids", "measurement_id", "marker_state_id", "denominator",
            "missing_policy", "positivity_gate",
        },
        "phenotype_density": {"phenotype_id"},
        "cell_measurement_aggregate": {
            "measurement_id",
            "statistic",
            "missing_policy",
        },
        "distance_to_primary": {"primary_class_id"},
        "overlap_fraction_with_primary": {"primary_class_id", "denominator"},
    }.get(kind, set())
    # A saved measurement identifies its channel as well as its cellular region
    # and statistic. An explicit channel binding, when supplied, is checked
    # against the measurement inventory by validate_semantic_profile_selectors.
    optional_selector = {"channel_role"} if kind in {"cell_measurement_aggregate", "population_marker_fraction"} else set()
    if not required_selector <= set(selector) or set(selector) - required_selector - optional_selector:
        raise ValueError(f"{kind} selector must use the exact approved fields")
    if (
        kind in {"phenotype_fraction", "marker_state_fraction"}
        and selector["denominator"] not in _FRACTION_DENOMINATORS
    ):
        raise ValueError(f"{kind} denominator is not approved")
    if (
        kind == "overlap_fraction_with_primary"
        and selector["denominator"] not in _OVERLAP_DENOMINATORS
    ):
        raise ValueError("overlap_fraction_with_primary denominator is not approved")
    if (
        kind in {"marker_state_count", "marker_state_fraction", "population_marker_fraction"}
        and selector["marker_state_id"] not in _MARKER_STATES
    ):
        raise ValueError(f"{kind} marker_state_id is not approved")
    if kind == "population_marker_fraction":
        from .population_marker import parse_population_marker_gate, parse_population_phenotype_ids
        if selector["denominator"] not in {"valid_marker_cells", "all_valid_marker_cells"} or selector["missing_policy"] != "exclude":
            raise ValueError("Population marker fraction requires a valid-marker denominator and exclusion of unknown measurements")
        parse_population_phenotype_ids(selector["phenotype_ids"])
        parse_population_marker_gate(selector["positivity_gate"], selector["measurement_id"])
    if kind == "cell_measurement_aggregate":
        if selector["statistic"] not in _MEASUREMENT_STATISTICS:
            raise ValueError("cell_measurement_aggregate statistic is not approved")
        if selector["missing_policy"] not in _MEASUREMENT_MISSING_POLICIES:
            raise ValueError("cell_measurement_aggregate missing_policy is not approved")
    return Criterion(kind, ident, comparator, threshold, required, unit, dict(selector))


def _aggregate(
    kind: str, children: tuple[SemanticCriterionResult, ...], k: int | None,
    criteria: tuple[Criterion, ...],
) -> RuleOutcome:
    """

    Combines required child outcomes while retaining the difference between missing input and pending review.

    For all_of, any failure decides failure; otherwise a pending manual decision
    precedes unavailable data. All passes produce pass. For any_of, one pass decides
    pass; all failures decide failure; otherwise a pending manual decision precedes
    unavailable data.

    For at_least, let p be the required-child pass count and u the number still
    unavailable or awaiting review. The result passes when p >= k and fails when
    p + u < k. The remaining cases need review if any manual decision is pending,
    otherwise they are unavailable. A composition with no required children passes
    and is itself marked optional by the calling evaluator.

    Args:
        kind: all_of, any_of or at_least.
        children: Evaluated child results, including their required flags.
        k: Required pass count for at_least; unused by the other compositions.

    Returns:
        Combined RuleOutcome. Optional child results are excluded from its counts.
    """

    # Optional checks remain in the report but do not change the required
    # pass count or the availability of the combined decision.
    def has_comparison(node):
        return (any(has_comparison(item) for item in node.children) if node.children else
                node.comparator is not None or node.kind == "manual_region_decision")
    comparisons = [child for node, child in zip(criteria, children) if has_comparison(node)]
    if not comparisons:
        return RuleOutcome.UNAVAILABLE if any(child.outcome is RuleOutcome.UNAVAILABLE for child in children) else RuleOutcome.AVAILABLE
    outcomes = [child.outcome for child in comparisons if child.required and child.outcome is not RuleOutcome.AVAILABLE]
    if not outcomes:
        return RuleOutcome.PASS
    if kind == "all_of":
        return (
            RuleOutcome.FAIL
            if RuleOutcome.FAIL in outcomes
            else (
                RuleOutcome.NEEDS_REVIEW
                if RuleOutcome.NEEDS_REVIEW in outcomes
                else (
                    RuleOutcome.PASS
                    if all(value is RuleOutcome.PASS for value in outcomes)
                    else RuleOutcome.UNAVAILABLE
                )
            )
        )
    if kind == "any_of":
        return (
            RuleOutcome.PASS
            if RuleOutcome.PASS in outcomes
            else (
                RuleOutcome.FAIL
                if all(value is RuleOutcome.FAIL for value in outcomes)
                else (
                    RuleOutcome.NEEDS_REVIEW
                    if RuleOutcome.NEEDS_REVIEW in outcomes
                    else RuleOutcome.UNAVAILABLE
                )
            )
        )
    # Pending children can still become passes. Failure is final only when
    # even all pending children passing would leave fewer than k passes.
    passed = outcomes.count(RuleOutcome.PASS)
    pending = outcomes.count(RuleOutcome.UNAVAILABLE) + outcomes.count(RuleOutcome.NEEDS_REVIEW)
    return (
        RuleOutcome.PASS
        if passed >= k
        else (
            RuleOutcome.FAIL
            if passed + pending < k
            else (
                RuleOutcome.NEEDS_REVIEW
                if RuleOutcome.NEEDS_REVIEW in outcomes
                else RuleOutcome.UNAVAILABLE
            )
        )
    )


def evaluate_criterion(
    criterion: Criterion,
    evidence: SemanticEvidenceSnapshot,
    decisions: Sequence[SemanticRegionDecision],
    profile_id: str,
    profile_version: str,
    profile_content_sha256: str = "",
) -> SemanticCriterionResult:
    """

    Evaluates a condition against the exact candidate measurements and applicable manual decisions.

    Recursively evaluate compositions, then combine their required outcomes. For a
    numerical leaf, construct the measurement key from its type and sorted selector
    fields. Missing or None values produce UNAVAILABLE. A present value must be a
    finite non-Boolean number before applying the selected comparison literally.

    A manual decision must match profile ID/version/content, criterion ID, image,
    section, domain, candidate, geometry and evidence checksum. Without such a decision,
    the result is NEEDS_REVIEW. The last matching decision determines pass or fail.

    Args:
        criterion: Parsed numerical, manual or composed condition.
        evidence: Candidate identity, geometry/membership identity and measured values.
        decisions: Previously recorded manual decisions to consider.
        profile_id: Selected semantic profile identity.
        profile_version: Explicit version used for this candidate evaluation.
        profile_content_sha256: Recorded content checksum for the selected profile.

    Returns:
        SemanticCriterionResult with the comparison value, outcome and measurement
        identity, or the specific input/manual decision still required.

    Raises:
        ValueError: A present numerical value is Boolean, nonnumeric or nonfinite.

    Threshold comparisons are exact gt/ge/lt/le/eq operations. This evaluator does not
    apply an additional numerical tolerance or infer a favorable value for missing data.
    """

    if criterion.children:
        children = tuple(
            evaluate_criterion(
                child, evidence, decisions, profile_id, profile_version, profile_content_sha256
            )
            for child in criterion.children
        )
        return SemanticCriterionResult(
            criterion.kind,
            _aggregate(criterion.kind, children, criterion.k, criterion.children),
            any(child.required for child in children),
            evidence_digest=evidence.digest,
        )
    # A previous review applies only to these same profile and candidate
    # facts; a changed boundary or cell population requires a new decision.
    if criterion.kind == "manual_region_decision":
        matches = [
            decision
            for decision in decisions
            if (
                decision.profile_id,
                decision.profile_version,
                decision.profile_content_sha256,
                decision.criterion_id,
                decision.slide_id,
                decision.section_id,
                decision.analysis_domain_id,
                decision.candidate_roi_id,
                decision.geometry_digest,
                decision.evidence_digest,
            )
            == (
                profile_id,
                profile_version,
                profile_content_sha256,
                criterion.criterion_id,
                evidence.slide_id,
                evidence.section_id,
                evidence.analysis_domain_id,
                evidence.candidate_roi_id,
                evidence.geometry_digest,
                evidence.digest,
            )
        ]
        if not matches:
            return SemanticCriterionResult(
                criterion.criterion_id or "",
                RuleOutcome.NEEDS_REVIEW,
                criterion.required,
                evidence_digest=evidence.digest,
                unavailable_evidence=(criterion.criterion_id or "",),
            )
        return SemanticCriterionResult(
            criterion.criterion_id or "",
            RuleOutcome.PASS if matches[-1].action == "accept" else RuleOutcome.FAIL,
            criterion.required,
            evidence_digest=evidence.digest,
        )
    evidence_key = (
        criterion.kind
        if not criterion.selector
        else criterion.kind
        + ":"
        + json.dumps(dict(criterion.selector), sort_keys=True, separators=(",", ":"))
    )
    if evidence_key not in evidence.values or evidence.values[evidence_key] is None:
        return SemanticCriterionResult(
            criterion.criterion_id or "",
            RuleOutcome.UNAVAILABLE,
            criterion.required,
            comparator=criterion.comparator,
            threshold=criterion.threshold,
            unit=criterion.unit,
            evidence_digest=evidence.digest,
            unavailable_evidence=(criterion.kind,),
        )
    observed = evidence.values[evidence_key]
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        raise ValueError(f"evidence {criterion.kind} must be numeric")
    if isinstance(observed, float) and not math.isfinite(observed):
        raise ValueError(f"evidence {criterion.kind} must be finite")
    if criterion.comparator is None:
        return SemanticCriterionResult(
            criterion.criterion_id or "", RuleOutcome.AVAILABLE, criterion.required,
            observed_value=observed, unit=criterion.unit, evidence_digest=evidence.digest,
        )
    op = {
        "gt": lambda: observed > criterion.threshold,
        "ge": lambda: observed >= criterion.threshold,
        "lt": lambda: observed < criterion.threshold,
        "le": lambda: observed <= criterion.threshold,
        "eq": lambda: observed == criterion.threshold,
    }[criterion.comparator]
    return SemanticCriterionResult(
        criterion.criterion_id or "",
        RuleOutcome.PASS if op() else RuleOutcome.FAIL,
        criterion.required,
        observed,
        criterion.unit,
        criterion.comparator,
        criterion.threshold,
        evidence.digest,
    )
