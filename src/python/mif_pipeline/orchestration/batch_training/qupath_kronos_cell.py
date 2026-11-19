"""Bind frozen KRONOS2 cell inputs to a native supervised object classifier.

Input: an already imported local model package, its isolated Python runtime,
ordered image channel names and model marker mappings, and the explicit cell
crop/mask policy. Output: a checked feature-source record retained inside the
native classifier, plus the exact 768 measurement keys consumed by its head.

These settings do not fit a graph or change encoder weights. Training and
application both regenerate cell embeddings using this same description.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from .qupath_kronos import KronosEncoderSettings, _inspect_encoder_readiness
from ._shared import BatchTrainingError
from mif_pipeline.cell_analysis.kronos_features import CELL_EXTRACTION, validate_cell_extraction


def kronos_cell_measurements() -> list[dict[str, str]]:
    """Return the native head's complete ordered frozen cell feature schema."""
    return [
        {"measurement_name": f"KRONOS2: cell embedding: {i:04d}",
         "semantic_role": "kronos_cell_embedding"}
        for i in range(768)
    ]


@dataclass(frozen=True, slots=True)
class KronosCellFeatureSource:
    """Persist image-to-marker mapping, runtime, and one declared cell policy.

    ``channel_names`` are exact names observed on the processed QuPath image.
    The encoder's ``channel_ids`` remain canonical acquisition identities; they
    are not interpreted as names or positions. Their common order determines
    the marker tensor order and is retained in the feature receipt.
    """

    schema: str
    encoder: KronosEncoderSettings
    channel_names: tuple[str, ...]
    extraction: Mapping[str, object]

    def __post_init__(self):
        if self.schema != "kronos-cell-feature-source/v1" or type(self.encoder) is not KronosEncoderSettings:
            raise BatchTrainingError("KRONOS cell features require their explicit feature-source schema")
        names = self.channel_names
        if (type(names) is not tuple or len(names) != len(self.encoder.channel_ids)
                or len(set(names)) != len(names)
                or any(type(name) is not str or not name.strip() or "\x00" in name for name in names)):
            raise BatchTrainingError("KRONOS cell input names must be distinct exact image channels in marker order")
        try:
            policy = validate_cell_extraction(self.extraction)
        except ValueError as error:
            raise BatchTrainingError(str(error)) from error
        from types import MappingProxyType
        object.__setattr__(self, "extraction", MappingProxyType(policy))

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != {"schema", "encoder", "channel_names", "extraction"}:
            raise BatchTrainingError("KRONOS cell feature-source fields are incomplete or unknown")
        if not isinstance(value["channel_names"], (list, tuple)):
            raise BatchTrainingError("KRONOS image channel names must be an ordered array")
        return cls(value["schema"], KronosEncoderSettings.from_mapping(value["encoder"]),
                   tuple(value["channel_names"]), value["extraction"])

    def to_mapping(self):
        return {"schema": self.schema, "encoder": self.encoder.to_mapping(),
                "channel_names": list(self.channel_names), "extraction": dict(self.extraction)}


def inspect_kronos_cell_readiness(source: KronosCellFeatureSource):
    """Return the local package directory and cell panel before native execution.

    We check required files and panel settings here. The worker verifies the
    complete package checksum against the saved selection before loading weights.
    """
    if type(source) is not KronosCellFeatureSource:
        raise BatchTrainingError("KRONOS cell readiness requires its checked feature source")
    return _inspect_encoder_readiness(source.encoder)


__all__ = ["CELL_EXTRACTION", "KronosCellFeatureSource", "kronos_cell_measurements",
           "inspect_kronos_cell_readiness"]
