"""Retain section measurement choices without changing accepted cell geometry.

We validate the same exact feature requests used by ordinary extraction. A
revision can add or change optional measurements while retaining requests
needed by the admitted marker rules and clustering configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re

from mif_pipeline.cell_analysis.measurement_setup import (
    configured_measurement_feature_requests, validate_measurement_feature_requests,
)
from mif_pipeline.pipeline_core.config import ChannelSpec
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from .batch_artifacts import BatchArtifactError, _closed_mapping
from .batch_settled_review_revision import SettledReviewRevision


@dataclass(frozen=True, slots=True)
class MeasurementSettings:
    channels: tuple[ChannelSpec, ...]
    feature_requests: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if (type(self.channels) is not tuple or not 1 <= len(self.channels) <= 1024
                or any(type(item) is not ChannelSpec for item in self.channels)
                or len({item.channel_id for item in self.channels}) != len(self.channels)):
            raise BatchArtifactError("Measurement options require the exact processed channels.")
        requests = validate_measurement_feature_requests(self.feature_requests, channels=self.channels)
        object.__setattr__(self, "feature_requests", requests)

    def to_mapping(self) -> dict[str, object]:
        return {"canonical_channels": [item.to_mapping() for item in self.channels],
                "feature_requests": [dict(item) for item in self.feature_requests]}

    @classmethod
    def from_mapping(cls, value: object) -> "MeasurementSettings":
        mapping = _closed_mapping(value, fields=frozenset({
            "canonical_channels", "feature_requests"
        }), field="measurement settings")
        rows = mapping["canonical_channels"]
        if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 1024:
            raise BatchArtifactError("Measurement options require processed channels.")
        fields = frozenset({"channel_id", "name", "component", "fluorophore", "biomarker"})
        channels = tuple(ChannelSpec.from_mapping(
            _closed_mapping(row, fields=fields, field="measurement channel"),
            field="measurement channel") for row in rows)
        return cls(channels, mapping["feature_requests"])


def validate_measurement_settings(settings: MeasurementSettings, *, config, channels):
    """Join selected channels and retain every configured downstream requirement."""
    if type(settings) is not MeasurementSettings or settings.channels != channels:
        raise BatchArtifactError("Measurement options differ from the accepted image channels.")
    required = configured_measurement_feature_requests(config)
    selected = {item["feature_id"]: dict(item) for item in settings.feature_requests}
    if any(selected.get(item["feature_id"]) != dict(item) for item in required):
        raise BatchArtifactError(
            "Keep measurements required by the configured marker rules and clustering. "
            "Their options must be revised before these measurements can be removed.")
    return settings.feature_requests


@dataclass(frozen=True, slots=True)
class MeasurementSettingsRevision:
    predecessor_node_id: str
    revision: int
    settings: MeasurementSettings
    supervised_reference_predecessor: SettledReviewRevision | None = None

    def __post_init__(self) -> None:
        if type(self.predecessor_node_id) is not str or re.fullmatch(
                r"node-[0-9a-f]{48}", self.predecessor_node_id) is None:
            raise BatchArtifactError("measurement revision requires its exact predecessor task")
        if type(self.revision) is not int or self.revision < 1:
            raise BatchArtifactError("measurement revision number must be positive")
        if type(self.settings) is not MeasurementSettings:
            raise BatchArtifactError("measurement revision settings must be typed")
        if self.supervised_reference_predecessor is not None and type(self.supervised_reference_predecessor) is not SettledReviewRevision:
            raise BatchArtifactError("measurement reference predecessor must be a typed settled review")

    def to_mapping(self) -> dict[str, object]:
        return {"predecessor_node_id": self.predecessor_node_id,
                "revision": self.revision, "settings": self.settings.to_mapping(),
                **({"supervised_reference_predecessor": self.supervised_reference_predecessor.to_mapping()}
                   if self.supervised_reference_predecessor is not None else {})}

    @classmethod
    def from_mapping(cls, value: object) -> "MeasurementSettingsRevision":
        optional = {"supervised_reference_predecessor"} if isinstance(value, Mapping) and "supervised_reference_predecessor" in value else set()
        mapping = _closed_mapping(value, fields=frozenset({
            "predecessor_node_id", "revision", "settings"
        } | optional), field="measurement settings revision")
        return cls(mapping["predecessor_node_id"], mapping["revision"],
                   MeasurementSettings.from_mapping(mapping["settings"]),
                   SettledReviewRevision.from_mapping(mapping["supervised_reference_predecessor"])
                   if optional else None)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_mapping())).hexdigest()
