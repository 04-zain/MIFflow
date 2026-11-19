"""

Applies configured object-count requirements to primary compartments and final secondary structures.

Counts are evaluated within one analysis domain. Each observed object has a
stable content identity, its parent identity and a geometry checksum. Keeping
these identities with the count makes it possible to distinguish a change
in object membership from the same number of newly generated objects.

If any required output falls below its minimum, the whole domain is excluded
from active analysis and its objects remain recorded for review. Primary
compartments above a configured maximum require review while retaining all
components. A final secondary structure can instead use an explicit maximum
with either seeded hash selection or a recorded manual choice.

Selected and unselected secondary objects keep their original geometry and
membership records. Capping changes the active selection, not whether those
objects satisfied the biological criteria that produced them.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import re

from mif_pipeline.pipeline_core import AnalysisDomainKind, AnalysisDomainRef, ROIAnalysisError
from mif_pipeline.pipeline_core.config import SecondaryExcessPolicy, StructureCountBounds

_SHA256 = re.compile(r"[0-9a-f]{64}")


# ==============================================================================
#                                    Count qc
# ==============================================================================


class InventoryDisposition(str, Enum):
    """

    Identifies whether an observed object remains active, was omitted by a cap, or belongs to an excluded domain.

    ACTIVE_INCLUDED contributes to subsequent analysis. VALID_NOT_SELECTED_BY_CAP
    retains a valid object outside the chosen capped subset. AUDIT_EXCLUDED_BELOW_MINIMUM
    retains an object only in the record of a domain that failed a required minimum.
    """

    ACTIVE_INCLUDED = "active_included"
    VALID_NOT_SELECTED_BY_CAP = "valid_not_selected_by_cap"
    AUDIT_EXCLUDED_BELOW_MINIMUM = "audit_excluded_below_minimum"


class OutputStatus(str, Enum):
    """

    Describes the count decision for one configured class or structure.

    WITHIN_BOUNDS needs no maximum selection, BELOW_MINIMUM triggers domain
    exclusion, PRIMARY_REVIEW_REQUIRED flags excess primary components, and
    CAPPED records an explicitly selected final-secondary subset.
    """

    WITHIN_BOUNDS = "within_bounds"
    BELOW_MINIMUM = "below_minimum"
    PRIMARY_REVIEW_REQUIRED = "primary_review_required"
    CAPPED = "capped"


class InclusionProbabilityStatus(str, Enum):
    """

    Marks whether the selection record supplies an inclusion-probability value.

    The implementation records KNOWN with maximum/observed_count for seeded hash
    selection and with 0 or 1 for complete exclusion/inclusion. Manual choices
    record UNKNOWN and a null probability. The stored hash-sampling fraction is
    not, by itself, a statement about biological representativeness or independence.
    """

    KNOWN = "known"
    UNKNOWN = "unknown"


class DomainExclusionStatus(str, Enum):
    """

    Records that a domain was excluded because at least one configured output missed its minimum.
    """

    EXCLUDED_BELOW_MINIMUM = "excluded_below_minimum"


class SelectionAlgorithm(str, Enum):
    """

    Identifies deterministic seeded hash ranking or an explicit manual object selection.
    """

    SHA256_RANK_V1 = "sha256-rank/v1"
    MANUAL_SELECT_V1 = "manual-select/v1"


def _nonblank(value: object, field_name: str) -> str:
    """

    Checks that a required identity or explanation contains nonblank text.

    Args:
        value: Text value to check.
        field_name: Field name used in the diagnostic.

    Returns:
        The supplied string, with its original capitalization and spacing.

    Raises:
        ROIAnalysisError: The value is not text or contains only whitespace.
    """

    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field_name} must be a non-empty string")
    return value


def _sha256(value: object, field_name: str) -> str:
    """

    Checks that a content identity is written as a lowercase SHA-256 checksum.

    Args:
        value: Expected 64-character lowercase hexadecimal text.
        field_name: Content field included in an error.

    Returns:
        The checked checksum text; the corresponding object or file is checked by its caller.

    Raises:
        ROIAnalysisError: The supplied value is blank, incorrectly sized or nonhexadecimal.
    """

    result = _nonblank(value, field_name)
    if _SHA256.fullmatch(result) is None:
        raise ROIAnalysisError(f"{field_name} must be a lowercase SHA-256")
    return result


def _count(value: object, field_name: str, *, positive: bool = False) -> int:
    """

    Checks a count or count bound, with an optional strictly positive requirement.

    Args:
        value: Integer count to check; booleans are rejected.
        field_name: Name of the count used in an error.
        positive: True requires a value of at least one; False permits zero.

    Returns:
        The supplied integer.

    Raises:
        ROIAnalysisError: The type or lower bound is invalid.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ROIAnalysisError(f"{field_name} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ROIAnalysisError(f"{field_name} must be {qualifier}")
    return value


def _integer(value: object, field_name: str) -> int:
    """

    Checks an integer parameter such as the deterministic-selection seed.

    Args:
        value: Candidate integer; positive, zero and negative values are permitted.
        field_name: Parameter name used in diagnostics.

    Returns:
        The supplied integer.

    Raises:
        ROIAnalysisError: The value is Boolean or not an integer.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ROIAnalysisError(f"{field_name} must be an integer")
    return value


def _closed_enum(enum_type: type[Enum], value: object, field_name: str) -> Enum:
    """

    Reads a declared count-decision option from its enum value or exact saved string.

    Args:
        enum_type: Enum defining the supported choices.
        value: Existing member or saved text value to decode.
        field_name: Setting name included in an error.

    Returns:
        Matching enum member.

    Raises:
        ROIAnalysisError: The value has an unsupported type or does not name an allowed choice.
    """

    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise ROIAnalysisError(f"{field_name} is invalid") from exc
    raise ROIAnalysisError(f"{field_name} is invalid")


def _fraction(value: object, field_name: str) -> float:
    """

    Checks a finite fraction and represents it as a Python float.

    Args:
        value: Integer or float between zero and one, with booleans rejected.
        field_name: Fraction field included in the diagnostic.

    Returns:
        Float value in the closed interval [0, 1].

    Raises:
        ROIAnalysisError: The value is nonnumeric, nonfinite or outside that interval.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ROIAnalysisError(f"{field_name} must be a finite fraction")
    result = float(value)
    if not (0.0 <= result <= 1.0):
        raise ROIAnalysisError(f"{field_name} must be between zero and one")
    return result


def _canonical_sha256(value: object) -> str:
    """

    Calculates a deterministic checksum of a JSON-compatible count or selection record.

    Mapping keys are sorted and incidental whitespace omitted. Sequence order
    remains meaningful, so callers sort object identities before hashing them.

    Args:
        value: Record to encode as compact JSON.

    Returns:
        SHA-256 checksum of its UTF-8 JSON representation.
    """

    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectionContext:
    """

    Identifies the domain, configuration and preceding result used to select a capped subset.

    Attributes:
        analysis_domain_sha256: Exact domain definition on which counts were evaluated.
        config_sha256: Settings defining the current structure and its count policy.
        result_sha256: Scientific result supplying the eligible object population.
    """

    analysis_domain_sha256: str
    config_sha256: str
    result_sha256: str

    def __post_init__(self) -> None:
        """

        Checks that all three selection-input identities have valid SHA-256 text.

        Raises:
            ROIAnalysisError: A domain, configuration or result identity is invalid.
        """

        _sha256(self.analysis_domain_sha256, "analysis_domain_sha256")
        _sha256(self.config_sha256, "config_sha256")
        _sha256(self.result_sha256, "result_sha256")


@dataclass(frozen=True, slots=True)
class DeterministicSelectionLineage:
    """

    Records the inputs needed to repeat a seeded hash selection of secondary objects.

    Attributes:
        analysis_domain_sha256: Domain definition used during selection.
        config_sha256: Count and structure configuration.
        result_sha256: Preceding accepted scientific result.
        inventory_sha256: Checksum of the complete eligible object list and geometry/parent identities.
        structure_id: One secondary structure represented by that list.
        algorithm: SHA256_RANK_V1, identifying the implemented ranking calculation.
        sampling_seed: Recorded integer included in each object's rank input.
    """

    analysis_domain_sha256: str
    config_sha256: str
    result_sha256: str
    inventory_sha256: str
    structure_id: str
    algorithm: SelectionAlgorithm
    sampling_seed: int

    def __post_init__(self) -> None:
        """

        Checks the exact inputs and algorithm used by a deterministic secondary-object selection.

        Raises:
            ROIAnalysisError: A checksum/structure ID is invalid, the algorithm differs
                from sha256-rank/v1, or the seed is not a non-Boolean integer.
        """

        _validate_lineage_identity(self)
        algorithm = _closed_enum(SelectionAlgorithm, self.algorithm, "selection algorithm")
        if algorithm is not SelectionAlgorithm.SHA256_RANK_V1:
            raise ROIAnalysisError("deterministic lineage requires sha256-rank/v1")
        object.__setattr__(self, "algorithm", algorithm)
        _integer(self.sampling_seed, "sampling_seed")


@dataclass(frozen=True, slots=True)
class ManualSelectionLineage:
    """

    Records a reviewer's exact selected objects and the reason for choosing them.

    Attributes:
        analysis_domain_sha256: Domain on which this review was performed.
        config_sha256: Structure/count settings used by the review.
        result_sha256: Scientific result defining the reviewed candidates.
        inventory_sha256: Complete eligible-object list reviewed before selection.
        structure_id: Secondary structure being capped.
        algorithm: MANUAL_SELECT_V1.
        selected_content_keys: Unique selected object identities in the required object-list order.
        selection_criterion: Description of how the researcher chose the objects.
        reviewer_id: Recorded reviewer identity.
        rationale: Explanation supplied for this selection.
    """

    analysis_domain_sha256: str
    config_sha256: str
    result_sha256: str
    inventory_sha256: str
    structure_id: str
    algorithm: SelectionAlgorithm
    selected_content_keys: tuple[str, ...]
    selection_criterion: str
    reviewer_id: str
    rationale: str

    def __post_init__(self) -> None:
        """

        Checks that a manual selection records unique object identities and complete reviewer information.

        Membership in the offered object list and the required selection count are
        checked when select_capped_secondary() receives the current list.

        Raises:
            ROIAnalysisError: Selection context, algorithm, selected keys or review text is invalid.
        """

        _validate_lineage_identity(self)
        algorithm = _closed_enum(SelectionAlgorithm, self.algorithm, "selection algorithm")
        if algorithm is not SelectionAlgorithm.MANUAL_SELECT_V1:
            raise ROIAnalysisError("manual lineage requires manual-select/v1")
        object.__setattr__(self, "algorithm", algorithm)
        keys = tuple(self.selected_content_keys)
        if not keys or len(set(keys)) != len(keys):
            raise ROIAnalysisError("manual selected content keys must be non-empty and unique")
        for key in keys:
            _sha256(key, "manual selected content key")
        object.__setattr__(self, "selected_content_keys", keys)
        _nonblank(self.selection_criterion, "selection_criterion")
        _nonblank(self.reviewer_id, "reviewer_id")
        _nonblank(self.rationale, "rationale")


SelectionLineage = DeterministicSelectionLineage | ManualSelectionLineage


def _validate_lineage_identity(lineage: SelectionLineage) -> None:
    """

    Checks the shared domain, result and object-list identities of a capped selection.

    Args:
        lineage: Manual or deterministic selection record.

    Raises:
        ROIAnalysisError: A checksum is malformed or the structure ID is blank.
    """

    _sha256(lineage.analysis_domain_sha256, "analysis_domain_sha256")
    _sha256(lineage.config_sha256, "config_sha256")
    _sha256(lineage.result_sha256, "result_sha256")
    _sha256(lineage.inventory_sha256, "inventory_sha256")
    _nonblank(lineage.structure_id, "structure_id")


@dataclass(frozen=True, slots=True)
class ObjectInventoryItem:
    """

    Describes one observed structure object and its subsequent selection status.

    Attributes:
        content_key: Stable identity for this object's recorded scientific content.
        structure_id: Configured structure to which the object belongs.
        parent_content_key: Identity of the material parent containing the object.
        geometry_sha256: Recorded boundary identity.
        disposition: Active, valid-but-capped-out, or retained only because its domain was excluded.
        observed_count: Full eligible-object count when a cap was applied; otherwise None.
        inclusion_fraction: Selected count divided by the full eligible count for a cap.
        inventory_sha256: Identity of that complete eligible-object list.
        inclusion_probability_status: KNOWN for the implemented hash-ranking convention,
            UNKNOWN for a manual choice, or None when no cap applies.
        inclusion_probability: Recorded probability value, or None for an unknown probability.
        selection_lineage: Complete deterministic/manual selection inputs and explanation.
    """

    content_key: str
    structure_id: str
    parent_content_key: str
    geometry_sha256: str
    disposition: InventoryDisposition = InventoryDisposition.ACTIVE_INCLUDED
    observed_count: int | None = None
    inclusion_fraction: float | None = None
    inventory_sha256: str | None = None
    inclusion_probability_status: InclusionProbabilityStatus | None = None
    inclusion_probability: float | None = None
    selection_lineage: SelectionLineage | None = None

    def __post_init__(self) -> None:
        """

        Checks that an object's disposition and cap metadata describe the same selection.

        Without a cap record, all optional cap fields must be absent and the object
        cannot be labelled capped-out. With a cap record, require the full observed
        count, fraction and object-list identity. The structure and list checksum
        must match the selection record. Deterministic selection stores a known
        probability equal to its fraction; manual selection stores a null probability.

        An object retained from a below-minimum domain cannot simultaneously carry
        secondary-cap metadata because whole-domain exclusion is decided first.

        Raises:
            ROIAnalysisError: Object identities, fractions, disposition or associated
                selection fields are incomplete or contradictory.
        """

        _sha256(self.content_key, "content_key")
        _nonblank(self.structure_id, "structure_id")
        _sha256(self.parent_content_key, "parent_content_key")
        _sha256(self.geometry_sha256, "geometry_sha256")
        disposition = _closed_enum(InventoryDisposition, self.disposition, "inventory disposition")
        object.__setattr__(self, "disposition", disposition)
        if self.observed_count is not None:
            _count(self.observed_count, "observed_count")
        if self.inclusion_fraction is not None:
            object.__setattr__(
                self,
                "inclusion_fraction",
                _fraction(self.inclusion_fraction, "inclusion_fraction"),
            )
        if self.inventory_sha256 is not None:
            _sha256(self.inventory_sha256, "inventory_sha256")
        probability_status = self.inclusion_probability_status
        if probability_status is not None:
            probability_status = _closed_enum(
                InclusionProbabilityStatus,
                probability_status,
                "inclusion_probability_status",
            )
            object.__setattr__(self, "inclusion_probability_status", probability_status)
        if self.inclusion_probability is not None:
            object.__setattr__(
                self,
                "inclusion_probability",
                _fraction(self.inclusion_probability, "inclusion_probability"),
            )
        cap_metadata = (
            self.observed_count,
            self.inclusion_fraction,
            self.inventory_sha256,
            probability_status,
            self.selection_lineage,
        )
        if self.selection_lineage is None:
            if any(value is not None for value in cap_metadata[:-1]):
                raise ROIAnalysisError("selection metadata requires closed selection lineage")
            if self.inclusion_probability is not None:
                raise ROIAnalysisError("selection probability requires closed selection lineage")
            if disposition is InventoryDisposition.VALID_NOT_SELECTED_BY_CAP:
                raise ROIAnalysisError("capped-out inventory item requires selection lineage")
            return
        if not isinstance(
            self.selection_lineage,
            (DeterministicSelectionLineage, ManualSelectionLineage),
        ):
            raise ROIAnalysisError("selection_lineage has an invalid type")
        if disposition is InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM:
            raise ROIAnalysisError("below-minimum audit item cannot carry cap lineage")
        if any(value is None for value in cap_metadata[:-1]):
            raise ROIAnalysisError("cap lineage requires complete item selection metadata")
        if self.inventory_sha256 != self.selection_lineage.inventory_sha256:
            raise ROIAnalysisError("item inventory digest must match selection lineage")
        if self.structure_id != self.selection_lineage.structure_id:
            raise ROIAnalysisError("item structure must match selection lineage")
        if probability_status is InclusionProbabilityStatus.KNOWN:
            if self.inclusion_probability is None:
                raise ROIAnalysisError("known inclusion probability requires a value")
        elif self.inclusion_probability is not None:
            raise ROIAnalysisError("unknown inclusion probability requires a null value")
        if isinstance(self.selection_lineage, DeterministicSelectionLineage):
            if (
                probability_status is not InclusionProbabilityStatus.KNOWN
                or self.inclusion_probability != self.inclusion_fraction
            ):
                raise ROIAnalysisError("deterministic item probability is inconsistent")
        elif (
            probability_status is not InclusionProbabilityStatus.UNKNOWN
            or self.inclusion_probability is not None
        ):
            raise ROIAnalysisError("manual item probability must be unknown")

    @property
    def selection_algorithm(self) -> str | None:
        """

        Returns the saved cap algorithm when this object participated in a capped selection.

        Returns:
            Algorithm text, or None when the object has no cap-selection record.
        """

        return None if self.selection_lineage is None else self.selection_lineage.algorithm.value

    @property
    def selection_criterion(self) -> str | None:
        """

        Returns the recorded basis for selecting this object population.

        Returns:
            The researcher's manual criterion, "lowest_sha256_rank" for deterministic
            ranking, or None when no cap was applied.
        """

        if isinstance(self.selection_lineage, ManualSelectionLineage):
            return self.selection_lineage.selection_criterion
        if isinstance(self.selection_lineage, DeterministicSelectionLineage):
            return "lowest_sha256_rank"
        return None


@dataclass(frozen=True, slots=True)
class StructureCountObservation:
    """

    Stores a structure's observed count with the complete corresponding object list.

    Attributes:
        output_id: Configured primary class or secondary structure identity.
        count: Number of observed objects.
        inventory: All ObjectInventoryItem records contributing to that count.
        inventory_sha256: Calculated checksum of sorted object identities, boundaries,
            parent identities and structure IDs.
    """

    output_id: str
    count: int
    inventory: tuple[ObjectInventoryItem, ...] = ()
    inventory_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """

        Checks that the observed count equals the unique supplied object list.

        Sort objects by content_key, require their structure IDs to match output_id,
        and calculate the checksum from their geometry and parent identities. The
        count can be zero, in which case the complete corresponding list is empty.

        Raises:
            ROIAnalysisError: The output/count is invalid, object identities repeat,
                list length differs from count, or objects belong to another structure.
        """

        _nonblank(self.output_id, "output_id")
        _count(self.count, "count")
        inventory = _canonical_inventory(self.inventory)
        if len(inventory) != self.count:
            raise ROIAnalysisError("inventory length must equal the observed count")
        if any(item.structure_id != self.output_id for item in inventory):
            raise ROIAnalysisError("inventory structure_id must equal observation output_id")
        object.__setattr__(self, "inventory", inventory)
        object.__setattr__(self, "inventory_sha256", _inventory_sha256(inventory))

    @property
    def observed_count(self) -> int:
        """

        Returns the recorded number of observed objects.

        Returns:
            The count associated with the complete checked object list.
        """

        return self.count


@dataclass(frozen=True, slots=True)
class OutputViolation:
    """

    Stores a structure's count decision, complete object list and selected subset.

    Despite its name, the record also represents a count within its configured
    bounds. Capped results preserve selected and unselected identities so the
    observed total remains distinguishable from the active count.

    Attributes:
        output_id: Primary class or secondary structure being evaluated.
        status: Within bounds, below minimum, primary review required, or capped.
        observed_count: Complete number of observed eligible objects.
        minimum: Configured minimum object count.
        maximum: Optional finite maximum; None leaves the upper count unrestricted.
        inventory_sha256: Checksum of the complete object-list scientific content.
        inventory: Full list with the decision's per-object disposition and cap details.
        included_content_keys: Active selected object identities, in object-list order.
        not_selected_content_keys: Valid objects omitted by an applied cap.
        inclusion_fraction: Active/observed fraction, or zero for domain exclusion.
        inclusion_probability_status: KNOWN or UNKNOWN under the recorded selection convention.
        inclusion_probability: Value associated with that status; None for manual selection.
        selection_lineage: Exact deterministic/manual cap inputs, or None for an uncapped result.
    """

    output_id: str
    status: OutputStatus
    observed_count: int
    minimum: int
    maximum: int | None
    inventory_sha256: str
    inventory: tuple[ObjectInventoryItem, ...]
    included_content_keys: tuple[str, ...]
    not_selected_content_keys: tuple[str, ...] = ()
    inclusion_fraction: float = 1.0
    inclusion_probability_status: InclusionProbabilityStatus = InclusionProbabilityStatus.KNOWN
    inclusion_probability: float | None = 1.0
    selection_lineage: SelectionLineage | None = None

    def __post_init__(self) -> None:
        """

        Checks count bounds, object identities and the complete active-selection description.

        Recalculate the object-list checksum and require observed_count to equal its
        length. Included and capped-out keys must be unique and disjoint. Validate
        fractions/probabilities, then apply the status-specific checks that relate
        the chosen subset, bound and every object's disposition.

        Raises:
            ROIAnalysisError: Counts, bounds, list checksum, selection IDs, fractions
                or status-specific relationships are inconsistent.
        """

        _nonblank(self.output_id, "output_id")
        status = _closed_enum(OutputStatus, self.status, "output status")
        object.__setattr__(self, "status", status)
        _count(self.observed_count, "observed_count")
        _count(self.minimum, "minimum")
        if self.maximum is not None:
            _count(self.maximum, "maximum", positive=True)
            if self.maximum < self.minimum:
                raise ROIAnalysisError("maximum must be greater than or equal to minimum")
        _sha256(self.inventory_sha256, "inventory_sha256")
        inventory = _canonical_inventory(self.inventory)
        if len(inventory) != self.observed_count:
            raise ROIAnalysisError("observed count must equal inventory length")
        if any(item.structure_id != self.output_id for item in inventory):
            raise ROIAnalysisError("output inventory structure IDs must match output_id")
        if _inventory_sha256(inventory) != self.inventory_sha256:
            raise ROIAnalysisError("output inventory digest does not match inventory content")
        object.__setattr__(self, "inventory", inventory)
        included = _content_keys(self.included_content_keys, "included_content_keys")
        not_selected = _content_keys(self.not_selected_content_keys, "not_selected_content_keys")
        if set(included) & set(not_selected):
            raise ROIAnalysisError("included and capped-out keys must be disjoint")
        object.__setattr__(self, "included_content_keys", included)
        object.__setattr__(self, "not_selected_content_keys", not_selected)
        fraction = _fraction(self.inclusion_fraction, "inclusion_fraction")
        object.__setattr__(self, "inclusion_fraction", fraction)
        probability_status = _closed_enum(
            InclusionProbabilityStatus,
            self.inclusion_probability_status,
            "inclusion_probability_status",
        )
        object.__setattr__(self, "inclusion_probability_status", probability_status)
        probability = self.inclusion_probability
        if probability is not None:
            probability = _fraction(probability, "inclusion_probability")
            object.__setattr__(self, "inclusion_probability", probability)
        if probability_status is InclusionProbabilityStatus.KNOWN and probability is None:
            raise ROIAnalysisError("known inclusion probability requires a value")
        if probability_status is InclusionProbabilityStatus.UNKNOWN and probability is not None:
            raise ROIAnalysisError("unknown inclusion probability requires a null value")
        self._validate_status_state(
            status=status,
            inventory=inventory,
            included=included,
            not_selected=not_selected,
            fraction=fraction,
            probability_status=probability_status,
            probability=probability,
        )

    def _validate_status_state(
        self,
        *,
        status: OutputStatus,
        inventory: tuple[ObjectInventoryItem, ...],
        included: tuple[str, ...],
        not_selected: tuple[str, ...],
        fraction: float,
        probability_status: InclusionProbabilityStatus,
        probability: float | None,
    ) -> None:
        """

        Checks the object partition and metadata required by a particular count status.

        A capped result must exceed its finite maximum and include exactly maximum
        objects. Selected and unselected keys partition the complete list in its
        sorted order. Their shared inclusion fraction is maximum/observed_count,
        and every object must carry the same selection record and matching status.

        A below-minimum result retains all objects for review with zero active
        inclusion. Other uncapped results either retain all objects actively or
        retain all as excluded when another output caused domain exclusion.

        Args:
            status: Parsed count-decision state.
            inventory: Checked complete object tuple.
            included: Unique selected keys.
            not_selected: Unique keys omitted by a cap.
            fraction: Checked selected/observed fraction.
            probability_status: Whether an inclusion probability is recorded.
            probability: Recorded probability value or None.

        Raises:
            ROIAnalysisError: The decision's count, partition, ordering, probability
                or per-object metadata contradicts its status.
        """

        inventory_keys = tuple(item.content_key for item in inventory)
        if status is OutputStatus.CAPPED:
            if self.maximum is None or self.observed_count <= self.maximum:
                raise ROIAnalysisError("capped output requires an exceeded finite maximum")
            if not isinstance(
                self.selection_lineage,
                (DeterministicSelectionLineage, ManualSelectionLineage),
            ):
                raise ROIAnalysisError("capped output requires closed selection lineage")
            if len(included) != self.maximum:
                raise ROIAnalysisError("included keys must equal the finite maximum")
            if set(included) | set(not_selected) != set(inventory_keys):
                raise ROIAnalysisError("included and capped-out keys must partition inventory")
            if included != tuple(key for key in inventory_keys if key in set(included)):
                raise ROIAnalysisError("included keys must follow canonical inventory order")
            if not_selected != tuple(key for key in inventory_keys if key in set(not_selected)):
                raise ROIAnalysisError("capped-out keys must follow canonical inventory order")
            expected_fraction = self.maximum / self.observed_count
            if fraction != expected_fraction:
                raise ROIAnalysisError("cap inclusion fraction is inconsistent")
            lineage = self.selection_lineage
            if (
                lineage.inventory_sha256 != self.inventory_sha256
                or lineage.structure_id != self.output_id
            ):
                raise ROIAnalysisError("selection lineage does not match output identity")
            if isinstance(lineage, ManualSelectionLineage):
                if included != lineage.selected_content_keys:
                    raise ROIAnalysisError("manual lineage selected keys do not match output")
                if (
                    probability_status is not InclusionProbabilityStatus.UNKNOWN
                    or probability is not None
                ):
                    raise ROIAnalysisError("manual inclusion probability must be unknown")
            elif (
                probability_status is not InclusionProbabilityStatus.KNOWN
                or probability != expected_fraction
            ):
                raise ROIAnalysisError("deterministic inclusion probability is inconsistent")
            for item in inventory:
                expected_disposition = (
                    InventoryDisposition.ACTIVE_INCLUDED
                    if item.content_key in set(included)
                    else InventoryDisposition.VALID_NOT_SELECTED_BY_CAP
                )
                if item.disposition is not expected_disposition:
                    raise ROIAnalysisError("inventory disposition does not match cap partition")
                if (
                    item.selection_lineage != lineage
                    or item.observed_count != self.observed_count
                    or item.inclusion_fraction != fraction
                    or item.inventory_sha256 != self.inventory_sha256
                    or item.inclusion_probability_status is not probability_status
                    or item.inclusion_probability != probability
                ):
                    raise ROIAnalysisError("inventory cap metadata does not match output")
            return

        if self.selection_lineage is not None or not_selected:
            raise ROIAnalysisError("uncapped output cannot carry cap lineage or capped-out keys")
        if status is OutputStatus.BELOW_MINIMUM:
            if self.observed_count >= self.minimum:
                raise ROIAnalysisError("below-minimum status requires a failed minimum")
            if included or fraction != 0.0 or probability != 0.0:
                raise ROIAnalysisError("below-minimum output cannot retain active inclusion")
            if any(
                item.disposition is not InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM
                for item in inventory
            ):
                raise ROIAnalysisError("below-minimum inventory must remain audit-only")
            return
        if self.observed_count < self.minimum:
            raise ROIAnalysisError("non-failing output cannot be below its minimum")
        if status is OutputStatus.PRIMARY_REVIEW_REQUIRED and (
            self.maximum is None or self.observed_count <= self.maximum
        ):
            raise ROIAnalysisError("primary review requires an exceeded finite maximum")
        active_state = (
            included == inventory_keys
            and fraction == 1.0
            and probability_status is InclusionProbabilityStatus.KNOWN
            and probability == 1.0
            and all(item.disposition is InventoryDisposition.ACTIVE_INCLUDED for item in inventory)
        )
        audit_state = (
            not included
            and fraction == 0.0
            and probability_status is InclusionProbabilityStatus.KNOWN
            and probability == 0.0
            and all(
                item.disposition is InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM
                for item in inventory
            )
        )
        if not (active_state or audit_state):
            raise ROIAnalysisError("uncapped output inclusion state is contradictory")
        if (
            active_state
            and status is OutputStatus.WITHIN_BOUNDS
            and self.maximum is not None
            and self.observed_count > self.maximum
        ):
            raise ROIAnalysisError("within-bounds output cannot exceed its maximum")

    @property
    def selection_algorithm(self) -> str | None:
        """

        Returns the cap algorithm recorded for this output.

        Returns:
            Saved algorithm text, or None when no cap was applied.
        """

        return None if self.selection_lineage is None else self.selection_lineage.algorithm.value

    @property
    def selection_criterion(self) -> str | None:
        """

        Returns the manual criterion or deterministic ranking rule for the selected subset.

        Returns:
            Manual description, "lowest_sha256_rank", or None for an uncapped output.
        """

        if isinstance(self.selection_lineage, ManualSelectionLineage):
            return self.selection_lineage.selection_criterion
        if isinstance(self.selection_lineage, DeterministicSelectionLineage):
            return "lowest_sha256_rank"
        return None


@dataclass(frozen=True, slots=True)
class DomainExclusion:
    """

    Records why an analysis domain lost active inclusion after count evaluation.

    Attributes:
        domain: Exact AnalysisDomainRef evaluated by the count step.
        status: EXCLUDED_BELOW_MINIMUM.
        failing_output_ids: Unique outputs whose observed counts missed their minima.
        reason: Recorded reason "one_or_more_outputs_below_configured_minimum".
    """

    domain: AnalysisDomainRef
    status: DomainExclusionStatus
    failing_output_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        """

        Checks the excluded domain and the explicit list of outputs that caused exclusion.

        Raises:
            ROIAnalysisError: The domain type, status, failing-output list or reason is invalid.
        """

        if not isinstance(self.domain, AnalysisDomainRef):
            raise ROIAnalysisError("domain must be an AnalysisDomainRef")
        status = _closed_enum(DomainExclusionStatus, self.status, "domain exclusion status")
        object.__setattr__(self, "status", status)
        failing = tuple(self.failing_output_ids)
        if not failing or len(set(failing)) != len(failing):
            raise ROIAnalysisError("domain exclusion requires unique failing outputs")
        for output_id in failing:
            _nonblank(output_id, "failing output ID")
        object.__setattr__(self, "failing_output_ids", failing)
        if self.reason != "one_or_more_outputs_below_configured_minimum":
            raise ROIAnalysisError("domain exclusion reason is invalid")


@dataclass(frozen=True, slots=True)
class StructureCountDecision:
    """

    Combines count results into one active-or-excluded analysis-domain decision.

    Attributes:
        domain: Selected tissue/analysis domain to which all results apply.
        outputs: One OutputViolation per configured output, without repeated output IDs.
        domain_exclusion: Required when any output fails its minimum; otherwise None.
        component_selection_action: Reserved None field. Primary components are
            reviewed as a complete population rather than thinned by this decision.
    """

    domain: AnalysisDomainRef
    outputs: tuple[OutputViolation, ...]
    domain_exclusion: DomainExclusion | None = None
    component_selection_action: str | None = None

    def __post_init__(self) -> None:
        """

        Checks that all output inclusion states agree with the domain-level decision.

        A failed minimum requires a matching exclusion record naming exactly the
        failing outputs. Every object's disposition then becomes audit-only, all
        active selections are empty and capped/review-required statuses are absent.
        Without exclusion, ordinary uncapped outputs keep all observed objects active.

        Raises:
            ROIAnalysisError: Output identities repeat, a forbidden component-selection
                action is supplied, or output dispositions conflict with domain exclusion.
        """

        if not isinstance(self.domain, AnalysisDomainRef):
            raise ROIAnalysisError("domain must be an AnalysisDomainRef")
        outputs = tuple(self.outputs)
        if not all(isinstance(output, OutputViolation) for output in outputs):
            raise ROIAnalysisError("outputs must contain OutputViolation values")
        if len({output.output_id for output in outputs}) != len(outputs):
            raise ROIAnalysisError("decision output IDs must be unique")
        object.__setattr__(self, "outputs", outputs)
        if self.component_selection_action is not None:
            raise ROIAnalysisError("primary component selection action is forbidden")
        failing = tuple(
            output.output_id for output in outputs if output.status is OutputStatus.BELOW_MINIMUM
        )
        if self.domain_exclusion is None:
            if failing:
                raise ROIAnalysisError("below-minimum outputs require a domain exclusion")
            for output in outputs:
                if output.status not in {
                    OutputStatus.WITHIN_BOUNDS,
                    OutputStatus.PRIMARY_REVIEW_REQUIRED,
                }:
                    continue
                inventory_keys = tuple(item.content_key for item in output.inventory)
                if (
                    output.included_content_keys != inventory_keys
                    or output.inclusion_fraction != 1.0
                    or output.inclusion_probability_status is not InclusionProbabilityStatus.KNOWN
                    or output.inclusion_probability != 1.0
                    or any(
                        item.disposition is not InventoryDisposition.ACTIVE_INCLUDED
                        for item in output.inventory
                    )
                ):
                    raise ROIAnalysisError("non-excluded uncapped output requires active semantics")
            return
        if not isinstance(self.domain_exclusion, DomainExclusion):
            raise ROIAnalysisError("domain_exclusion has an invalid type")
        if self.domain_exclusion.domain != self.domain:
            raise ROIAnalysisError("domain exclusion must match decision domain")
        if any(
            output.status not in {OutputStatus.BELOW_MINIMUM, OutputStatus.WITHIN_BOUNDS}
            for output in outputs
        ):
            raise ROIAnalysisError(
                "domain exclusion forbids review-required or capped output status"
            )
        if self.domain_exclusion.failing_output_ids != failing:
            raise ROIAnalysisError("domain exclusion failing outputs do not match decision")
        if any(
            output.included_content_keys
            or output.inclusion_fraction != 0.0
            or output.inclusion_probability_status is not InclusionProbabilityStatus.KNOWN
            or output.inclusion_probability != 0.0
            for output in outputs
        ):
            raise ROIAnalysisError("excluded domain outputs require zero inclusion semantics")
        if any(
            item.disposition is not InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM
            for output in outputs
            for item in output.inventory
        ):
            raise ROIAnalysisError("excluded domain inventory must remain audit-only")

    @property
    def included_content_keys(self) -> tuple[str, ...]:
        """

        Collects the active selected object keys across the domain's ordered outputs.

        Returns:
            Flattened tuple of included keys. Excluded domains return an empty tuple.
        """

        return tuple(key for output in self.outputs for key in output.included_content_keys)


def _canonical_inventory(
    items: Sequence[ObjectInventoryItem],
) -> tuple[ObjectInventoryItem, ...]:
    """

    Checks unique object identities and sorts the complete list by content_key.

    Args:
        items: Sequence of ObjectInventoryItem records for one count evaluation.

    Returns:
        Tuple sorted by object content identity, independent of input-row order.

    Raises:
        ROIAnalysisError: An element has the wrong record type or an identity repeats.
    """

    inventory = tuple(items)
    if not all(isinstance(item, ObjectInventoryItem) for item in inventory):
        raise ROIAnalysisError("inventory must contain ObjectInventoryItem values")
    keys = tuple(item.content_key for item in inventory)
    if len(set(keys)) != len(keys):
        raise ROIAnalysisError("inventory content keys must be unique")
    return tuple(sorted(inventory, key=lambda item: item.content_key))


def _content_keys(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    """

    Checks a sequence of unique object-content checksums without changing its order.

    Args:
        values: Object identities selected or omitted by a count decision.
        field_name: Selection-field name used in diagnostics.

    Returns:
        Tuple of checked lowercase SHA-256 strings.

    Raises:
        ROIAnalysisError: The input is a single text value, a key is invalid or a key repeats.
    """

    if isinstance(values, (str, bytes)):
        raise ROIAnalysisError(f"{field_name} must be a sequence of content keys")
    keys = tuple(values)
    for key in keys:
        _sha256(key, field_name)
    if len(set(keys)) != len(keys):
        raise ROIAnalysisError(f"{field_name} must contain unique content keys")
    return keys


def _inventory_sha256(items: Sequence[ObjectInventoryItem]) -> str:
    """

    Calculates the identity of an object's complete count population and its geometry/parent assignments.

    Sort by content_key, then encode each content key, structure ID, parent key
    and geometry checksum under object-inventory/v1. Disposition and cap metadata
    are excluded from this calculation so marking a subset active does not change
    the identity of the original population from which it was selected.

    Args:
        items: Complete sequence of object records with checked scientific identities.

    Returns:
        SHA-256 checksum of the sorted object-content description.

    Raises:
        ROIAnalysisError: Records or object identities fail the list checks.
    """

    return _canonical_sha256(
        {
            "items": [
                {
                    "content_key": item.content_key,
                    "geometry_sha256": item.geometry_sha256,
                    "parent_content_key": item.parent_content_key,
                    "structure_id": item.structure_id,
                }
                for item in _canonical_inventory(items)
            ],
            "schema_version": "object-inventory/v1",
        }
    )


def _selection_context(context: SelectionContext) -> tuple[str, str, str]:
    """

    Reads the three checked input identities used to reproduce a secondary-object selection.

    Args:
        context: SelectionContext for the current domain, configuration and result.

    Returns:
        Domain checksum, configuration checksum and result checksum in that order.

    Raises:
        ROIAnalysisError: context has the wrong record type.
    """

    if not isinstance(context, SelectionContext):
        raise ROIAnalysisError("selection context must be a SelectionContext")
    return (
        context.analysis_domain_sha256,
        context.config_sha256,
        context.result_sha256,
    )


def select_capped_secondary(
    items: Sequence[ObjectInventoryItem],
    *,
    maximum: int,
    sampling_seed: int | None,
    context: SelectionContext,
    manual_selection: ManualSelectionLineage | None = None,
) -> OutputViolation:
    """

    Selects exactly the configured maximum from an overflowing final-secondary object population.

    First sort the complete object list and calculate its checksum. All objects
    must represent one structure, and the observed count must exceed maximum.
    Selection then follows the explicitly chosen method.

    With an integer seed, each object receives a SHA-256 rank calculated from
    the domain, configuration, result, seed, structure, parent and geometry.
    Sort by that rank with content_key breaking a tie, then retain the first
    maximum objects. The returned selected/unselected lists are ordered by
    content_key, allowing repeatable comparison even when input rows are shuffled.

    With sampling_seed=None, require a manual record referring to those exact
    inputs and listing exactly maximum unique eligible keys. Its criterion,
    reviewer and rationale are retained with the complete population.

    Args:
        items: Complete eligible object list for one final secondary structure.
        maximum: Positive number of objects to keep, smaller than len(items).
        sampling_seed: Integer for deterministic hash ranking, or None for manual choice.
        context: Exact domain/configuration/result identifiers for this selection.
        manual_selection: Required manual selection record when sampling_seed is None;
            must be absent for a seeded selection.

    Returns:
        CAPPED OutputViolation with active and omitted objects, the full observed
        count, selection record and inclusion fraction maximum/observed_count.
        Omitted objects remain valid_not_selected_by_cap in the complete record.

    Raises:
        ROIAnalysisError: The population does not overflow, structures are mixed,
            identities differ, or a manual selection lacks the required count/order.

    The stored hash-selection probability uses maximum/observed_count. For one
    fixed seed the chosen subset is deterministic. Statistical weighting based on
    that value additionally requires a defensible random-selection design; manual
    selection records its probability as unknown.
    """

    inventory = _canonical_inventory(items)
    maximum = _count(maximum, "maximum", positive=True)
    if len(inventory) <= maximum:
        raise ROIAnalysisError("a capped selection requires an overflowing inventory")
    if not inventory:
        raise ROIAnalysisError("a capped selection requires an inventory")
    structure_ids = {item.structure_id for item in inventory}
    if len(structure_ids) != 1:
        raise ROIAnalysisError("a capped inventory must contain one structure_id")
    domain_sha256, config_sha256, result_sha256 = _selection_context(context)
    structure_id = next(iter(structure_ids))
    inventory_sha256 = _inventory_sha256(inventory)

    if sampling_seed is None:
        if not isinstance(manual_selection, ManualSelectionLineage):
            raise ROIAnalysisError("manual selection lineage is required")
        expected_identity = (
            domain_sha256,
            config_sha256,
            result_sha256,
            inventory_sha256,
            structure_id,
        )
        actual_identity = (
            manual_selection.analysis_domain_sha256,
            manual_selection.config_sha256,
            manual_selection.result_sha256,
            manual_selection.inventory_sha256,
            manual_selection.structure_id,
        )
        if actual_identity != expected_identity:
            raise ROIAnalysisError(
                "manual selection lineage does not match bound inventory context"
            )
        selected_keys = manual_selection.selected_content_keys
        if len(selected_keys) != maximum or len(set(selected_keys)) != maximum:
            raise ROIAnalysisError("manual selection requires exactly maximum unique content keys")
        inventory_keys = {item.content_key for item in inventory}
        if not set(selected_keys) <= inventory_keys:
            raise ROIAnalysisError("manual selection keys must belong to the bound inventory")
        canonical_selected = tuple(
            item.content_key for item in inventory if item.content_key in set(selected_keys)
        )
        if selected_keys != canonical_selected:
            raise ROIAnalysisError("manual selection keys must use canonical inventory order")
        included_set = set(selected_keys)
        lineage: SelectionLineage = manual_selection
        probability_status = InclusionProbabilityStatus.UNKNOWN
        probability = None
    else:
        seed = _integer(sampling_seed, "sampling_seed")
        if manual_selection is not None:
            raise ROIAnalysisError("hash selection cannot also declare manual selection lineage")
        # Rank scientific object content with the recorded seed. Input
        # row order cannot determine which objects enter the capped subset.
        ranked = sorted(
            inventory,
            key=lambda item: (
                _canonical_sha256(
                    {
                        "analysis_domain_sha256": domain_sha256,
                        "config_sha256": config_sha256,
                        "geometry_sha256": item.geometry_sha256,
                        "parent_content_key": item.parent_content_key,
                        "result_sha256": result_sha256,
                        "sampling_seed": seed,
                        "schema_version": "sha256-rank/v1",
                        "structure_id": structure_id,
                    }
                ),
                item.content_key,
            ),
        )
        included_set = {item.content_key for item in ranked[:maximum]}
        selected_keys = tuple(
            item.content_key for item in inventory if item.content_key in included_set
        )
        lineage = DeterministicSelectionLineage(
            analysis_domain_sha256=domain_sha256,
            config_sha256=config_sha256,
            result_sha256=result_sha256,
            inventory_sha256=inventory_sha256,
            structure_id=structure_id,
            algorithm=SelectionAlgorithm.SHA256_RANK_V1,
            sampling_seed=seed,
        )
        probability_status = InclusionProbabilityStatus.KNOWN
        probability = maximum / len(inventory)

    if sampling_seed is None:
        selected_keys = tuple(
            item.content_key for item in inventory if item.content_key in included_set
        )
    not_selected = tuple(
        item.content_key for item in inventory if item.content_key not in included_set
    )
    inclusion_fraction = maximum / len(inventory)
    # Retain the whole observed population, marking which valid objects
    # were selected and which were omitted only by the maximum-count policy.
    disposition_inventory = tuple(
        replace(
            item,
            disposition=(
                InventoryDisposition.ACTIVE_INCLUDED
                if item.content_key in included_set
                else InventoryDisposition.VALID_NOT_SELECTED_BY_CAP
            ),
            observed_count=len(inventory),
            inclusion_fraction=inclusion_fraction,
            inventory_sha256=inventory_sha256,
            inclusion_probability_status=probability_status,
            inclusion_probability=probability,
            selection_lineage=lineage,
        )
        for item in inventory
    )
    count = len(inventory)
    return OutputViolation(
        output_id=structure_id,
        status=OutputStatus.CAPPED,
        observed_count=count,
        minimum=0,
        maximum=maximum,
        inventory_sha256=inventory_sha256,
        inventory=disposition_inventory,
        included_content_keys=selected_keys,
        not_selected_content_keys=not_selected,
        inclusion_fraction=inclusion_fraction,
        inclusion_probability_status=probability_status,
        inclusion_probability=probability,
        selection_lineage=lineage,
    )


def _validate_inputs(
    *,
    domain: AnalysisDomainRef,
    observations: Sequence[StructureCountObservation],
    policies: Mapping[str, StructureCountBounds],
) -> tuple[StructureCountObservation, ...]:
    """

    Checks that the count observations and configured bounds describe exactly the same outputs.

    Args:
        domain: AnalysisDomainRef for this count evaluation.
        observations: StructureCountObservation records with complete object lists.
        policies: Output-ID mapping of StructureCountBounds settings.

    Returns:
        Observation tuple in its supplied order, after checking unique output IDs
        and exact policy coverage.

    Raises:
        ROIAnalysisError: The domain, observation/policy types or output coverage is invalid.
    """

    if not isinstance(domain, AnalysisDomainRef):
        raise ROIAnalysisError("domain must be an AnalysisDomainRef")
    values = tuple(observations)
    if not all(isinstance(item, StructureCountObservation) for item in values):
        raise ROIAnalysisError("observations must contain StructureCountObservation values")
    output_ids = tuple(item.output_id for item in values)
    if len(set(output_ids)) != len(output_ids):
        raise ROIAnalysisError("observation output IDs must be unique")
    if set(output_ids) != set(policies):
        raise ROIAnalysisError("policies must match observation output IDs exactly")
    if not all(isinstance(policy, StructureCountBounds) for policy in policies.values()):
        raise ROIAnalysisError("policies must contain StructureCountBounds values")
    return values


def _ordinary_output(
    observation: StructureCountObservation,
    policy: StructureCountBounds,
    *,
    status: OutputStatus,
    disposition: InventoryDisposition = InventoryDisposition.ACTIVE_INCLUDED,
) -> OutputViolation:
    """

    Builds an uncapped count result with either complete active inclusion or complete domain exclusion.

    Copy each object with the supplied disposition. Active inclusion retains
    every key and fraction/probability 1.0; below-minimum domain exclusion keeps
    no active keys and records 0.0. No cap-selection record is added.

    Args:
        observation: Checked output count and complete object list.
        policy: Configured minimum and optional maximum for that output.
        status: Count status already chosen by the evaluating function.
        disposition: ACTIVE_INCLUDED or AUDIT_EXCLUDED_BELOW_MINIMUM.

    Returns:
        OutputViolation containing the copied objects and consistent whole-list inclusion.
    """

    inventory = tuple(replace(item, disposition=disposition) for item in observation.inventory)
    included = (
        ()
        if disposition is InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM
        else tuple(item.content_key for item in inventory)
    )
    probability = 0.0 if disposition is InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM else 1.0
    return OutputViolation(
        output_id=observation.output_id,
        status=status,
        observed_count=observation.count,
        minimum=policy.minimum,
        maximum=policy.maximum,
        inventory_sha256=observation.inventory_sha256,
        inventory=inventory,
        included_content_keys=included,
        inclusion_fraction=probability,
        inclusion_probability_status=InclusionProbabilityStatus.KNOWN,
        inclusion_probability=probability,
    )


def _minimum_exclusion(
    domain: AnalysisDomainRef,
    observations: tuple[StructureCountObservation, ...],
    policies: Mapping[str, StructureCountBounds],
) -> StructureCountDecision | None:
    """

    Excludes the complete domain when any configured output misses its minimum count.

    Collect the failing output IDs first. If any fail, create an audit-only
    result for every output, including those whose own minima were satisfied.
    This ensures later analysis does not retain selected parts of a domain
    after the domain-level eligibility requirement has failed.

    Args:
        domain: Analysis domain being evaluated.
        observations: Complete checked counts for its configured outputs.
        policies: Minimum/maximum settings for those same output IDs.

    Returns:
        StructureCountDecision with a DomainExclusion, or None when all minima pass.
        Maxima are not evaluated by this helper.
    """

    # Minimum eligibility applies to the domain before any maximum cap.
    failing = tuple(
        item.output_id for item in observations if item.count < policies[item.output_id].minimum
    )
    if not failing:
        return None
    failing_set = set(failing)
    outputs = tuple(
        _ordinary_output(
            item,
            policies[item.output_id],
            status=(
                OutputStatus.BELOW_MINIMUM
                if item.output_id in failing_set
                else OutputStatus.WITHIN_BOUNDS
            ),
            disposition=InventoryDisposition.AUDIT_EXCLUDED_BELOW_MINIMUM,
        )
        for item in observations
    )
    exclusion = DomainExclusion(
        domain=domain,
        status=DomainExclusionStatus.EXCLUDED_BELOW_MINIMUM,
        failing_output_ids=failing,
        reason="one_or_more_outputs_below_configured_minimum",
    )
    return StructureCountDecision(
        domain=domain,
        outputs=outputs,
        domain_exclusion=exclusion,
    )


@dataclass(frozen=True, slots=True)
class PrimaryCountOutput:
    """Count and bounds for one explicit class selection in the committed table.

    The aggregate selects every configured biological class. A class output
    selects exactly its named class; Ignore is never a biological selection.
    Primary decisions keep or exclude the complete domain and carry no subsets.
    """

    output_id: str
    class_ids: tuple[str, ...]
    observed_count: int
    minimum: int
    maximum: int | None
    status: OutputStatus

    def __post_init__(self) -> None:
        _nonblank(self.output_id, "output_id")
        classes = tuple(self.class_ids)
        if not classes or classes != tuple(sorted(set(classes))):
            raise ROIAnalysisError("primary count classes must be unique and canonical")
        for class_id in classes:
            _nonblank(class_id, "primary class ID")
            if class_id in {"Ignore", "primary_classifier"}:
                raise ROIAnalysisError("primary count class is reserved")
        if self.output_id != "primary_classifier" and classes != (self.output_id,):
            raise ROIAnalysisError("primary class output must select its own class")
        object.__setattr__(self, "class_ids", classes)
        _count(self.observed_count, "observed_count")
        _count(self.minimum, "minimum")
        if self.maximum is not None:
            _count(self.maximum, "maximum", positive=True)
            if self.maximum < self.minimum:
                raise ROIAnalysisError("maximum must be greater than or equal to minimum")
        status = _closed_enum(OutputStatus, self.status, "primary count status")
        if status is OutputStatus.CAPPED:
            raise ROIAnalysisError("primary outputs cannot select a capped subset")
        if (status is OutputStatus.BELOW_MINIMUM) != (self.observed_count < self.minimum):
            raise ROIAnalysisError("primary count minimum status differs")
        if status is OutputStatus.PRIMARY_REVIEW_REQUIRED and (
            self.maximum is None or self.observed_count <= self.maximum
        ):
            raise ROIAnalysisError("primary review requires an exceeded finite maximum")
        object.__setattr__(self, "status", status)

    def to_mapping(self) -> dict[str, object]:
        return {
            "output_id": self.output_id, "class_ids": list(self.class_ids),
            "observed_count": self.observed_count, "minimum": self.minimum,
            "maximum": self.maximum, "status": self.status.value,
        }


def _primary_domain_mapping(domain: AnalysisDomainRef) -> dict[str, object]:
    return {
        "analysis_domain_id": domain.analysis_domain_id, "kind": domain.kind.value,
        "slide_id": domain.slide_id, "section_id": domain.section_id,
        "tissue_generation_id": domain.tissue_generation_id,
    }


@dataclass(frozen=True, slots=True)
class PrimaryCountDecision:
    """Compact primary observations bound to one immutable ROI table and domain.

    Every failing minimum excludes the whole domain. Otherwise an exceeded
    maximum requests review while retaining all observed objects. The digest
    describes these observations, independently of the researcher's later action.
    """

    domain: AnalysisDomainRef
    roi_inventory_sha256: str
    outputs: tuple[PrimaryCountOutput, ...]
    domain_exclusion: DomainExclusion | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.domain, AnalysisDomainRef):
            raise ROIAnalysisError("primary decision domain must be typed")
        _sha256(self.roi_inventory_sha256, "roi_inventory_sha256")
        outputs = tuple(self.outputs)
        if not outputs or not all(isinstance(item, PrimaryCountOutput) for item in outputs):
            raise ROIAnalysisError("primary decision requires typed count outputs")
        aggregate, *classes = outputs
        expected_ids = aggregate.class_ids
        if (
            aggregate.output_id != "primary_classifier"
            or tuple(item.output_id for item in classes) != expected_ids
            or aggregate.observed_count != sum(item.observed_count for item in classes)
        ):
            raise ROIAnalysisError("primary aggregate differs from its class counts")
        failing = tuple(item.output_id for item in outputs if item.status is OutputStatus.BELOW_MINIMUM)
        if failing:
            exclusion = self.domain_exclusion
            if (
                not isinstance(exclusion, DomainExclusion) or exclusion.domain != self.domain
                or exclusion.failing_output_ids != failing
                or any(item.status is OutputStatus.PRIMARY_REVIEW_REQUIRED for item in outputs)
            ):
                raise ROIAnalysisError("primary minimum exclusion differs from observed counts")
        elif self.domain_exclusion is not None:
            raise ROIAnalysisError("primary domain exclusion lacks a failed minimum")
        else:
            for item in outputs:
                review = item.maximum is not None and item.observed_count > item.maximum
                if review != (item.status is OutputStatus.PRIMARY_REVIEW_REQUIRED):
                    raise ROIAnalysisError("primary maximum review status differs")
        object.__setattr__(self, "outputs", outputs)

    def to_mapping(self) -> dict[str, object]:
        domain = _primary_domain_mapping(self.domain)
        exclusion = self.domain_exclusion
        unsigned = {
            "schema_version": "primary-count-decision/v1", "domain": domain,
            "roi_inventory_sha256": self.roi_inventory_sha256,
            "outputs": [item.to_mapping() for item in self.outputs],
            "domain_exclusion": None if exclusion is None else {
                "domain": domain, "status": exclusion.status.value,
                "failing_output_ids": list(exclusion.failing_output_ids), "reason": exclusion.reason,
            },
        }
        return {**unsigned, "decision_sha256": _canonical_sha256(unsigned)}

    @classmethod
    def from_mapping(cls, value: object) -> "PrimaryCountDecision":
        """Decode only this role's closed persisted record and verify its digest."""
        def closed(raw: object, keys: set[str], label: str) -> Mapping:
            if not isinstance(raw, Mapping) or set(raw) != keys:
                raise ROIAnalysisError(f"{label} has invalid fields")
            return raw
        raw = closed(value, {"schema_version", "domain", "roi_inventory_sha256", "outputs",
                             "domain_exclusion", "decision_sha256"}, "primary count decision")
        if raw["schema_version"] != "primary-count-decision/v1":
            raise ROIAnalysisError("primary count decision schema differs")
        domain_raw = closed(raw["domain"], {"analysis_domain_id", "kind", "slide_id", "section_id",
                                            "tissue_generation_id"}, "primary count domain")
        domain = AnalysisDomainRef(**{**domain_raw, "kind": _closed_enum(
            AnalysisDomainKind, domain_raw["kind"], "domain kind")})
        rows = raw["outputs"]
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ROIAnalysisError("primary count outputs must be a sequence")
        outputs = []
        for row in rows:
            row = closed(row, {"output_id", "class_ids", "observed_count", "minimum", "maximum",
                               "status"}, "primary count output")
            if isinstance(row["class_ids"], (str, bytes)) or not isinstance(row["class_ids"], Sequence):
                raise ROIAnalysisError("primary class selection must be a sequence")
            outputs.append(PrimaryCountOutput(**row))
        exclusion_raw = raw["domain_exclusion"]
        exclusion = None
        if exclusion_raw is not None:
            exclusion_raw = closed(exclusion_raw, {"domain", "status", "failing_output_ids", "reason"},
                                   "primary domain exclusion")
            if exclusion_raw["domain"] != domain_raw:
                raise ROIAnalysisError("primary exclusion domain differs")
            failing = exclusion_raw["failing_output_ids"]
            if isinstance(failing, (str, bytes)) or not isinstance(failing, Sequence):
                raise ROIAnalysisError("primary failing outputs must be a sequence")
            exclusion = DomainExclusion(domain, exclusion_raw["status"], tuple(failing), exclusion_raw["reason"])
        result = cls(domain, raw["roi_inventory_sha256"], tuple(outputs), exclusion)
        if result.to_mapping() != dict(raw):
            raise ROIAnalysisError("primary count decision digest or canonical content differs")
        return result


def evaluate_primary_counts(
    *,
    domain: AnalysisDomainRef,
    roi_inventory_sha256: str,
    counts_by_class: Mapping[str, int],
    overall_policy: StructureCountBounds,
    class_policies: Mapping[str, StructureCountBounds],
) -> PrimaryCountDecision:
    """Evaluate table-derived primary counts with whole-domain minimum precedence.

    Inputs contain exactly the configured biological classes, including zero
    counts for absent classes. Minima run before maxima; any failed minimum
    suppresses maximum review. The immutable table digest commits the contributing
    objects without repeating identities, geometry or per-object inclusion state.
    Secondary subset selection remains in evaluate_final_secondary_counts.
    """
    if not isinstance(counts_by_class, Mapping) or not isinstance(class_policies, Mapping):
        raise ROIAnalysisError("primary counts and policies must be mappings")
    if set(counts_by_class) != set(class_policies) or not class_policies:
        raise ROIAnalysisError("primary counts must match configured class policies exactly")
    if not isinstance(overall_policy, StructureCountBounds) or not all(
        isinstance(policy, StructureCountBounds) for policy in class_policies.values()
    ):
        raise ROIAnalysisError("primary policies must be StructureCountBounds")
    for class_id, count in counts_by_class.items():
        _nonblank(class_id, "primary class ID")
        _count(count, "primary class count")
    classes = tuple(sorted(class_policies))
    observations = [("primary_classifier", classes, sum(counts_by_class.values()), overall_policy)]
    observations.extend((class_id, (class_id,), counts_by_class[class_id], class_policies[class_id])
                        for class_id in classes)
    failing = tuple(output_id for output_id, _, count, policy in observations if count < policy.minimum)
    outputs = tuple(PrimaryCountOutput(
        output_id, selection, count, policy.minimum, policy.maximum,
        OutputStatus.BELOW_MINIMUM if count < policy.minimum else (
            OutputStatus.PRIMARY_REVIEW_REQUIRED
            if not failing and policy.maximum is not None and count > policy.maximum
            else OutputStatus.WITHIN_BOUNDS),
    ) for output_id, selection, count, policy in observations)
    exclusion = None if not failing else DomainExclusion(
        domain, DomainExclusionStatus.EXCLUDED_BELOW_MINIMUM, failing,
        "one_or_more_outputs_below_configured_minimum",
    )
    return PrimaryCountDecision(domain, roi_inventory_sha256, outputs, exclusion)


def evaluate_final_secondary_counts(
    *,
    domain: AnalysisDomainRef,
    observations: Sequence[StructureCountObservation],
    policies: Mapping[str, StructureCountBounds],
    selection_contexts: Mapping[str, SelectionContext] | None = None,
    manual_selections: Mapping[str, ManualSelectionLineage] | None = None,
) -> StructureCountDecision:
    """

    Applies count bounds and explicit maximum selection to biologically accepted final-secondary objects.

    As with primary counts, any failed minimum first excludes the entire domain.
    If all minima pass, outputs within their bounds retain every object. An
    overflowing final-secondary output uses its configured deterministic hash
    or manual selection, preserving unselected valid objects in the saved result.

    The caller supplies final leaf-secondary populations after their semantic
    criteria have been evaluated. This function does not classify candidates or
    recalculate memberships to decide whether an object is biologically valid.

    Args:
        domain: Analysis domain whose final-secondary outputs are being counted.
        observations: Complete accepted object lists and counts, one per output.
        policies: Count bounds with an explicit excess policy for finite maxima.
        selection_contexts: Per-output domain/configuration/result identities required
            when that output actually exceeds its maximum.
        manual_selections: Optional per-output manual decisions for manual-select caps.

    Returns:
        StructureCountDecision containing excluded, fully included or explicitly
        capped populations and their selection records.

    Raises:
        ROIAnalysisError: Coverage differs, an overflow lacks its required context/
            policy, or selected keys do not match the current eligible population.
    """

    values = _validate_inputs(domain=domain, observations=observations, policies=policies)
    excluded = _minimum_exclusion(domain, values, policies)
    if excluded is not None:
        return excluded
    contexts = {} if selection_contexts is None else selection_contexts
    manual = {} if manual_selections is None else manual_selections
    outputs: list[OutputViolation] = []
    for item in values:
        policy = policies[item.output_id]
        if policy.maximum is None or item.count <= policy.maximum:
            outputs.append(_ordinary_output(item, policy, status=OutputStatus.WITHIN_BOUNDS))
            continue
        if item.output_id not in contexts:
            raise ROIAnalysisError("secondary cap requires a canonical selection context")
        if policy.excess_policy is SecondaryExcessPolicy.DETERMINISTIC_HASH_SAMPLE:
            output = select_capped_secondary(
                item.inventory,
                maximum=policy.maximum,
                sampling_seed=policy.sampling_seed,
                context=contexts[item.output_id],
            )
        elif policy.excess_policy is SecondaryExcessPolicy.MANUAL_SELECT:
            output = select_capped_secondary(
                item.inventory,
                maximum=policy.maximum,
                sampling_seed=None,
                context=contexts[item.output_id],
                manual_selection=manual.get(item.output_id),
            )
        else:
            raise ROIAnalysisError("finite secondary maximum requires an excess policy")
        outputs.append(replace(output, minimum=policy.minimum))
    return StructureCountDecision(domain=domain, outputs=tuple(outputs))


__all__ = [
    "DeterministicSelectionLineage",
    "DomainExclusion",
    "DomainExclusionStatus",
    "InclusionProbabilityStatus",
    "InventoryDisposition",
    "ManualSelectionLineage",
    "ObjectInventoryItem",
    "OutputViolation",
    "PrimaryCountOutput",
    "PrimaryCountDecision",
    "OutputStatus",
    "SelectionAlgorithm",
    "SelectionContext",
    "StructureCountDecision",
    "StructureCountObservation",
    "evaluate_final_secondary_counts",
    "evaluate_primary_counts",
    "select_capped_secondary",
]
