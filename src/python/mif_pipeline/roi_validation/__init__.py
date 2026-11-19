"""Evaluate the selected measurements for a candidate region.

Current profiles report measured values and optional comparisons. The researcher
records acceptance and an optional subtype separately; these calculations never
assign a subtype. The retained v3 evaluator preserves its original rule outcomes
when reading an earlier profile.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .criteria import (
    AllOf,
    AnyOf,
    AtLeast,
    Criterion,
    RuleOutcome,
    SemanticCriterionResult,
    evaluate_criterion,
    parse_criterion,
)
from .evidence import SemanticEvidenceSnapshot, SemanticRegionDecision
from .profiles import (
    SemanticClassificationRule,
    SemanticProfileV3,
    load_semantic_profile_v3,
)

# ==============================================================================
#                               Public interfaces
# ==============================================================================


class SemanticClassificationStatus(str, Enum):
    """

    Describes the result of assigning a qualifying region to optional semantic classes.

    NOT_CONFIGURED means the profile has no class rules; NOT_EVALUATED means its
    required root criteria did not pass. CLASSIFIED identifies exactly one passing
    class with no unresolved inputs. UNCLASSIFIED means all class rules failed.
    INDETERMINATE retains unavailable measurements, NEEDS_REVIEW retains unanswered
    manual decisions, and AMBIGUOUS records multiple passing classes.
    """

    NOT_CONFIGURED = "not_configured"
    NOT_EVALUATED = "not_evaluated"
    CLASSIFIED = "classified"
    UNCLASSIFIED = "unclassified"
    INDETERMINATE = "indeterminate"
    NEEDS_REVIEW = "needs_review"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SemanticClassificationRuleResult:
    """

    Pairs one configured semantic class with its evaluated criteria.

    Attributes:
        class_id: Class named by the profile rule.
        result: Numerical/manual comparison outcome and supporting evidence identity.
    """

    class_id: str
    result: SemanticCriterionResult | None


@dataclass(frozen=True, slots=True)
class SemanticEvaluationResult:
    """

    Stores required-criterion acceptance and any optional semantic classification result.

    Attributes:
        profile: Parsed profile and content checksum used for this evaluation.
        evidence_digest: Checksum of the candidate measurements used by all criteria.
        root: Combined result of the required profile criterion tree.
        classification_status: State of the optional class-assignment step.
        semantic_class_id: Unique accepted class ID when status is CLASSIFIED; otherwise None.
        classification_rule_results: Results for every evaluated class rule in profile order.
    """

    profile: SemanticProfileV3
    evidence_digest: str
    root: SemanticCriterionResult | None
    classification_status: SemanticClassificationStatus = (
        SemanticClassificationStatus.NOT_CONFIGURED
    )
    semantic_class_id: str | None = None
    classification_rule_results: tuple[SemanticClassificationRuleResult, ...] = ()


def evaluate_semantic_profile(
    profile: SemanticProfileV3,
    evidence: SemanticEvidenceSnapshot,
    decisions: Sequence[SemanticRegionDecision] = (),
) -> SemanticEvaluationResult:
    """

    Evaluates required structure criteria, then assigns a semantic class only when the remaining decisions are resolved.

    Check that the profile and candidate describe the same configured structure.
    Evaluate the root criterion tree first. Without class rules, return that
    result directly. When the root does not pass, mark classification as
    NOT_EVALUATED; class-specific conditions are not yet needed.

    For a qualifying region, evaluate every class rule and inspect its manual
    conditions. The classification precedence is: pending manual decision,
    multiple passing classes, unavailable measurements, one passing class, or
    no passing class. A pending manual leaf keeps NEEDS_REVIEW even when another
    branch already determined its enclosing rule. A passing class is therefore
    reported only after competing rules have been resolved sufficiently.

    Args:
        profile: Validated SemanticProfileV3 containing required and optional class criteria.
        evidence: Measured values and identities of the exact candidate being considered.
        decisions: Saved manual answers that may apply to the same profile/candidate facts.

    Returns:
        SemanticEvaluationResult with the root decision, optional class-rule results
        and either a unique class ID or the reason class assignment remains unresolved.

    Raises:
        ValueError: Profile/evidence types or structure identities differ, or a criterion
            cannot be evaluated from the supplied value type.
    """

    if not isinstance(profile, SemanticProfileV3) or not isinstance(
        evidence, SemanticEvidenceSnapshot
    ):
        raise ValueError("profile and evidence must be typed semantic contracts")
    if profile.structure_id != evidence.structure_id:
        raise ValueError("profile structure_id must match evidence structure_id")
    if profile.schema_version == "semantic-profile/v4":
        def calculate(value):
            return None if value is None else evaluate_criterion(
                parse_criterion(value, allow_observations=True), evidence, (), profile.profile_id,
                profile.profile_version, profile.content_sha256,
            )
        return SemanticEvaluationResult(
            profile, evidence.digest, calculate(profile.criteria),
            SemanticClassificationStatus.NEEDS_REVIEW if profile.classification_rules
            else SemanticClassificationStatus.NOT_CONFIGURED,
            None, tuple(SemanticClassificationRuleResult(rule.class_id, calculate(rule.criteria))
                        for rule in profile.classification_rules),
        )
    root = evaluate_criterion(
        parse_criterion(profile.criteria),
        evidence,
        decisions,
        profile.profile_id,
        profile.profile_version,
        profile.content_sha256,
    )
    if not profile.classification_rules:
        return SemanticEvaluationResult(profile, evidence.digest, root)
    # Class-specific criteria apply only after the candidate satisfies
    # the required definition of this structure.
    if root.outcome is not RuleOutcome.PASS:
        return SemanticEvaluationResult(
            profile,
            evidence.digest,
            root,
            SemanticClassificationStatus.NOT_EVALUATED,
        )

    def manual_leaves(criterion: Criterion) -> tuple[Criterion, ...]:
        """

        Collects all manual questions nested in one classification rule.

        Descend through the criterion tree and retain leaves whose type is
        manual_region_decision. These are checked separately so an unanswered manual
        question remains visible even when another condition has determined a parent
        composition's result.

        Args:
            criterion: Parsed class-rule criterion tree.

        Returns:
            Tuple of manual leaf criteria in traversal order, empty when the rule is fully numerical.
        """

        if criterion.children:
            return tuple(leaf for child in criterion.children for leaf in manual_leaves(child))
        return (criterion,) if criterion.kind == "manual_region_decision" else ()

    evaluated_rules: list[SemanticClassificationRuleResult] = []
    unresolved_manual = False
    for rule in profile.classification_rules:
        criterion = parse_criterion(rule.criteria)
        evaluated_rules.append(
            SemanticClassificationRuleResult(
                rule.class_id,
                evaluate_criterion(
                    criterion,
                    evidence,
                    decisions,
                    profile.profile_id,
                    profile.profile_version,
                    profile.content_sha256,
                ),
            )
        )
        unresolved_manual = unresolved_manual or any(
            evaluate_criterion(
                leaf,
                evidence,
                decisions,
                profile.profile_id,
                profile.profile_version,
                profile.content_sha256,
            ).outcome
            is RuleOutcome.NEEDS_REVIEW
            for leaf in manual_leaves(criterion)
        )
    rule_results = tuple(evaluated_rules)
    outcomes = tuple(item.result.outcome for item in rule_results)
    passed = tuple(
        item.class_id for item in rule_results if item.result.outcome is RuleOutcome.PASS
    )
    # Resolve manual questions and competing classes before assigning
    # one semantic label; rule order does not supply class precedence.
    if unresolved_manual or RuleOutcome.NEEDS_REVIEW in outcomes:
        status = SemanticClassificationStatus.NEEDS_REVIEW
        class_id = None
    elif len(passed) >= 2:
        status = SemanticClassificationStatus.AMBIGUOUS
        class_id = None
    elif RuleOutcome.UNAVAILABLE in outcomes:
        status = SemanticClassificationStatus.INDETERMINATE
        class_id = None
    elif len(passed) == 1:
        status = SemanticClassificationStatus.CLASSIFIED
        class_id = passed[0]
    else:
        status = SemanticClassificationStatus.UNCLASSIFIED
        class_id = None
    return SemanticEvaluationResult(
        profile,
        evidence.digest,
        root,
        status,
        class_id,
        rule_results,
    )


from .semantic import SemanticValidationError, validate_polygonal_geometry

__all__ = [
    "SemanticClassificationRule",
    "SemanticClassificationRuleResult",
    "SemanticClassificationStatus",
    "SemanticProfileV3",
    "AllOf",
    "AnyOf",
    "AtLeast",
    "SemanticEvidenceSnapshot",
    "SemanticCriterionResult",
    "SemanticEvaluationResult",
    "SemanticRegionDecision",
    "evaluate_semantic_profile",
    "load_semantic_profile_v3",
    "RuleOutcome",
    "SemanticValidationError",
    "validate_polygonal_geometry",
]
