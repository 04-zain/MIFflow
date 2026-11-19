"""

Records the measurements used to evaluate a candidate region and any associated manual decisions.

SemanticEvidenceSnapshot joins the candidate's image, tissue section, analysis
domain and structure with its measured values. Separate checksums identify the
candidate boundary and contributing cell membership. The complete record has
its own checksum, so changed measurements can be distinguished from unchanged
region geometry.

A manual decision copies these identities together with the profile, criterion,
reviewer and current review checkpoint. The criterion evaluator applies a
saved decision only when those inputs still match the candidate being evaluated.
This prevents a decision about an earlier boundary or cell population from
silently carrying over after the region changes.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

# ==============================================================================
#                                    Evidence
# ==============================================================================


def _string(value: object, field: str) -> str:
    """

    Checks that a required identifier or description contains nonblank text.

    Args:
        value: Candidate text value.
        field: Field name used in the error message.

    Returns:
        The supplied string, preserving its capitalization and spacing.

    Raises:
        ValueError: The value is not a string or contains only whitespace.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _digest(value: object, field: str) -> str:
    """

    Checks the lowercase hexadecimal representation of a recorded SHA-256 checksum.

    Args:
        value: Expected 64-character checksum text.
        field: Checksum field name included in an error.

    Returns:
        The checked checksum string. The referenced measurements or geometry are
        compared by their consuming workflow.

    Raises:
        ValueError: The value is blank, has the wrong length or contains a nonhexadecimal character.
    """

    value = _string(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _freeze_json_value(value: Any) -> Any:
    """

    Copies nested measurement values into read-only mappings and tuples.

    Visit mapping values and sequence elements recursively. Mapping keys must be
    strings, while leaf values may be text, integers, finite floats, booleans or
    None. This prevents a caller's later list or dictionary edit from changing a
    recorded measurement or its checksum.

    Args:
        value: JSON-compatible scalar, mapping, list or tuple.

    Returns:
        Equivalent nested values with copied read-only mappings and tuple sequences.

    Raises:
        ValueError: A key is non-text, a number is nonfinite, or an unsupported object
            occurs in the measurement values.
    """

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("evidence value mapping keys must be strings")
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json_value(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("evidence numeric values must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("evidence values must be JSON values")


def _thaw_json_value(value: Any) -> Any:
    """

    Builds ordinary dictionaries and lists from the stored read-only measurement values.

    Args:
        value: Nested scalar, mapping or sequence from a checked evidence record.

    Returns:
        JSON-ready values in newly created dictionaries and lists, with sequence order preserved.
    """

    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json_value(item) for item in value]
    return value


def _canonical_digest(value: Mapping[str, Any]) -> str:
    """

    Calculates a checksum from a measurement or decision record's complete dictionary.

    Sort dictionary keys and remove incidental JSON whitespace before encoding.
    Reordering dictionary insertion therefore leaves the checksum unchanged,
    whereas changing a stored value or sequence order changes it.

    Args:
        value: JSON-ready record with finite numerical values.

    Returns:
        SHA-256 checksum of the deterministically encoded JSON text.
    """

    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticEvidenceSnapshot:
    """

    Stores the exact candidate measurements supplied to its semantic criteria.

    Attributes:
        slide_id: Image identity containing the candidate region.
        section_id: Tissue-section identity within that image.
        analysis_domain_id: Selected analysis domain containing the candidate.
        candidate_roi_id: Candidate-region ID used by the semantic and finalization steps.
        structure_id: Configured biological structure being evaluated.
        geometry_digest: Checksum identifying the candidate's recorded polygon geometry.
        membership_digest: Checksum identifying the recorded contributing cell membership.
        values: Named numerical measurements, with optional selectors encoded in their keys.
            None retains an unavailable measurement; it is distinct from a measured zero.
        producer_version: Version of the calculation that assembled these measurements.
    """

    slide_id: str
    section_id: str
    analysis_domain_id: str
    candidate_roi_id: str
    structure_id: str
    geometry_digest: str
    membership_digest: str
    values: Mapping[str, object]
    producer_version: str = "semantic-evidence/v1"

    def __post_init__(self) -> None:
        """

        Checks the candidate identifiers and checksums, then copies its measurements into read-only containers.

        Numerical criteria may require a particular value to be a finite number. This
        record also supports text, booleans and None for descriptive or unavailable
        inputs; the relevant criterion checks its own expected measurement type.

        Raises:
            ValueError: An identity, checksum, measurement key or nested value is invalid.
        """

        for name in (
            "slide_id",
            "section_id",
            "analysis_domain_id",
            "candidate_roi_id",
            "structure_id",
            "producer_version",
        ):
            object.__setattr__(self, name, _string(getattr(self, name), name))
        object.__setattr__(
            self, "geometry_digest", _digest(self.geometry_digest, "geometry_digest")
        )
        object.__setattr__(
            self, "membership_digest", _digest(self.membership_digest, "membership_digest")
        )
        if not isinstance(self.values, Mapping):
            raise ValueError("values must be a mapping")
        # Copy nested containers so later caller edits cannot change the
        # measurements or the checksum on which a manual decision relies.
        object.__setattr__(self, "values", _freeze_json_value(self.values))

    @property
    def digest(self) -> str:
        """

        Calculates the checksum of the full candidate evidence record.

        The checksum includes geometry and membership identifiers, measured values,
        image/domain/candidate identities and the producer version. Any change in
        these recorded inputs therefore invalidates an earlier manual decision.

        Returns:
            Lowercase SHA-256 checksum of to_mapping().
        """

        return _canonical_digest(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        """

        Builds the dictionary saved for this candidate's measured evidence.

        Returns:
            Candidate identifiers, geometry/membership checksums, producer version
            and detached measurement values under the semantic-evidence/v1 format.
        """

        return {
            "schema_version": "semantic-evidence/v1",
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "analysis_domain_id": self.analysis_domain_id,
            "candidate_roi_id": self.candidate_roi_id,
            "structure_id": self.structure_id,
            "geometry_digest": self.geometry_digest,
            "membership_digest": self.membership_digest,
            "producer_version": self.producer_version,
            "values": _thaw_json_value(self.values),
        }

    def manual_decision(
        self,
        profile_id: str,
        profile_version: str,
        profile_content_sha256: str,
        criterion_id: str,
        checkpoint_id: str,
        reviewer_id: str,
        action: str,
        rationale: str | None = None,
    ) -> "SemanticRegionDecision":
        """

        Creates a reviewer decision tied to this exact candidate and measured evidence.

        Copy the image/domain/candidate IDs and geometry checksum from this record,
        and calculate its complete evidence checksum. Combine them with the selected
        profile content, criterion and review checkpoint so the evaluator can later
        determine whether the decision still applies.

        Args:
            profile_id: Semantic profile name used during review.
            profile_version: Explicit profile version.
            profile_content_sha256: Checksum of the parsed profile contents.
            criterion_id: Manual criterion answered by the reviewer.
            checkpoint_id: Current workflow review checkpoint.
            reviewer_id: Recorded reviewer identity.
            action: "accept" or "reject" for this criterion.
            rationale: Optional nonblank explanation of the decision.

        Returns:
            SemanticRegionDecision with this record's candidate and evidence identities.

        Raises:
            ValueError: A required identifier/checksum, action or supplied rationale is invalid.
        """

        # Bind the answer to the complete current evidence, including its
        # measured values, as well as the selected profile and criterion.
        return SemanticRegionDecision(
            self.slide_id,
            self.section_id,
            self.analysis_domain_id,
            self.candidate_roi_id,
            _string(profile_id, "profile_id"),
            _string(profile_version, "profile_version"),
            _digest(profile_content_sha256, "profile_content_sha256"),
            _string(criterion_id, "criterion_id"),
            self.geometry_digest,
            self.digest,
            _string(checkpoint_id, "checkpoint_id"),
            _string(reviewer_id, "reviewer_id"),
            action,
            rationale,
        )


    def region_decision(self, profile, checkpoint_id, reviewer_id, action, *,
                        subtype_status=None, semantic_class_id=None, rationale=None):
        """Bind one explicit region decision to its complete measured evidence."""
        if profile.schema_version != "semantic-profile/v4" or profile.structure_id != self.structure_id:
            raise ValueError("region review requires the exact current structure profile")
        classes = {rule.class_id for rule in profile.classification_rules}
        if action == "reject":
            subtype_status, semantic_class_id = "not_evaluated", None
        elif not classes:
            if semantic_class_id is not None or subtype_status not in {None, "not_configured"}:
                raise ValueError("this structure has no configured subtypes")
            subtype_status = "not_configured"
        elif subtype_status == "assigned":
            if semantic_class_id not in classes:
                raise ValueError("choose one declared subtype")
        elif subtype_status != "cannot_assess" or semantic_class_id is not None:
            raise ValueError("choose a subtype or Cannot assess")
        return SemanticRegionDecision(
            self.slide_id, self.section_id, self.analysis_domain_id, self.candidate_roi_id,
            profile.profile_id, profile.profile_version, profile.content_sha256, None,
            self.geometry_digest, self.digest, checkpoint_id, reviewer_id, action, rationale,
            "semantic-region-decision/v2", subtype_status, semantic_class_id,
        )


@dataclass(frozen=True, slots=True)
class SemanticRegionDecision:
    """

    Stores a manual criterion decision and the candidate facts it was based on.

    Attributes:
        slide_id: Image containing the reviewed candidate.
        section_id: Tissue section containing the candidate.
        analysis_domain_id: Selected domain of that section.
        candidate_roi_id: Reviewed candidate-region identity.
        profile_id: Semantic profile name.
        profile_version: Selected profile version.
        profile_content_sha256: Exact parsed profile contents used in this review.
        criterion_id: Manual condition answered by the reviewer.
        geometry_digest: Reviewed candidate boundary checksum.
        evidence_digest: Checksum of its complete measured evidence record.
        checkpoint_id: Workflow checkpoint at which the decision was supplied.
        reviewer_id: Person supplying the decision.
        action: "accept" or "reject".
        rationale: Optional explanation saved alongside the decision.
    """

    slide_id: str
    section_id: str
    analysis_domain_id: str
    candidate_roi_id: str
    profile_id: str
    profile_version: str
    profile_content_sha256: str
    criterion_id: str | None
    geometry_digest: str
    evidence_digest: str
    checkpoint_id: str
    reviewer_id: str
    action: str
    rationale: str | None = None
    schema_version: str = "semantic-region-decision/v1"
    subtype_status: str | None = None
    semantic_class_id: str | None = None

    def __post_init__(self) -> None:
        """

        Checks that the manual decision identifies its candidate, profile and review context completely.

        Required identities must be nonblank, the three content identifiers must have
        valid SHA-256 text, and the action must be accept or reject. A supplied
        rationale must contain text. Matching these values to current analysis
        inputs is performed when the decision is evaluated.

        Raises:
            ValueError: A required field, checksum, action or supplied rationale is invalid.
        """

        for name in (
            "slide_id",
            "section_id",
            "analysis_domain_id",
            "candidate_roi_id",
            "profile_id",
            "profile_version",
            "checkpoint_id",
            "reviewer_id",
        ):
            object.__setattr__(self, name, _string(getattr(self, name), name))
        object.__setattr__(
            self, "geometry_digest", _digest(self.geometry_digest, "geometry_digest")
        )
        object.__setattr__(
            self, "evidence_digest", _digest(self.evidence_digest, "evidence_digest")
        )
        object.__setattr__(
            self,
            "profile_content_sha256",
            _digest(self.profile_content_sha256, "profile_content_sha256"),
        )
        if self.schema_version == "semantic-region-decision/v1":
            _string(self.criterion_id, "criterion_id")
            if self.subtype_status is not None or self.semantic_class_id is not None:
                raise ValueError("criterion decisions cannot assign subtypes")
        elif self.schema_version == "semantic-region-decision/v2":
            if self.criterion_id is not None:
                raise ValueError("region decisions do not answer a criterion")
            if self.subtype_status not in {"assigned", "cannot_assess", "not_configured", "not_evaluated"}:
                raise ValueError("region subtype status is invalid")
            if (self.subtype_status == "assigned") != (self.semantic_class_id is not None):
                raise ValueError("region subtype identity is inconsistent")
            if self.semantic_class_id is not None:
                _string(self.semantic_class_id, "semantic_class_id")
            if (self.action == "reject") != (self.subtype_status == "not_evaluated"):
                raise ValueError("rejected regions must clear subtype assignment")
        else:
            raise ValueError("semantic region decision schema is invalid")
        if self.action not in {"accept", "reject"}:
            raise ValueError("manual decision action must be accept or reject")
        if self.rationale is not None:
            object.__setattr__(self, "rationale", _string(self.rationale, "rationale"))

    def matches_region(self, profile, evidence):
        """Return whether this current decision still names these exact facts."""
        return self.schema_version == "semantic-region-decision/v2" and (
            self.slide_id, self.section_id, self.analysis_domain_id, self.candidate_roi_id,
            self.profile_id, self.profile_version, self.profile_content_sha256,
            self.geometry_digest, self.evidence_digest,
        ) == (
            evidence.slide_id, evidence.section_id, evidence.analysis_domain_id, evidence.candidate_roi_id,
            profile.profile_id, profile.profile_version, profile.content_sha256,
            evidence.geometry_digest, evidence.digest,
        )

    def to_mapping(self) -> dict[str, Any]:
        """

        Writes the manual decision fields into the semantic-region-decision/v1 dictionary.

        Returns:
            All candidate, profile, evidence and reviewer fields, including rationale=None
            when no explanation was supplied.
        """

        result = {
            "schema_version": self.schema_version,
            "slide_id": self.slide_id,
            "section_id": self.section_id,
            "analysis_domain_id": self.analysis_domain_id,
            "candidate_roi_id": self.candidate_roi_id,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_content_sha256": self.profile_content_sha256,
            "criterion_id": self.criterion_id,
            "geometry_digest": self.geometry_digest,
            "evidence_digest": self.evidence_digest,
            "checkpoint_id": self.checkpoint_id,
            "reviewer_id": self.reviewer_id,
            "action": self.action,
            "rationale": self.rationale,
        }
        if self.schema_version == "semantic-region-decision/v2":
            result.pop("criterion_id")
            result.update(subtype_status=self.subtype_status, semantic_class_id=self.semantic_class_id)
        return result

    @classmethod
    def from_mapping(cls, raw: object) -> "SemanticRegionDecision":
        """

        Reads a saved manual decision and checks its complete field set before construction.

        Require the semantic-region-decision/v1 format and every declared field,
        including a rationale key whose value may be None. The constructor then
        checks identities, checksum text and the accept/reject action.

        Args:
            raw: Mapping decoded from a saved manual-decision record.

        Returns:
            Checked SemanticRegionDecision ready to be matched to its candidate and profile.

        Raises:
            ValueError: The format, field set or any required field value is invalid.
        """

        if not isinstance(raw, Mapping):
            raise ValueError("semantic region decision must be a mapping")
        required = {
            "schema_version",
            "slide_id",
            "section_id",
            "analysis_domain_id",
            "candidate_roi_id",
            "profile_id",
            "profile_version",
            "profile_content_sha256",
            "criterion_id",
            "geometry_digest",
            "evidence_digest",
            "checkpoint_id",
            "reviewer_id",
            "action",
            "rationale",
        }
        version = raw.get("schema_version")
        if version == "semantic-region-decision/v2":
            required = required - {"criterion_id"} | {"subtype_status", "semantic_class_id"}
        if set(raw) != required or version not in {"semantic-region-decision/v1", "semantic-region-decision/v2"}:
            raise ValueError("semantic region decision schema is invalid")
        return cls(
            slide_id=raw["slide_id"],
            section_id=raw["section_id"],
            analysis_domain_id=raw["analysis_domain_id"],
            candidate_roi_id=raw["candidate_roi_id"],
            profile_id=raw["profile_id"],
            profile_version=raw["profile_version"],
            profile_content_sha256=raw["profile_content_sha256"],
            criterion_id=raw.get("criterion_id"),
            geometry_digest=raw["geometry_digest"],
            evidence_digest=raw["evidence_digest"],
            checkpoint_id=raw["checkpoint_id"],
            reviewer_id=raw["reviewer_id"],
            action=raw["action"],
            rationale=raw["rationale"],
            schema_version=version,
            subtype_status=raw.get("subtype_status"),
            semantic_class_id=raw.get("semantic_class_id"),
        )
