"""

Reads a structure's semantic-validation profile and records the exact parsed criteria.

A profile identifies the structure, a required criterion tree and optional
classification rules. The criterion tree can combine measured counts,
fractions, densities and geometry with explicit manual questions. The parser
checks criterion selectors and IDs before any candidate region is evaluated.

The profile checksum is calculated from the normalized parsed dictionary.
YAML comments, indentation and mapping-key order therefore do not change its
identity, while changing a criterion, threshold, class order or version does.
Nested criteria are copied into read-only containers so an in-memory edit
cannot change a loaded profile after its checksum has been recorded.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

# ==============================================================================
#                                    Profiles
# ==============================================================================


def _freeze_json(value: object) -> object:
    """

    Copies nested profile mappings and sequences into read-only containers.

    Args:
        value: Parsed criterion data containing mappings, lists/tuples and scalar values.

    Returns:
        Copied read-only mappings and tuples with the same field values and sequence order.

    Raises:
        ValueError: A mapping key is non-text or a float is nonfinite. The criterion
            parser checks the permitted field and leaf-value types separately.
    """

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("semantic profile mapping keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("semantic profile numeric values must be finite")
    return value


def _thaw_json(value: object) -> object:
    """

    Converts stored profile mappings and tuple sequences to ordinary JSON-ready containers.

    Args:
        value: Nested profile value to serialize.

    Returns:
        Newly created dictionaries and lists with unchanged scalar values and sequence order.
    """

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """

    Copies a criterion dictionary and requires its root to remain a mapping.

    Args:
        value: Parsed criterion mapping.

    Returns:
        Read-only mapping with recursively copied nested containers.

    Raises:
        TypeError: The root is not a mapping. Invalid nested keys or numbers raise ValueError.
    """

    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - type narrowing invariant
        raise TypeError("semantic criterion root must remain a mapping")
    return frozen


def _mutable_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """

    Makes a detached dictionary representation of stored criterion data.

    Args:
        value: Read-only criterion mapping to export.

    Returns:
        Dictionary containing new nested dictionaries/lists, suitable for JSON or YAML output.

    Raises:
        TypeError: The supplied root cannot be represented as a dictionary.
    """

    thawed = _thaw_json(value)
    if not isinstance(thawed, dict):  # pragma: no cover - type narrowing invariant
        raise TypeError("semantic criterion root must remain a mapping")
    return thawed


def _canonical(value: object) -> str:
    """

    Renders parsed profile contents deterministically for checksum calculation.

    Args:
        value: Profile dictionary or its read-only representation.

    Returns:
        Compact JSON with sorted mapping keys, preserved list order and literal
        Unicode text. Nonfinite numerical values are rejected during serialization.
    """

    return json.dumps(
        _thaw_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class SemanticClassificationRule:
    """

    Pairs a final region class with the criteria that permit assigning that class.

    Attributes:
        class_id: Configured output class identity.
        criteria: Parsed condition tree associated with that class.
    """

    class_id: str
    criteria: Mapping[str, object] | None

    def __post_init__(self) -> None:
        """

        Copies the rule's criteria into read-only nested containers.

        The containing profile checks its class ID and criterion syntax. This
        method prevents later dictionary edits from altering the supplied rule.
        """

        object.__setattr__(self, "criteria", None if self.criteria is None else _freeze_mapping(self.criteria))

    def to_mapping(self) -> dict[str, object]:
        """

        Exports the classification rule using a detached criterion dictionary.

        Returns:
            Mapping containing class_id and criteria for profile serialization.
        """

        return {
            "class_id": self.class_id,
            "criteria": None if self.criteria is None else _mutable_mapping(self.criteria),
        }


@dataclass(frozen=True, slots=True)
class SemanticProfileV3:
    """

    Stores a validated semantic profile with its normalized content checksum.

    Attributes:
        profile_id: Researcher-supplied profile name.
        profile_version: Explicit version label used with that name.
        structure_id: Configured structure evaluated by this profile.
        criteria: Selected measurements and optional comparisons; may be absent in v4.
        content_sha256: Checksum of the parsed profile, including optional class rules.
        classification_rules: Declared subtypes and their optional measurement selections.
    """

    profile_id: str
    profile_version: str
    structure_id: str
    criteria: Mapping[str, object] | None
    content_sha256: str = field(init=False)
    classification_rules: tuple[SemanticClassificationRule, ...] = ()
    schema_version: str = "semantic-profile/v3"

    def __post_init__(self) -> None:
        """

        Validates and freezes the profile's scientific meaning and derives its checksum.

        Main and class criteria share one ID set. The normalized criteria retain
        registered quantities, selectors and comparison rules before their identity
        is calculated. Every constructed profile therefore has the same checked,
        immutable representation, including profiles created directly in Python.
        """

        for name in ("profile_id", "profile_version", "structure_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        from .criteria import parse_criterion

        seen: set[str] = set()
        if self.schema_version not in {"semantic-profile/v3", "semantic-profile/v4"}:
            raise ValueError("semantic profile schema is invalid")
        def parse_conditions(value):
            if value is None and self.schema_version == "semantic-profile/v4":
                return None
            parsed = parse_criterion(value, seen, allow_observations=self.schema_version == "semantic-profile/v4")
            def check(node):
                if node.kind == "manual_region_decision":
                    raise ValueError("manual questions are not supported by semantic-profile/v4")
                for child in node.children:
                    check(child)
            if self.schema_version == "semantic-profile/v4":
                check(parsed)
            return parsed
        criterion = parse_conditions(self.criteria)
        class_ids: set[str] = set()
        rules: list[SemanticClassificationRule] = []
        for rule in self.classification_rules:
            if not isinstance(rule, SemanticClassificationRule):
                raise ValueError("classification rules must be typed semantic rules")
            if not isinstance(rule.class_id, str) or not rule.class_id.strip():
                raise ValueError("classification rule class_id must be a nonblank string")
            if rule.class_id in class_ids:
                raise ValueError("classification rule class_id values must be unique")
            class_ids.add(rule.class_id)
            class_criterion = parse_conditions(rule.criteria)
            rules.append(SemanticClassificationRule(rule.class_id, None if class_criterion is None else class_criterion.to_mapping()))
        object.__setattr__(self, "criteria", None if criterion is None else _freeze_mapping(criterion.to_mapping()))
        object.__setattr__(self, "classification_rules", tuple(rules))
        object.__setattr__(
            self, "content_sha256", sha256(_canonical(self.to_mapping()).encode()).hexdigest()
        )

    @property
    def identifier(self) -> str:
        """

        Combines the profile name and version for display and reference lookup.

        Returns:
            Identifier text in the form "profile_id@profile_version". The separate
            content_sha256 identifies the exact parsed criteria.
        """

        return f"{self.profile_id}@{self.profile_version}"

    @classmethod
    def from_mapping(cls, raw: object) -> "SemanticProfileV3":
        """

        Parses the required criterion tree and optional class rules into one checked profile.

        Require a supported profile schema, a profile name/version, a
        structure ID and criteria. Parse the required tree first, then each class
        rule using the same set of previously seen criterion IDs. A criterion ID
        must therefore be unique across the whole profile, not merely one branch.
        Class IDs must also be unique among the additional classification rules.

        The constructor normalizes the parsed criteria and derives their SHA-256
        checksum after this decoder checks the closed external field sets. Implicit
        defaults such as required=True are normalized by the criterion writer.
        Ordered class rules remain in their supplied order.

        Args:
            raw: Mapping decoded from a semantic-profile YAML document or equivalent
                structured input, with only the declared profile fields.

        Returns:
            SemanticProfileV3 with parsed criteria, ordered class rules and the checksum
            used to associate candidate results and manual decisions with this profile.

        Raises:
            ValueError: Format, fields, IDs, criteria, selectors or class rules are invalid.
        """

        if not isinstance(raw, Mapping):
            raise ValueError("semantic profile must be a mapping")
        required = {"schema_version", "profile_id", "profile_version", "structure_id", "criteria"}
        allowed = required | {"classification_rules"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown semantic profile fields: {", ".join(sorted(unknown))}")
        if not required <= set(raw):
            raise ValueError("semantic profile requires every field")
        if raw["schema_version"] not in {"semantic-profile/v3", "semantic-profile/v4"}:
            raise ValueError("semantic profile schema_version must equal 'semantic-profile/v3' or 'semantic-profile/v4'")
        raw_rules = raw.get("classification_rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("classification_rules must be an array")
        rules: list[SemanticClassificationRule] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, Mapping):
                raise ValueError("classification rule must be a mapping")
            unknown_rule_fields = set(raw_rule) - {"class_id", "criteria"}
            if unknown_rule_fields:
                raise ValueError(
                    "unknown semantic classification rule fields: "
                    + ", ".join(sorted(unknown_rule_fields))
                )
            if set(raw_rule) != {"class_id", "criteria"}:
                raise ValueError("classification rule requires class_id and criteria")
            if not isinstance(raw_rule["criteria"], Mapping) and not (raw["schema_version"] == "semantic-profile/v4" and raw_rule["criteria"] is None):
                raise ValueError("criterion must be a mapping")
            rules.append(SemanticClassificationRule(raw_rule["class_id"], raw_rule["criteria"]))
        return cls(
            raw["profile_id"],
            raw["profile_version"],
            raw["structure_id"],
            raw["criteria"],
            tuple(rules),
            raw["schema_version"],
        )

    def to_mapping(self) -> dict[str, object]:
        """

        Exports the normalized semantic profile with detached criteria and ordered class rules.

        Returns:
            Dictionary containing the exact profile schema fields. Empty optional
            classification_rules are omitted; content_sha256 is derived from this
            content rather than embedded in it.
        """

        result = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "structure_id": self.structure_id,
            "criteria": None if self.criteria is None else _mutable_mapping(self.criteria),
        }
        if self.classification_rules:
            result["classification_rules"] = [
                rule.to_mapping() for rule in self.classification_rules
            ]
        return result


def load_semantic_profile_v3(path: str | Path) -> SemanticProfileV3:
    """

    Reads a UTF-8 YAML profile, then validates its criteria through the shared parser.

    yaml.safe_load() constructs the ordinary mapping/list/scalar values. The
    profile parser then restricts those values to the supported semantic format,
    preventing an unrecognized condition from reaching candidate evaluation.

    Args:
        path: Local YAML filename containing one supported semantic profile.

    Returns:
        SemanticProfileV3 with the parsed content and its deterministic checksum.

    Raises:
        ValueError: The file cannot be read, YAML parsing fails, or profile validation fails.
    """

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load semantic profile: {exc}") from exc
    return SemanticProfileV3.from_mapping(raw)
