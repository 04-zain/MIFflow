"""

Collects reviewed image and acquisition inputs for spectral-reference calculations.

We use the selected image's channel order, digital range, offset, exposure,
gain and binning area to define its normalized measurement bands. Reference
controls are compared with the target image using their instrument, optical
path, detector mode and response-calibration information before their spectra
can be combined.

Inputs can come from a standardized OME-TIFF or directly from an inspected
vendor image with completed OME review. Both routes produce the same
channel/normalization description. The vendor route retains the selected
original series and reviewed metadata so later regional reads use the
original pixels without an intermediate raw TIFF conversion.

The records also retain the source file location and its observed file
identity. Reopening checks those values against the reviewed input before
and after reading. Image/normalization content is identified separately from
its location, allowing an explicitly checked relocation of identical files.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.io.metadata.canonical import is_autofluorescence_component

from dataclasses import dataclass, field, fields, is_dataclass
from contextlib import contextmanager
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np
from ome_types import OME, from_xml, to_xml

from mif_pipeline.io import (
    BioFormatsBackend,
    BioFormatsSource,
    CanonicalOmeTiffError,
    CanonicalOmeTiffHeader,
    DigitalRangeAuthority,
    EffectiveDigitalRange,
    InspectedSourceAuthority,
    OpenedSourceIdentity,
    SourceImageFacts,
    UnmixingInputMetadata,
    open_reviewed_source,
    resolve_acquisition_digital_range,
    resolve_source_image_facts,
    resolve_unmixing_input_metadata,
    inspect_canonical_ome_tiff,
    resolve_component_identity_inventory,
    resolve_channel_metadata,
)

from .reference_models import (
    QuantitativeMeasurementTransform,
    QuantitativeTransformSignature,
    ReferenceInputEvidence,
    SpectralMeasurement,
    SpectralMeasurementAxis,
    SpectralOpticalSetting,
    _record_fields,
    StructuralCompatibilitySignature,
)
from .compatibility import (
    CompatibilityFieldReport,
    QuantitativeTransformabilityReport,
    StructuralCompatibilityReport,
    compare_quantitative_transformability,
    compare_structural_compatibility,
)

# ==============================================================================
#                           Canonical reference input
# ==============================================================================


class CanonicalReferenceInputError(ValueError):
    """

    Reports image, acquisition or reference-target settings that fail their required comparisons.
    """

_CANONICAL_INPUT_PROOF = object()
_IDENTITY_RESPONSE_TRANSFORM_ID = "canonical_identity_response_v1"
_IDENTITY_RESPONSE_TRANSFORM_AUTHORITY = "canonical_ome_tiff_identity_response_v1"
_IDENTITY_RESPONSE_TRANSFORM_SEMANTIC = "multiply_normalized_measurement"


def _text(value: object, label: str) -> str:
    """

    Checks a required image, channel or configuration identifier as nonempty text.

    Args:
        value: Literal identifier being read from a saved record.
        label: Field name included in the error.

    Returns:
        String with no surrounding whitespace.

    Raises:
        CanonicalReferenceInputError: The value is not text, is empty or includes
            surrounding whitespace that would change its identifier.
    """

    if type(value) is not str or not value or value != value.strip():
        raise CanonicalReferenceInputError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    """

    Checks the format of a recorded file or settings checksum.

    Args:
        value: Expected lowercase 64-character hexadecimal SHA-256 text.
        label: Checksum field included in the error.

    Returns:
        Checked checksum text for comparison with the corresponding contents.

    Raises:
        CanonicalReferenceInputError: The checksum type, length or characters are invalid.
    """

    result = _text(value, label)
    if len(result) != 64 or any(item not in "0123456789abcdef" for item in result):
        raise CanonicalReferenceInputError(f"{label} must be a lowercase SHA-256")
    return result


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    """

    Checks a count or index before it is used to interpret the selected image.

    Args:
        value: Integer count or index; booleans are rejected.
        label: Field name used in the error.
        positive: True when zero is invalid, such as an image dimension.

    Returns:
        Checked nonnegative or positive integer.

    Raises:
        CanonicalReferenceInputError: The type or permitted sign is invalid.
    """

    if type(value) is not int or value < 0 or (positive and value == 0):
        requirement = "positive" if positive else "nonnegative"
        raise CanonicalReferenceInputError(f"{label} must be a {requirement} integer")
    return value


def _authority_value(value: object) -> object:
    """

    Converts the recorded source and normalization values into a repeatable JSON description.

    We retain integers, strings, booleans and missing values directly. Floating
    values are written as hexadecimal text so the complete stored value is
    retained when checksums are calculated. Enums include their type and value;
    dataclasses include their type and each declared field. Dictionary keys are
    sorted, and tuples retain their original element order.

    Args:
        value: Supported scalar, enum, tuple, dictionary or dataclass used by
            the reviewed reference input.

    Returns:
        JSON-compatible values representing the complete supplied information.

    Raises:
        CanonicalReferenceInputError: A float is nonfinite, dictionary keys are
            not strings, or a supplied type is unsupported.
    """

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not float("-inf") < value < float("inf"):
            raise CanonicalReferenceInputError("authority contains nonfinite float")
        return {"float_hex": value.hex()}
    if isinstance(value, Enum):
        return {
            "enum_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _authority_value(value.value),
        }
    if type(value) is tuple:
        return [_authority_value(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise CanonicalReferenceInputError("authority mapping keys must be text")
        return {key: _authority_value(value[key]) for key in sorted(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "record_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                field.name: _authority_value(getattr(value, field.name)) for field in _record_fields(value)
            },
        }
    raise CanonicalReferenceInputError(
        f"unsupported canonical-input authority value {type(value).__name__}"
    )


def _authority_digest(value: object) -> str:
    """

    Calculates a checksum of the complete reviewed values using their stable JSON representation.

    Args:
        value: Source, acquisition or compatibility fields to identify together.

    Returns:
        Lowercase SHA-256 of the sorted, compact UTF-8 JSON produced from
        _authority_value().
    """

    return hashlib.sha256(
        json.dumps(
            _authority_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalReferenceMeasurementPlane:
    """

    Links one normalized measurement band to its selected source channel.

    Attributes:
        measurement_id: Identifier used by the spectral measurement order.
        channel_id: Corresponding OME Channel ID.
        channel_index: Zero-based channel position used for image-region reads.
    """

    measurement_id: str
    channel_id: str
    channel_index: int

    def __post_init__(self) -> None:
        """

        Checks the measurement/channel identifiers and nonnegative channel position.

        Raises:
            CanonicalReferenceInputError: An identifier is missing or the index is invalid.
        """

        _text(self.measurement_id, "measurement ID")
        _text(self.channel_id, "canonical channel ID")
        _integer(self.channel_index, "canonical channel index")


@dataclass(frozen=True, slots=True)
class CanonicalReferenceHeader:
    """

    Retains the selected image's dimensions, pixel storage and physical calibration.

    We obtain these values from the common image-reading checks before reference
    sampling. The native resolution dimensions remain in their recorded order,
    beginning with the full-resolution image. File location is stored separately
    so the same quantitative image can be checked after an explicit relocation.

    Attributes:
        source_file_sha256: Checksum of the complete selected source file.
        ome_image_id: OME Image ID for the selected analytical image.
        ome_pixels_id: Its OME Pixels ID.
        size_x: Full-resolution width in pixels.
        size_y: Full-resolution height in pixels.
        size_z: Number of Z positions.
        size_c: Number of measurement channels.
        size_t: Number of time positions.
        dimension_order: Recorded OME plane-dimension order.
        container_dtype: Decoded NumPy scalar pixel type.
        container_bits: Storage width associated with that dtype.
        significant_bits: Meaningful-bit declaration from OME, when available.
        physical_size_x: Original physical pixel-width value.
        physical_size_x_unit: Unit attached to that width.
        physical_size_y: Original physical pixel-height value.
        physical_size_y_unit: Unit attached to that height.
        native_resolution_dimensions: Ordered (width, height) pairs for the
            selected image's stored resolution levels.
    """

    source_file_sha256: str
    ome_image_id: str
    ome_pixels_id: str
    size_x: int
    size_y: int
    size_z: int
    size_c: int
    size_t: int
    dimension_order: str
    container_dtype: str
    container_bits: int
    significant_bits: int | None
    physical_size_x: float | None
    physical_size_x_unit: str | None
    physical_size_y: float | None
    physical_size_y_unit: str | None
    native_resolution_dimensions: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        """

        Checks the recorded image dimensions, storage counts and native-level sizes.

        All X/Y/Z/C/T dimensions must be positive integers. SignificantBits, when
        provided, must fit inside the pixel container. A physical size must be a
        positive finite float with its optional recorded unit text checked separately.

        The first native level must have the full-resolution width and height. Later
        levels cannot increase either dimension. The source-reading functions perform
        their additional native-pyramid and channel checks before creating this record.

        Raises:
            CanonicalReferenceInputError: An identifier, bit count, dimension, physical
                size or ordered resolution pair is invalid.
        """

        _sha256(self.source_file_sha256, "canonical source SHA-256")
        _text(self.ome_image_id, "canonical OME Image ID")
        _text(self.ome_pixels_id, "canonical OME Pixels ID")
        for name in ("size_x", "size_y", "size_z", "size_c", "size_t"):
            _integer(getattr(self, name), name, positive=True)
        _text(self.dimension_order, "canonical dimension order")
        _text(self.container_dtype, "canonical container dtype")
        _integer(self.container_bits, "canonical container bits", positive=True)
        if self.significant_bits is not None:
            _integer(self.significant_bits, "OME significant bits", positive=True)
            if self.significant_bits > self.container_bits:
                raise CanonicalReferenceInputError("OME significant bits exceed container bits")
        for value, label in (
            (self.physical_size_x, "physical size X"),
            (self.physical_size_y, "physical size Y"),
        ):
            if value is not None and (
                type(value) is not float or not math.isfinite(value) or value <= 0.0
            ):
                raise CanonicalReferenceInputError(f"{label} must be positive finite float")
        for value, label in (
            (self.physical_size_x_unit, "physical size X unit"),
            (self.physical_size_y_unit, "physical size Y unit"),
        ):
            if value is not None:
                _text(value, label)
        dimensions = self.native_resolution_dimensions
        if type(dimensions) is not tuple or not dimensions:
            raise CanonicalReferenceInputError("native resolution dimensions are invalid")
        previous = (self.size_x, self.size_y)
        for index, pair in enumerate(dimensions):
            if type(pair) is not tuple or len(pair) != 2:
                raise CanonicalReferenceInputError("native resolution dimensions are invalid")
            width, height = pair
            _integer(width, "native resolution X", positive=True)
            _integer(height, "native resolution Y", positive=True)
            if index == 0 and pair != previous:
                raise CanonicalReferenceInputError(
                    "native level zero differs from image dimensions"
                )
            if width > previous[0] or height > previous[1]:
                raise CanonicalReferenceInputError("native resolution dimensions increase")
            previous = pair

    @classmethod
    def from_header(cls, header: CanonicalOmeTiffHeader) -> "CanonicalReferenceHeader":
        """

        Copies the checked standardized-TIFF description into the spectral input record.

        Args:
            header: CanonicalOmeTiffHeader obtained by comparing actual TIFF pages,
                embedded OME metadata and the selected image reader.

        Returns:
            CanonicalReferenceHeader containing its source checksum, dimensions,
            dtype, bit declarations, calibration and native-level dimensions.

        Raises:
            TypeError: header is not the expected checked TIFF-header type.
        """

        if type(header) is not CanonicalOmeTiffHeader:
            raise TypeError("header must be exact CanonicalOmeTiffHeader")
        return cls(
            source_file_sha256=header.sha256,
            ome_image_id=header.image_id,
            ome_pixels_id=header.pixels_id,
            size_x=header.size_x,
            size_y=header.size_y,
            size_z=header.size_z,
            size_c=header.size_c,
            size_t=header.size_t,
            dimension_order=header.dimension_order,
            container_dtype=header.container_dtype,
            container_bits=header.container_bits,
            significant_bits=header.significant_bits,
            physical_size_x=header.physical_size_x,
            physical_size_x_unit=header.physical_size_x_unit,
            physical_size_y=header.physical_size_y,
            physical_size_y_unit=header.physical_size_y_unit,
            native_resolution_dimensions=tuple(
                (item.size_x, item.size_y) for item in header.native_pyramid_levels
            ),
        )

    @classmethod
    def from_source_facts(cls, facts: SourceImageFacts) -> "CanonicalReferenceHeader":
        """

        Copies the reviewed vendor-image facts and native resolution sizes for spectral sampling.

        We request the validated native-pyramid descriptions from the source-reading
        module, then retain their actual dimensions. That module resolves which
        stored levels belong to the analytical image and checks their channel/dtype
        consistency before these values are used for spectral input.

        Args:
            facts: SourceImageFacts for the selected reviewed vendor image.

        Returns:
            CanonicalReferenceHeader with the vendor source's recorded image facts.

        Raises:
            TypeError: facts is not a SourceImageFacts record.
            CanonicalReferenceInputError: Its native levels fail the source-image checks.
        """

        if type(facts) is not SourceImageFacts:
            raise TypeError("source facts must be exact SourceImageFacts")
        # The source-image validator identifies the analytical pyramid
        # and checks each reduced level before these dimensions are retained.
        from mif_pipeline.io import SourceFactsError

        try:
            levels = facts.native_pyramid_levels
        except SourceFactsError as exc:
            raise CanonicalReferenceInputError(str(exc)) from exc
        return cls(
            source_file_sha256=facts.identity.file_sha256,
            ome_image_id=facts.image_id,
            ome_pixels_id=facts.pixels_id,
            size_x=facts.size_x,
            size_y=facts.size_y,
            size_z=facts.size_z,
            size_c=facts.size_c,
            size_t=facts.size_t,
            dimension_order=facts.dimension_order,
            container_dtype=facts.container_dtype,
            container_bits=facts.container_bits,
            significant_bits=facts.significant_bits,
            physical_size_x=facts.physical_size_x,
            physical_size_x_unit=facts.physical_size_x_unit,
            physical_size_y=facts.physical_size_y,
            physical_size_y_unit=facts.physical_size_y_unit,
            native_resolution_dimensions=tuple((item.size_x, item.size_y) for item in levels),
        )


@dataclass(frozen=True, slots=True)
class CanonicalReferenceSourceIdentity:
    """

    Retains file observations used to check the source when it is reopened.

    The checksum identifies file contents. Byte size, modification time, device
    and file ID describe the particular file observed during inspection. They
    are compared when a later operation reopens that recorded location.

    Attributes:
        file_sha256: Checksum recorded for the complete source file.
        size_bytes: File size at inspection.
        modified_time_ns: Recorded modification time in nanoseconds.
        device_id: Filesystem device identity.
        inode_id: File identity supplied by the source-reading implementation.
    """

    file_sha256: str
    size_bytes: int
    modified_time_ns: int
    device_id: int
    inode_id: int

    def __post_init__(self) -> None:
        """

        Checks the source checksum and nonnegative filesystem observations.

        Raises:
            CanonicalReferenceInputError: The checksum or an integer file observation is invalid.
        """

        _sha256(self.file_sha256, "opened source SHA-256")
        for name in ("size_bytes", "modified_time_ns", "device_id", "inode_id"):
            _integer(getattr(self, name), name)

    @classmethod
    def from_opened(cls, identity: OpenedSourceIdentity) -> "CanonicalReferenceSourceIdentity":
        """

        Copies the file observations obtained when the source reader opened the image.

        Args:
            identity: OpenedSourceIdentity associated with the active image reader.

        Returns:
            CanonicalReferenceSourceIdentity containing the same file observations.

        Raises:
            TypeError: The supplied identity has an incompatible type.
        """

        if type(identity) is not OpenedSourceIdentity:
            raise TypeError("source identity must be exact OpenedSourceIdentity")
        return cls(
            file_sha256=identity.file_sha256,
            size_bytes=identity.size_bytes,
            modified_time_ns=identity.modified_time_ns,
            device_id=identity.device_id,
            inode_id=identity.inode_id,
        )


@dataclass(frozen=True)
class CanonicalReferenceLocatorBinding:
    """

    Records where to reopen the selected image and which metadata review to apply.

    Standardized TIFF input reads its reviewed metadata from the image file.
    Direct vendor input instead retains its source-inspection JSON and reviewed
    OME XML with the original file path. These fields allow regional reads to
    recover the selected analytical image and its completed imaging settings.

    Attributes:
        source_locator: Absolute source-image filename.
        source_identity: File observations recorded during inspection or opening.
        source_kind: "canonical_tiff" or "reviewed_source" opening procedure.
        inspected_authority_json: Saved source-inspection JSON for reviewed vendor
            input; None for the standardized-TIFF procedure.
        reviewed_ome_xml: Completed OME metadata for reviewed vendor input.
        binding_sha256: Calculated checksum of this location, file observation
            and opening-procedure description.
    """

    source_locator: str
    source_identity: CanonicalReferenceSourceIdentity
    source_kind: str = "canonical_tiff"
    inspected_authority_json: str | None = None
    reviewed_ome_xml: str | None = None
    binding_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """

        Checks the source location and the metadata needed by its selected opening procedure.

        A standardized TIFF has its own embedded review fields. Reviewed vendor
        input must provide both saved inspection and reviewed OME XML. We parse
        those records and require their original source path and file observations
        to match this opening description, then calculate the combined checksum.

        Raises:
            TypeError: source_identity has an incompatible type.
            CanonicalReferenceInputError: The path, source kind or associated
                inspection/XML records disagree.
        """

        locator = Path(_text(self.source_locator, "canonical source locator"))
        if not locator.is_absolute():
            raise CanonicalReferenceInputError("canonical source locator must be absolute")
        if type(self.source_identity) is not CanonicalReferenceSourceIdentity:
            raise TypeError("canonical locator source identity must be exact")
        # Retain the small component/marker projection from the XML parse already
        # required below. These derived values are not additional saved fields.
        object.__setattr__(self, "_component_inventory", None)
        object.__setattr__(self, "_channel_biomarkers", ())
        if self.source_kind == "canonical_tiff":
            if self.inspected_authority_json is not None or self.reviewed_ome_xml is not None:
                raise CanonicalReferenceInputError(
                    "TIFF binding cannot carry reviewed-source authority"
                )
        elif self.source_kind == "reviewed_source":
            if (
                type(self.inspected_authority_json) is not str
                or type(self.reviewed_ome_xml) is not str
            ):
                raise CanonicalReferenceInputError(
                    "reviewed binding requires inspection and accepted OME"
                )
            try:
                inspection = InspectedSourceAuthority.from_json_bytes(
                    self.inspected_authority_json.encode("utf-8")
                )
                reviewed = from_xml(self.reviewed_ome_xml)
            except (UnicodeError, TypeError, ValueError) as exc:
                raise CanonicalReferenceInputError("reviewed binding payload is invalid") from exc
            if (
                inspection.source_identity.resolved_path != locator
                or CanonicalReferenceSourceIdentity.from_opened(inspection.source_identity)
                != self.source_identity
                or to_xml(reviewed) != self.reviewed_ome_xml
            ):
                raise CanonicalReferenceInputError("reviewed binding differs from inspected source")
            object.__setattr__(self, "_component_inventory",
                               resolve_component_identity_inventory(reviewed, 0))
            object.__setattr__(self, "_channel_biomarkers", tuple(
                channel.biomarker for channel in resolve_channel_metadata(reviewed, 0)
            ))
        else:
            raise CanonicalReferenceInputError("canonical source kind is unsupported")
        object.__setattr__(
            self,
            "binding_sha256",
            _authority_digest(
                {
                    "source_locator": self.source_locator,
                    "source_identity": self.source_identity,
                    "source_kind": self.source_kind,
                    "inspected_authority_json": self.inspected_authority_json,
                    "reviewed_ome_xml": self.reviewed_ome_xml,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CanonicalReferenceCompatibilityEvidence:
    """

    Retains the target-image description and comparisons used to admit a spectral input.

    A target image is checked against its own quantitative settings. An empirical
    control is also compared with that target's instrument/imaging settings
    and measurement-band order. Both comparisons remain available when the source
    is reopened for regional sampling.

    Attributes:
        scope: Whether the record describes the target itself or a control
            compared with that specific target.
        target_image_sha256: Target-image source checksum.
        target_axis: Target measurement-band identifiers in their fitted order.
        target_structural_signature: Recorded instrument, optical path, detector
            and calibration selection, when available for the target.
        target_quantitative_signature: Target digital range and per-band
            normalization settings.
        structural_report: Acquisition/band comparison for an empirical control;
            absent for the target's own input check.
        quantitative_report: Comparison of the available normalization settings.
    """

    scope: str
    target_image_sha256: str
    target_axis: SpectralMeasurementAxis
    target_structural_signature: StructuralCompatibilitySignature | None
    target_quantitative_signature: QuantitativeTransformSignature
    structural_report: StructuralCompatibilityReport | None
    quantitative_report: QuantitativeTransformabilityReport

    def __post_init__(self) -> None:
        """

        Checks the target and comparison records required for the selected input role.

        A control compared with a target needs both a target acquisition description
        and its structural comparison result. The target's own input check retains
        the quantitative comparison and does not add a second structural report.

        Raises:
            TypeError: A measurement, acquisition or comparison record has an
                incompatible type.
            CanonicalReferenceInputError: The role or required comparison fields are invalid.
        """

        if self.scope not in {
            "target_self_admissibility",
            "target_bound_compatibility",
        }:
            raise CanonicalReferenceInputError("canonical compatibility scope is unsupported")
        _sha256(self.target_image_sha256, "compatibility target image SHA-256")
        if type(self.target_axis) is not SpectralMeasurementAxis:
            raise TypeError("compatibility target axis must be exact")
        if self.scope == "target_self_admissibility":
            if self.structural_report is not None:
                raise CanonicalReferenceInputError(
                    "target self-admissibility has no structural report"
                )
        elif self.target_structural_signature is None or self.structural_report is None:
            raise CanonicalReferenceInputError(
                "target-bound compatibility requires structural authority"
            )
        if (
            self.target_structural_signature is not None
            and type(self.target_structural_signature) is not StructuralCompatibilitySignature
        ):
            raise TypeError("compatibility target structural signature must be exact")
        if type(self.target_quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("compatibility target quantitative signature must be exact")
        if (
            self.structural_report is not None
            and type(self.structural_report) is not StructuralCompatibilityReport
        ):
            raise TypeError("canonical structural compatibility report must be exact")
        if type(self.quantitative_report) is not QuantitativeTransformabilityReport:
            raise TypeError("canonical quantitative compatibility report must be exact")


@dataclass(frozen=True, slots=True, init=False)
class CanonicalReferenceInput:
    """

    Combines the selected image, ordered measurement channels and reviewed normalization settings.

    We construct this record after comparing the opened source with its OME
    metadata and, for a reference control, with the selected target image.
    The record retains those comparisons so the same input can be checked
    when a sampling operation reopens the source.

    The source's scientific content is identified from the image checksum,
    measurement order, calibration and compatibility results. Its file location
    and file-opening observations are stored separately, allowing an explicitly
    checked relocation to retain the same quantitative input identity.

    Attributes:
        evidence: Input role, source checksum, ordered bands and per-band
            normalization description with the completed comparison results.
        header: Checked image dimensions, scalar storage and native resolutions.
        channel_fluors: OME fluorophore labels in the original channel order.
        measurement_planes: Mapping from each normalized measurement band to its
            OME Channel ID and source-read channel index.
        compatibility: Target description and acquisition/normalization comparisons.
        locator_binding: Current source filename, file observations and metadata
            needed by its selected opening procedure.
        authority_sha256: Checksum identifying the complete quantitative input
            fields independently of the source filename.
    """

    evidence: ReferenceInputEvidence
    header: CanonicalReferenceHeader
    channel_fluors: tuple[str | None, ...]
    measurement_planes: tuple[CanonicalReferenceMeasurementPlane, ...]
    compatibility: CanonicalReferenceCompatibilityEvidence
    locator_binding: CanonicalReferenceLocatorBinding = field(compare=False)
    authority_sha256: str = field(init=False)

    @property
    def component_inventory(self):
        """Return the reviewed output components retained with the source metadata."""
        return self.locator_binding._component_inventory

    @property
    def component_names(self) -> tuple[str, ...]:
        """Return output component names independently of the acquired band count."""
        inventory = self.component_inventory
        if inventory is not None:
            return inventory.component_names
        return tuple(dict.fromkeys(name for name in self.channel_fluors if name is not None))

    @property
    def fluorophore_names(self) -> tuple[str, ...]:
        """Return stained components; the AF spectrum is obtained separately."""
        return tuple(name for name in self.component_names if not is_autofluorescence_component(name))

    @property
    def channel_biomarkers(self) -> tuple[str | None, ...]:
        """Return explicit markers recorded for source channels, when available."""
        return self.locator_binding._channel_biomarkers

    def __init__(
        self,
        *,
        evidence: ReferenceInputEvidence,
        header: CanonicalReferenceHeader,
        channel_fluors: tuple[str | None, ...],
        measurement_planes: tuple[CanonicalReferenceMeasurementPlane, ...],
        compatibility: CanonicalReferenceCompatibilityEvidence,
        locator_binding: CanonicalReferenceLocatorBinding,
        _proof: object,
    ) -> None:
        """

        Stores the checked source fields and calculates their combined quantitative-input checksum.

        Construction is requested by the module's source-validation functions after
        they have inspected the image. The private construction value identifies
        that path, and __post_init__() then checks the stored field relationships
        and repeats the compatibility calculations.

        Args:
            evidence: Source role, measurement order and normalization settings.
            header: Selected image facts retained from its reader checks.
            channel_fluors: Ordered source fluorescence labels.
            measurement_planes: Ordered source-channel mapping for the measured bands.
            compatibility: Target-specific comparison description and results.
            locator_binding: Source location and reviewed opening settings.
            _proof: Internal construction value supplied by the module's _create().

        Raises:
            TypeError: Construction did not use the checked module path or a field
                has an incompatible type.
            CanonicalReferenceInputError: Stored field relationships or comparisons fail.
        """

        if _proof is not _CANONICAL_INPUT_PROOF:
            raise TypeError(
                "CanonicalReferenceInput records must be created by canonical validation"
            )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "header", header)
        object.__setattr__(self, "channel_fluors", channel_fluors)
        object.__setattr__(self, "measurement_planes", measurement_planes)
        object.__setattr__(self, "compatibility", compatibility)
        object.__setattr__(self, "locator_binding", locator_binding)
        object.__setattr__(
            self,
            "authority_sha256",
            _authority_digest(
                {
                    "evidence": evidence,
                    "header": header,
                    "channel_fluors": channel_fluors,
                    "measurement_planes": measurement_planes,
                    "compatibility": compatibility,
                }
            ),
        )
        self.__post_init__()

    @classmethod
    def _create(
        cls,
        *,
        evidence: ReferenceInputEvidence,
        header: CanonicalReferenceHeader,
        channel_fluors: tuple[str | None, ...],
        measurement_planes: tuple[CanonicalReferenceMeasurementPlane, ...],
        compatibility: CanonicalReferenceCompatibilityEvidence,
        locator_binding: CanonicalReferenceLocatorBinding,
    ) -> "CanonicalReferenceInput":
        """

        Constructs a reference input from fields resolved by the image-validation functions.

        Args:
            evidence: Input role, source measurement order and normalization description.
            header: Checked source-image dimensions and calibration.
            channel_fluors: Source labels in channel order.
            measurement_planes: Mapping used to read each normalized measurement band.
            compatibility: Completed target and control comparisons.
            locator_binding: Source file location and opening description.

        Returns:
            CanonicalReferenceInput with its calculated checksum and constructor checks.
        """

        return cls(
            evidence=evidence,
            header=header,
            channel_fluors=channel_fluors,
            measurement_planes=measurement_planes,
            compatibility=compatibility,
            locator_binding=locator_binding,
            _proof=_CANONICAL_INPUT_PROOF,
        )

    def __post_init__(self) -> None:
        """

        Checks channel order, source identity and the target-specific acquisition comparisons.

        The number of fluorescence labels and measurement-plane mappings must equal
        the image channel count. Measurement IDs must match the selected spectral
        axis, and channel indices must retain the original source order. The image
        header, input description and file-opening record must identify the same
        source contents.

        We recalculate acquisition compatibility and quantitative transformability
        from their retained settings. These results must agree with the recorded
        input flags and comparison checksums. A target-image input compares its own
        quantitative settings; a reference control retains the selected target's
        settings and the required structural comparison.

        Finally, we recalculate the complete input checksum and compare it with the
        saved value. This prevents a changed band order or correction factor from
        retaining the identity of an earlier reference calculation.

        Raises:
            TypeError: A source, plane, measurement or comparison record has an
                incompatible type.
            CanonicalReferenceInputError: Channel coverage/order, input role,
                source checksums, comparison results or complete checksum disagree.
        """

        if type(self.evidence) is not ReferenceInputEvidence:
            raise TypeError("canonical reference evidence must be exact")
        if type(self.header) is not CanonicalReferenceHeader:
            raise TypeError("canonical reference header must be exact")
        if type(self.compatibility) is not CanonicalReferenceCompatibilityEvidence:
            raise TypeError("canonical compatibility evidence must be exact")
        if type(self.locator_binding) is not CanonicalReferenceLocatorBinding:
            raise TypeError("canonical locator binding must be exact")
        if type(self.channel_fluors) is not tuple or len(self.channel_fluors) != self.header.size_c:
            raise CanonicalReferenceInputError("canonical channel fluor inventory is invalid")
        for fluor in self.channel_fluors:
            if fluor is not None:
                _text(fluor, "canonical Channel.Fluor")
        if (
            type(self.measurement_planes) is not tuple
            or len(self.measurement_planes) != self.header.size_c
            or any(
                type(item) is not CanonicalReferenceMeasurementPlane
                for item in self.measurement_planes
            )
        ):
            raise CanonicalReferenceInputError("canonical measurement-plane mapping is invalid")
        expected_ids = self.evidence.axis.measurement_ids
        if tuple(item.measurement_id for item in self.measurement_planes) != expected_ids:
            raise CanonicalReferenceInputError(
                "measurement planes differ from reference evidence axis"
            )
        if tuple(item.channel_index for item in self.measurement_planes) != tuple(
            range(self.header.size_c)
        ):
            raise CanonicalReferenceInputError(
                "measurement planes must retain canonical channel order"
            )
        if self.evidence.source_image_sha256 != self.header.source_file_sha256:
            raise CanonicalReferenceInputError(
                "reference evidence differs from canonical header identity"
            )
        if self.locator_binding.source_identity.file_sha256 != self.header.source_file_sha256:
            raise CanonicalReferenceInputError(
                "opened source identity differs from canonical header"
            )
        if (
            self.compatibility.quantitative_report.transformable
            != self.evidence.quantitatively_transformable
            or self.compatibility.quantitative_report.content_sha256
            != self.evidence.quantitative_transform_evidence_sha256
        ):
            raise CanonicalReferenceInputError(
                "reference evidence differs from retained compatibility reports"
            )
        if self.compatibility.structural_report is None:
            if (
                self.evidence.input_role != "target_image"
                or self.evidence.structurally_compatible is not False
                or self.evidence.structural_compatibility_evidence_sha256 is not None
            ):
                raise CanonicalReferenceInputError("target structural authority is inconsistent")
            expected_structural = None
        else:
            if self.evidence.structural_signature is None:
                raise CanonicalReferenceInputError("structural report lacks input signature")
            expected_structural = compare_structural_compatibility(
                reference_axis=self.compatibility.target_axis,
                candidate_axis=self.evidence.axis,
                reference_signature=self.compatibility.target_structural_signature,
                candidate_signature=self.evidence.structural_signature,
            )
            if (
                self.compatibility.structural_report.compatible
                != self.evidence.structurally_compatible
                or self.compatibility.structural_report.content_sha256
                != self.evidence.structural_compatibility_evidence_sha256
            ):
                raise CanonicalReferenceInputError(
                    "reference structural report differs from evidence"
                )
        expected_quantitative = compare_quantitative_transformability(
            reference_signature=self.compatibility.target_quantitative_signature,
            candidate_signature=self.evidence.quantitative_signature,
        )
        if (
            expected_structural != self.compatibility.structural_report
            or expected_quantitative != self.compatibility.quantitative_report
        ):
            raise CanonicalReferenceInputError("canonical compatibility reports are stale")
        if self.evidence.input_role == "target_image":
            if (
                self.compatibility.scope != "target_self_admissibility"
                or self.compatibility.target_image_sha256 != self.header.source_file_sha256
            ):
                raise CanonicalReferenceInputError(
                    "canonical target requires self-admissibility scope"
                )
            if (
                self.compatibility.target_axis != self.evidence.axis
                or self.compatibility.target_structural_signature
                != self.evidence.structural_signature
                or self.compatibility.target_quantitative_signature
                != self.evidence.quantitative_signature
            ):
                raise CanonicalReferenceInputError(
                    "canonical target self-admissibility must retain its own evidence"
                )
        elif self.compatibility.scope != "target_bound_compatibility":
            raise CanonicalReferenceInputError(
                "canonical non-target requires target-bound compatibility scope"
            )
        _sha256(self.authority_sha256, "canonical input authority SHA-256")
        expected = _authority_digest(
            {
                "evidence": self.evidence,
                "header": self.header,
                "channel_fluors": self.channel_fluors,
                "measurement_planes": self.measurement_planes,
                "compatibility": self.compatibility,
            }
        )
        if self.authority_sha256 != expected:
            raise CanonicalReferenceInputError("canonical input authority digest differs")

    @property
    def input_id(self) -> str:
        """

        Returns the input identifier assigned to this target image or reference control.

        Returns:
            Input ID retained in the reviewed reference description.
        """

        return self.evidence.input_id

    @property
    def input_role(self) -> str:
        """

        Returns the source's selected role in the spectral-reference calculation.

        Returns:
            Recorded role, such as the target image or a declared reference control.
        """

        return self.evidence.input_role

    @property
    def scientific_identity_sha256(self) -> str:
        """

        Returns the checksum of the selected image and its complete quantitative input settings.

        Returns:
            Checksum independent of the file's current location.
        """

        return self.authority_sha256

    @property
    def source_identity(self) -> CanonicalReferenceSourceIdentity:
        """

        Returns the file observations used to check the source on reopening.

        Returns:
            Source checksum, size, modification time and filesystem identity retained
            in the current source-location description.
        """

        return self.locator_binding.source_identity

    def reopen_path(self) -> Path:
        """

        Returns the original-image filename selected for the next checked regional read.

        Returns:
            Path recorded with this input's current opening settings.
        """

        return Path(self.locator_binding.source_locator)

    def validate_source_location(self) -> None:
        """Check recorded file observations during metadata-only review.

        Pixel sampling still uses ``open_source`` to check complete file bytes,
        decoder geometry and the reviewed metadata around the actual reads.
        """
        from mif_pipeline.io.canonical_ingestion import _validate_inspected_source_stat

        identity = self.source_identity
        _validate_inspected_source_stat(OpenedSourceIdentity(
            resolved_path=self.reopen_path(),
            file_sha256=identity.file_sha256,
            size_bytes=identity.size_bytes,
            modified_time_ns=identity.modified_time_ns,
            device_id=identity.device_id,
            inode_id=identity.inode_id,
        ))

    def has_exact_durable_binding(self, other: object) -> bool:
        """

        Compares both quantitative input fields and the recorded file-opening description.

        Args:
            other: Candidate CanonicalReferenceInput obtained when the source is reopened.

        Returns:
            True when the input records and their location/file observations match.
        """

        return (
            type(other) is CanonicalReferenceInput
            and self == other
            and self.locator_binding == other.locator_binding
        )

    def bind_locator_from_opened_source(
        self,
        path: str | Path,
        source: BioFormatsSource,
        *,
        compatibility_target: "CanonicalReferenceInput | None" = None,
    ) -> "CanonicalReferenceInput":
        """

        Rechecks a standardized source at a new location while retaining its quantitative identity.

        We rebuild the input from the newly opened file using the original measurement
        description and channel mapping. The rebuilt input must produce the same
        quantitative checksum. For a reference control, we also require its currently
        selected target so the control-to-target comparisons can be repeated.

        Args:
            path: Location of the newly opened standardized image.
            source: BioFormatsSource opened at that location.
            compatibility_target: Current target input when relocating a reference
                control; omitted for the target's own source.

        Returns:
            Rechecked CanonicalReferenceInput with the updated location and file observations.

        Raises:
            CanonicalReferenceInputError: The existing input uses the reviewed-vendor
                opening route, a required target is absent, or the quantitative input
                contents differ at the new location.
        """

        if self.locator_binding.source_kind != "canonical_tiff":
            raise CanonicalReferenceInputError(
                "reviewed source relocation requires explicit inspected and reviewed authority"
            )

        if self.input_role != "target_image" and compatibility_target is None:
            raise CanonicalReferenceInputError(
                "non-target locator binding requires the exact live target authority"
            )
        rebound = _canonical_reference_input_from_opened_source(
            path,
            source,
            evidence=self.evidence,
            measurement_planes=self.measurement_planes,
            compatibility_target=compatibility_target,
            compatibility=(self.compatibility if self.input_role == "target_image" else None),
        )
        if rebound.authority_sha256 != self.authority_sha256:
            raise CanonicalReferenceInputError(
                "rebound locator does not contain the same scientific input"
            )
        return rebound

    @contextmanager
    def open_source(
        self,
        backend: BioFormatsBackend | None = None,
        *,
        compatibility_target: "CanonicalReferenceInput | None" = None,
    ) -> Iterator[BioFormatsSource]:
        """

        Opens the recorded source and checks its image and normalization fields around regional reading.

        Reviewed vendor input is reopened with its saved source inspection and
        completed OME XML. Standardized input uses the selected backend directly.
        We rebuild and compare the complete source description before yielding the
        reader, then repeat that comparison after the caller's reads finish.

        The surrounding context closes the reader when control leaves this function.
        This gives spectral sampling a checked reader for the required interval
        without retaining it throughout unrelated review steps.

        Args:
            backend: Optional BioFormatsBackend; omitted to use the installed default.
            compatibility_target: Selected target required when reopening a reference
                control. The target's own source omits this argument.

        Yields:
            Open BioFormatsSource ready for the selected measurement-plane reads.

        Raises:
            CanonicalReferenceInputError: The target role, opened source or its
                recorded image/normalization relationships differ.
            OSError: A required source or metadata file cannot be read.
        """

        if self.input_role == "target_image":
            if compatibility_target is not None:
                raise CanonicalReferenceInputError("target reopen has no compatibility target")
        elif (
            type(compatibility_target) is not CanonicalReferenceInput
            or compatibility_target.input_role != "target_image"
        ):
            raise CanonicalReferenceInputError(
                "non-target reopen requires the exact live target authority"
            )
        if backend is None:
            from mif_pipeline.io import get_default_backend

            backend = get_default_backend()
        binding = self.locator_binding
        if binding.source_kind == "reviewed_source":
            assert binding.inspected_authority_json is not None
            assert binding.reviewed_ome_xml is not None
            inspection = InspectedSourceAuthority.from_json_bytes(
                binding.inspected_authority_json.encode("utf-8")
            )
            reviewed = from_xml(binding.reviewed_ome_xml)
            opened = open_reviewed_source(inspection, reviewed, backend=backend,
                                          require_stage1_review=self.input_role == "target_image")
        else:
            opened = backend.open(self.reopen_path())
        with opened as source:
            self._fence_opened_source(source, compatibility_target=compatibility_target)
            yield source
            self._fence_opened_source(source, compatibility_target=compatibility_target)

    def _fence_opened_source(
        self,
        source: BioFormatsSource,
        *,
        compatibility_target: "CanonicalReferenceInput | None",
    ) -> None:
        """

        Reconstructs the input from the open reader and compares it with the recorded input.

        We reuse the original measurement description, channel mapping and reviewed
        vendor metadata where applicable. Rebuilding repeats source and compatibility
        checks, after which both the quantitative input and its file observations
        must match the record used to open the source.

        Args:
            source: Reader currently used for the selected image.
            compatibility_target: Target input required for reference-control comparisons.

        Raises:
            CanonicalReferenceInputError: The reopened input or its file-location
                observations differ from the retained description.
        """

        rebound = _canonical_reference_input_from_opened_source(
            self.reopen_path(),
            source,
            evidence=self.evidence,
            measurement_planes=self.measurement_planes,
            compatibility_target=compatibility_target,
            reviewed_binding=(
                self.locator_binding
                if self.locator_binding.source_kind == "reviewed_source"
                else None
            ),
        )
        if not self.has_exact_durable_binding(rebound):
            raise CanonicalReferenceInputError(
                "opened source differs from durable reference binding"
            )


def canonical_reference_input_to_wire(record: CanonicalReferenceInput) -> dict[str, object]:
    """

    Serializes the reviewed source input for saving between spectral-reference operations.

    We include the image facts, ordered measurement/channel mapping, normalization
    description, compatibility results and current file-opening settings. The
    shared typed encoder retains exact float values and record types so decoding
    can repeat the same numerical and cross-field checks.

    Args:
        record: Completed CanonicalReferenceInput.

    Returns:
        Current version-6 dictionary containing every field needed to restore
        this source input.

    Raises:
        TypeError: record is not a CanonicalReferenceInput.
    """

    if type(record) is not CanonicalReferenceInput:
        raise TypeError("canonical input wire requires an exact authority record")
    from .reference_evidence_wire import _encode

    return {
        "schema": "mif_pipeline.canonical_reference_input",
        "version": 6,
        "evidence": _encode(record.evidence),
        "header": _encode(record.header),
        "channel_fluors": _encode(record.channel_fluors),
        "measurement_planes": _encode(record.measurement_planes),
        "compatibility": _encode(record.compatibility),
        "locator_binding": _encode(record.locator_binding),
        "authority_sha256": _encode(record.authority_sha256),
    }


def _canonical_wire_registry() -> dict[str, type]:
    """

    Lists the record types permitted inside a saved spectral input.

    The decoder resolves the recorded type names only through this explicit
    list. It includes source/header descriptions, ordered measurement bands,
    intensity correction settings and the corresponding compatibility reports.

    Returns:
        Mapping from each permitted qualified type name to its current class.
    """

    from mif_pipeline.io import OffsetAuthority
    from .reference_models import (
        QuantitativeMeasurementTransform,
        QuantitativeTransformSignature,
        SpectralMeasurement,
        SpectralMeasurementAxis,
        StructuralCompatibilitySignature,
    )
    from .reference_evidence_wire import _type_id

    values = (
        CanonicalReferenceHeader,
        CanonicalReferenceMeasurementPlane,
        CanonicalReferenceSourceIdentity,
        CanonicalReferenceLocatorBinding,
        CanonicalReferenceCompatibilityEvidence,
        ReferenceInputEvidence,
        SpectralMeasurement,
        SpectralMeasurementAxis,
        SpectralOpticalSetting,
        StructuralCompatibilitySignature,
        QuantitativeMeasurementTransform,
        QuantitativeTransformSignature,
        DigitalRangeAuthority,
        OffsetAuthority,
        CompatibilityFieldReport,
        StructuralCompatibilityReport,
        QuantitativeTransformabilityReport,
    )
    return {_type_id(value): value for value in values}


def _canonical_reference_wire_types() -> tuple[type, ...]:
    """

    Returns the source-input record types used by the shared typed serializer.

    Returns:
        Tuple containing header, plane mapping, file observation, source-opening
        and target-compatibility record classes.
    """

    return (
        CanonicalReferenceHeader,
        CanonicalReferenceMeasurementPlane,
        CanonicalReferenceSourceIdentity,
        CanonicalReferenceLocatorBinding,
        CanonicalReferenceCompatibilityEvidence,
    )


def canonical_reference_input_from_wire(value: object) -> CanonicalReferenceInput:
    """

    Restores a saved source input and recalculates its field relationships and checksum.

    We require the current version and complete field set before decoding the
    nested records. Construction repeats channel-order, source and compatibility
    checks, then calculates the quantitative-input checksum from those values.
    That checksum must agree with the one stored in the file.

    Args:
        value: Version-6 input dictionary returned by JSON decoding.

    Returns:
        Checked CanonicalReferenceInput ready for its recorded opening procedure.

    Raises:
        CanonicalReferenceInputError: The version, fields, nested types or
            reconstructed input checksum differ from the required values.
    """

    if type(value) is not dict:
        raise CanonicalReferenceInputError("canonical input wire must be an object")
    wire_version = value.get("version")
    if (
        value.get("schema") == "mif_pipeline.canonical_reference_input"
        and type(wire_version) is int
        and wire_version in {1, 2, 3, 4, 5}
    ):
        raise CanonicalReferenceInputError(
            "legacy canonical input wire requires explicit source-backed migration"
        )
    expected = {
        "schema",
        "version",
        "evidence",
        "header",
        "channel_fluors",
        "measurement_planes",
        "compatibility",
        "locator_binding",
        "authority_sha256",
    }
    if (
        set(value) != expected
        or value["schema"] != "mif_pipeline.canonical_reference_input"
        or type(value["version"]) is not int
        or value["version"] != 6
    ):
        raise CanonicalReferenceInputError("canonical input wire schema is unsupported")
    from .reference_evidence_wire import Task6WireError, _decode

    try:
        decoded = {
            name: _decode(value[name], _canonical_wire_registry())
            for name in expected - {"schema", "version"}
        }
    except Task6WireError as exc:
        raise CanonicalReferenceInputError("canonical input wire payload is invalid") from exc
    if (
        type(decoded["evidence"]) is not ReferenceInputEvidence
        or type(decoded["header"]) is not CanonicalReferenceHeader
        or type(decoded["channel_fluors"]) is not tuple
        or type(decoded["measurement_planes"]) is not tuple
        or type(decoded["compatibility"]) is not CanonicalReferenceCompatibilityEvidence
        or type(decoded["locator_binding"]) is not CanonicalReferenceLocatorBinding
        or type(decoded["authority_sha256"]) is not str
    ):
        raise CanonicalReferenceInputError("canonical input wire types differ")
    record = CanonicalReferenceInput._create(
        evidence=decoded["evidence"],
        header=decoded["header"],
        channel_fluors=decoded["channel_fluors"],
        measurement_planes=decoded["measurement_planes"],
        compatibility=decoded["compatibility"],
        locator_binding=decoded["locator_binding"],
    )
    if decoded["authority_sha256"] != record.authority_sha256:
        raise CanonicalReferenceInputError("canonical input wire authority digest is stale")
    return record


def _measurement_planes(
    values: Iterable[object],
    *,
    channel_ids: tuple[str, ...],
    evidence: ReferenceInputEvidence,
) -> tuple[CanonicalReferenceMeasurementPlane, ...]:
    """

    Checks that normalized measurement bands map to the selected source channels in order.

    We construct one plane record per supplied mapping. Measurement IDs must
    equal the spectral axis order, Channel IDs must equal the OME channel order,
    and source indices must be consecutive from zero. This ensures that band
    correction and spectral fitting receive the same channel rows that the
    source reader returns.

    Args:
        values: Tuple of measurement/channel/index objects supplied for this input.
        channel_ids: Ordered Channel IDs obtained from the selected OME image.
        evidence: Input description containing the spectral measurement order.

    Returns:
        Tuple of checked CanonicalReferenceMeasurementPlane records.

    Raises:
        TypeError: The supplied mappings are not a tuple.
        CanonicalReferenceInputError: Measurement IDs, Channel IDs or read indices differ.
    """

    if type(values) is not tuple:
        raise TypeError("measurement planes must be an exact tuple")
    result = tuple(
        CanonicalReferenceMeasurementPlane(
            measurement_id=getattr(value, "measurement_id"),
            channel_id=getattr(value, "channel_id"),
            channel_index=getattr(value, "channel_index"),
        )
        for value in values
    )
    if tuple(item.measurement_id for item in result) != evidence.axis.measurement_ids:
        raise CanonicalReferenceInputError("measurement plane IDs differ from evidence axis")
    if tuple(item.channel_id for item in result) != channel_ids:
        raise CanonicalReferenceInputError("measurement plane channel IDs differ from OME")
    if tuple(item.channel_index for item in result) != tuple(range(len(channel_ids))):
        raise CanonicalReferenceInputError("measurement plane channel order differs from OME")
    return result


def _validate_quantitative_input(
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    evidence: ReferenceInputEvidence,
    *,
    band_order: tuple[str, ...],
    acquisition,
) -> None:
    """

    Compares the recorded band correction with the actual reviewed imaging settings.

    The selected policy determines which recorded factors are applied: full acquisition
    correction, exposure alone, or unchanged calibrated values. Metadata-known dark
    correction supplies zero applied offset while preserving the original setting.
    This input route records a response multiplier of one for every measurement.
    We compare the policy and retained settings with the selected image before sampling.

    Args:
        header: Checked TIFF or vendor-source dimensions and digital storage facts.
        evidence: Proposed measurement axis and quantitative correction description.
        band_order: Ordered measurement bands obtained from the reviewed metadata.
        acquisition: Completed UnmixingInputMetadata for the selected image.

    Raises:
        CanonicalReferenceInputError: Band order, correction policy, response
            multiplier, digital range or any per-band acquisition factor differs.
    """

    if evidence.axis.parent_band_ids != band_order:
        raise CanonicalReferenceInputError(
            "reference measurement axis differs from canonical band order"
        )
    transforms = evidence.quantitative_signature.measurements
    if tuple(item.measurement_id for item in transforms) != evidence.axis.measurement_ids:
        raise CanonicalReferenceInputError("reference quantitative transform axis differs")
    expected_policy = {"full": "mif_pipeline_offset_exposure_v1", "exposure_only": "mif_pipeline_exposure_only_v1", "as_stored": "mif_pipeline_as_stored_v1"}[acquisition.correction_mode]
    if evidence.quantitative_signature.normalization_policy != expected_policy:
        raise CanonicalReferenceInputError("reference normalization policy is unsupported")
    if (
        evidence.quantitative_signature.response_transform_id != _IDENTITY_RESPONSE_TRANSFORM_ID
        or evidence.quantitative_signature.response_transform_authority
        != _IDENTITY_RESPONSE_TRANSFORM_AUTHORITY
        or evidence.quantitative_signature.response_transform_semantic
        != _IDENTITY_RESPONSE_TRANSFORM_SEMANTIC
        or tuple(item.response_correction_multiplier for item in transforms)
        != (1.0,) * header.size_c
    ):
        raise CanonicalReferenceInputError(
            "reference response transform is not the supported identity response transform"
        )
    if len(transforms) != header.size_c:
        raise CanonicalReferenceInputError("reference quantitative transform count differs")
    resolved = _effective_digital_range(header, acquisition)
    if any(
        item.effective_digital_maximum != (resolved.maximum if resolved is not None else None)
        or item.digital_range_meaningful_bits != (resolved.meaningful_bits if resolved is not None else None)
        or item.digital_range_authority is not (resolved.authority if resolved is not None else None)
        or item.digital_range_profile_id != (resolved.profile_id if resolved is not None else None)
        for item in transforms
    ):
        raise CanonicalReferenceInputError(
            "reference digital range differs from canonical authority"
        )
    expected = zip(
        transforms,
        acquisition.exposure_seconds,
        acquisition.gain,
        acquisition.binning_area,
        acquisition.offsets,
        strict=True,
    )
    for transform, exposure, gain, binning_area, offset in expected:
        if (
            transform.correction_mode != acquisition.correction_mode
            or transform.intensity_representation != acquisition.intensity_representation
            or transform.exposure_seconds != exposure
            or transform.gain != gain
            or transform.binning_area != binning_area
            or transform.offset != offset
            or transform.offset_authority is not acquisition.offset_authority
            or transform.offset_profile_identifier != acquisition.offset_profile_identifier
        ):
            raise CanonicalReferenceInputError(
                "reference quantitative transform differs from canonical metadata"
            )


def _effective_digital_range(header, acquisition) -> EffectiveDigitalRange | None:
    """

    Resolves the accepted normalization maximum from the image header and acquisition review.

    Args:
        header: Checked image dtype, bit declarations and source-storage facts.
        acquisition: Reviewed acquisition record with any explicit digital-range profile.

    Returns:
        EffectiveDigitalRange containing the meaningful maximum and its recorded source.

    Raises:
        CanonicalReferenceInputError: The header/profile values cannot establish
            a valid quantitative range for this image.
    """

    try:
        return resolve_acquisition_digital_range(header, acquisition)
    except CanonicalOmeTiffError as exc:
        raise CanonicalReferenceInputError(str(exc)) from exc


def _opened_reference_material(
    path: Path,
    source: BioFormatsSource,
    *,
    reviewed_binding: CanonicalReferenceLocatorBinding | None,
    correction_mode: str | None = None,
) -> tuple[
    CanonicalOmeTiffHeader | SourceImageFacts,
    CanonicalReferenceHeader,
    UnmixingInputMetadata,
]:
    """

    Reads checked image facts and completed image metadata through the selected source procedure.

    Standardized TIFF input obtains its header and acquisition together from
    one checked inspection of actual TIFF pages and the open reader's OME
    description. Reviewed vendor input uses the selected
    reader's source facts and completed OME acquisition annotation. Both produce
    a common retained header and require image metadata for image zero,
    which represents the selected analytical image in this workflow.

    Args:
        path: Absolute source-image filename.
        source: Open BioFormatsSource for the selected image.
        reviewed_binding: Saved vendor inspection and reviewed OME selection,
            or None for standardized-TIFF input.
        correction_mode: Selected target policy when deriving or revalidating a
            reference; exposure-only retains unapplied acquisition values.

    Returns:
        Source facts, retained spectral-input header and selected acquisition record.

    Raises:
        CanonicalReferenceInputError: Source/header inspection fails or the
            selected image lacks completed image metadata.
    """

    try:
        if reviewed_binding is None:
            try:
                inspection = inspect_canonical_ome_tiff(path, source)
            except Exception as exc:
                raise CanonicalReferenceInputError(
                    "canonical source identity/header/imaging-settings validation failed"
                ) from exc
            facts = inspection.header
            acquisition = inspection.acquisition
            retained = CanonicalReferenceHeader.from_header(facts)
        else:
            if reviewed_binding.source_kind != "reviewed_source":
                raise CanonicalReferenceInputError("reviewed source binding kind differs")
            facts = resolve_source_image_facts(source)
            acquisition = resolve_unmixing_input_metadata(source.metadata, 0)
            retained = CanonicalReferenceHeader.from_source_facts(facts)
    except CanonicalReferenceInputError:
        raise
    except Exception as exc:
        raise CanonicalReferenceInputError(
            f"canonical reference source metadata authority cannot be resolved: {type(exc).__name__}: {exc}"
        ) from exc
    if correction_mode is not None or acquisition is None:
        from mif_pipeline.io.metadata.calibration import acquisition_for_correction_mode
        acquisition = acquisition_for_correction_mode(source.metadata, correction_mode or "full")
    if acquisition is None or acquisition.image_index != 0:
        raise CanonicalReferenceInputError(
            "canonical input lacks image-zero unmixing metadata authority"
        )
    return facts, retained, acquisition


def _explicit_structural_signature(
    acquisition, *, allow_absent_target: bool = False
) -> StructuralCompatibilitySignature | None:
    """

    Collects the measurement identifiers used when comparing a control with the target image.

    Retain recorded instrument, optical-path and detector facts without inventing
    identifiers when they are absent. The optional profile ID/version pair remains
    a provenance checksum; a response-calibration label does not apply a response
    curve. The guided acquisition review supplies the matching-optical-settings
    condition where the image cannot establish it.

    Args:
        acquisition: Completed image acquisition description.
        allow_absent_target: Whether a target may omit the entire five-field
            acquisition-compatibility declaration. This preserves older saved
            targets; new targets retain optional facts in a signature.

    Returns:
        StructuralCompatibilitySignature, or None when a target is permitted
        to omit all five declaration fields.

    Raises:
        CanonicalReferenceInputError: A supplied identifier is empty or padded with whitespace.
    """

    values = (
        acquisition.acquisition_compatibility_profile_id,
        acquisition.acquisition_compatibility_profile_version,
        acquisition.instrument_compatibility_id,
        acquisition.optical_path_compatibility_id,
        acquisition.detector_mode_compatibility_id,
    )
    if any(value is not None and (
        type(value) is not str or not value or value != value.strip()
    ) for value in values):
        raise CanonicalReferenceInputError("recorded measurement identifiers must be non-empty literal text")
    if (allow_absent_target and acquisition.correction_mode == "full"
            and all(item is None for item in values)):
        # Retain the representation of previously saved full-mode targets.
        return None
    profile_id, profile_version, instrument_id, optical_path_id, detector_mode = values
    return StructuralCompatibilitySignature(
        instrument_id=instrument_id,
        optical_path_id=optical_path_id,
        detector_mode=detector_mode,
        acquisition_profile_id=(
            "explicit-acquisition-profile-v1:" + _authority_digest((profile_id, profile_version))
            if profile_id is not None and profile_version is not None else None
        ),
        response_calibration_id=acquisition.response_calibration_id,
        correction_mode=acquisition.correction_mode,
    )


def _compatibility_evidence(
    evidence: ReferenceInputEvidence,
    *,
    target_evidence: ReferenceInputEvidence,
    target_image_sha256: str,
) -> tuple[ReferenceInputEvidence, CanonicalReferenceCompatibilityEvidence]:
    """

    Recalculates the structural and quantitative comparisons for one input and its target.

    A reference control requires the target's acquisition profile and response
    calibration. We compare the ordered bands and structural signatures, then
    compare the quantitative correction descriptions. The target's own input
    receives its quantitative self-comparison.

    We return a new input description whose compatibility flags and report
    checksums come from these calculations, together with the target fields
    needed to repeat the comparisons when the source is reopened.

    Args:
        evidence: Proposed target or control input description.
        target_evidence: Selected target's measurement and imaging settings.
        target_image_sha256: Target source-image checksum.

    Returns:
        Updated ReferenceInputEvidence and its complete target comparison record.

    Raises:
        CanonicalReferenceInputError: An empirical reference or target lacks
            the acquisition/calibration information required for comparison.
    """

    if evidence.input_role == "target_image":
        structural = None
    else:
        if target_evidence.structural_signature is None:
            raise CanonicalReferenceInputError(
                "empirical reference requires target imaging-settings compatibility profile"
            )
        if evidence.structural_signature is None:
            raise CanonicalReferenceInputError("empirical input lacks structural signature")
        structural = compare_structural_compatibility(
            reference_axis=target_evidence.axis,
            candidate_axis=evidence.axis,
            reference_signature=target_evidence.structural_signature,
            candidate_signature=evidence.structural_signature,
        )
    quantitative = compare_quantitative_transformability(
        reference_signature=target_evidence.quantitative_signature,
        candidate_signature=evidence.quantitative_signature,
    )
    scope = (
        "target_self_admissibility"
        if evidence.input_role == "target_image"
        else "target_bound_compatibility"
    )
    resolved = ReferenceInputEvidence(
        input_id=evidence.input_id,
        input_role=evidence.input_role,
        fluorophore_identity=evidence.fluorophore_identity,
        source_image_sha256=evidence.source_image_sha256,
        axis=evidence.axis,
        structural_signature=evidence.structural_signature,
        quantitative_signature=evidence.quantitative_signature,
        structurally_compatible=False if structural is None else structural.compatible,
        quantitatively_transformable=quantitative.transformable,
        structural_compatibility_evidence_sha256=(
            None if structural is None else structural.content_sha256
        ),
        quantitative_transform_evidence_sha256=quantitative.content_sha256,
    )
    compatibility = CanonicalReferenceCompatibilityEvidence(
        scope=scope,
        target_image_sha256=target_image_sha256,
        target_axis=target_evidence.axis,
        target_structural_signature=target_evidence.structural_signature,
        target_quantitative_signature=target_evidence.quantitative_signature,
        structural_report=structural,
        quantitative_report=quantitative,
    )
    return resolved, compatibility


def canonical_reference_input_from_opened_source(
    path: str | Path,
    source: BioFormatsSource,
    evidence: ReferenceInputEvidence,
    measurement_planes: tuple[object, ...],
    *,
    compatibility_target: CanonicalReferenceInput | None = None,
) -> CanonicalReferenceInput:
    """

    Checks a proposed spectral input against an already-open standardized image.

    We compare its supplied measurement and acquisition description with the
    image's actual OME/TIFF values, then establish its relationship to the selected
    target. The common implementation below performs the source, band-order,
    normalization and compatibility checks.

    Args:
        path: Standardized source-image filename.
        source: BioFormatsSource already opened for that file.
        evidence: Proposed input role, measurement axis and correction settings.
        measurement_planes: Ordered source-channel mappings used for spectral reads.
        compatibility_target: Selected target required for a reference control.

    Returns:
        CanonicalReferenceInput whose recorded fields agree with the opened source.

    Raises:
        CanonicalReferenceInputError: Source, channel, normalization or target
            comparisons fail.
    """

    return _canonical_reference_input_from_opened_source(
        path,
        source,
        evidence=evidence,
        measurement_planes=measurement_planes,
        compatibility_target=compatibility_target,
    )


def _canonical_reference_input_from_opened_source(
    path: str | Path,
    source: BioFormatsSource,
    *,
    evidence: ReferenceInputEvidence,
    measurement_planes: tuple[object, ...],
    compatibility_target: CanonicalReferenceInput | None = None,
    compatibility: CanonicalReferenceCompatibilityEvidence | None = None,
    reviewed_binding: CanonicalReferenceLocatorBinding | None = None,
) -> CanonicalReferenceInput:
    """

    Validates the opened image and constructs its complete spectral-reference input.

    We resolve the source path, confirm that the reader opened that file and
    obtain its checked dimensions and image metadata. Channel IDs and
    fluorophores must cover the source channels in order. The supplied measurement
    planes, digital range, correction factors and acquisition profile are then
    compared with those reviewed image values.

    For a reference control, we recalculate compatibility with its live selected
    target. A retained comparison may instead be repeated when reopening the
    target's own input; the recalculated results must equal the saved results.
    Structural incompatibility or unavailable quantitative correction prevents
    the input from being returned for reference sampling.

    Args:
        path: Source filename expected by this opening operation.
        source: Open reader carrying its file identity and selected OME image.
        evidence: Proposed input role, measurement and correction description.
        measurement_planes: Ordered measurement-to-source-channel mappings.
        compatibility_target: Current target used for a reference-control comparison.
        compatibility: Existing target comparison to recheck while reopening;
            cannot be supplied with another compatibility_target.
        reviewed_binding: Vendor-source inspection and reviewed metadata, or
            None for the standardized-TIFF opening procedure.

    Returns:
        CanonicalReferenceInput containing the verified quantitative input and
        its current file location/observations.

    Raises:
        TypeError: Required input records or mutually exclusive comparison
            arguments are invalid.
        CanonicalReferenceInputError: The source identity, channel order,
            acquisition factors or target compatibility fails its checks.
    """

    if type(evidence) is not ReferenceInputEvidence:
        raise TypeError("reference evidence must be exact ReferenceInputEvidence")
    if compatibility_target is not None and (
        type(compatibility_target) is not CanonicalReferenceInput
        or compatibility_target.input_role != "target_image"
    ):
        raise CanonicalReferenceInputError(
            "compatibility target must be an exact canonical target authority"
        )
    if not hasattr(source, "identity") or not hasattr(source, "metadata"):
        raise TypeError("canonical reference input requires an opened Bio-Formats source")
    source_path = Path(path).resolve(strict=True)
    identity = source.identity
    if type(identity) is not OpenedSourceIdentity:
        raise CanonicalReferenceInputError("opened source identity must be IO-owned")
    if identity.resolved_path != source_path:
        raise CanonicalReferenceInputError("opened source path differs from requested locator")
    facts, retained_header, acquisition = _opened_reference_material(
        source_path, source, reviewed_binding=reviewed_binding,
        correction_mode=evidence.quantitative_signature.measurements[0].correction_mode,
    )
    if identity.file_sha256 != retained_header.source_file_sha256:
        raise CanonicalReferenceInputError("opened source hash differs from canonical header")
    ome = source.metadata
    pixels = ome.images[0].pixels
    channel_ids = tuple(channel.id for channel in pixels.channels)
    channel_fluors = tuple(channel.fluor for channel in pixels.channels)
    if (
        len(channel_ids) != retained_header.size_c
        or any(type(item) is not str or not item or item != item.strip() for item in channel_ids)
        or len(channel_ids) != len(set(channel_ids))
        or any(item is not None and (type(item) is not str or not item or item != item.strip())
               for item in channel_fluors)
    ):
        raise CanonicalReferenceInputError(
            "canonical OME channels require exact IDs and valid optional Fluor identities"
        )
    if (
        acquisition.image_index != 0
        or tuple(acquisition.band_order) != evidence.axis.parent_band_ids
    ):
        raise CanonicalReferenceInputError(
            "canonical measurement axis differs from reference evidence"
        )
    expected_structural = _explicit_structural_signature(
        acquisition, allow_absent_target=(evidence.input_role == "target_image"
                                          and evidence.structural_signature is None)
    )
    if evidence.structural_signature != expected_structural:
        raise CanonicalReferenceInputError(
            "reference structural signature differs from the explicit canonical compatibility profile"
        )
    _validate_quantitative_input(
        facts,
        evidence,
        band_order=tuple(acquisition.band_order),
        acquisition=acquisition,
    )
    planes = _measurement_planes(
        measurement_planes,
        channel_ids=channel_ids,
        evidence=evidence,
    )
    retained_identity = CanonicalReferenceSourceIdentity.from_opened(identity)
    if reviewed_binding is not None and (
        reviewed_binding.source_locator != str(source_path)
        or reviewed_binding.source_identity != retained_identity
    ):
        raise CanonicalReferenceInputError("reviewed source binding differs from opened identity")
    if (
        evidence.input_role != "target_image"
        and compatibility_target is None
        and compatibility is None
    ):
        raise CanonicalReferenceInputError(
            "non-target canonical input requires the exact target opened live for compatibility"
        )
    if compatibility is not None:
        if compatibility_target is not None:
            raise TypeError("locator rebinding cannot replace compatibility target")
        target_evidence = ReferenceInputEvidence(
            input_id="compatibility-target",
            input_role="target_image",
            fluorophore_identity=None,
            source_image_sha256=compatibility.target_image_sha256,
            axis=compatibility.target_axis,
            structural_signature=compatibility.target_structural_signature,
            quantitative_signature=compatibility.target_quantitative_signature,
            structurally_compatible=False,
            quantitatively_transformable=True,
            structural_compatibility_evidence_sha256=None,
            quantitative_transform_evidence_sha256=compatibility.quantitative_report.content_sha256,
        )
        resolved_evidence, resolved_compatibility = _compatibility_evidence(
            evidence,
            target_evidence=target_evidence,
            target_image_sha256=compatibility.target_image_sha256,
        )
        if resolved_compatibility != compatibility:
            raise CanonicalReferenceInputError(
                "canonical compatibility reports changed on locator reopen"
            )
    else:
        target_evidence = (
            evidence if compatibility_target is None else compatibility_target.evidence
        )
        target_sha256 = (
            retained_header.source_file_sha256
            if compatibility_target is None
            else compatibility_target.header.source_file_sha256
        )
        resolved_evidence, resolved_compatibility = _compatibility_evidence(
            evidence,
            target_evidence=target_evidence,
            target_image_sha256=target_sha256,
        )
    if (
        resolved_compatibility.structural_report is not None
        and not resolved_compatibility.structural_report.compatible
    ) or not resolved_compatibility.quantitative_report.transformable:
        raise CanonicalReferenceInputError("canonical input is incompatible with the exact target")
    return CanonicalReferenceInput._create(
        evidence=resolved_evidence,
        header=retained_header,
        channel_fluors=channel_fluors,
        measurement_planes=planes,
        compatibility=resolved_compatibility,
        locator_binding=reviewed_binding
        or CanonicalReferenceLocatorBinding(
            source_locator=str(source_path),
            source_identity=retained_identity,
        ),
    )


def canonical_reference_input_authority_from_opened_source(
    path: str | Path,
    source: BioFormatsSource,
    *,
    input_id: str,
    input_role: str,
    fluorophore_identity: str | None,
    compatibility_target: CanonicalReferenceInput | None = None,
) -> CanonicalReferenceInput:
    """

    Derives a spectral input directly from the opened standardized image's reviewed metadata.

    The image supplies its Channel IDs, measurement bands, digital range and
    correction factors. The caller selects the source role and any declared
    fluorophore, while the common constructor calculates the target comparison.

    Args:
        path: Standardized image filename.
        source: Reader already opened for that image.
        input_id: Identifier assigned to this target or control input.
        input_role: Selected target/reference role.
        fluorophore_identity: Declared dye identity where required by that control.
        compatibility_target: Target image against which an empirical control
            is compared.

    Returns:
        CanonicalReferenceInput derived from and checked against the actual image.
    """

    return _authority_from_opened_source(
        path,
        source,
        input_id=input_id,
        input_role=input_role,
        fluorophore_identity=fluorophore_identity,
        compatibility_target=compatibility_target,
        reviewed_binding=None,
    )


def _reference_measurement_axis(channels, acquisition, structural, compatibility_target, *, ome=None):
    """Bind analytical rows to recorded bands while preserving source-local IDs.

    Targets retain their OME Channel IDs and typed emission wavelengths. Controls
    adopt target row IDs only after ordered-band, known-wavelength and optical
    compatibility succeeds. Their original channel IDs remain in pixel-read planes
    and their complete metadata remains in the reviewed source binding.
    """
    from .optical_settings import recorded_optical_settings
    settings = recorded_optical_settings(channels, ome=ome)
    measurements = tuple(
        SpectralMeasurement(
            measurement_id=channel.id,
            parent_band_id=acquisition.band_order[index],
            sample_id=channel.id,
            wavelength=(float(channel.emission_wavelength_quantity.to("nanometer").magnitude)
                        if channel.emission_wavelength is not None else None),
            wavelength_unit="nm" if channel.emission_wavelength is not None else None,
            optical_setting=settings[index],
        )
        for index, channel in enumerate(channels)
    )
    axis = SpectralMeasurementAxis(measurements)
    if compatibility_target is not None:
        report = compare_structural_compatibility(
            reference_axis=compatibility_target.evidence.axis, candidate_axis=axis,
            reference_signature=compatibility_target.evidence.structural_signature,
            candidate_signature=structural,
        )
        if not report.compatible:
            if any(item.reason_code == "recorded_wavelength_mismatch" for item in report.fields):
                raise CanonicalReferenceInputError("Recorded reference wavelengths differ from the sample. Check the selected reference image and measurement bands.")
            conflicts = ", ".join(item.field_name for item in report.fields if not item.accepted)
            raise CanonicalReferenceInputError(f"Reference imaging settings differ from the sample: {conflicts}")
        # Pixel reads retain this file's Channel IDs below. Analysis rows use the
        # target coordinates only after the recorded acquisition order agrees.
        axis = compatibility_target.evidence.axis
    return axis


def _authority_from_opened_source(
    path: str | Path,
    source: BioFormatsSource,
    *,
    input_id: str,
    input_role: str,
    fluorophore_identity: str | None,
    compatibility_target: CanonicalReferenceInput | None,
    reviewed_binding: CanonicalReferenceLocatorBinding | None,
) -> CanonicalReferenceInput:
    """

    Builds the ordered measurement axis and per-band correction from reviewed image metadata.

    We associate each OME Channel ID with its corresponding acquisition band and
    retain that same order for source reads. Each quantitative-band record includes
    the effective digital maximum, meaningful bits and range source, detector
    offset and offset declaration, exposure, gain and binning area. The selected
    correction mode controls which recorded factors are applied.
    This procedure uses a response multiplier of one for every band.

    These values form the proposed input description. The common source-validation
    function then repeats their agreement with the actual image and calculates
    control-to-target compatibility before returning the completed input.

    Args:
        path: Original source filename.
        source: Open standardized or reviewed-vendor reader.
        input_id: Identifier used for this target/control.
        input_role: Target image or selected reference-control role.
        fluorophore_identity: Declared fluorophore where required by the selected role.
        compatibility_target: Current target required for an empirical reference.
        reviewed_binding: Vendor-source metadata/opening description, or None
            for the standardized-TIFF procedure.

    Returns:
        CanonicalReferenceInput with derived measurement order, correction factors
        and completed compatibility results.

    Raises:
        CanonicalReferenceInputError: Image count, Channel IDs, acquisition-band
            coverage or normalization/target comparisons are invalid.
    """

    if not hasattr(source, "metadata"):
        raise TypeError("canonical target derivation requires opened Bio-Formats source")
    source_path = Path(path).resolve(strict=True)
    facts, retained_header, acquisition = _opened_reference_material(
        source_path, source, reviewed_binding=reviewed_binding,
        correction_mode=(compatibility_target.evidence.quantitative_signature.measurements[0].correction_mode
                         if compatibility_target is not None else None),
    )
    ome = source.metadata
    if len(ome.images) != 1:
        raise CanonicalReferenceInputError("canonical target derivation requires one OME image")
    image = ome.images[0]
    pixels = image.pixels
    channels = tuple(pixels.channels)
    if len(channels) != retained_header.size_c:
        raise CanonicalReferenceInputError(
            "canonical target channel inventory differs from its header"
        )
    channel_ids = tuple(channel.id for channel in channels)
    if any(type(item) is not str or not item.strip() for item in channel_ids) or len(
        channel_ids
    ) != len(set(channel_ids)):
        raise CanonicalReferenceInputError(
            "canonical target requires exact unique channel measurement IDs"
        )
    if len(acquisition.band_order) != len(channels):
        raise CanonicalReferenceInputError(
            "canonical target measurement axis differs from channel topology"
        )
    structural = _explicit_structural_signature(
        acquisition
    )
    axis = _reference_measurement_axis(channels, acquisition, structural, compatibility_target, ome=ome)
    effective_range = _effective_digital_range(facts, acquisition)
    # Record each correction in the same measurement order used for
    # source-channel reads and spectral fitting.
    quantitative = QuantitativeTransformSignature(
        normalization_policy={"full": "mif_pipeline_offset_exposure_v1", "exposure_only": "mif_pipeline_exposure_only_v1", "as_stored": "mif_pipeline_as_stored_v1"}[acquisition.correction_mode],
        response_transform_id=_IDENTITY_RESPONSE_TRANSFORM_ID,
        response_transform_semantic=_IDENTITY_RESPONSE_TRANSFORM_SEMANTIC,
        measurements=tuple(
            QuantitativeMeasurementTransform(
                measurement_id=measurement.measurement_id,
                effective_digital_maximum=(effective_range.maximum if effective_range is not None else None),
                digital_range_meaningful_bits=(effective_range.meaningful_bits if effective_range is not None else None),
                digital_range_authority=(effective_range.authority if effective_range is not None else None),
                digital_range_profile_id=(effective_range.profile_id if effective_range is not None else None),
                offset=acquisition.offsets[index],
                offset_authority=acquisition.offset_authority,
                offset_profile_identifier=acquisition.offset_profile_identifier,
                exposure_seconds=acquisition.exposure_seconds[index],
                gain=acquisition.gain[index],
                binning_area=acquisition.binning_area[index],
                response_correction_multiplier=1.0,
                correction_mode=acquisition.correction_mode,
                intensity_representation=acquisition.intensity_representation,
            )
            for index, measurement in enumerate(axis.measurements)
        ),
        response_transform_authority=_IDENTITY_RESPONSE_TRANSFORM_AUTHORITY,
    )
    evidence = ReferenceInputEvidence(
        input_id=input_id,
        input_role=input_role,
        fluorophore_identity=fluorophore_identity,
        source_image_sha256=retained_header.source_file_sha256,
        axis=axis,
        structural_signature=structural,
        quantitative_signature=quantitative,
        structurally_compatible=False,
        quantitatively_transformable=False,
        structural_compatibility_evidence_sha256=None if input_role == "target_image" else "0" * 64,
        quantitative_transform_evidence_sha256="0" * 64,
    )
    planes = tuple(
        CanonicalReferenceMeasurementPlane(
            measurement_id=axis.measurements[index].measurement_id,
            channel_id=channel.id,
            channel_index=index,
        )
        for index, channel in enumerate(channels)
    )
    return _canonical_reference_input_from_opened_source(
        source_path,
        source,
        evidence=evidence,
        measurement_planes=planes,
        compatibility_target=compatibility_target,
        reviewed_binding=reviewed_binding,
    )


def _reviewed_locator_binding(
    authority: InspectedSourceAuthority,
    reviewed_ome: OME,
) -> CanonicalReferenceLocatorBinding:
    """

    Stores the completed vendor-image inspection and reviewed OME with its source filename.

    Args:
        authority: InspectedSourceAuthority recorded while the original source
            was opened and its analytical image selected.
        reviewed_ome: Completed OME metadata for that selected image.

    Returns:
        Source-opening description containing the original file observations,
        inspection JSON and reviewed XML.

    Raises:
        TypeError: The supplied inspection or OME value has an incompatible type.
    """

    if type(authority) is not InspectedSourceAuthority or not isinstance(reviewed_ome, OME):
        raise TypeError("reviewed admission requires exact inspection authority and OME")
    return CanonicalReferenceLocatorBinding(
        source_locator=str(authority.source_identity.resolved_path),
        source_identity=CanonicalReferenceSourceIdentity.from_opened(authority.source_identity),
        source_kind="reviewed_source",
        inspected_authority_json=authority.to_json_bytes().decode("utf-8"),
        reviewed_ome_xml=to_xml(reviewed_ome),
    )


def canonical_reference_input_authority_from_reviewed_source(
    authority: InspectedSourceAuthority,
    reviewed_ome: OME,
    *,
    input_id: str,
    input_role: str,
    fluorophore_identity: str | None,
    compatibility_target: CanonicalReferenceInput | None = None,
    backend: BioFormatsBackend | None = None,
) -> CanonicalReferenceInput:
    """

    Creates a spectral input by reopening the selected vendor image with its reviewed metadata.

    We retain the original source selection and completed OME XML, open the
    reviewed source once, and derive its measurement and normalization settings.
    The resulting input is checked against the selected target where its role
    requires an empirical-reference comparison. The reader closes after construction.

    Args:
        authority: Completed source inspection with its original image/series selection.
        reviewed_ome: Reviewed channel and image metadata.
        input_id: Identifier assigned to this source input.
        input_role: Target image or reference-control role.
        fluorophore_identity: Declared dye for a role requiring one.
        compatibility_target: Selected target required for an empirical reference.
        backend: Optional reader used for the reopened vendor source.

    Returns:
        CanonicalReferenceInput retaining the vendor-image opening procedure for
        subsequent regional reads.
    """

    binding = _reviewed_locator_binding(authority, reviewed_ome)
    with open_reviewed_source(authority, reviewed_ome, backend=backend,
                              require_stage1_review=input_role == "target_image") as source:
        return _authority_from_opened_source(
            authority.source_identity.resolved_path,
            source,
            input_id=input_id,
            input_role=input_role,
            fluorophore_identity=fluorophore_identity,
            compatibility_target=compatibility_target,
            reviewed_binding=binding,
        )


def canonical_target_reference_input_from_reviewed_source(
    authority: InspectedSourceAuthority,
    reviewed_ome: OME,
    *,
    backend: BioFormatsBackend | None = None,
) -> CanonicalReferenceInput:
    """

    Constructs the unmixing target from its vendor-image inspection and completed OME review.

    We select the target-image role, derive its measurement order and acquisition
    correction, and retain the direct vendor-source opening description. Reference
    controls can subsequently be compared with this target.

    Args:
        authority: Completed source inspection for the target image.
        reviewed_ome: Target channel and image metadata after review.
        backend: Optional reader used to reopen the selected image.

    Returns:
        Checked target CanonicalReferenceInput.
    """

    return canonical_reference_input_authority_from_reviewed_source(
        authority,
        reviewed_ome,
        input_id=f"canonical-target:{authority.selection.image_id}",
        input_role="target_image",
        fluorophore_identity=None,
        backend=backend,
    )


__all__ = [
    "CanonicalReferenceHeader",
    "CanonicalReferenceInput",
    "CanonicalReferenceInputError",
    "CanonicalReferenceMeasurementPlane",
    "CanonicalReferenceSourceIdentity",
    "canonical_reference_input_from_opened_source",
    "canonical_reference_input_authority_from_opened_source",
    "canonical_reference_input_authority_from_reviewed_source",
    "canonical_target_reference_input_from_reviewed_source",
    "canonical_reference_input_from_wire",
    "canonical_reference_input_to_wire",
]
