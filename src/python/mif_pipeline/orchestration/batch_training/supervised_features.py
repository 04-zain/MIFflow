"""Authenticate generated cell features separately from measured marker columns.

Only an explicit, typed encoder configuration may introduce the fixed KRONOS
measurement schema. The image channel inventory is independently reopened by the
caller; this module never treats generated features as existing measured columns.
"""
from __future__ import annotations

from collections.abc import Mapping

from ._shared import BatchTrainingError
from .qupath_kronos_cell import KronosCellFeatureSource, kronos_cell_measurements


def supervised_feature_source(
    configuration: Mapping[str, object],
) -> KronosCellFeatureSource | None:
    """Decode the source through its owning classifier, including a grouped search."""
    from .cross_validation import QuPathGroupedCVConfiguration
    from .qupath_rtrees import QuPathObjectRTreesConfiguration
    from .qupath_svm import QuPathObjectSvmConfiguration
    from .qupath_logistic_regression import QuPathObjectLogisticRegressionConfiguration

    raw = dict(configuration)
    raw.pop("point_selection_radius_level0", None)
    schema = raw.get("configuration_schema")
    if schema == "qupath-grouped-cross-validation/v1":
        base = QuPathGroupedCVConfiguration.from_mapping(raw).base_configuration
        return getattr(base, "feature_source", None)
    parser = {
        "qupath-object-rtrees/v1": QuPathObjectRTreesConfiguration,
        "qupath-object-svm/v1": QuPathObjectSvmConfiguration,
        "qupath-object-logistic-regression/v1": QuPathObjectLogisticRegressionConfiguration,
    }.get(schema)
    if "feature_source" not in raw:
        return None
    if parser is None or raw["feature_source"] is None:
        raise BatchTrainingError(
            "generated cell features require an explicit supported feature source"
        )
    return parser.from_mapping(raw).feature_source


def require_kronos_cell_channels(source: KronosCellFeatureSource, channels: object) -> None:
    """Match every selected encoder channel id and name to the current image."""
    from mif_pipeline.pipeline_core.config import ChannelSpec

    if (
        type(source) is not KronosCellFeatureSource
        or not isinstance(channels, (list, tuple))
        or not channels
    ):
        raise BatchTrainingError("KRONOS cell features require the current image channel inventory")
    parsed = tuple(
        item
        if type(item) is ChannelSpec
        else ChannelSpec.from_mapping(item, field="supervised channel")
        for item in channels
    )
    by_id = {item.channel_id: item.name for item in parsed}
    if len(by_id) != len(parsed) or len({item.name for item in parsed}) != len(parsed):
        raise BatchTrainingError("supervised image channel identities are ambiguous")
    if any(
        by_id.get(channel_id) != name
        for channel_id, name in zip(source.encoder.channel_ids, source.channel_names, strict=True)
    ):
        raise BatchTrainingError("KRONOS source channels differ from the current processed image")


def require_kronos_cell_schema(source: KronosCellFeatureSource, schema: object) -> None:
    """Require all 768 dimensions in the order emitted by the frozen encoder."""
    if type(source) is not KronosCellFeatureSource or not isinstance(schema, (list, tuple)):
        raise BatchTrainingError("generated cell features require a typed source and ordered schema")
    if list(schema) != kronos_cell_measurements():
        raise BatchTrainingError(
            "KRONOS cell feature schema must contain the exact ordered 768 measurements"
        )


def supervised_snapshot_source(
    configuration: Mapping[str, object], schema: object,
) -> dict[str, object]:
    """Return the optional native snapshot field after authenticating its schema."""
    source = supervised_feature_source(configuration)
    if source is None:
        return {}
    require_kronos_cell_schema(source, schema)
    return {"feature_source": source.to_mapping()}
