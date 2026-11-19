"""Bind a selected KRONOS encoder and graph representation to native RF/SVM settings.

The configuration records model-package identity, channels, patch geometry, graph training
and worker release. Readiness checks the selected package, Python and worker path before
execution. Model bytes retain their integrity checks while worker compatibility uses the
explicit release/protocol handshake."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from collections.abc import Mapping
import math
from ._shared import BatchTrainingError, _closed_mapping, _sha256, _text
from .qupath_rtrees import QuPathPixelRTreesConfiguration
from .qupath_svm import QuPathPixelSvmConfiguration
from mif_pipeline.compartment_analysis.graph_training import GraphTrainingSettings
from mif_pipeline.compartment_analysis.kronos_encoder import validate_activation_budget
from mif_pipeline.compartment_analysis.kronos_marker_preparation import (
    KronosAdditionalMarker, KronosAdditionalMarkerDefinition, KronosMarkerTextModel,
    additional_marker_from_mapping, has_pending_marker_statistics, validate_marker_selection,
)

KRONOS_SCHEMAS = ("qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2")


# ==============================================================================
#                                 Qupath kronos
# ==============================================================================


@dataclass(frozen=True, slots=True)
class KronosEncoderSettings:
    package_directory: str
    package_sha256: str
    worker_command: tuple[str, ...]
    worker_release: str
    channel_ids: tuple[str, ...]
    marker_names: tuple[str, ...]
    intensity_divisors: tuple[float, ...]
    pixel_size_um: tuple[float, float]
    patch_size: int
    additional_markers: tuple[KronosAdditionalMarker | KronosAdditionalMarkerDefinition, ...]
    marker_text_model: KronosMarkerTextModel | None
    nuclear_channel: int | None
    threads: int
    activation_budget_bytes: int

    def __post_init__(self):
        # Own the numeric sequence at construction, so downstream feature
        # sources can reuse these settings without another serialization pass.
        if type(self.intensity_divisors) not in (list, tuple):
            raise BatchTrainingError("KRONOS intensity divisors must be an ordered array")
        object.__setattr__(self, "intensity_divisors", tuple(self.intensity_divisors))
        for name in ("package_directory",):
            value = _text(getattr(self, name), name)
            if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
                raise BatchTrainingError(f"{name} must be an absolute local path")
        if (not isinstance(self.worker_command, (list, tuple)) or not 1 <= len(self.worker_command) <= 8
                or any(type(v) is not str or not v or "\0" in v for v in self.worker_command)
                or not (PurePosixPath(self.worker_command[0]).is_absolute() or PureWindowsPath(self.worker_command[0]).is_absolute())):
            raise BatchTrainingError("KRONOS requires the installed application worker command")
        object.__setattr__(self, "worker_command", tuple(self.worker_command))
        _sha256(self.package_sha256, "package_sha256")
        from mif_pipeline._release import RELEASE_ID

        if self.worker_release != RELEASE_ID:
            raise BatchTrainingError(
                "The configured KRONOS worker release differs from this application."
            )
        count = len(self.channel_ids)
        if (
            not 1 <= count <= 32
            or len(self.marker_names) != count
            or len(self.intensity_divisors) != count
        ):
            raise BatchTrainingError(
                "KRONOS channel, marker and intensity conventions must have identical lengths"
            )
        for name in ("channel_ids", "marker_names"):
            values = getattr(self, name)
            if (
                type(values) is not tuple
                or len(set(values)) != count
                or any(type(v) is not str or not v.strip() for v in values)
            ):
                raise BatchTrainingError(f"{name} must contain distinct explicit identities")
        if type(self.pixel_size_um) is not tuple or len(self.pixel_size_um) != 2:
            raise BatchTrainingError(
                "KRONOS input sampling requires independent X and Y pixel sizes"
            )
        for values in (self.intensity_divisors, self.pixel_size_um):
            if any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0 for v in values):
                raise BatchTrainingError(
                    "KRONOS intensity divisors and pixel sizes must be finite and positive"
                )
        for name in ("intensity_divisors", "pixel_size_um"):
            object.__setattr__(self, name, tuple(float(value) for value in getattr(self, name)))
        if self.nuclear_channel is not None and (
            type(self.nuclear_channel) is not int or not 0 <= self.nuclear_channel < count
        ):
            raise BatchTrainingError(
                "The nuclear channel must be an explicit channel index or absent"
            )
        if type(self.threads) is not int or not 1 <= self.threads <= 8:
            raise BatchTrainingError("KRONOS worker threads must be between one and eight")
        if type(self.additional_markers) is not tuple or any(
            type(row) not in (KronosAdditionalMarker, KronosAdditionalMarkerDefinition) for row in self.additional_markers
        ):
            raise BatchTrainingError("Additional markers require ordered checked metadata")
        for row in self.additional_markers:
            if type(row) is KronosAdditionalMarkerDefinition and row.marker_name not in self.marker_names:
                raise BatchTrainingError(
                    f"Use the exact selected marker spelling for the new marker {row.marker_name!r}"
                )
        if bool(self.additional_markers) != (self.marker_text_model is not None) or (
            self.marker_text_model is not None and type(self.marker_text_model) is not KronosMarkerTextModel
        ):
            raise BatchTrainingError("Select BioLinkBERT for additional markers; omit it for a known-only panel")
        validate_activation_budget(count, self.patch_size, self.activation_budget_bytes)

    @classmethod
    def from_mapping(cls, value):
        row = _closed_mapping(
            value, fields=frozenset(cls.__dataclass_fields__), field="KRONOS encoder settings"
        )
        arrays = ("worker_command", "channel_ids", "marker_names", "intensity_divisors", "pixel_size_um")
        if any(not isinstance(row[name], (list, tuple)) for name in arrays):
            raise BatchTrainingError("KRONOS ordered settings must be arrays")
        prepared = {name: tuple(row[name]) if name in arrays else row[name]
                    for name in cls.__dataclass_fields__}
        if not isinstance(row["additional_markers"], (list, tuple)):
            raise BatchTrainingError("Additional markers must be an ordered array")
        try:
            prepared["additional_markers"] = tuple(
                additional_marker_from_mapping(item) for item in row["additional_markers"])
            prepared["marker_text_model"] = (None if row["marker_text_model"] is None else
                                              KronosMarkerTextModel.from_mapping(row["marker_text_model"]))
        except ValueError as exc:
            raise BatchTrainingError(str(exc)) from exc
        return cls(**prepared)

    def to_mapping(self):
        result = {name: list(getattr(self, name)) if isinstance(getattr(self, name), tuple)
                  else getattr(self, name) for name in self.__dataclass_fields__}
        result["additional_markers"] = [row.to_mapping() for row in self.additional_markers]
        result["marker_text_model"] = (None if self.marker_text_model is None
                                       else self.marker_text_model.to_mapping())
        return result


@dataclass(frozen=True, slots=True)
class QuPathKronosConfiguration:
    configuration_schema: str
    native_configuration: QuPathPixelRTreesConfiguration | QuPathPixelSvmConfiguration
    encoder: KronosEncoderSettings
    graph: GraphTrainingSettings

    def __post_init__(self):
        expected = {
            "qupath-kronos-rtrees/v2": QuPathPixelRTreesConfiguration,
            "qupath-kronos-svm/v2": QuPathPixelSvmConfiguration,
        }.get(self.configuration_schema)
        if expected is None or type(self.native_configuration) is not expected:
            raise BatchTrainingError(
                "The KRONOS method and native estimator configuration disagree"
            )
        if (
            type(self.encoder) is not KronosEncoderSettings
            or type(self.graph) is not GraphTrainingSettings
        ):
            raise BatchTrainingError("KRONOS requires explicit encoder and graph settings")
        native = self.native_configuration
        if not native.image_features.include_intensity or native.image_features.scales:
            raise BatchTrainingError("KRONOS uses encoder features; image_features must remain raw-only")
        # Pixel subsampling/weights/boundary rasterization are not a patch attribution policy.
        if (
            native.maximum_training_samples != 0
            or native.reweight_samples
            or native.boundary_strategy != "skip"
        ):
            raise BatchTrainingError(
                "KRONOS trains on unique complete patches without pixel resampling or boundary labels"
            )
        if native.pca_retained_variance != 0 or native.pca_normalize:
            raise BatchTrainingError(
                "The initial KRONOS route uses its learned graph features without additional PCA"
            )
        if any(
            abs(value - native.resolution_um_per_pixel) > 1e-9
            for value in self.encoder.pixel_size_um
        ):
            raise BatchTrainingError(
                "Encoder sampling must match the explicitly selected native input resolution"
            )

    @property
    def engine_id(self):
        return (
            "qupath-kronos-svm-classifier"
            if self.configuration_schema == KRONOS_SCHEMAS[1]
            else "qupath-kronos-rtrees-classifier"
        )

    @property
    def output_labels(self):
        return self.native_configuration.output_labels

    @classmethod
    def from_mapping(cls, value):
        row = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="KRONOS classifier configuration",
        )
        schema = row["configuration_schema"]
        parser = {
            "qupath-kronos-rtrees/v2": QuPathPixelRTreesConfiguration,
            "qupath-kronos-svm/v2": QuPathPixelSvmConfiguration,
        }.get(schema)
        if parser is None:
            raise BatchTrainingError("The KRONOS classifier schema is unsupported")
        graph = _closed_mapping(
            row["graph"],
            fields=frozenset(GraphTrainingSettings.__dataclass_fields__),
            field="KRONOS graph settings",
        )
        return cls(
            schema,
            parser.from_mapping(row["native_configuration"]),
            KronosEncoderSettings.from_mapping(row["encoder"]),
            GraphTrainingSettings(**graph),
        )

    def to_mapping(self):
        return {
            "configuration_schema": self.configuration_schema,
            "native_configuration": self.native_configuration.to_mapping(),
            "encoder": self.encoder.to_mapping(),
            "graph": {name: getattr(self.graph, name) for name in self.graph.__dataclass_fields__},
        }


def native_estimator_configuration(configuration):
    return (
        configuration.native_configuration
        if type(configuration) is QuPathKronosConfiguration
        else configuration
    )


def _inspect_encoder_readiness(selected: KronosEncoderSettings):
    """Check local files and selection; pending statistics produce no runtime panel.

    Cell and tissue classification share this preparation. The worker checks the
    complete package checksum against the saved selection before model loading.
    """
    from mif_pipeline.compartment_analysis.kronos_encoder import inspect_kronos_resources
    from mif_pipeline.compartment_analysis.kronos_panel import KronosPanel
    from mif_pipeline.orchestration.setup.kronos_setup import require_worker_command

    require_worker_command(selected.worker_command)
    root, _weights, markers, _configuration, _resources = inspect_kronos_resources(
        Path(selected.package_directory))
    if has_pending_marker_statistics(selected):
        validate_marker_selection((root / markers).read_bytes(), selected.marker_names,
                                  selected.nuclear_channel, selected.additional_markers)
        from mif_pipeline.compartment_analysis.kronos_marker_preparation import inspect_marker_text_model
        inspected = inspect_marker_text_model(selected.marker_text_model.directory,
                                               revision=selected.marker_text_model.revision)
        if inspected != selected.marker_text_model:
            raise BatchTrainingError("The selected BioLinkBERT snapshot differs from its saved identity")
        return root, None
    panel = KronosPanel.from_csv(
        root / markers,
        patch_size=selected.patch_size,
        additional_markers=selected.additional_markers, marker_text_model=selected.marker_text_model,
        marker_names=selected.marker_names,
        intensity_divisors=selected.intensity_divisors,
        pixel_size_um=selected.pixel_size_um,
        nuclear_channel=selected.nuclear_channel,
    )
    return root, panel


def inspect_kronos_readiness(configuration: QuPathKronosConfiguration):
    """Return local package and a resolved panel, or None for training-only intent."""
    if type(configuration) is not QuPathKronosConfiguration:
        raise BatchTrainingError("KRONOS readiness requires its exact configuration")
    return _inspect_encoder_readiness(configuration.encoder)
