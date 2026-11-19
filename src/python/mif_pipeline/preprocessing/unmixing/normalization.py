"""

Corrects spectral measurements for the imaging settings used to collect each band.

A stored intensity depends on exposure time, detector gain, acquisition binning
and digital range. The correction subtracts the detector offset, divides by those
acquisition factors, and applies the selected response multiplier:

    corrected = (raw - offset) / (maximum * gain * binning_area * exposure) * response

Exposure is expressed in seconds, and binning_area is the number of acquisition
pixels combined into one bin. For 2-by-2 binning it is 4. Factors are prepared once
per image and reused as each region is read, with arrays ordered as bands × rows × columns.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import NDArray

from mif_pipeline.io import (
    EffectiveDigitalRange,
    MetadataConflictError,
    UnmixingInputMetadata,
    validate_offset_specification,
)

from mif_pipeline.io.metadata.canonical import applied_detector_offset

# ==============================================================================
#                                 Normalization
# ==============================================================================


class NormalizationError(ValueError):
    """

    Reports acquisition values or pixel-array dimensions that prevent the correction from being applied.
    """

@dataclass(frozen=True, slots=True)
class ResponseCorrectionTransform:
    """

    Records a response multiplier for each measurement band.

    The multiplier is applied after the intensity correction. Its position in the
    tuple must match the corresponding measurement ID and pixel-array band.

    Attributes:
        transform_id: Name identifying this correction.
        semantic: "multiply_normalized_measurement", the supported operation.
        authority: Recorded source or policy that supplied the correction.
        measurement_ids: Ordered identifiers of the affected measurement bands.
        multipliers: One finite positive float per measurement; 1.0 leaves its value as is.
    """

    transform_id: str
    semantic: str
    authority: str
    measurement_ids: tuple[str, ...]
    multipliers: tuple[float, ...]

    def __post_init__(self) -> None:
        """

        Checks that the response correction names unique measurements and supplies one
        positive finite multiplier for each of them.

        Raises:
            NormalizationError: Identifiers, operation, measurement coverage or multipliers are invalid.
        """

        for value, label in (
            (self.transform_id, "response transform ID"),
            (self.authority, "response transform authority"),
        ):
            if type(value) is not str or not value or value.strip() != value:
                raise NormalizationError(f"{label} must be nonempty canonical text")
        if self.semantic != "multiply_normalized_measurement":
            raise NormalizationError(
                "response transform semantic must be multiply_normalized_measurement"
            )
        if (
            type(self.measurement_ids) is not tuple
            or not self.measurement_ids
            or any(
                type(item) is not str or not item or item.strip() != item
                for item in self.measurement_ids
            )
            or len(self.measurement_ids) != len(set(self.measurement_ids))
        ):
            raise NormalizationError(
                "response measurement IDs must be a nonempty unique canonical tuple"
            )
        if (
            type(self.multipliers) is not tuple
            or len(self.multipliers) != len(self.measurement_ids)
            or any(
                type(value) is not float or not math.isfinite(value) or value <= 0.0
                for value in self.multipliers
            )
        ):
            raise NormalizationError(
                "response correction multipliers must be exact finite positive floats "
                "aligned with measurement IDs"
            )

    @classmethod
    def identity(
        cls,
        measurement_ids: tuple[str, ...],
    ) -> "ResponseCorrectionTransform":
        """

        Creates the explicit default response correction with a multiplier of 1.0 for every band.

        Args:
            measurement_ids: Measurement-band identifiers in pixel-reading order.

        Returns:
            A checked ResponseCorrectionTransform whose multipliers are all 1.0.
        """

        return cls(
            transform_id="mif_pipeline_identity_response_correction_v1",
            semantic="multiply_normalized_measurement",
            authority="mif_pipeline_legacy_library_path_identity_v1",
            measurement_ids=measurement_ids,
            multipliers=(1.0,) * len(measurement_ids),
        )


@dataclass(frozen=True, slots=True)
class NormalizationFactors:
    """

    Stores the per-band arrays reused while correcting successive image regions.

    The factor builder creates three float32 vectors: detector offsets, acquisition
    divisors and response multipliers. Their order follows the acquired bands. It marks
    them read-only because every region of the same image uses the same correction.

    Attributes:
        policy: Versioned name of the correction rule.
        formula: Recorded expression for subtraction, division and multiplication.
        effective_maximum: Meaningful stored-intensity maximum; applied only in full correction.
        digital_range_authority: Metadata field or profile supplying that maximum.
        offsets: Applied detector-offset vector, or None for exposure-only/as-stored processing.
            None leaves recorded pixels unchanged before division; it is not a zero baseline.
        offset_authority: Source of the recorded offset values.
        per_band_divisor: Maximum × gain × binning area × exposure seconds in full
            correction, or actual exposure seconds alone in exposure-only correction.
        response_transform_id: Name of the selected response correction.
        response_transform_semantic: Operation applied to the normalized measurements.
        response_transform_authority: Source or policy supplying that correction.
        response_measurement_ids: Band identifiers associated with the response values.
        response_correction_multipliers: Original float values retained in the saved settings.
        per_band_response_multiplier: Float32 response vector used for pixel calculations.
    """

    policy: str
    formula: str
    effective_maximum: float | None
    digital_range_authority: str
    offsets: NDArray[np.float32] | None
    offset_authority: str
    per_band_divisor: NDArray[np.float32]
    response_transform_id: str
    response_transform_semantic: str
    response_transform_authority: str
    response_measurement_ids: tuple[str, ...]
    response_correction_multipliers: tuple[float, ...]
    per_band_response_multiplier: NDArray[np.float32]


class CanonicalOffsetExposureNormalization:
    """

    Prepares the intensity correction used before spectral fitting.

    Full correction subtracts offset, divides by meaningful digital maximum, gain,
    binning area and exposure, then applies the selected response multiplier.
    Exposure-only correction divides by actual exposure with every other recorded
    setting unapplied. Metadata-known exposure-inclusive calibrated values are used as
    stored. Preparing the selected factors once reuses them for each region.

    Attributes:
        policy: Identifier recorded with the applied normalization settings.
        formula: Written expression describing the correction order.
    """

    policy = "mif_pipeline_offset_exposure_v1"
    formula = (
        "Y_star[b] = (Y_raw[b] - applied_offset[b]) / (effective_maximum * "
        "gain[b] * binning_area[b] * exposure_seconds[b]) * "
        "response_correction_multiplier[b]"
    )

    def build_factors(
        self,
        acquisition: UnmixingInputMetadata,
        digital_range: EffectiveDigitalRange | None,
        *,
        response_transform: ResponseCorrectionTransform | None = None,
    ) -> NormalizationFactors:
        """

        Builds the offset, divisor and response vectors for the acquired spectral bands.

        Exposure-only correction requires a positive actual exposure per band and
        prohibits response calibration. Full correction requires complete per-band
        acquisition vectors with positive finite gain, exposure and binning area.
        Recorded offsets are validated and retained. Metadata-known dark-corrected
        counts use zero applied offsets, including when the original setting is absent.
        Known exposure-inclusive calibrated values use identity factors and no range.

        Form the divisor in float64, then convert the working vectors to float32. Check
        that this conversion neither creates infinity nor reduces a positive divisor or
        response value to zero. The resulting vectors are reused for all regional reads.

        Args:
            acquisition: Ordered imaging settings for the selected image.
            digital_range: Resolved meaningful intensity range supplying the maximum divisor term.
            response_transform: Optional response correction in acquisition-band order.
                None uses multipliers of 1.0.

        Returns:
            NormalizationFactors containing the working arrays and descriptions needed
            to record how intensities were corrected.

        Raises:
            NormalizationError: Acquisition coverage, values, offset policy or float32
                conversion is invalid.
            TypeError: The supplied response correction has an unsupported type.
        """

        # Check vector lengths before multiplying: each position must describe
        # the same acquired spectral band across all four quantities.
        count = len(acquisition.band_order)
        if acquisition.correction_mode == "as_stored":
            if acquisition.intensity_representation != "normalized_counts" or digital_range is not None:
                raise NormalizationError("As-stored fitting requires known normalized units and no applied digital range")
            response_transform = response_transform or ResponseCorrectionTransform.identity(acquisition.band_order)
            if type(response_transform) is not ResponseCorrectionTransform or response_transform.multipliers != (1.0,) * count:
                raise NormalizationError("As-stored fitting cannot apply another response correction")
            identity = np.ones(count, dtype=np.float32)
            identity.setflags(write=False)
            return NormalizationFactors(
                policy="mif_pipeline_as_stored_v1", formula="Y_star[b] = Y_raw[b]",
                effective_maximum=None, digital_range_authority="not_applied",
                offsets=None, offset_authority="not_applied", per_band_divisor=identity,
                response_transform_id=response_transform.transform_id,
                response_transform_semantic=response_transform.semantic,
                response_transform_authority=response_transform.authority,
                response_measurement_ids=response_transform.measurement_ids,
                response_correction_multipliers=response_transform.multipliers,
                per_band_response_multiplier=identity,
            )
        if acquisition.intensity_representation == "normalized_counts":
            raise NormalizationError("Already-normalized intensities cannot receive intensity correction again")
        if digital_range is None:
            raise NormalizationError("Intensity correction requires a known digital range")
        if acquisition.correction_mode == "exposure_only":
            if len(acquisition.exposure_seconds) != count:
                raise NormalizationError("exposure band count mismatch")
            _finite_positive(acquisition.exposure_seconds, "exposure")
            if response_transform is None:
                response_transform = ResponseCorrectionTransform.identity(acquisition.band_order)
            if type(response_transform) is not ResponseCorrectionTransform or response_transform.multipliers != (1.0,) * count:
                raise NormalizationError("Exposure-only correction cannot apply a response calibration")
            with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                divisor = np.asarray(acquisition.exposure_seconds, dtype=np.float32)
            if not np.all(np.isfinite(divisor)) or np.any(divisor <= 0):
                raise NormalizationError("Exposure must remain finite and positive in float32")
            divisor.setflags(write=False)
            multipliers = np.ones(count, dtype=np.float32)
            multipliers.setflags(write=False)
            return NormalizationFactors(
                policy="mif_pipeline_exposure_only_v1", formula="Y_star[b] = Y_raw[b] / exposure_seconds[b]",
                effective_maximum=digital_range.maximum, digital_range_authority=digital_range.authority.value,
                offsets=None, offset_authority="not_applied", per_band_divisor=divisor,
                response_transform_id=response_transform.transform_id,
                response_transform_semantic=response_transform.semantic,
                response_transform_authority=response_transform.authority,
                response_measurement_ids=response_transform.measurement_ids,
                response_correction_multipliers=response_transform.multipliers,
                per_band_response_multiplier=multipliers,
            )
        if acquisition.correction_mode != "full":
            raise NormalizationError("Unsupported intensity correction mode")
        if len(acquisition.exposure_seconds) != count:
            raise NormalizationError("exposure band count mismatch")
        if len(acquisition.gain) != count:
            raise NormalizationError("gain band count mismatch")
        if len(acquisition.binning_area) != count:
            raise NormalizationError("binning area band count mismatch")
        if len(acquisition.offsets) != count:
            raise NormalizationError("offset band count mismatch")
        _finite_positive(acquisition.exposure_seconds, "exposure")
        _finite_positive(acquisition.gain, "gain")
        _finite_positive(acquisition.binning_area, "binning area")
        try:
            validate_offset_specification(
                tuple(value for value in acquisition.offsets if value is not None),
                acquisition.offset_authority,
                acquisition.offset_profile_identifier,
            )
        except MetadataConflictError as exc:
            raise NormalizationError(str(exc)) from exc
        if not math.isfinite(digital_range.maximum) or digital_range.maximum <= 0:
            raise NormalizationError("effective digital maximum must be finite and positive")
        if response_transform is None:
            response_transform = ResponseCorrectionTransform.identity(acquisition.band_order)
        elif type(response_transform) is not ResponseCorrectionTransform:
            raise TypeError("response_transform must be exact ResponseCorrectionTransform")
        if len(response_transform.measurement_ids) != count:
            raise NormalizationError("response transform band count mismatch")

        # Form the acquisition product at higher precision, then check the
        # float32 values that will actually be used for regional pixel reads.
        gain = np.asarray(acquisition.gain, dtype=np.float64)
        area = np.asarray(acquisition.binning_area, dtype=np.float64)
        exposure = np.asarray(acquisition.exposure_seconds, dtype=np.float64)

        # Suppress conversion warnings here because the checks below turn
        # overflow and underflow into specific normalization errors.
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            offsets = np.asarray(
                [applied_detector_offset(value, acquisition.intensity_representation)
                 for value in acquisition.offsets], dtype=np.float32,
            )
            divisor = np.asarray(
                digital_range.maximum * gain * area * exposure,
                dtype=np.float32,
            )
            multipliers = np.asarray(
                response_transform.multipliers,
                dtype=np.float32,
            )
        if not np.all(np.isfinite(offsets)):
            raise NormalizationError("offset values must remain finite after float32 conversion")
        if not np.all(np.isfinite(divisor)):
            raise NormalizationError(
                "normalization divisor must remain finite after float32 conversion"
            )
        if np.any(divisor <= 0):
            raise NormalizationError(
                "normalization divisor must remain positive after float32 conversion"
            )
        if not np.all(np.isfinite(multipliers)):
            raise NormalizationError(
                "response correction multipliers must remain finite after float32 conversion"
            )
        if np.any(multipliers <= 0):
            raise NormalizationError(
                "response correction multipliers must remain positive after float32 conversion"
            )

        # Reuse these arrays for every region without allowing an ordinary
        # NumPy assignment to change the factors midway through the image.
        offsets.setflags(write=False)
        divisor.setflags(write=False)
        multipliers.setflags(write=False)
        return NormalizationFactors(
            policy=self.policy,
            formula=self.formula,
            effective_maximum=digital_range.maximum,
            digital_range_authority=digital_range.authority.value,
            offsets=offsets,
            offset_authority=acquisition.offset_authority.value,
            per_band_divisor=divisor,
            response_transform_id=response_transform.transform_id,
            response_transform_semantic=response_transform.semantic,
            response_transform_authority=response_transform.authority,
            response_measurement_ids=response_transform.measurement_ids,
            response_correction_multipliers=response_transform.multipliers,
            per_band_response_multiplier=multipliers,
        )


def normalize_region(
    raw: NDArray[np.generic],
    factors: NormalizationFactors,
) -> NDArray[np.float32]:
    """

    Applies the prepared intensity correction to one band-by-row-by-column pixel region.

    Convert the input to float32. Full correction subtracts the detector offset,
    preserving negative corrected values below its baseline. Exposure-only copies
    the converted pixels without offset subtraction. Both divide by their prepared
    divisor; the exposure-only divisor contains actual exposure seconds alone.
    As-stored fitting copies the converted values without intensity correction.

    Each per-band vector is indexed with [:, None, None], giving shape (bands, 1, 1).
    NumPy then applies that band's value across all its rows and columns. This performs
    the correction without constructing a full-size factor image for every band.
    Subtraction converts the input directly into an independent working array;
    routes without subtraction make one converted copy. This avoids retaining a
    second region-sized float32 array for integer or float64 input. Division and
    response correction reuse the working array in place. Source pixels and shared
    factors remain unchanged.

    Args:
        raw: Pixel array with shape (bands, height, width), in acquisition order.
        factors: The image's prepared offsets, acquisition divisors and response multipliers.

    Returns:
        An independent float32 array of corrected measurements with the same shape as raw.

    Raises:
        NormalizationError: The regional shape, band count or response vector is invalid.
    """

    array = np.asarray(raw)
    if array.ndim != 3:
        raise NormalizationError("raw region must have shape (bands, y, x)")
    if array.shape[0] != factors.per_band_divisor.size:
        raise NormalizationError(
            f"raw region band count {array.shape[0]} does not match "
            f"normalization band count {factors.per_band_divisor.size}"
        )
    if factors.policy == "mif_pipeline_as_stored_v1":
        return array.astype(np.float32, copy=True)
    response = np.asarray(factors.per_band_response_multiplier)
    if (
        response.dtype != np.dtype("float32")
        or response.shape != (array.shape[0],)
        or not np.all(np.isfinite(response))
        or np.any(response <= 0.0)
    ):
        raise NormalizationError(
            "response correction requires one finite positive float32 multiplier "  "per input band"
        )

    # Floating-point subtraction preserves values below the detector offset.
    # It also prevents unsigned integer wraparound during subtraction.
    corrected = (
        array.astype(np.float32, copy=True) if factors.offsets is None else np.subtract(
            array, factors.offsets[:, None, None], dtype=np.float32,
        )
    )

    # The two singleton axes broadcast one divisor over each entire band.
    np.divide(
        corrected,
        factors.per_band_divisor[:, None, None],
        dtype=np.float32,
        out=corrected,
    )

    # Apply the selected response multiplier after offset subtraction and acquisition division.
    np.multiply(
        corrected,
        response[:, None, None],
        dtype=np.float32,
        out=corrected,
    )
    return corrected


def _finite_positive(values: tuple[float, ...] | tuple[int, ...], label: str) -> None:
    """

    Requires every acquisition value to be finite and greater than zero.

    Args:
        values: Values of one per-band acquisition quantity.
        label: Quantity name included in an error message.

    Raises:
        NormalizationError: A value is zero, negative, NaN or infinite.
    """

    if any(not math.isfinite(float(value)) or value <= 0 for value in values):
        raise NormalizationError(f"{label} values must be finite and positive")
