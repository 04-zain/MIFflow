"""

Determines whether an image requires unmixing and checks the inputs for that processing path.

Raw spectral images require imaging settings and a reference matrix whose
measurement-band order matches the image. Already-unmixed images instead require
a recorded component order matching the selected components and are marked to skip
spectral fitting. Unknown source state requires an explicit researcher decision.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from numbers import Integral

import numpy as np
from ome_types import OME

from mif_pipeline.io import (
    MetadataCanonicalizationError,
    MetadataConflictError,
    UnmixingInputMetadata,
    validate_offset_specification,
)
from mif_pipeline.io.metadata.calibration import raw_intensity_error

from .library import SpectralLibrary
from mif_pipeline._defaults import default_section

_DEFAULTS = default_section("unmixing")


# ==============================================================================
#                                   Validation
# ==============================================================================


class UnmixingValidationError(ValueError):
    """

    Reports missing or conflicting image information that prevents choosing a valid unmixing path.
    """

class SpectralState(str, Enum):
    """

    Distinguishes raw measurements, already-unmixed components and unknown source state.

    ISOLATED is a researcher-selected copy route, never a recorded source state.
    ABORT is the researcher's instruction to stop when the state cannot be established.
    """

    RAW = "raw"
    UNMIXED = "unmixed"
    ISOLATED = "isolated"
    UNKNOWN = "unknown"
    ABORT = "abort"


class SolverKind(str, Enum):
    """

    Selects NNLS or clipped least squares for a raw spectral image.

    NNLS constrains coefficients during fitting. Clipped least squares fits without
    that constraint and replaces negative coefficients with zero afterward.
    """

    NNLS = "nnls"
    CLIPPED_LS = "clipped_ls"


class AfBehavior(str, Enum):
    """Reviewed target choice, independent of source AF-subtraction metadata."""

    RETAIN = "retain"
    ALREADY_REMOVED = "already_removed"


@dataclass(frozen=True, slots=True)
class UnmixingConfig:
    """

    Stores the selected spectral method and regional processing settings.

    New values begin with the packaged unmixing presets. The configuration is checked
    when constructed, then retained with the operation that uses it.

    Attributes:
        solver: NNLS or clipped least squares.
        spectral_state_override: Explicit mixed, previously-unmixed or measurement-band-isolated choice.
        af_behavior: Supported handling of an included AF component.
        chunk_size: Processing-region width and height in source pixels.
        output_dtype: Float32 or float64 storage for the component image.
        calculation_precision: Saved solver precision; None retains an earlier preference.
        measurement_band_components: Component row index for every isolated measurement band.
        nnls_tolerance: Requested NNLS stopping tolerance.
        nnls_max_iterations: NNLS fitting budget.
        overwrite: Whether the surrounding workflow permits output replacement.
    """

    solver: SolverKind
    spectral_state_override: SpectralState | None = None
    af_behavior: AfBehavior = AfBehavior(_DEFAULTS["af_behavior"])
    chunk_size: tuple[int, int] = tuple(_DEFAULTS["chunk_size"])
    output_dtype: str = _DEFAULTS["output_dtype"]
    nnls_tolerance: float = _DEFAULTS["nnls_tolerance"]
    nnls_max_iterations: int = _DEFAULTS["nnls_max_iterations"]
    overwrite: bool = _DEFAULTS["overwrite"]
    # None preserves the precision selection of previously saved requests.
    calculation_precision: str | None = None
    measurement_band_components: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """

        Checks the selected methods, region dimensions, output dtype and numerical settings.

        Region sizes and the iteration limit must be positive integers, and tolerance
        must be finite and positive. Method and AF values use their supported
        enums so an unrecognized option is caught before processing.

        Raises:
            ValueError: A method, dimension, precision, tolerance or output setting is invalid.
        """

        if not isinstance(self.solver, SolverKind):
            raise ValueError("solver must be a SolverKind")
        if self.spectral_state_override is not None and not isinstance(
            self.spectral_state_override,
            SpectralState,
        ):
            raise ValueError("spectral state override must be a SpectralState")
        if not isinstance(self.af_behavior, AfBehavior):
            raise ValueError("AF behavior must be an AfBehavior")
        if len(self.chunk_size) != 2 or any(
            not isinstance(value, Integral) or isinstance(value, bool) or value <= 0
            for value in self.chunk_size
        ):
            raise ValueError("chunk size must contain two positive integers")
        if self.calculation_precision not in {None, "float32", "float64"}:
            raise ValueError("calculation precision must be float32 or float64")
        if self.output_dtype not in {"float32", "float64"}:
            raise ValueError("output dtype must be float32 or float64")
        if (
            isinstance(self.nnls_tolerance, bool)
            or not math.isfinite(self.nnls_tolerance)
            or self.nnls_tolerance <= 0
        ):
            raise ValueError("NNLS tolerance must be finite and positive")
        if (
            not isinstance(self.nnls_max_iterations, Integral)
            or isinstance(self.nnls_max_iterations, bool)
            or self.nnls_max_iterations <= 0
        ):
            raise ValueError("NNLS maximum iterations must be positive")
        if not isinstance(self.overwrite, bool):
            raise ValueError("overwrite must be boolean")
        if self.spectral_state_override == SpectralState.UNKNOWN:
            raise ValueError("spectral state override cannot be unknown")
        if (type(self.measurement_band_components) is not tuple
                or any(type(index) is not int or index < 0 for index in self.measurement_band_components)):
            raise ValueError("Measurement-band mapping must contain component row indices")
        if self.measurement_band_components and self.spectral_state_override is not SpectralState.ISOLATED:
            raise ValueError("Measurement-band mapping requires the isolated-signals choice")


@dataclass(frozen=True, slots=True)
class ValidatedUnmixingInput:
    """

    Records the checked image geometry, measurement order and decision to run or skip unmixing.

    Attributes:
        image_index: Selected OME image position.
        spectral_state: Resolved RAW or UNMIXED state.
        state_source: Metadata or user choice establishing that state.
        skip_unmixing: Whether the source already contains the required components.
        size_x: Full-resolution width in pixels.
        size_y: Full-resolution height in pixels.
        input_band_count: Number of source measurement or component channels.
        component_count: Number of selected output components.
        band_indices: Ordered source-channel positions to read.
        acquisition: Per-band imaging settings required for raw processing.
        config: The selected processing configuration.
        imaging_cycles: Complete zero-based band-cycle mapping when recorded; empty otherwise.
    """

    image_index: int
    spectral_state: SpectralState
    state_source: str
    skip_unmixing: bool
    size_x: int
    size_y: int
    input_band_count: int
    component_count: int
    band_indices: tuple[int, ...]
    acquisition: UnmixingInputMetadata | None
    config: UnmixingConfig
    imaging_cycles: tuple[int, ...] = ()


def validate_unmixing_input(
    ome: OME,
    image_index: int,
    acquisition: UnmixingInputMetadata | None,
    library: SpectralLibrary,
    config: UnmixingConfig,
    *,
    canonical_component_order: tuple[str, ...] | None = None,
) -> ValidatedUnmixingInput:
    """

    Resolves the source's spectral state and checks the matching processing requirements.

    Require one Z plane and time point and unique channel IDs covering the image's
    channel count. A known acquisition state must agree with any explicit state choice.
    Unknown state requires the researcher to choose raw, unmixed or abort.

    For already-unmixed data, compare the saved component order and return a skip
    instruction. For raw data, require complete acquisition vectors and exact agreement
    between measurement-band order and reference-matrix rows. The selected columns must
    be independent and cannot outnumber the bands. Check positive exposure/gain, square
    binning area and the offset source.

    Args:
        ome: Metadata for the selected source image.
        image_index: Image position to inspect.
        acquisition: Recorded imaging settings, when available.
        library: Selected spectral-component columns and their band order.
        config: Requested unmixing method, state choice and processing settings.
        canonical_component_order: Saved component order required for already-unmixed data.

    Returns:
        A ValidatedUnmixingInput with the processing decision and ordered source inputs.

    Raises:
        UnmixingValidationError: Geometry, state, band/component order, acquisition
            values or reference-matrix dimensions/rank are incompatible.
    """

    if image_index < 0 or image_index >= len(ome.images):
        raise UnmixingValidationError(f"OME image index out of range: {image_index}")
    image = ome.images[image_index]
    pixels = image.pixels
    if pixels.size_z != 1 or pixels.size_t != 1:
        raise UnmixingValidationError("spectral unmixing currently requires SizeZ=1 and SizeT=1")
    channel_ids = tuple(channel.id for channel in pixels.channels)
    if (
        len(channel_ids) != pixels.size_c
        or any(not channel_id.strip() for channel_id in channel_ids)
        or len(channel_ids) != len(set(channel_ids))
    ):
        raise UnmixingValidationError("OME channels must provide a complete immutable ID order")
    channel_count = len(channel_ids)

    metadata_state = (
        SpectralState(acquisition.spectral_state) if acquisition else SpectralState.UNKNOWN
    )
    override = config.spectral_state_override
    if override == SpectralState.ABORT:
        raise UnmixingValidationError("spectral state resolution was aborted")

    if override is SpectralState.ISOLATED:
        from mif_pipeline.io.metadata.spectral import resolve_prepared_channel_order, _resolve_source_spectral_state, resolve_spectral_state
        from mif_pipeline.io.metadata.measurement_band_copy import validate_measurement_band_mapping

        try:
            validate_measurement_band_mapping(ome, config.measurement_band_components)
            order = resolve_prepared_channel_order(ome, image_index)
            recorded, _ = _resolve_source_spectral_state(ome, image_index,
                spectral=resolve_spectral_state(ome, image_index), acquisition=acquisition)
        except (ValueError, MetadataCanonicalizationError) as exc:
            raise UnmixingValidationError(str(exc)) from exc
        if order is None or order != library.component_names or len(order) != channel_count:
            raise UnmixingValidationError("Confirm one component for every isolated measurement band")
        return ValidatedUnmixingInput(
            image_index=image_index, spectral_state=SpectralState(recorded),
            state_source="user_confirmed_measurement_band_isolation", skip_unmixing=True,
            size_x=pixels.size_x, size_y=pixels.size_y, input_band_count=channel_count,
            component_count=channel_count, band_indices=tuple(range(channel_count)),
            acquisition=acquisition, config=config,
        )

    # A researcher can resolve missing state, but cannot turn known raw
    # measurements into components merely by selecting the copy route.
    if metadata_state != SpectralState.UNKNOWN:
        if override is not None and override != metadata_state:
            raise UnmixingValidationError(
                f"spectral state override {override.value} conflicts with "
                f"metadata {metadata_state.value}"
            )
        state = metadata_state
        state_source = "metadata"
    elif override is not None:
        state = override
        state_source = "user_override"
    else:
        raise UnmixingValidationError(
            "spectral state is unknown; provide an explicit raw/unmixed override"
        )

    # Check the saved component order before taking the already-unmixed path.
    if state == SpectralState.UNMIXED:
        if channel_count != library.component_count:
            raise UnmixingValidationError(
                "unmixed image channel count is incompatible with spectral components"
            )
        if canonical_component_order is None:
            raise UnmixingValidationError(
                "unmixed image requires canonical component order metadata"
            )
        if canonical_component_order != library.component_names:
            raise UnmixingValidationError(
                "unmixed image component order is incompatible with the spectral library"
            )
        # Prior background subtraction does not remove the identity of a retained
        # AF output channel. Copy every declared component without another fit.
        return ValidatedUnmixingInput(
            image_index=image_index,
            spectral_state=state,
            state_source=state_source,
            skip_unmixing=True,
            size_x=pixels.size_x,
            size_y=pixels.size_y,
            input_band_count=channel_count,
            component_count=library.component_count,
            band_indices=tuple(range(channel_count)),
            acquisition=acquisition,
            config=config,
        )

    if acquisition is None:
        raise UnmixingValidationError("raw unmixing requires canonical image metadata")
    from mif_pipeline.io.metadata.cycles import resolve_channel_cycles
    from .cyclic import cycle_solve_groups
    try:
        imaging_cycles = resolve_channel_cycles(ome, image_index, require_complete=True)
        cycle_solve_groups(imaging_cycles, library.component_names, library.matrix)
    except (ValueError, MetadataConflictError) as exc:
        raise UnmixingValidationError(str(exc)) from exc
    if (
        library.component_count > library.band_count
        or np.linalg.matrix_rank(library.matrix) != library.component_count
    ):
        raise UnmixingValidationError(
            "raw unmixing requires a selected spectral basis with no more "
            "components than bands and full column rank"
        )
    if acquisition.image_index != image_index:
        raise UnmixingValidationError("canonical image metadata belongs to another image")
    if len(acquisition.band_order) != channel_count:
        raise UnmixingValidationError(
            "canonical measurement band order conflicts with channel topology"
        )

    # Each matrix row describes one exact acquired band. Reordering by
    # alphabetical display names could silently fit the wrong measurement.
    if acquisition.band_order != library.band_names:
        raise UnmixingValidationError(
            "canonical measurement band order must exactly match spectral-library row order"
        )
    count = channel_count
    for field_name in (
        "exposure_seconds",
        "gain",
        "binning",
        "binning_area",
        "offsets",
    ):
        values = getattr(acquisition, field_name)
        if len(values) != count:
            raise UnmixingValidationError(
                f"{field_name} has {len(values)} values for {count} measurement bands"
            )
    if any((value is None and acquisition.correction_mode != "as_stored") or
           (value is not None and (not math.isfinite(value) or value <= 0))
           for value in acquisition.exposure_seconds):
        raise UnmixingValidationError("Applied exposure times must be finite and positive")
    if acquisition.correction_mode not in {"full", "exposure_only", "as_stored"}:
        raise UnmixingValidationError("Unsupported intensity correction mode")
    if (acquisition.correction_mode == "as_stored") != (acquisition.intensity_representation == "normalized_counts"):
        raise UnmixingValidationError("Known normalized values require metadata-driven as-stored fitting")
    message = raw_intensity_error(acquisition.intensity_representation)
    if message is not None:
        raise UnmixingValidationError(message)
    if acquisition.correction_mode == "full":
        required = (acquisition.gain, acquisition.binning, acquisition.binning_area)
        if acquisition.intensity_representation != "dark_corrected_counts":
            required += (acquisition.offsets,)
        if any(value is None for vector in required for value in vector):
            raise UnmixingValidationError("Full intensity correction requires every applied gain, binning and offset value")
    if any(value is not None and (not math.isfinite(value) or value <= 0) for value in acquisition.gain):
        raise UnmixingValidationError("gain values must be finite and positive")
    if any(value is not None and value <= 0 for value in acquisition.binning_area):
        raise UnmixingValidationError("binning area values must be positive")
    try:
        validate_offset_specification(
            tuple(value for value in acquisition.offsets if value is not None),
            acquisition.offset_authority,
            acquisition.offset_profile_identifier,
        )
    except MetadataConflictError as exc:
        raise UnmixingValidationError(str(exc)) from exc

    # Acquisition binning is a square side length: 2-by-2 combines four
    # acquisition pixels. It is unrelated to this run's processing block size.
    expected_areas = tuple(None if value is None else value * value for value in acquisition.binning)
    if acquisition.binning_area != expected_areas:
        raise UnmixingValidationError(
            "binning area must equal the square of each detector binning value"
        )
    if bool(library.autofluorescence_components) != (config.af_behavior is AfBehavior.RETAIN):
        raise UnmixingValidationError(
            "The accepted reference AF components differ from the reviewed target AF choice"
        )
    return ValidatedUnmixingInput(
        image_index=image_index,
        spectral_state=state,
        state_source=state_source,
        skip_unmixing=False,
        size_x=pixels.size_x,
        size_y=pixels.size_y,
        input_band_count=count,
        component_count=library.component_count,
        band_indices=tuple(range(count)),
        acquisition=acquisition,
        config=config,
        imaging_cycles=imaging_cycles or (),
    )
