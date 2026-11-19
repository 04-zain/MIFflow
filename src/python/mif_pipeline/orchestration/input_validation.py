"""Bounded, source-format-independent preflight for completed analysis images.

The `InputValidationReport` record carries `image_path`, `provenance_path`, `series_index`,
`canonical_image_id`, `canonical_image_sha256` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ome_types import from_xml
import pint
import tifffile

from mif_pipeline.io import resolve_channel_metadata, resolve_spectral_state
from mif_pipeline.io.metadata.canonical import ChannelMetadata, resolve_unmixing_input_metadata
from mif_pipeline.io.metadata.spectral import (
    _resolve_source_spectral_state,
    resolve_prepared_channel_order,
)

from mif_pipeline.pipeline_core.config import AnalysisConfig, ChannelSpec
from mif_pipeline.pipeline_core.models import ROIAnalysisError

# ==============================================================================
#                                Input validation
# ==============================================================================


class InputContractError(ROIAnalysisError):
    """Raised when an image or its provenance is not analysis-ready."""


@dataclass(frozen=True, slots=True)
class InputValidationReport:
    """Validated metadata needed by later ROI-analysis orchestration."""

    image_path: Path
    provenance_path: Path
    series_index: int
    canonical_image_id: str
    canonical_image_sha256: str
    level0_size_x_px: int
    level0_size_y_px: int
    channels: tuple[ChannelMetadata, ...]
    physical_size_x_um: float
    physical_size_y_um: float
    provenance: Mapping[str, Any]
    component_order: tuple[str, ...] | None = None


def analysis_channel_specs(report: InputValidationReport) -> tuple[ChannelSpec, ...]:
    """Project explicit semantics from one validated processed OME report."""
    if not isinstance(report, InputValidationReport):
        raise InputContractError("analysis channels require an input validation report")
    channels = report.channels
    components = report.component_order
    if not channels or components is None or len(components) != len(channels):
        raise InputContractError("processed OME component order must match channels")
    if len({channel.channel_id for channel in channels}) != len(channels):
        raise InputContractError("input OME channel IDs are not deterministic")
    specs = []
    for channel, component in zip(channels, components, strict=True):
        if (
            not isinstance(channel.name, str)
            or not channel.name.strip()
            or channel.name != channel.name.strip()
        ):
            raise InputContractError("input OME channel display names are not deterministic")
        if (
            not isinstance(component, str)
            or not component.strip()
            or component != component.strip()
        ):
            raise InputContractError("processed OME component order is invalid")
        raw = {
            "channel_id": channel.channel_id,
            "name": channel.name,
            "component": component,
            "fluorophore": channel.fluorophore,
            "biomarker": channel.biomarker,
        }
        try:
            spec = ChannelSpec.from_mapping(raw, field="processed_ome_channel")
        except ROIAnalysisError as exc:
            raise InputContractError("processed OME channel semantics are invalid") from exc
        if spec.to_mapping() != raw or spec.channel_id != spec.channel_id.strip():
            raise InputContractError("processed OME channel semantics are not canonical")
        specs.append(spec)
    return tuple(specs)


def validate_analysis_input(config: AnalysisConfig) -> InputValidationReport:
    """Validate OME semantics and provenance without loading image pixels."""
    if config.input_image.is_dir():
        from mif_pipeline.io.processed_image import ProcessedImageSource
        with ProcessedImageSource(config.input_image, cache_bytes=0) as source:
            canonical_image_sha256 = source.identity.file_sha256
            ome = source.metadata
    else:
        if not config.input_image.is_file():
            raise InputContractError(f"Input image does not exist: {config.input_image}")
        canonical_image_sha256 = _stream_sha256(config.input_image)
        ome = _read_ome_header(config.input_image)
    image = ome.images[0]
    pixels = image.pixels
    physical_size_x_um, physical_size_y_um = _validate_physical_calibration(pixels)
    spectral_state = resolve_spectral_state(ome, 0)
    prepared_order = resolve_prepared_channel_order(ome, 0)
    observed_state, _ = _resolve_source_spectral_state(
        ome, 0, spectral=spectral_state,
        acquisition=resolve_unmixing_input_metadata(ome, 0),
    )
    from mif_pipeline.io.metadata.measurement_band_copy import resolve_measurement_band_copy_mapping
    isolated = resolve_measurement_band_copy_mapping(ome) is not None
    if (observed_state == "raw" and not isolated) or (spectral_state.state != "unmixed" and prepared_order is None):
        raise InputContractError(
            "ROI analysis requires unmixed components or an explicit reviewed prepared-channel copy; "
            f"got spectral state {observed_state!r}"
        )
    channels = resolve_channel_metadata(ome, 0)
    provenance = _read_provenance(config.provenance, config.input_image)
    report = InputValidationReport(
        image_path=config.input_image,
        provenance_path=config.provenance,
        series_index=0,
        canonical_image_id=image.id,
        canonical_image_sha256=canonical_image_sha256,
        level0_size_x_px=int(pixels.size_x),
        level0_size_y_px=int(pixels.size_y),
        channels=channels,
        physical_size_x_um=physical_size_x_um,
        physical_size_y_um=physical_size_y_um,
        provenance=MappingProxyType(provenance),
        component_order=(spectral_state.component_order
                         if spectral_state.state == "unmixed" else prepared_order),
    )
    _validate_channels(config, report)
    return report


def _stream_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Read the full canonical file in bounded chunks on every invocation."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise InputContractError(f"input OME-TIFF cannot be hashed: {path}") from exc
    return digest.hexdigest()


def _read_ome_header(path: Path):
    """Read the analytical series and its OME channel/calibration description.

    We accept a readable level-zero image for both individual fields and whole
    slides. Stored reduced levels are available to later methods that select a
    processing resolution; the selected method checks its own sampling needs.
    The returned OME tree supplies channel identities and physical pixel sizes.
    """
    try:
        with tifffile.TiffFile(path) as image_file:
            if not image_file.is_ome or not image_file.ome_metadata:
                raise InputContractError(f"input is not an OME-TIFF: {path}")
            if not image_file.series:
                raise InputContractError(f"input OME-TIFF has no readable image series: {path}")
            ome_xml = image_file.ome_metadata
    except tifffile.TiffFileError as exc:
        raise InputContractError(f"input is not a readable TIFF: {path}") from exc
    try:
        ome = from_xml(ome_xml)
    except Exception as exc:  # ome-types raises several parser-specific errors.
        raise InputContractError(f"input OME metadata is invalid: {path}") from exc
    if not ome.images:
        raise InputContractError("input OME metadata contains no images")
    return ome


def _validate_physical_calibration(pixels: Any) -> tuple[float, float]:
    x_value = pixels.physical_size_x
    y_value = pixels.physical_size_y
    x_unit = pixels.physical_size_x_unit
    y_unit = pixels.physical_size_y_unit
    if x_value is None or y_value is None or x_unit is None or y_unit is None:
        raise InputContractError("input requires positive X/Y physical pixel calibration")
    try:
        x_size = float(x_value)
        y_size = float(y_value)
    except (TypeError, ValueError) as exc:
        raise InputContractError("input requires positive X/Y physical pixel calibration") from exc
    if x_size <= 0 or y_size <= 0:
        raise InputContractError("input requires positive X/Y physical pixel calibration")
    try:
        units = pint.UnitRegistry()
        x_um = float((x_size * units(x_unit.value)).to("micrometer").magnitude)
        y_um = float((y_size * units(y_unit.value)).to("micrometer").magnitude)
    except Exception as exc:
        raise InputContractError(
            "physical pixel calibration units must be compatible lengths"
        ) from exc
    return x_um, y_um


def _validate_channels(
    config: AnalysisConfig,
    report: InputValidationReport,
) -> None:
    actual = analysis_channel_specs(report)
    if config.channels is None:
        return
    if len(config.channels) != len(actual):
        raise InputContractError("configured channels do not match input OME channel count")
    for configured, observed in zip(config.channels, actual, strict=True):
        if configured != observed:
            raise InputContractError(
                "configured channel semantics do not match processed OME metadata "
                f"for channel {observed.channel_id!r}"
            )


def _read_provenance(path: Path, input_image: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InputContractError(f"processing provenance does not exist: {path}")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputContractError(f"processing provenance is not valid JSON: {path}") from exc
    if not isinstance(decoded, dict):
        raise InputContractError("processing provenance must be a JSON object")
    input_path = decoded.get("input_path")
    output_path = decoded.get("output_path")
    if (
        not isinstance(input_path, str)
        or not input_path.strip()
        or not isinstance(output_path, str)
        or not output_path.strip()
    ):
        raise InputContractError("processing provenance must identify input_path and output_path")
    planned = Path(output_path).expanduser().resolve(strict=False)
    accepted = input_image.resolve(strict=False)
    if planned != accepted:
        # Publication keeps the original request locator in provenance, while
        # readers pin an immutable generation. Later selection changes do not
        # invalidate an already accepted older generation.
        from mif_pipeline.preprocessing.unmixing.output_publication_records import manifest_from_bytes
        generation = accepted.parent
        expected_root = planned.with_name(f".{planned.name}.publication-generations")
        try:
            manifest = manifest_from_bytes((generation / "manifest.json").read_bytes())
            artifacts = {item.role: item for item in manifest.artifacts}
            bound = (generation.parent == expected_root and generation.name == manifest.generation_id
                     and accepted.name == artifacts["image"].relative_name
                     and artifacts["image"].alias_name == planned.name
                     and path.resolve() == generation / artifacts["provenance"].relative_name
                     and hashlib.sha256(path.read_bytes()).hexdigest() == artifacts["provenance"].content_sha256)
        except (OSError, ValueError, KeyError) as exc:
            raise InputContractError("Processing provenance has no matching image generation") from exc
        if not bound:
            raise InputContractError("Processing provenance does not identify the accepted image generation")
    return decoded
