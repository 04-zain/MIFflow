"""

Checks the standardized OME-TIFF layout and resolves its meaningful intensity range.

A TIFF image directory describes one stored pixel plane. OME TiffData links
those directories to channel, Z and time positions. The supported layout
stores each channel in its own plane, uses one Z position and time point,
and keeps corresponding reduced-resolution planes together as a pyramid.

The file is inspected through both tifffile and the already-open Bio-Formats
reader. Their descriptions must agree on dimensions, channels, dtype and
calibration before the image is passed to quantitative processing. Header
inspection avoids decoding a full-resolution pixel array; the file checksum
is calculated separately by reading its bytes in blocks.

The effective digital maximum describes the meaningful measurement range.
An unsigned 16-bit container can hold 0 through 65535, but a recorded
SignificantBits value of 12 gives a meaningful maximum of 4095. Explicit
calibration profiles can supply a different accepted range. Camera ADC
depth remains an acquisition fact and is not substituted for stored range.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import stat
from typing import Any

import numpy as np
from ome_types import OME, from_xml
from ome_types.units import ureg
import tifffile

from .backends import (
    BioFormatsSource,
    OpenedSourceIdentity,
    SeriesInfo,
    snapshot_opened_source_identity,
)
from .metadata.canonical import UnmixingInputMetadata, resolve_unmixing_input_metadata
from .source_facts import SourceImageFacts

_OME_TIFF_SUFFIXES = (".ome.tif", ".ome.tiff")
_APPROVED_LOSSLESS_COMPRESSIONS = frozenset(
    {
        "none",
        "uncompressed",
        "lzw",
        "adobe_deflate",
        "deflate",
        "zlib",
        "zstd",
    }
)
# ==============================================================================
#                               Canonical ome tiff
# ==============================================================================


class CanonicalOmeTiffError(RuntimeError):
    """

    Reports an image layout, calibration or meaningful-range declaration that fails its checks.
    """

class DigitalRangeAuthority(str, Enum):
    """

    Identifies the recorded source used to choose the meaningful intensity maximum.

    OME_SIGNIFICANT_BITS uses the image's SignificantBits declaration.
    TIFF_BITS_PER_SAMPLE uses the actual TIFF sample width. The corresponding
    SOURCE_STORAGE_BITS_PER_SAMPLE choice records an equivalent source-reader
    fact for vendor images. EXPLICIT_PROFILE uses a named, reviewed range.

    Recording this choice explains how the normalization maximum was obtained,
    independently of the dtype in which an image reader returns its arrays.
    """

    OME_SIGNIFICANT_BITS = "ome_significant_bits"
    TIFF_BITS_PER_SAMPLE = "tiff_bits_per_sample"
    SOURCE_STORAGE_BITS_PER_SAMPLE = "source_storage_bits_per_sample"
    EXPLICIT_PROFILE = "explicit_profile"


@dataclass(frozen=True, slots=True)
class CanonicalTiffDataPlane:
    """

    Records how one OME channel maps to its full-resolution TIFF image directory.

    The supported format uses one explicit TiffData entry per channel, with
    channel j mapped to TIFF directory j. This avoids relying on an ambiguous
    default plane order when the image is reopened by another reader.

    Attributes:
        channel_index: Zero-based channel position in the OME Pixels record.
        ifd: TIFF image-directory index containing that channel's full-resolution plane.
        first_c: Channel position declared by TiffData, matching channel_index.
        first_z: Z position, required to be zero.
        first_t: Time position, required to be zero.
        plane_count: Number of planes described by this entry, required to be one.
    """

    channel_index: int
    ifd: int
    first_c: int
    first_z: int
    first_t: int
    plane_count: int


@dataclass(frozen=True, slots=True)
class CanonicalPyramidLevel:
    """

    Records the dimensions and storage of one level in a standardized image pyramid.

    All channel planes at a level share the same spatial dimensions, dtype,
    sample layout, TIFF tile dimensions and compression. Lower levels preserve
    channel meaning while reducing spatial resolution.

    Attributes:
        level_index: Native resolution position, starting at zero for full resolution.
        size_x: Pixel width of this level.
        size_y: Pixel height of this level.
        channel_count: Number of channel planes.
        dtype: NumPy dtype name of the stored scalar values.
        container_bits: Bits per stored sample, matching the dtype width.
        samples_per_pixel: Scalar samples within each channel pixel, required to be one.
        tile_width: Width of a stored TIFF tile in pixels.
        tile_height: Height of a stored TIFF tile in pixels.
        compression: Compression name obtained from the actual TIFF page.
    """

    level_index: int
    size_x: int
    size_y: int
    channel_count: int
    dtype: str
    container_bits: int
    samples_per_pixel: int
    tile_width: int
    tile_height: int
    compression: str


@dataclass(frozen=True, slots=True)
class CanonicalOmeTiffHeader:
    """

    Retains the checked relationship between a standardized file, its OME metadata and its reader.

    This record supplies source geometry, calibration and intensity-range facts
    to later processing. It contains the image description and file checksum,
    not decoded full-slide pixel arrays.

    Attributes:
        path: Standardized image filename inspected by the validator.
        sha256: SHA-256 of the complete file, calculated using sequential byte reads.
        image_id: Embedded OME Image ID.
        pixels_id: Embedded OME Pixels ID.
        size_x: Full-resolution image width in pixels.
        size_y: Full-resolution image height in pixels.
        size_z: Z count, required to be one.
        size_c: Number of separately stored fluorescence channels.
        size_t: Time-point count, required to be one.
        dimension_order: Embedded OME plane-dimension order.
        container_dtype: NumPy dtype name of the scalar pixel values.
        container_bits: Storage width associated with that dtype.
        tiff_bits_per_sample: TIFF sample width checked against the dtype.
        significant_bits: Explicit OME meaningful-bit declaration, when available.
        physical_size_x: Pixel width in its original recorded physical unit.
        physical_size_x_unit: Unit of the original pixel-width value.
        physical_size_x_um: Pixel width converted to µm/pixel.
        physical_size_y: Pixel height in its original recorded physical unit.
        physical_size_y_unit: Unit of the original pixel-height value.
        physical_size_y_um: Pixel height converted to µm/pixel.
        tiff_data_planes: Explicit channel-to-full-resolution-directory mappings.
        native_pyramid_levels: Checked stored levels in decreasing spatial resolution.
    """

    path: Path
    sha256: str
    image_id: str
    pixels_id: str
    size_x: int
    size_y: int
    size_z: int
    size_c: int
    size_t: int
    dimension_order: str
    container_dtype: str
    container_bits: int
    tiff_bits_per_sample: int
    significant_bits: int | None
    physical_size_x: float | None
    physical_size_x_unit: str | None
    physical_size_x_um: float | None
    physical_size_y: float | None
    physical_size_y_unit: str | None
    physical_size_y_um: float | None
    tiff_data_planes: tuple[CanonicalTiffDataPlane, ...]
    native_pyramid_levels: tuple[CanonicalPyramidLevel, ...]


@dataclass(frozen=True, slots=True)
class CanonicalOmeTiffInspection:
    """Retain the checked header and matching embedded/reader acquisition record.

    Both values come from one inspection of the current source. Acquisition is
    None when neither the embedded OME nor the open reader has a completed record.
    """

    header: CanonicalOmeTiffHeader
    acquisition: UnmixingInputMetadata | None


@dataclass(frozen=True, slots=True)
class EffectiveDigitalRange:
    """

    Describes the meaningful intensity range used for acquisition normalization.

    The storage container and meaningful range can differ. For an unsigned
    image with b meaningful bits, the header-derived maximum is 2**b - 1.
    An explicit profile supplies a named accepted maximum when header bits
    alone do not describe the measurement scale.

    Attributes:
        container_dtype: NumPy dtype of the source measurements.
        container_bits: Bits occupied by that dtype.
        meaningful_bits: Bit count associated with the accepted meaningful range.
        maximum: Positive finite intensity maximum used in the normalization divisor.
        authority: Source of the range declaration, represented by DigitalRangeAuthority.
        profile_id: Required name for EXPLICIT_PROFILE, otherwise None.
    """

    container_dtype: str
    container_bits: int
    meaningful_bits: int
    maximum: float
    authority: DigitalRangeAuthority
    profile_id: str | None = None

    def __post_init__(self) -> None:
        """

        Checks the range record's bit counts, positive maximum and profile declaration.

        An explicit profile requires a nonempty unpadded identifier. Other range
        sources must leave profile_id absent so the saved record clearly states
        whether a profile or an image-header field supplied the value.

        Raises:
            CanonicalOmeTiffError: Bit counts, maximum or range-source declaration
                are invalid. The resolver separately checks compatibility with the
                actual source dtype and container width.
        """

        if self.container_bits <= 0 or self.meaningful_bits <= 0:
            raise CanonicalOmeTiffError("digital-range bit counts must be positive")
        if not math.isfinite(self.maximum) or self.maximum <= 0:
            raise CanonicalOmeTiffError("digital-range maximum must be finite and positive")
        if not isinstance(self.authority, DigitalRangeAuthority):
            raise CanonicalOmeTiffError("digital-range authority must be a DigitalRangeAuthority")
        if self.authority is DigitalRangeAuthority.EXPLICIT_PROFILE:
            if (
                type(self.profile_id) is not str
                or not self.profile_id
                or self.profile_id != self.profile_id.strip()
            ):
                raise CanonicalOmeTiffError(
                    "explicit digital-range authority requires a stable profile ID"
                )
        elif self.profile_id is not None:
            raise CanonicalOmeTiffError(
                "digital-range profile ID is permitted only for explicit profiles"
            )


def validate_canonical_ome_tiff_header(
    path: str | Path,
    source: BioFormatsSource,
) -> CanonicalOmeTiffHeader:
    """

    Validates a standardized file against its already-open Bio-Formats reader.

    We compare the embedded OME and TIFF pages with the reader's selected
    series between two fresh file-identity snapshots. If the reader supplies
    an opened-file identity, both snapshots must describe that same opening.
    This standalone boundary checks the header without requiring acquisition
    metadata to be present or equal through both metadata routes.

    Args:
        path: Standardized .ome.tif or .ome.tiff filename.
        source: Already-open BioFormatsSource describing the same file.

    Returns:
        CanonicalOmeTiffHeader containing the checked source and pyramid facts.

    Raises:
        CanonicalOmeTiffError: The file layout, reader description, source path
            or opened-file identity is inconsistent.
    """

    if Path(path).is_dir():
        return _processed_image_header(path, source)
    with _checked_canonical_tiff_inspection(path, source) as (header, _embedded):
        return header


@contextmanager
def _checked_canonical_tiff_inspection(
    path: str | Path,
    source: BioFormatsSource,
) -> Iterator[tuple[CanonicalOmeTiffHeader, OME]]:
    """Inspect one TIFF/OME description within a checked current-file extent.

    We require a fresh identity before reading structure and embedded metadata,
    compare it with the reader opening when available, and take another fresh
    identity after the caller consumes the parsed metadata. Equality retains
    the path, content digest, size, mtime, device and inode checks across that
    whole operation. A changed file cannot produce a successful result.

    The yielded header uses the first checked digest. Its embedded OME is the
    same parse used to validate every TIFF page and pyramid level. The caller
    can resolve acquisition from it before the final identity check, without
    reopening the TIFF or repeating the structural inspection.
    """

    if not hasattr(source, "metadata") or not hasattr(source, "series"):
        raise CanonicalOmeTiffError("canonical header validation requires an open BioFormatsSource")
    candidate = Path(path)
    _validate_regular_ome_tiff(candidate)
    identity = getattr(source, "identity", None)
    if identity is not None and type(identity) is not OpenedSourceIdentity:
        raise CanonicalOmeTiffError("canonical source identity must be IO-owned")
    try:
        before = snapshot_opened_source_identity(candidate)
    except (OSError, ValueError) as exc:
        raise CanonicalOmeTiffError("canonical source locator cannot be revalidated") from exc
    if identity is not None:
        if before.resolved_path != identity.resolved_path:
            raise CanonicalOmeTiffError("opened canonical source locator differs")
        if before != identity:
            raise CanonicalOmeTiffError(
                "canonical source identity changed after the Bio-Formats handle opened"
            )
    header, embedded = _validate_canonical_ome_tiff_structure(
        candidate,
        ome=source.metadata,
        series=source.series,
        sha256=before.file_sha256,
    )
    yield header, embedded
    try:
        after = snapshot_opened_source_identity(candidate)
    except (OSError, ValueError) as exc:
        raise CanonicalOmeTiffError("canonical source locator cannot be revalidated") from exc
    if after != before:
        raise CanonicalOmeTiffError(
            "canonical source identity changed during inspection"
        )


def _processed_image_header(path, source) -> CanonicalOmeTiffHeader:
    """Reuse the completed generation's accepted array and OME description.

    Historical field names stay compatible with quantitative callers. Zarr has
    no TIFF plane mapping; its identity is completed metadata, not pixel bytes.
    """
    from .processed_image import ProcessedImageSource
    if not isinstance(source, ProcessedImageSource):
        raise CanonicalOmeTiffError("A processed image requires its direct Zarr source")
    candidate = Path(path).resolve(strict=True)
    if source.identity.resolved_path != candidate:
        raise CanonicalOmeTiffError("Opened processed generation differs from requested path")
    image = source.metadata.images[0]
    pixels = image.pixels
    resolutions = source.series[0].resolutions
    dtype = resolutions[0].dtype
    bits = dtype.itemsize * 8
    significant = pixels.significant_bits
    if significant is not None and not 0 < significant <= bits:
        raise CanonicalOmeTiffError("SignificantBits exceeds the stored scalar width")
    xv, xu, xm = _physical_calibration(pixels.physical_size_x, pixels.physical_size_x_unit, "X")
    yv, yu, ym = _physical_calibration(pixels.physical_size_y, pixels.physical_size_y_unit, "Y")
    levels = tuple(CanonicalPyramidLevel(level.index, level.size_x, level.size_y,
        level.size_c, level.dtype.name, level.dtype.itemsize * 8, 1, 256, 256,
        "blosc_zstd") for level in resolutions)
    return CanonicalOmeTiffHeader(candidate, source.identity.file_sha256, image.id, pixels.id,
        pixels.size_x, pixels.size_y, pixels.size_z, pixels.size_c, pixels.size_t,
        _enum_value(pixels.dimension_order), dtype.name, bits, bits, significant,
        xv, xu, xm, yv, yu, ym, (), levels)


def inspect_canonical_ome_tiff(
    path: str | Path,
    source: BioFormatsSource,
) -> CanonicalOmeTiffInspection:
    """

    Reads checked structure and matching embedded/reader acquisition in one inspection.

    A quantitative image must expose the same imaging settings through both
    routes. We validate the TIFF structure once, resolve acquisition from that
    same embedded OME parse and compare it with the reader's record. Fresh file
    identities bracket the complete operation and must match the reader opening.
    Completed processed images retain their existing direct metadata route.

    Args:
        path: Standardized image whose image metadata should be read.
        source: BioFormatsSource already opened for that image.

    Returns:
        Checked header and shared UnmixingInputMetadata. Acquisition is None
        when both views lack a completed record. Typed embedded acquisition fields
        receive the same initial collection as the live reader before comparison.

    Raises:
        CanonicalOmeTiffError: File inspection fails or embedded and reader
            acquisition records disagree.
    """

    if Path(path).is_dir():
        return CanonicalOmeTiffInspection(
            _processed_image_header(path, source),
            resolve_unmixing_input_metadata(source.metadata, 0),
        )
    with _checked_canonical_tiff_inspection(path, source) as (header, embedded):
        source_metadata = resolve_unmixing_input_metadata(source.metadata, 0)
        from .metadata.canonical import canonicalize_ome

        embedded_metadata = resolve_unmixing_input_metadata(
            canonicalize_ome(embedded, source_image_indices=(0,)), 0
        )
        if source_metadata != embedded_metadata:
            raise CanonicalOmeTiffError("embedded and Bio-Formats unmixing metadata differ")
        return CanonicalOmeTiffInspection(header, source_metadata)


def resolve_effective_digital_range(
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    *,
    explicit_profile: EffectiveDigitalRange | None = None,
    storage_authority: DigitalRangeAuthority | None = None,
) -> EffectiveDigitalRange:
    """

    Selects the meaningful maximum from an explicit profile or recorded image-header bits.

    An explicit profile takes precedence and must match the actual container
    dtype and width. Otherwise, use OME SignificantBits when available, followed
    by actual stored sample width. For an unsigned image, b meaningful bits
    give maximum = 2**b - 1. Signed and floating containers require an explicit
    profile because their dtype limits do not establish the measured signal range.

    For vendor sources, a dtype returned by the decoder is insufficient by
    itself to establish stored sample width. The reader must provide the actual
    stored-bit fact, or the image must declare SignificantBits.

    Args:
        header: Checked CanonicalOmeTiffHeader or SourceImageFacts for the image.
        explicit_profile: Optional accepted EffectiveDigitalRange with profile_id.
        storage_authority: Optional source label for a vendor reader's actual
            stored-bit evidence. A TIFF label requires evidence originating
            from the actual TIFF BitsPerSample field.

    Returns:
        EffectiveDigitalRange with the selected maximum and its recorded source.

    Raises:
        CanonicalOmeTiffError: A profile conflicts with the container, meaningful
            bits exceed storage, or the source lacks a supported range declaration.
    """

    if type(header) not in (CanonicalOmeTiffHeader, SourceImageFacts):
        raise CanonicalOmeTiffError("digital-range resolution requires IO-owned image facts")
    source_facts = type(header) is SourceImageFacts
    if storage_authority is not None:
        if (
            not source_facts
            or type(storage_authority) is not DigitalRangeAuthority
            or storage_authority
            not in (
                DigitalRangeAuthority.TIFF_BITS_PER_SAMPLE,
                DigitalRangeAuthority.SOURCE_STORAGE_BITS_PER_SAMPLE,
            )
        ):
            raise CanonicalOmeTiffError("storage authority is not supported for these image facts")
        if header.storage_bits is None:
            raise CanonicalOmeTiffError(
                "source lacks validated actual-storage evidence for the requested authority"
            )
        if (
            storage_authority is DigitalRangeAuthority.TIFF_BITS_PER_SAMPLE
            and header.storage_bits.origin != "tiff_bits_per_sample"
        ):
            raise CanonicalOmeTiffError(
                "TIFF storage authority requires actual TIFF BitsPerSample evidence"
            )
    dtype = np.dtype(header.container_dtype)
    if explicit_profile is not None:
        if explicit_profile.authority is not DigitalRangeAuthority.EXPLICIT_PROFILE:
            raise CanonicalOmeTiffError(
                "an explicit digital-range profile must declare explicit_profile authority"
            )
        if (
            explicit_profile.container_dtype != header.container_dtype
            or explicit_profile.container_bits != header.container_bits
        ):
            raise CanonicalOmeTiffError(
                "explicit digital-range profile conflicts with the TIFF container"
            )
        if explicit_profile.meaningful_bits > header.container_bits:
            raise CanonicalOmeTiffError("explicit meaningful bits exceed the TIFF container")
        if np.issubdtype(dtype, np.unsignedinteger) and explicit_profile.maximum > float(
            (1 << header.container_bits) - 1
        ):
            raise CanonicalOmeTiffError(
                "explicit digital maximum exceeds the unsigned TIFF container"
            )
        return explicit_profile
    if not np.issubdtype(dtype, np.unsignedinteger):
        raise CanonicalOmeTiffError(
            "signed and floating containers require an explicit validated digital-range profile"
        )
    # Use the meaningful bits declared by OME before considering the
    # full stored sample width. A larger container may contain fewer
    # meaningful measurement bits.
    if header.significant_bits is not None:
        meaningful_bits = header.significant_bits
        authority = DigitalRangeAuthority.OME_SIGNIFICANT_BITS
    elif source_facts:
        if header.storage_bits is None:
            raise CanonicalOmeTiffError(
                "source lacks OME SignificantBits or validated actual-storage evidence"
            )
        meaningful_bits = header.storage_bits.bits
        if type(meaningful_bits) is not int or not 0 < meaningful_bits <= header.container_bits:
            raise CanonicalOmeTiffError("meaningful source bits exceed the decoded container")
        authority = storage_authority or DigitalRangeAuthority.SOURCE_STORAGE_BITS_PER_SAMPLE
    else:
        meaningful_bits = header.tiff_bits_per_sample
        authority = DigitalRangeAuthority.TIFF_BITS_PER_SAMPLE
    return EffectiveDigitalRange(
        container_dtype=header.container_dtype,
        container_bits=header.container_bits,
        meaningful_bits=meaningful_bits,
        maximum=float((1 << meaningful_bits) - 1),
        authority=authority,
    )


def resolve_acquisition_digital_range(
    header: CanonicalOmeTiffHeader | SourceImageFacts,
    acquisition: UnmixingInputMetadata,
) -> EffectiveDigitalRange | None:
    """

    Resolves the image's normalization maximum using its reviewed acquisition profile when present.

    The acquisition record can supply a range-profile ID, meaningful-bit count
    and maximum. These three values must be provided together. Construct the
    explicit range using the actual image container, then pass it through the
    same compatibility checks used for header-derived ranges.

    Args:
        header: Checked standardized or vendor-source image facts.
        acquisition: Completed UnmixingInputMetadata with optional reviewed range fields.

    Returns:
        EffectiveDigitalRange selected from the complete profile or image header,
        or None for metadata-known normalized values used without intensity correction.

    Raises:
        CanonicalOmeTiffError: Input record types, profile completeness or the
            resulting range/container relationship are invalid.
    """

    if type(header) not in (CanonicalOmeTiffHeader, SourceImageFacts):
        raise CanonicalOmeTiffError("digital-range resolution requires IO-owned image facts")
    if type(acquisition) is not UnmixingInputMetadata:
        raise CanonicalOmeTiffError(
            "digital-range resolution requires canonical image metadata"
        )
    if acquisition.correction_mode == "as_stored":
        # No acquisition divisor or count-range QC is inferred from calibrated pixels.
        return None
    profile_values = (
        acquisition.digital_range_profile_id,
        acquisition.digital_range_meaningful_bits,
        acquisition.digital_range_maximum,
    )
    if profile_values == (None, None, None):
        return resolve_effective_digital_range(header)
    profile_id, meaningful_bits, maximum = profile_values
    if (
        type(profile_id) is not str
        or not profile_id
        or profile_id != profile_id.strip()
        or type(meaningful_bits) is not int
        or type(maximum) is not float
    ):
        raise CanonicalOmeTiffError("canonical digital-range profile is incomplete")
    profile = EffectiveDigitalRange(
        container_dtype=header.container_dtype,
        container_bits=header.container_bits,
        meaningful_bits=meaningful_bits,
        maximum=maximum,
        authority=DigitalRangeAuthority.EXPLICIT_PROFILE,
        profile_id=profile_id,
    )
    return resolve_effective_digital_range(header, explicit_profile=profile)


def _validate_canonical_ome_tiff_structure(
    path: str | Path,
    *,
    ome: OME,
    series: tuple[SeriesInfo, ...],
    sha256: str,
) -> tuple[CanonicalOmeTiffHeader, OME]:
    """

    Compares the embedded OME, TIFF pages and reader description of one standardized image.

    Require one analytical BigTIFF series with embedded OME metadata. The
    image/channel dimensions and calibration must agree with Bio-Formats.
    Each channel must map to one full-resolution TIFF image directory, and
    every stored pyramid level must retain the same channels and scalar dtype.

    After checking those relationships, convert physical calibration to
    µm/pixel and retain the digest supplied by the owning checked inspection.
    Return the parsed embedded OME for acquisition comparison without another
    TIFF read. No complete pixel array is decoded by this inspection.

    Args:
        path: Image file to inspect.
        ome: OME metadata supplied by the selected reader.
        series: Reader series descriptions, containing exactly the selected series zero.
        sha256: Current complete-file digest supplied by the checked inspection.

    Returns:
        CanonicalOmeTiffHeader with checked file, dimension, calibration,
        channel-mapping and pyramid facts, together with its parsed embedded OME.

    Raises:
        CanonicalOmeTiffError: The file cannot be interpreted in the supported
            standardized layout or differs from the supplied reader metadata.
    """

    candidate = Path(path)
    if not isinstance(ome, OME) or len(ome.images) != 1:
        raise CanonicalOmeTiffError("canonical input must contain exactly one OME Image")
    if len(series) != 1:
        raise CanonicalOmeTiffError("canonical input must contain exactly one Bio-Formats series")
    if series[0].index != 0:
        raise CanonicalOmeTiffError("the canonical Bio-Formats series must have index 0")
    try:
        with tifffile.TiffFile(candidate) as tif:
            if not tif.is_bigtiff:
                raise CanonicalOmeTiffError("canonical OME-TIFF must be BigTIFF")
            if tif.ome_metadata is None:
                raise CanonicalOmeTiffError("canonical TIFF has no embedded OME metadata")
            try:
                embedded = from_xml(tif.ome_metadata)
            except Exception as exc:
                raise CanonicalOmeTiffError(
                    f"canonical TIFF has invalid OME metadata: {exc}"
                ) from exc
            if len(embedded.images) != 1:
                raise CanonicalOmeTiffError("canonical input must contain exactly one OME Image")
            if len(tif.series) != 1:
                raise CanonicalOmeTiffError(
                    "canonical input must contain exactly one TIFF image series"
                )
            pixels = embedded.images[0].pixels
            source_pixels = ome.images[0].pixels
            _validate_ome_reader_contract(ome.images[0], embedded.images[0], series[0])
            mappings = _validate_tiff_data(pixels)
            levels = _validate_tiff_levels(tif, pixels, series[0])
            significant_bits = pixels.significant_bits
            container_bits = levels[0].container_bits
            if significant_bits is not None and not 0 < significant_bits <= container_bits:
                raise CanonicalOmeTiffError(
                    "OME SignificantBits must be positive and no greater than "
                    "the unsigned container bits"
                )
            x_value, x_unit, x_um = _physical_calibration(
                pixels.physical_size_x,
                pixels.physical_size_x_unit,
                "X",
            )
            y_value, y_unit, y_um = _physical_calibration(
                pixels.physical_size_y,
                pixels.physical_size_y_unit,
                "Y",
            )
            header = CanonicalOmeTiffHeader(
                path=candidate,
                sha256=sha256,
                image_id=embedded.images[0].id,
                pixels_id=pixels.id,
                size_x=pixels.size_x,
                size_y=pixels.size_y,
                size_z=pixels.size_z,
                size_c=pixels.size_c,
                size_t=pixels.size_t,
                dimension_order=_enum_value(pixels.dimension_order),
                container_dtype=levels[0].dtype,
                container_bits=container_bits,
                tiff_bits_per_sample=levels[0].container_bits,
                significant_bits=significant_bits,
                physical_size_x=x_value,
                physical_size_x_unit=x_unit,
                physical_size_x_um=x_um,
                physical_size_y=y_value,
                physical_size_y_unit=y_unit,
                physical_size_y_um=y_um,
                tiff_data_planes=mappings,
                native_pyramid_levels=levels,
            )
    except CanonicalOmeTiffError:
        raise
    except Exception as exc:
        raise CanonicalOmeTiffError(f"failed to validate canonical OME-TIFF: {exc}") from exc
    return header, embedded


def _validate_regular_ome_tiff(path: Path) -> None:
    """

    Checks that an image path refers to a readable regular file with the required extension.

    Args:
        path: File to inspect before opening TIFF directories.

    Raises:
        CanonicalOmeTiffError: The name is unsupported, the file cannot be inspected,
            or the path refers to another filesystem object such as a directory.
    """

    if not path.name.casefold().endswith(_OME_TIFF_SUFFIXES):
        raise CanonicalOmeTiffError("canonical input must end in .ome.tif or .ome.tiff")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise CanonicalOmeTiffError(f"canonical input is not a readable file: {path}") from exc
    if not stat.S_ISREG(mode):
        raise CanonicalOmeTiffError("canonical input must be a regular file")


def _validate_ome_reader_contract(
    source_image: Any,
    embedded_image: Any,
    series: SeriesInfo,
) -> None:
    """

    Checks that Bio-Formats and embedded OME identify the same image planes.

    The Image and Pixels IDs must agree, followed by X/Y/Z/C/T dimensions,
    dimension order, dtype, per-channel sample counts and physical calibration.
    Channel IDs and fluorophores must also appear in the same order. Finally,
    compare these OME facts with the reader's actual level-zero descriptor.

    Args:
        source_image: OME Image returned by Bio-Formats.
        embedded_image: Image decoded directly from the TIFF's embedded OME text.
        series: Actual reader series containing the level-zero description.

    Raises:
        CanonicalOmeTiffError: The descriptions identify different dimensions,
            channels, calibration or interleaved/RGB sample organization.
    """

    if source_image.id != embedded_image.id:
        raise CanonicalOmeTiffError("embedded OME Image ID conflicts with Bio-Formats OME")
    source_pixels = source_image.pixels
    embedded_pixels = embedded_image.pixels
    if source_pixels.id != embedded_pixels.id:
        raise CanonicalOmeTiffError("embedded OME Pixels ID conflicts with Bio-Formats OME")
    for label, pixels in (("Bio-Formats OME", source_pixels), ("embedded OME", embedded_pixels)):
        if pixels.size_z != 1 or pixels.size_t != 1:
            raise CanonicalOmeTiffError(f"{label} must have SizeZ=SizeT=1")
        if len(pixels.channels) != pixels.size_c:
            raise CanonicalOmeTiffError(f"{label} must contain exactly one Channel per C plane")
        samples = tuple(int(channel.samples_per_pixel or 1) for channel in pixels.channels)
        if any(value != 1 for value in samples):
            raise CanonicalOmeTiffError(f"{label} Channel SamplesPerPixel must equal 1")
        if pixels.interleaved is True:
            raise CanonicalOmeTiffError(f"{label} must be planar and non-RGB")
    source_contract = _ome_pixel_contract(source_pixels)
    embedded_contract = _ome_pixel_contract(embedded_pixels)
    if source_contract != embedded_contract:
        raise CanonicalOmeTiffError(
            "embedded OME dimensions/dtype/sample topology conflicts with Bio-Formats OME"
        )
    source_channels = tuple((channel.id, channel.fluor) for channel in source_pixels.channels)
    embedded_channels = tuple((channel.id, channel.fluor) for channel in embedded_pixels.channels)
    if source_channels != embedded_channels:
        raise CanonicalOmeTiffError(
            "embedded OME Channel IDs or Fluor identities conflict with Bio-Formats OME"
        )
    base = series.resolutions[0]
    reader_contract = (
        base.size_x,
        base.size_y,
        base.size_z,
        base.size_c,
        base.size_t,
        base.rgb_samples,
        np.dtype(base.dtype),
        base.dimension_order,
    )
    ome_contract = (
        embedded_pixels.size_x,
        embedded_pixels.size_y,
        embedded_pixels.size_z,
        embedded_pixels.size_c,
        embedded_pixels.size_t,
        1,
        _ome_dtype(embedded_pixels.type),
        _enum_value(embedded_pixels.dimension_order),
    )
    if reader_contract != ome_contract:
        raise CanonicalOmeTiffError(
            "Bio-Formats dimensions/dtype/sample topology conflicts with OME"
        )


def _ome_pixel_contract(pixels: Any) -> tuple[Any, ...]:
    """

    Collects the OME pixel fields compared between embedded metadata and the image reader.

    Args:
        pixels: OME Pixels object from either metadata view.

    Returns:
        Tuple containing dimensions, dimension order, NumPy dtype, per-channel
        sample counts and the original X/Y physical calibration declarations.
    """

    return (
        pixels.size_x,
        pixels.size_y,
        pixels.size_z,
        pixels.size_c,
        pixels.size_t,
        _enum_value(pixels.dimension_order),
        _ome_dtype(pixels.type),
        tuple(int(channel.samples_per_pixel or 1) for channel in pixels.channels),
        _calibration_contract(pixels),
    )


def _calibration_contract(pixels: Any) -> tuple[Any, ...]:
    """

    Collects the physical pixel width and height with their declared units.

    Args:
        pixels: OME Pixels record to compare.

    Returns:
        Tuple of X value/unit followed by Y value/unit. Units remain attached
        so the reader comparison cannot confuse numerically equal values in
        different units.
    """

    return (
        pixels.physical_size_x,
        _optional_enum_value(pixels.physical_size_x_unit),
        pixels.physical_size_y,
        _optional_enum_value(pixels.physical_size_y_unit),
    )


def _validate_tiff_data(pixels: Any) -> tuple[CanonicalTiffDataPlane, ...]:
    """

    Checks the explicit mapping from OME channels to full-resolution TIFF directories.

    For channel j, the supported mapping is IFD=j, FirstC=j, FirstZ=0,
    FirstT=0 and PlaneCount=1. Requiring that mapping for every channel ensures
    complete, unambiguous coverage of the planes in this one image file.

    Args:
        pixels: Embedded OME Pixels record containing its TiffData entries.

    Returns:
        Tuple of CanonicalTiffDataPlane records in channel order.

    Raises:
        CanonicalOmeTiffError: A channel mapping is missing, points to an external
            file, changes channel order, or describes a different Z/time/plane count.
    """

    blocks = tuple(pixels.tiff_data_blocks)
    if len(blocks) != pixels.size_c:
        raise CanonicalOmeTiffError("OME TiffData must map exactly SizeC primary IFDs")
    mappings: list[CanonicalTiffDataPlane] = []
    for channel_index, block in enumerate(blocks):
        if block.uuid is not None:
            raise CanonicalOmeTiffError("external UUID OME TiffData mappings are forbidden")
        actual = (
            block.ifd,
            block.first_c,
            block.first_z,
            block.first_t,
            block.plane_count,
        )
        # Channel j maps to full-resolution directory j in this file,
        # with one scalar plane at the only Z and time positions.
        expected = (channel_index, channel_index, 0, 0, 1)
        if actual != expected:
            raise CanonicalOmeTiffError(
                "OME TiffData must be a bijective channel-to-primary-IFD mapping"
            )
        mappings.append(
            CanonicalTiffDataPlane(
                channel_index=channel_index,
                ifd=block.ifd,
                first_c=block.first_c,
                first_z=block.first_z,
                first_t=block.first_t,
                plane_count=block.plane_count,
            )
        )
    return tuple(mappings)


def _validate_tiff_levels(
    tif: tifffile.TiffFile,
    pixels: Any,
    series: SeriesInfo,
) -> tuple[CanonicalPyramidLevel, ...]:
    """

    Checks every stored pyramid level against OME and Bio-Formats.

    Each level must contain one tiled scalar page per channel. Pages within
    that level must share dtype, sample width, tile layout and lossless
    compression. TIFF's level shape must equal the reader's channel and spatial
    dimensions, with the same dtype and OME dimension order.

    As levels progress, width and height may stay equal on one axis but must
    decrease on at least one. This accepts the actual stored reduction pattern
    while rejecting repeated or enlarged levels.

    Args:
        tif: Open tifffile.TiffFile for the candidate image.
        pixels: Embedded OME Pixels record.
        series: Matching Bio-Formats series and ordered native resolutions.

    Returns:
        Tuple of CanonicalPyramidLevel descriptions in resolution order.

    Raises:
        CanonicalOmeTiffError: Page counts, level counts, shapes, channel layout,
            dtype, sample width, tiling, compression or decreasing dimensions
            fail their comparisons.
    """

    if len(tif.pages) != pixels.size_c:
        raise CanonicalOmeTiffError("TIFF must contain exactly SizeC primary IFDs")
    tiff_levels = tuple(tif.series[0].levels)
    if len(tiff_levels) != len(series.resolutions):
        raise CanonicalOmeTiffError("TIFF native pyramid count conflicts with Bio-Formats")
    result: list[CanonicalPyramidLevel] = []
    previous: tuple[int, int] | None = None
    for level_index, (tiff_level, resolution) in enumerate(
        zip(tiff_levels, series.resolutions, strict=True)
    ):
        if resolution.index != level_index:
            raise CanonicalOmeTiffError("Bio-Formats native pyramid order is invalid")
        pages = tuple(tiff_level.pages)
        if len(pages) != pixels.size_c:
            raise CanonicalOmeTiffError(
                f"TIFF pyramid level {level_index} does not contain SizeC pages"
            )
        page_facts = tuple(_page_storage_facts(page) for page in pages)
        if len(set(page_facts)) != 1:
            raise CanonicalOmeTiffError(
                f"TIFF pyramid level {level_index} has heterogeneous storage pages"
            )
        (
            page_dtype,
            bits,
            samples,
            tile_width,
            tile_height,
            compression,
            photometric,
        ) = page_facts[0]
        if photometric == "rgb" or samples != 1:
            raise CanonicalOmeTiffError("canonical TIFF pages must be planar and non-RGB")
        if tile_width is None or tile_height is None:
            raise CanonicalOmeTiffError("canonical TIFF pages must be tiled")
        if compression not in _APPROVED_LOSSLESS_COMPRESSIONS:
            raise CanonicalOmeTiffError(
                f"TIFF compression {compression!r} is not approved lossless compression"
            )
        dtype = np.dtype(tiff_level.dtype)
        shape = tuple(tiff_level.shape)
        expected_shape = (
            (pixels.size_c, resolution.size_y, resolution.size_x)
            if pixels.size_c > 1
            else (resolution.size_y, resolution.size_x)
        )
        if shape != expected_shape:
            raise CanonicalOmeTiffError(
                f"TIFF pyramid level {level_index} dimensions conflict with Bio-Formats"
            )
        if (
            dtype != np.dtype(resolution.dtype)
            or dtype != _ome_dtype(pixels.type)
            or page_dtype != dtype.name
        ):
            raise CanonicalOmeTiffError(
                f"TIFF pyramid level {level_index} dtype conflicts with OME/Bio-Formats"
            )
        if (
            resolution.rgb_samples != 1
            or resolution.size_c != pixels.size_c
            or resolution.size_z != 1
            or resolution.size_t != 1
            or resolution.dimension_order != _enum_value(pixels.dimension_order)
        ):
            raise CanonicalOmeTiffError(
                f"Bio-Formats pyramid level {level_index} sample topology is non-planar"
            )
        if bits != dtype.itemsize * 8:
            raise CanonicalOmeTiffError(
                f"TIFF pyramid level {level_index} BitsPerSample conflicts with dtype"
            )
        dimensions = (resolution.size_x, resolution.size_y)
        if previous is not None and (
            dimensions[0] > previous[0] or dimensions[1] > previous[1] or dimensions == previous
        ):
            raise CanonicalOmeTiffError("native pyramid dimensions are not ordered")
        previous = dimensions
        result.append(
            CanonicalPyramidLevel(
                level_index=level_index,
                size_x=resolution.size_x,
                size_y=resolution.size_y,
                channel_count=pixels.size_c,
                dtype=dtype.name,
                container_bits=bits,
                samples_per_pixel=samples,
                tile_width=tile_width,
                tile_height=tile_height,
                compression=compression,
            )
        )
    if not result:
        raise CanonicalOmeTiffError("canonical TIFF has no native pyramid levels")
    return tuple(result)


def _page_storage_facts(page: Any) -> tuple[Any, ...]:
    """

    Reads the storage fields used to compare channel pages within one TIFF level.

    Some tifffile pages reuse a keyframe's storage description. Follow that
    description when present, then normalize compression and photometric names
    for comparison. A non-tiled page reports missing tile dimensions, which the
    enclosing level check rejects for standardized output.

    Args:
        page: TIFF page or frame to inspect.

    Returns:
        Tuple of dtype, sample bits, sample count, tile width/height,
        compression name and photometric interpretation.
    """

    storage = getattr(page, "keyframe", page)
    compression = str(getattr(storage.compression, "name", storage.compression)).casefold()
    if compression == "1":
        compression = "none"
    photometric = str(getattr(storage.photometric, "name", storage.photometric)).casefold()
    return (
        np.dtype(storage.dtype).name,
        int(storage.bitspersample),
        int(storage.samplesperpixel),
        int(storage.tilewidth) if storage.is_tiled else None,
        int(storage.tilelength) if storage.is_tiled else None,
        compression,
        photometric,
    )


def _physical_calibration(
    value: float | None,
    unit: Any | None,
    axis: str,
) -> tuple[float | None, str | None, float | None]:
    """

    Converts one declared physical pixel size to micrometres while retaining its original value and unit.

    A present value must be finite, positive and accompanied by a length unit.
    The unit library performs the conversion; for example, 0.0005 mm becomes
    0.5 µm. An absent value remains missing so the review workflow can request it.

    Args:
        value: Original OME physical pixel size, or None.
        unit: Unit associated with that physical size.
        axis: "X" or "Y", included in any calibration error.

    Returns:
        Original numerical value, its unit name and the value in µm/pixel.
        An absent physical size returns three None values.

    Raises:
        CanonicalOmeTiffError: The value is invalid, lacks a unit, or the unit
            cannot be converted to a physical length.
    """

    if value is None:
        return None, None, None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise CanonicalOmeTiffError(
            f"OME physical pixel calibration for {axis} must be finite and positive"
        )
    unit_value = _optional_enum_value(unit)
    if unit_value is None:
        raise CanonicalOmeTiffError(
            f"OME physical pixel calibration for {axis} requires an explicit unit"
        )
    try:
        micrometers = float((numeric * ureg(unit_value)).to(ureg.micrometer).magnitude)
    except Exception as exc:
        raise CanonicalOmeTiffError(
            f"OME physical pixel calibration for {axis} is not a length"
        ) from exc
    return numeric, unit_value, micrometers


def _ome_dtype(pixel_type: Any) -> np.dtype:
    """

    Converts a supported OME pixel-type name to its NumPy dtype.

    OME "float" corresponds to float32 and "double" to float64. Signed and
    unsigned integer types retain their declared 8-, 16- or 32-bit widths.

    Args:
        pixel_type: OME type value or enum from the Pixels record.

    Returns:
        NumPy dtype used to compare reader and TIFF scalar samples.

    Raises:
        CanonicalOmeTiffError: The OME type is outside the supported real-valued types.
    """

    value = _enum_value(pixel_type)
    mapping = {
        "int8": "int8",
        "int16": "int16",
        "int32": "int32",
        "uint8": "uint8",
        "uint16": "uint16",
        "uint32": "uint32",
        "float": "float32",
        "double": "float64",
    }
    try:
        return np.dtype(mapping[value])
    except KeyError as exc:
        raise CanonicalOmeTiffError(f"unsupported canonical OME pixel type: {value!r}") from exc


def _enum_value(value: Any) -> str:
    """

    Reads a stable text value from an OME enum or already textual field.

    Args:
        value: Enum or scalar value. Its value attribute is preferred, followed
            by its name attribute and then the value itself.

    Returns:
        Text used by the metadata comparisons.
    """

    return str(getattr(value, "value", getattr(value, "name", value)))


def _optional_enum_value(value: Any | None) -> str | None:
    """

    Reads an optional enum value while keeping a missing declaration as None.

    Args:
        value: Enum/text field or None.

    Returns:
        Text from _enum_value(), or None when the input is absent.
    """

    return None if value is None else _enum_value(value)
