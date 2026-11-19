"""

Records the measured cell features selected for phenotyping and clustering.

QuPath returns the exact measurement name with its channel, biomarker,
cellular compartment, units and producing program. We check those fields
against the reviewed image channels, collect the available features, then
retain the selected feature IDs in their requested order. The selected
measurement names determine which values are read from each cell.

We keep feature selection and clustering preprocessing as successive steps.
After a configured clustering feature has matched the selected measurement,
its transformation and numerical weight are added to the FeatureSpec used
by clustering. This preserves the same channel/compartment meaning while
allowing the analysis to choose how that measurement enters its distance
calculation.

The actual intensity calculations run in QuPath's cell-measurement script.
This module checks their descriptions and selected order; it does not open
image pixels or recalculate a compartment statistic.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from types import MappingProxyType

from mif_pipeline.cell_analysis.clustering import (
    CellClusteringError,
    FeatureRole,
    FeatureSpec,
)
from mif_pipeline.pipeline_core.errors import ROIAnalysisError

# ==============================================================================
#                                Measurement plan
# ==============================================================================


class CellFeatureError(ROIAnalysisError):
    """

    Reports an incomplete or inconsistent cell-measurement selection.
    """

class FeatureKind(str, Enum):
    """

    Records which image values or cell properties produced a selected feature.

    We use EXPRESSION for a marker-channel intensity, DAPI for nuclear-stain
    intensity and AUTOFLUORESCENCE for measured AF intensity. MORPHOLOGY and
    NUCLEAR_MORPHOLOGY identify cell-shape and nuclear-shape measurements.
    CONTEXTUAL identifies a value calculated from surrounding tissue or objects.

    These kinds are retained with the measurement name and inclusion choice
    when we build a cell-feature matrix. The corresponding FeatureRole supplies
    the downstream selection rules.
    """

    EXPRESSION = "expression"
    MORPHOLOGY = "morphology"
    NUCLEAR_MORPHOLOGY = "nuclear_morphology"
    CONTEXTUAL = "contextual"
    DAPI = "dapi"
    AUTOFLUORESCENCE = "autofluorescence"


_ROLE_BY_KIND = {
    FeatureKind.EXPRESSION: FeatureRole.EXPRESSION,
    FeatureKind.MORPHOLOGY: FeatureRole.MORPHOLOGY,
    FeatureKind.NUCLEAR_MORPHOLOGY: FeatureRole.MORPHOLOGY,
    FeatureKind.CONTEXTUAL: FeatureRole.CONTEXTUAL,
    FeatureKind.DAPI: FeatureRole.DAPI,
    FeatureKind.AUTOFLUORESCENCE: FeatureRole.AUTOFLUORESCENCE,
}
_CHANNEL_KINDS = frozenset(
    {
        FeatureKind.EXPRESSION,
        FeatureKind.DAPI,
        FeatureKind.AUTOFLUORESCENCE,
    }
)


def shape_measurement_choices() -> tuple[tuple[str, str, dict[str, object]], ...]:
    """Describe the existing calibrated QuPath shape values offered for classification.

    Segmentation calculates the standard shape measurements from finalized outlines.
    The measurement stage derives the optional diameter ratios and nuclear-centre
    distance from those saved measurements and outlines, without reading pixels.
    We retain QuPath's exact stored names, including Length for polygon perimeter,
    and give the form readable labels. Nuclear measurements require a nucleus;
    the measurement stage reports unavailable values through its existing review.
    """
    choices = []
    for key, stored, label, units in (
        ("area", "Area µm^2", "area", "µm²"),
        ("perimeter", "Length µm", "perimeter", "µm"),
        ("circularity", "Circularity", "circularity", None),
        ("solidity", "Solidity", "solidity", None),
        ("max_diameter", "Max diameter µm", "maximum diameter", "µm"),
        ("min_diameter", "Min diameter µm", "minimum diameter", "µm"),
    ):
        for compartment, kind in (("cell", "morphology"), ("nucleus", "nuclear_morphology")):
            feature_id = f"shape-{compartment}-{key}"
            choices.append((feature_id, f"{compartment.capitalize()} {label}" + (f" ({units})" if units else ""), {
                "feature_id": feature_id,
                "measurement_name": f"{compartment.capitalize()}: {stored}",
                "kind": kind, "channel_id": None, "biomarker": None,
                "compartment": compartment, "units": units, "opt_in": True,
            }))
    for compartment in ("cell", "nucleus"):
        key = f"shape-{compartment}-diameter-ratio"
        choices.append((key, f"{compartment.capitalize()} maximum-to-minimum diameter ratio", {
            "feature_id": key, "measurement_name": f"{compartment.capitalize()}: Diameter ratio",
            "kind": "morphology" if compartment == "cell" else "nuclear_morphology",
            "channel_id": None, "biomarker": None, "compartment": compartment,
            "units": None, "opt_in": True,
        }))
    choices.append(("shape-nucleus-cell-ratio", "Nucleus-to-cell area ratio", {
        "feature_id": "shape-nucleus-cell-ratio", "measurement_name": "Nucleus/Cell area ratio",
        "kind": "nuclear_morphology", "channel_id": None, "biomarker": None,
        "compartment": "nucleus", "units": None, "opt_in": True,
    }))
    choices.append(("shape-nuclear-offset", "Distance between nuclear and cell centres (µm)", {
        "feature_id": "shape-nuclear-offset", "measurement_name": "Nucleus: Centroid offset µm",
        "kind": "nuclear_morphology", "channel_id": None, "biomarker": None,
        "compartment": "nucleus", "units": "µm", "opt_in": True,
    }))
    return tuple(choices)


# ==============================================================================
#                               Measurement setup
# ==============================================================================

@dataclass(frozen=True, slots=True)
class MeasurementPlan:
    """

    Retains the ordered measurements selected from the available QuPath cell features.

    The order defines the association between selected feature IDs and their
    exact measurement names. We use both when constructing numerical cell
    arrays and when comparing a saved feature selection with current measurements.

    Attributes:
        features: Nonempty tuple of FeatureSpec records. Each supplies a unique
            feature ID, exact QuPath measurement name and its channel/compartment
            description. Clustering transformations and weights are supplied
            separately when that plan is used for clustering.
    """

    features: tuple[FeatureSpec, ...]

    def __post_init__(self) -> None:
        """

        Checks that the measurement selection is nonempty and identifies each selected column uniquely.

        We collect the feature IDs and exact measurement names from the supplied
        records. Every ID must be present, and neither list may contain duplicates.
        Individual feature contents are checked when their FeatureSpecs are read.

        Raises:
            CellFeatureError: The selection is empty, an ID is missing or repeated,
                or two selected features name the same measurement.
        """

        if not self.features:
            raise CellFeatureError("measurement plan must select at least one feature")
        ids = tuple(item.feature_id for item in self.features)
        names = tuple(item.measurement for item in self.features)
        if any(item is None for item in ids) or len(set(ids)) != len(ids):
            raise CellFeatureError("measurement plan feature IDs must be unique")
        if len(set(names)) != len(names):
            raise CellFeatureError("measurement plan exact measurement names must be unique")

    @property
    def selected_feature_ids(self) -> tuple[str, ...]:
        """

        Returns the feature IDs in the selected measurement order.

        Returns:
            Tuple of selected nonmissing feature IDs.
        """

        return tuple(item.feature_id for item in self.features if item.feature_id is not None)

    @property
    def exact_measurement_names(self) -> tuple[str, ...]:
        """

        Returns the QuPath measurement names in the same order as the selected feature IDs.

        Returns:
            Tuple of complete names used to retrieve values from the cells.
        """

        return tuple(item.measurement for item in self.features)

    @property
    def unsupervised_feature_ids(self) -> tuple[str, ...]:
        """

        Selects the protein-intensity feature IDs used as the initial unsupervised input.

        We retain entries with kind="expression" and opt_in=False. DAPI, AF, shape
        and contextual measurements remain available in the complete plan for
        explicit selection through their supported analysis settings.

        Returns:
            Tuple of selected expression-feature IDs in plan order.
        """

        return tuple(
            item.feature_id
            for item in self.features
            if item.feature_id is not None
            and item.kind == FeatureKind.EXPRESSION.value
            and not item.opt_in
        )

    def to_mapping(self) -> dict[str, object]:
        """

        Encodes the selected measurement descriptions in their saved order.

        Each saved entry contains its ID, exact name, kind, channel, biomarker,
        compartment, units, producer/version, missing/nonfinite rates and inclusion
        setting. A clustering operation stores its selected transformations and
        weights with its own configuration.

        Returns:
            Current qupath-measurement-plan/v1 dictionary containing the selected
            feature descriptions.
        """

        return {
            "schema_version": "qupath-measurement-plan/v1",
            "features": [_feature_mapping(item) for item in self.features],
        }

    def bind_clustering_features(
        self, configured_features: Sequence[object]
    ) -> tuple[FeatureSpec, ...]:
        """

        Adds the selected clustering transformation and weight to each matched cell measurement.

        We look up each requested feature ID in the measurement plan and compare
        its exact source name, kind, channel and biomarker. Any supplied compartment,
        unit, producer/version or inclusion value must also agree. These comparisons
        establish the numerical column that a clustering setting applies to.

        We then copy the matched FeatureSpec with its requested transformation and
        weight. An omitted transformation or weight retains that feature's current
        setting. FeatureSpec checks the resulting preprocessing choice before it
        enters the clustering calculation.

        Args:
            configured_features: Ordered clustering mappings with feature_id,
                source measurement name, kind, channel_id, biomarker and optional
                transformation, weight and descriptive fields.

        Returns:
            FeatureSpecs in the requested clustering order, with checked
            measurement identities and their selected preprocessing settings.

        Raises:
            CellFeatureError: IDs are absent or repeated, the requested measurement
                differs from the plan, or a transformation/weight fails validation.
        """

        configured = tuple(
            _mapping(item, f"clustering feature_specs[{index}]")
            for index, item in enumerate(configured_features)
        )
        configured_ids = tuple(item.get("feature_id") for item in configured)
        if (
            not configured_ids
            or any(not isinstance(item, str) or not item.strip() for item in configured_ids)
            or len(set(configured_ids)) != len(configured_ids)
        ):
            raise CellFeatureError("clustering feature IDs must be non-empty and unique")
        owners = {item.feature_id: item for item in self.features}
        missing = tuple(item for item in configured_ids if item not in owners)
        if missing:
            raise CellFeatureError(
                f"clustering feature ID {missing[0]!r} is not present in the "
                "owner measurement plan"
            )
        bound: list[FeatureSpec] = []
        # Match the saved measurement description before applying a
        # selected transformation or numerical weight to its values.
        for index, raw in enumerate(configured):
            owner = owners[configured_ids[index]]
            expected = {
                "feature_id": owner.feature_id,
                "source": owner.measurement,
                "kind": owner.kind,
                "channel_id": owner.channel_id,
                "biomarker": owner.biomarker,
            }
            for field, value in expected.items():
                if field not in raw or raw[field] != value:
                    raise CellFeatureError(
                        f"clustering feature_specs[{index}].{field} does not match "
                        "the owner measurement plan"
                    )
            for field in (
                "compartment",
                "units",
                "producer",
                "producer_version",
                "opt_in",
            ):
                if field in raw and raw[field] != getattr(owner, field):
                    raise CellFeatureError(
                        f"clustering feature_specs[{index}].{field} does not match "
                        "the owner measurement plan"
                    )
            try:
                bound.append(
                    replace(
                        owner,
                        transform=raw.get("transform", owner.transform),
                        weight=raw.get("weight", owner.weight),
                    )
                )
            except CellClusteringError as exc:
                raise CellFeatureError(str(exc)) from exc
        return tuple(bound)

    @classmethod
    def from_mapping(cls, value: object, canonical_channels: Iterable[object]) -> "MeasurementPlan":
        """

        Reads a saved measurement plan against the image's current channel descriptions.

        After checking the saved format and feature list, we construct the channel
        lookup and parse each selected measurement through the same checks used
        for newly returned QuPath features. Construction then checks that their
        feature IDs and complete measurement names remain unique.

        Args:
            value: Current measurement-plan dictionary with its ordered feature list.
            canonical_channels: Reviewed processed-image channel mappings or records
                supplying channel IDs, names and biomarker assignments.

        Returns:
            MeasurementPlan containing the checked features in saved order.

        Raises:
            CellFeatureError: The format, channel descriptions or individual feature
                values are incomplete, invalid or inconsistent.
        """

        mapping = _mapping(value, "measurement plan")
        if mapping.get("schema_version") != "qupath-measurement-plan/v1":
            raise CellFeatureError("measurement plan schema_version is unsupported")
        raw_features = mapping.get("features")
        if not isinstance(raw_features, Sequence) or isinstance(raw_features, (str, bytes)):
            raise CellFeatureError("measurement plan features must be an array")
        channels = _canonical_channels(canonical_channels)
        return cls(tuple(_feature_from_inventory(item, channels) for item in raw_features))


@dataclass(frozen=True, slots=True)
class FeatureCatalog:
    """

    Collects the measurement descriptions returned for the current segmented cells.

    We keep every reported selected feature available for review, including
    its missing/nonfinite proportions and inclusion setting. build_plan() then
    chooses the features to retain and their order for subsequent analysis.

    Attributes:
        features: Nonempty tuple of FeatureSpec records with unique IDs and
            exact measurement names.
    """

    features: tuple[FeatureSpec, ...]

    def __post_init__(self) -> None:
        """

        Requires a nonempty collection with unique feature IDs and measurement names.

        Raises:
            CellFeatureError: No features are available, an ID is missing/repeated,
                or an exact measurement name occurs more than once.
        """

        if not self.features:
            raise CellFeatureError("QuPath measurement inventory is empty")
        ids = [item.feature_id for item in self.features]
        names = [item.measurement for item in self.features]
        if any(item is None for item in ids) or len(set(ids)) != len(ids):
            raise CellFeatureError("QuPath measurement inventory has duplicate feature IDs")
        if len(set(names)) != len(names):
            raise CellFeatureError("QuPath measurement inventory has duplicate exact keys")

    @classmethod
    def from_inventory(
        cls,
        inventory: Iterable[object],
        canonical_channels: Iterable[object],
    ) -> "FeatureCatalog":
        """

        Reads the feature descriptions produced by the native measurement step.

        We create a lookup of the reviewed image channels, then check each feature's
        complete measurement name, channel assignment, producer and missing-value
        fractions. The resulting collection retains the native feature order for
        later default or explicit selection.

        Args:
            inventory: Measurement-description mappings returned by QuPath, with
                the rate of absent and nonfinite values across the current cells.
            canonical_channels: Reviewed processed-image channels used to compare
                the feature's channel and biomarker values.

        Returns:
            FeatureCatalog containing the checked descriptions.

        Raises:
            CellFeatureError: A channel or feature description is incomplete,
                repeated or incompatible with the reviewed image.
        """

        channels = _canonical_channels(canonical_channels)
        features = tuple(_feature_from_inventory(item, channels) for item in inventory)
        return cls(features)

    def build_plan(self, *, selected_ids: Iterable[str] | None = None) -> MeasurementPlan:
        """

        Chooses an ordered feature set from the available cell measurements.

        With no explicit selection, we retain all features marked opt_in=False.
        An explicit list determines the requested order and can include features
        that require opt-in. Each selected ID must exist and appear once; the plan
        must retain at least one feature.

        Args:
            selected_ids: Optional ordered feature IDs. None uses the inclusion
                choices recorded in the available feature descriptions.

        Returns:
            MeasurementPlan containing the selected FeatureSpecs.

        Raises:
            CellFeatureError: The selection is empty, contains duplicate IDs or
                refers to a feature absent from the current measurements.
        """

        indexed = {item.feature_id: item for item in self.features}
        if selected_ids is None:
            selected = tuple(item.feature_id for item in self.features if not item.opt_in)
        else:
            selected = tuple(selected_ids)
        if not selected:
            raise CellFeatureError("measurement plan must select at least one feature")
        if len(set(selected)) != len(selected):
            raise CellFeatureError("selected feature IDs must be unique")
        missing = [item for item in selected if not isinstance(item, str) or item not in indexed]
        if missing:
            raise CellFeatureError(
                "selected feature ID is not present in QuPath inventory: " + repr(missing[0])
            )
        return MeasurementPlan(tuple(indexed[item] for item in selected))


def _canonical_channels(values: Iterable[object]) -> Mapping[str, Mapping[str, object]]:
    """

    Indexes the reviewed image channels for measurement-to-channel comparisons.

    We obtain the ID, display name, component, fluorophore and biomarker from
    each typed channel record or mapping. IDs must be nonempty and unique.
    Independent read-only mappings retain those fields while features are parsed.

    Args:
        values: Reviewed image-channel records or mappings.

    Returns:
        Read-only mapping from channel IDs to their copied descriptions.

    Raises:
        CellFeatureError: A channel ID is missing, blank or repeated.
    """

    channels: dict[str, Mapping[str, object]] = {}
    for raw in values:
        if isinstance(raw, Mapping):
            channel = raw
        else:
            channel = {
                "channel_id": getattr(raw, "channel_id", None),
                "name": getattr(raw, "name", None),
                "component": getattr(raw, "component", None),
                "fluorophore": getattr(raw, "fluorophore", None),
                "biomarker": getattr(raw, "biomarker", None),
            }
        channel_id = channel.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise CellFeatureError("canonical channel_id must be a non-empty string")
        if channel_id in channels:
            raise CellFeatureError("canonical channel IDs must be unique")
        channels[channel_id] = MappingProxyType(dict(channel))
    return MappingProxyType(channels)


def _feature_from_inventory(
    raw: object, channels: Mapping[str, Mapping[str, object]]
) -> FeatureSpec:
    """

    Reads one native cell-feature description and checks its image-channel assignment.

    We require the feature ID, exact measurement name, kind, channel, biomarker,
    compartment, units, producing program/version and missing/nonfinite fractions.
    Each rate must be a finite number in [0, 1]. These rates describe the current
    measurements; this reader retains them rather than selecting a cutoff for
    excluding columns.

    Expression, DAPI and AF measurements must refer to a reviewed channel. For
    expression, the reported biomarker must equal that channel's assignment.
    DAPI and AF retain biomarker=None. Shape and contextual measurements instead
    have neither a channel ID nor an antibody-target assignment. We then create
    the FeatureSpec using its standard transformation and weight defaults.

    Args:
        raw: Measurement-description mapping returned by the native calculation.
        channels: Checked channel-ID lookup for the same processed image.

    Returns:
        FeatureSpec retaining the selected quantity, native name, observed
        missing-value rates and inclusion choice.

    Raises:
        CellFeatureError: Required fields, feature kind, rates, opt_in or the
            channel/biomarker relationship are invalid.
    """

    mapping = _mapping(raw, "QuPath measurement inventory entry")
    required = (
        "feature_id",
        "measurement_name",
        "kind",
        "channel_id",
        "biomarker",
        "compartment",
        "units",
        "producer",
        "producer_version",
        "missing_rate",
        "non_finite_rate",
    )
    missing = [field for field in required if field not in mapping]
    if missing:
        raise CellFeatureError("QuPath measurement inventory is missing " + missing[0])
    try:
        kind = FeatureKind(mapping["kind"])
    except (TypeError, ValueError) as exc:
        raise CellFeatureError("QuPath measurement inventory kind is unsupported") from exc
    feature_id = _nonempty(mapping["feature_id"], "feature_id")
    name = _nonempty(mapping["measurement_name"], "measurement_name")
    channel_id = _optional(mapping["channel_id"], "channel_id")
    biomarker = _optional(mapping["biomarker"], "biomarker")
    compartment = _optional(mapping["compartment"], "compartment")
    units = _optional(mapping["units"], "units")
    producer = _nonempty(mapping["producer"], "producer")
    producer_version = _nonempty(mapping["producer_version"], "producer_version")
    missing_rate = _rate(mapping["missing_rate"], "missing_rate")
    non_finite_rate = _rate(mapping["non_finite_rate"], "non_finite_rate")
    opt_in = mapping.get("opt_in", False)
    if not isinstance(opt_in, bool):
        raise CellFeatureError("opt_in must be boolean")
    # Check the channel assignment for intensity features and retain
    # the specific feature kind used by downstream selection.
    if kind in _CHANNEL_KINDS:
        channel = channels.get(channel_id or "")
        if channel is None:
            raise CellFeatureError("inventory channel_id is not a canonical channel")
        canonical_biomarker = channel.get("biomarker")
        if kind is FeatureKind.EXPRESSION:
            if canonical_biomarker != biomarker:
                raise CellFeatureError(
                    "protein-marker intensity feature canonical biomarker does " "not match channel"
                )
        elif biomarker is not None:
            raise CellFeatureError("DAPI/AF feature biomarker must remain unknown")
    elif channel_id is not None or biomarker is not None:
        raise CellFeatureError("morphology/context feature cannot declare channel or biomarker")
    try:
        return FeatureSpec(
            measurement=name,
            role=_ROLE_BY_KIND[kind],
            channel_id=channel_id,
            feature_id=feature_id,
            kind=kind.value,
            biomarker=biomarker,
            compartment=compartment,
            units=units,
            producer=producer,
            producer_version=producer_version,
            missing_rate=missing_rate,
            non_finite_rate=non_finite_rate,
            opt_in=opt_in,
        )
    except CellClusteringError as exc:
        raise CellFeatureError(str(exc)) from exc


def _feature_mapping(spec: FeatureSpec) -> dict[str, object]:
    """

    Collects the fields saved for one selected cell measurement.

    Args:
        spec: FeatureSpec describing the native measurement and its image origin.

    Returns:
        Dictionary of the feature identity, compartment, units, producing
        program/version, observed rates and inclusion choice.
    """

    return {
        "feature_id": spec.feature_id,
        "measurement_name": spec.measurement,
        "kind": spec.kind,
        "channel_id": spec.channel_id,
        "biomarker": spec.biomarker,
        "compartment": spec.compartment,
        "units": spec.units,
        "producer": spec.producer,
        "producer_version": spec.producer_version,
        "missing_rate": spec.missing_rate,
        "non_finite_rate": spec.non_finite_rate,
        "opt_in": spec.opt_in,
    }


def _mapping(value: object, field: str) -> Mapping[str, object]:
    """

    Checks that a selected feature or plan provides named fields.

    Args:
        value: Candidate Mapping to read.
        field: Description identifying that object in an error.

    Returns:
        Mapping used by the specific feature or plan reader.

    Raises:
        CellFeatureError: The value does not support the required mapping interface.
    """

    if not isinstance(value, Mapping):
        raise CellFeatureError(f"{field} must be a mapping")
    return value


def _nonempty(value: object, field: str) -> str:
    """

    Checks that a required measurement-description field contains text.

    Args:
        value: Field value to inspect.
        field: Field name used in an error message.

    Returns:
        Nonblank text, retaining the exact spelling used for measurement-name comparisons.

    Raises:
        CellFeatureError: The field is non-text or contains only whitespace.
    """

    if not isinstance(value, str) or not value.strip():
        raise CellFeatureError(f"{field} must be a non-empty string")
    return value


def _optional(value: object, field: str) -> str | None:
    """

    Reads an optional text value from a measurement description.

    Args:
        value: Description text or None.
        field: Field name used when text is invalid.

    Returns:
        None for an absent value, otherwise checked nonblank text.
    """

    if value is None:
        return None
    return _nonempty(value, field)


def _rate(value: object, field: str) -> float:
    """

    Checks an observed fraction of missing or nonfinite cell measurements.

    Args:
        value: Integer or floating-point rate; booleans are excluded.
        field: Name of the fraction being checked.

    Returns:
        Finite float in the closed interval [0, 1].

    Raises:
        CellFeatureError: The value has the wrong type, is nonfinite or lies
            outside the permitted interval.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise CellFeatureError(f"{field} must be finite")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise CellFeatureError(f"{field} must be between zero and one")
    return number


__all__ = ["CellFeatureError", "FeatureCatalog", "FeatureKind", "FeatureSpec", "MeasurementPlan"]
