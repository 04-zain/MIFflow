"""

Selects the cell measurements that we request from the processed image.

For a marker-intensity measurement, we record (i) the processed channel,
(ii) its assigned biomarker, (iii) the cellular compartment and (iv) the
summary statistic. The supported compartment names are nucleus, cytoplasm,
membrane and cell. The request format accepts Mean, Median, Min, Max,
Std.Dev. and Variance in the exact QuPath measurement name, such as
"Cell: CD8: Mean". The native measurement step then checks which requested
combinations actually exist for the segmented cells.

We also describe measurements of cell shape, nuclear shape and surrounding
tissue through the same request structure. Each measurement receives a
distinct feature ID and name so marker rules and clustering can select it
consistently after the values are calculated. DAPI, autofluorescence and
contextual measurements retain their specific kind and inclusion choice.

This module checks the requested names and their image-channel assignments.
QuPath calculates the values from the segmented cell masks; its returned
measurements are checked again before they enter downstream cell analysis.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from types import MappingProxyType

from mif_pipeline.pipeline_core.config import AnalysisConfig, ChannelSpec

from .clustering import CellClusteringError, FeatureRole, FeatureSpec
from .measurement_plan import CellFeatureError, FeatureKind

_FIELDS = frozenset(
    {
        "feature_id",
        "measurement_name",
        "kind",
        "channel_id",
        "biomarker",
        "compartment",
        "units",
        "opt_in",
    }
)
_COMPARTMENTS = frozenset({"nucleus", "cytoplasm", "membrane", "cell"})
_STATISTICS = frozenset({"Mean", "Median", "Min", "Max", "Std.Dev.", "Variance"})
_CHANNEL_ROLES = {
    FeatureKind.EXPRESSION: FeatureRole.EXPRESSION,
    FeatureKind.DAPI: FeatureRole.DAPI,
    FeatureKind.AUTOFLUORESCENCE: FeatureRole.AUTOFLUORESCENCE,
}
_OTHER_ROLES = {
    FeatureKind.MORPHOLOGY: FeatureRole.MORPHOLOGY,
    FeatureKind.NUCLEAR_MORPHOLOGY: FeatureRole.MORPHOLOGY,
    FeatureKind.CONTEXTUAL: FeatureRole.CONTEXTUAL,
}


# ==============================================================================
#                               Measurement setup
# ==============================================================================


def _canonical_string(value: object, field: str) -> str:
    """

    Checks a required measurement name or identifier before using it in an exact comparison.

    Args:
        value: Expected text with at least one character and no surrounding whitespace.
        field: Field name included in an error.

    Returns:
        Checked measurement text.

    Raises:
        CellFeatureError: The value is missing, empty, non-text or padded with whitespace.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise CellFeatureError(f"{field} must be a canonical non-empty string")
    return value


def _nullable_string(value: object, field: str) -> str | None:
    """

    Reads an optional text field while retaining None for an absent assignment.

    Args:
        value: Measurement-description text or None.
        field: Name used to identify invalid text.

    Returns:
        Checked text, or None when that assignment is absent.

    Raises:
        CellFeatureError: A supplied value fails the required-text check.
    """

    return None if value is None else _canonical_string(value, field)


def _channels_by_id(channels: object) -> dict[str, ChannelSpec]:
    """

    Builds the processed-channel lookup used to check measurement requests.

    We check the channel ID and display name, followed by any recorded component,
    fluorophore and biomarker text. Repeated channel IDs are rejected before
    constructing the lookup because they would associate one requested feature
    with more than one image channel.

    Args:
        channels: Nonempty tuple of ChannelSpec records from the processed image.

    Returns:
        Dictionary mapping each channel ID to its recorded channel description.

    Raises:
        CellFeatureError: The tuple is empty, contains another record type or
            has incomplete text or repeated channel IDs.
    """

    if not isinstance(channels, tuple) or not channels:
        raise CellFeatureError("processed channels must be a non-empty tuple")
    result: dict[str, ChannelSpec] = {}
    for channel in channels:
        if not isinstance(channel, ChannelSpec):
            raise CellFeatureError("processed channels must contain ChannelSpec values")
        channel_id = _canonical_string(channel.channel_id, "channel_id")
        _canonical_string(channel.name, "channel name")
        _nullable_string(channel.component, "channel component")
        _nullable_string(channel.fluorophore, "channel fluorophore")
        _nullable_string(channel.biomarker, "channel biomarker")
        if channel_id in result:
            raise CellFeatureError("processed channel IDs must be unique")
        result[channel_id] = channel
    return result


