"""Closed channel and output-label projections for native QuPath fitting.

The `QuPathPixelChannelFeature` record carries `canonical_channel_id`,
`qupath_channel_index`, `observed_qupath_channel_name`.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from ._shared import BatchTrainingError, _array, _closed_mapping, _text

_JAVA_INT_MAX = 2_147_483_647


# ==============================================================================
#                               Native projection
# ==============================================================================


def _native_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise BatchTrainingError(f"{field} must be an integer")
    if value < 0 or value > _JAVA_INT_MAX:
        raise BatchTrainingError(f"{field} must be between 0 and {_JAVA_INT_MAX}")
    return value


@dataclass(frozen=True, slots=True)
class QuPathPixelChannelFeature:
    """One explicit canonical-channel to observed QuPath-channel binding."""

    canonical_channel_id: str
    qupath_channel_index: int
    observed_qupath_channel_name: str

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "canonical_channel_id",
            "qupath_channel_index",
            "observed_qupath_channel_name",
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_channel_id",
            _text(self.canonical_channel_id, "canonical_channel_id"),
        )
        object.__setattr__(
            self,
            "qupath_channel_index",
            _native_integer(self.qupath_channel_index, "qupath_channel_index"),
        )
        object.__setattr__(
            self,
            "observed_qupath_channel_name",
            _text(
                self.observed_qupath_channel_name,
                "observed_qupath_channel_name",
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathPixelChannelFeature":
        mapping = _closed_mapping(
            value,
            fields=cls._FIELDS,
            field="qupath_pixel_channel_feature",
        )
        return cls(
            canonical_channel_id=mapping["canonical_channel_id"],  # type: ignore[arg-type]
            qupath_channel_index=mapping["qupath_channel_index"],  # type: ignore[arg-type]
            observed_qupath_channel_name=mapping["observed_qupath_channel_name"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "canonical_channel_id": self.canonical_channel_id,
            "qupath_channel_index": self.qupath_channel_index,
            "observed_qupath_channel_name": self.observed_qupath_channel_name,
        }


@dataclass(frozen=True, slots=True)
class QuPathPixelChannelFeatures:
    """Exact ordered pixel feature projection for the supported native asset."""

    schema: Literal["qupath-pixel-channel-features/v1"]
    ordered_channels: tuple[QuPathPixelChannelFeature, ...]

    _FIELDS: ClassVar[frozenset[str]] = frozenset({"schema", "ordered_channels"})

    def __post_init__(self) -> None:
        if self.schema != "qupath-pixel-channel-features/v1":
            raise BatchTrainingError("schema must be qupath-pixel-channel-features/v1")
        channels = tuple(self.ordered_channels)
        if not channels or not all(type(item) is QuPathPixelChannelFeature for item in channels):
            raise BatchTrainingError("ordered_channels must contain exact channel feature rows")
        if len({item.qupath_channel_index for item in channels}) != len(channels):
            raise BatchTrainingError("ordered_channels must use unique nonnegative QuPath indices")
        canonical_ids = tuple(item.canonical_channel_id for item in channels)
        if len(set(canonical_ids)) != len(channels):
            raise BatchTrainingError(
                "ordered_channels must use unique canonical channel identities"
            )
        object.__setattr__(self, "ordered_channels", channels)

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathPixelChannelFeatures":
        mapping = _closed_mapping(
            value,
            fields=cls._FIELDS,
            field="qupath_pixel_channel_features",
        )
        return cls(
            schema=mapping["schema"],  # type: ignore[arg-type]
            ordered_channels=tuple(
                QuPathPixelChannelFeature.from_mapping(item)
                for item in _array(mapping["ordered_channels"], "ordered_channels")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "ordered_channels": [item.to_mapping() for item in self.ordered_channels],
        }


@dataclass(frozen=True, slots=True)
class QuPathPixelEntryChannelFeatures:
    """Typed projector input; later runtime authentication remains mandatory."""

    project_entry_id: str
    channel_features: QuPathPixelChannelFeatures

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_entry_id",
            _text(self.project_entry_id, "project_entry_id"),
        )
        if type(self.channel_features) is not QuPathPixelChannelFeatures:
            raise BatchTrainingError("channel_features must be exact QuPathPixelChannelFeatures")


def project_pixel_channel_features(
    *,
    training_entry_ids: tuple[str, ...],
    entry_features: tuple[QuPathPixelEntryChannelFeatures, ...],
) -> QuPathPixelChannelFeatures:
    """Close equal per-entry observations without treating them as authority."""

    entry_ids = tuple(
        _text(item, f"training_entry_ids[{index}]") for index, item in enumerate(training_entry_ids)
    )
    if (
        not entry_ids
        or entry_ids != tuple(sorted(entry_ids))
        or len(set(entry_ids)) != len(entry_ids)
    ):
        raise BatchTrainingError("training_entry_ids must be nonempty, canonical, and unique")
    projected = tuple(entry_features)
    if not all(type(item) is QuPathPixelEntryChannelFeatures for item in projected):
        raise BatchTrainingError("entry_features must contain exact typed projector inputs")
    projected_ids = tuple(item.project_entry_id for item in projected)
    if projected_ids != entry_ids:
        raise BatchTrainingError(
            "entry channel mapping does not provide exact training-entry coverage"
        )
    first = projected[0].channel_features
    if any(item.channel_features != first for item in projected[1:]):
        raise BatchTrainingError("entry channel mapping is ambiguous across training entries")
    return first


@dataclass(frozen=True, slots=True)
class QuPathOutputLabel:
    canonical_class_id: str
    qupath_label: int

    _FIELDS: ClassVar[frozenset[str]] = frozenset({"canonical_class_id", "qupath_label"})

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_class_id",
            _text(self.canonical_class_id, "canonical_class_id"),
        )
        object.__setattr__(
            self,
            "qupath_label",
            _native_integer(self.qupath_label, "qupath_label"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathOutputLabel":
        mapping = _closed_mapping(value, fields=cls._FIELDS, field="qupath_output_label")
        return cls(
            canonical_class_id=mapping["canonical_class_id"],  # type: ignore[arg-type]
            qupath_label=mapping["qupath_label"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "canonical_class_id": self.canonical_class_id,
            "qupath_label": self.qupath_label,
        }


@dataclass(frozen=True, slots=True)
class QuPathOutputLabels:
    schema: Literal["qupath-output-labels/v1"]
    ordered_labels: tuple[QuPathOutputLabel, ...]

    _FIELDS: ClassVar[frozenset[str]] = frozenset({"schema", "ordered_labels"})

    def __post_init__(self) -> None:
        if self.schema != "qupath-output-labels/v1":
            raise BatchTrainingError("schema must be qupath-output-labels/v1")
        labels = tuple(self.ordered_labels)
        if not labels or not all(type(item) is QuPathOutputLabel for item in labels):
            raise BatchTrainingError("ordered_labels must contain exact output label rows")
        if len({item.qupath_label for item in labels}) != len(labels):
            raise BatchTrainingError("ordered_labels must use unique nonnegative native labels")
        class_ids = tuple(item.canonical_class_id for item in labels)
        if len(set(class_ids)) != len(labels):
            raise BatchTrainingError("ordered_labels must use unique canonical class identities")
        object.__setattr__(self, "ordered_labels", labels)

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathOutputLabels":
        mapping = _closed_mapping(value, fields=cls._FIELDS, field="qupath_output_labels")
        return cls(
            schema=mapping["schema"],  # type: ignore[arg-type]
            ordered_labels=tuple(
                QuPathOutputLabel.from_mapping(item)
                for item in _array(mapping["ordered_labels"], "ordered_labels")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "ordered_labels": [item.to_mapping() for item in self.ordered_labels],
        }


__all__ = [
    "QuPathOutputLabel",
    "QuPathOutputLabels",
    "QuPathPixelChannelFeature",
    "QuPathPixelChannelFeatures",
    "QuPathPixelEntryChannelFeatures",
    "project_pixel_channel_features",
]