def segmentation_compartments(boundary_source=None):
    """Offer only compartments supported by the saved detector's explicit output."""
    if boundary_source == "learned_cell":
        return ("membrane", "cell")
    if boundary_source == "nuclear_outline":
        return ("nucleus",)
    if boundary_source not in (None, "estimated_nuclear_expansion"):
        raise ValueError("Unknown saved segmentation boundary source")
    return ("nucleus", "cytoplasm", "membrane", "cell")


def validate_segmentation_compartments(features, boundary_source=None):
    """Reject unavailable compartments even when settings bypass the form."""
    available = segmentation_compartments(boundary_source)
    for feature in features:
        if feature.get("compartment") not in (*available, None):
            raise ValueError("The selected segmentation does not provide the requested measurement compartment")
        if boundary_source == "nuclear_outline" and feature.get("measurement_name") in {
            "Nucleus/Cell area ratio", "Nucleus: Centroid offset µm"
        }:
            raise ValueError("This measurement requires a cell boundary distinct from the nucleus")
    return features


def validate_measurement_feature_requests(
    value: object, *, channels: tuple[ChannelSpec, ...]
) -> tuple[Mapping[str, object], ...]:
    """

    Checks that each requested feature names the intended image measurement.

    We require one to 1024 feature requests, each containing the same eight
    fields. Their feature IDs and complete measurement names must be unique.
    For expression, DAPI and autofluorescence, the channel ID must exist in the
    processed image and the compartment must be nucleus, cytoplasm, membrane
    or cell. We then build the expected measurement prefix:

        capitalized_compartment + ": " + channel_display_name + ": "

    The remaining text must be Mean, Median, Min, Max, Std.Dev. or Variance.
    For example, a CD8 channel named "CD8" and the cell compartment give the
    prefix "Cell: CD8: "; selecting the mean produces "Cell: CD8: Mean".
    The expression biomarker must equal the marker assigned to that channel.
    Each channel uses one compartment and can request several measures there.
    Marker-threshold and clustering setup later require Mean or Median for
    their selected marker inputs. Other statistics can be measured for export
    and supervised classification without requiring either statistic here.
    DAPI and autofluorescence measurements use their corresponding kind and
    retain biomarker=None. Shape and contextual features have no channel or
    antibody-target assignment.

    We construct a FeatureSpec for each checked request so the downstream
    feature-inclusion rules are applied consistently. At least one expression
    measurement must be present. A correctly formed request still needs its
    value to be present in the actual QuPath output; that is checked after
    measurement execution.

    Args:
        value: Ordered list or tuple of feature_id, measurement_name, kind,
            channel_id, biomarker, compartment, units and opt_in mappings.
        channels: Processed-image ChannelSpec records used for channel and
            biomarker comparisons.

    Returns:
        Ordered tuple of read-only feature-request mappings. The unit labels
        are recorded with the selected measurements.

    Raises:
        CellFeatureError: Fields or IDs repeat, the measurement name is invalid,
            channel and biomarker assignments disagree, or the selected feature
            kind and inclusion setting are inconsistent.
    """

    by_id = _channels_by_id(channels)
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 1024:
        raise CellFeatureError("feature requests must be an array of 1 to 1024 entries")

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    channel_compartments: dict[str, str] = {}
    expression_count = 0
    validated: list[Mapping[str, object]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != _FIELDS:
            raise CellFeatureError(f"feature_requests[{index}] must have exactly eight fields")
        feature_id = _canonical_string(raw["feature_id"], "feature_id")
        name = _canonical_string(raw["measurement_name"], "measurement_name")
        if feature_id in seen_ids or name in seen_names:
            raise CellFeatureError(
                "feature requests require unique IDs and exact measurement names"
            )
        seen_ids.add(feature_id)
        seen_names.add(name)
        try:
            kind = FeatureKind(raw["kind"])
        except (TypeError, ValueError) as exc:
            raise CellFeatureError("feature kind is unsupported") from exc
        channel_id = _nullable_string(raw["channel_id"], "channel_id")
        biomarker = _nullable_string(raw["biomarker"], "biomarker")
        compartment = _nullable_string(raw["compartment"], "compartment")
        units = _nullable_string(raw["units"], "units")
        opt_in = raw["opt_in"]
        if not isinstance(opt_in, bool):
            raise CellFeatureError("opt_in must be boolean")

        if kind in _CHANNEL_ROLES:
            if compartment not in _COMPARTMENTS:
                raise CellFeatureError("feature compartment is unsupported")
            channel = by_id.get(channel_id or "")
            if channel is None:
                raise CellFeatureError("feature channel_id is not a processed channel")
            previous_compartment = channel_compartments.setdefault(channel.channel_id, compartment)
            if previous_compartment != compartment:
                raise CellFeatureError(
                    f"Choose one cellular compartment for {channel.name}; "
                    "all its intensity measures use that compartment."
                )
            if kind is FeatureKind.EXPRESSION:
                if biomarker != channel.biomarker:
                    raise CellFeatureError("expression biomarker does not match canonical channel")
                expression_count += 1
            elif biomarker is not None:
                raise CellFeatureError("DAPI/AF biomarker must remain unknown")
            # Construct the expected compartment/channel prefix, then check
            # that its final term is one of the supported statistics.
            prefix = f"{compartment.capitalize()}: {channel.name}: "
            if not name.startswith(prefix) or name[len(prefix) :] not in _STATISTICS:
                raise CellFeatureError(
                    "feature measurement_name is not an exact supported QuPath key"
                )
            role = _CHANNEL_ROLES[kind]
        else:
            if channel_id is not None or biomarker is not None:
                raise CellFeatureError(
                    "morphology/context feature cannot declare channel or biomarker"
                )
            role = _OTHER_ROLES[kind]

        try:
            FeatureSpec(
                measurement=name,
                role=role,
                channel_id=channel_id,
                feature_id=feature_id,
                kind=kind.value,
                biomarker=biomarker,
                compartment=compartment,
                units=units,
                opt_in=opt_in,
            )
        except CellClusteringError as exc:
            raise CellFeatureError(str(exc)) from exc
        validated.append(
            MappingProxyType(
                {
                    "feature_id": feature_id,
                    "measurement_name": name,
                    "kind": kind.value,
                    "channel_id": channel_id,
                    "biomarker": biomarker,
                    "compartment": compartment,
                    "units": units,
                    "opt_in": opt_in,
                }
            )
        )
    if not expression_count:
        raise CellFeatureError("feature requests require an expression measurement")
    return tuple(validated)


def configured_measurement_feature_requests(
    config: AnalysisConfig,
) -> tuple[Mapping[str, object], ...]:
    """

    Combines the measurements required by configured marker rules and clustering features.

    We first collect every threshold rule's measurement name, image channel,
    biomarker and cellular compartment. The measurement name provides a
    reproducible initial feature ID. We then add the clustering selections,
    using their exact source measurement names to reuse an existing request
    when the same value is needed by both analyses.

    A clustering selection supplies its own feature ID and kind. It can retain
    the previously recorded channel, biomarker, compartment and units when
    those fields are omitted from that selection. We keep one request per
    measurement name, in the order each measurement first appeared. The combined
    requests undergo the full name/channel checks before native measurement.

    Args:
        config: AnalysisConfig containing threshold-style known-marker rules
            and selected clustering feature definitions.

    Returns:
        Ordered feature-request dictionaries. An empty tuple means the analysis
        still needs a measurement selection.

    Raises:
        CellFeatureError: Configuration, rule or feature definitions have an
            unsupported type or lack their required measurement identity.
    """

    if type(config) is not AnalysisConfig:
        raise CellFeatureError("configured measurements require AnalysisConfig")
    by_measurement: dict[str, Mapping[str, object]] = {}
    for marker, raw in config.phenotyping.known_marker_rules.items():
        if not isinstance(raw, Mapping):
            raise CellFeatureError("configured marker rule must be a mapping")
        if raw.get("mode") != "threshold":
            continue
        measurement = _canonical_string(raw.get("measurement"), "measurement_name")
        by_measurement[measurement] = {
            "feature_id": "marker-" + hashlib.sha256(measurement.encode("utf-8")).hexdigest()[:16],
            "measurement_name": measurement,
            "kind": FeatureKind.EXPRESSION.value,
            "channel_id": raw.get("channel_id"),
            "biomarker": marker,
            "compartment": raw.get("compartment"),
            "units": None,
            "opt_in": False,
        }
    for raw in config.clustering.feature_specs:
        if not isinstance(raw, Mapping):
            raise CellFeatureError("configured clustering feature must be a mapping")
        measurement = _canonical_string(raw.get("source"), "measurement_name")
        feature_id = _canonical_string(raw.get("feature_id"), "feature_id")
        try:
            kind = FeatureKind(raw.get("kind")).value
        except (TypeError, ValueError) as exc:
            raise CellFeatureError("configured measurement feature kind is unsupported") from exc
        # Reuse a measurement already required by a marker rule. Retain its
        # recorded channel details for fields the clustering selection omits.
        inherited = by_measurement.get(measurement, {})
        by_measurement[measurement] = {
            "feature_id": feature_id,
            "measurement_name": measurement,
            "kind": kind,
            "channel_id": raw.get("channel_id", inherited.get("channel_id")),
            "biomarker": raw.get("biomarker", inherited.get("biomarker")),
            "compartment": raw.get("compartment", inherited.get("compartment")),
            "units": raw.get("units", inherited.get("units")),
            "opt_in": raw.get("opt_in", False),
        }
    return tuple(dict(item) for item in by_measurement.values())


__all__ = ["configured_measurement_feature_requests", "validate_measurement_feature_requests"]
