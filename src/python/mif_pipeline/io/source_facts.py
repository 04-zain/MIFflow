"""

Collects the selected image's dimensions, storage type and physical calibration.

The opened image reader and OME metadata both describe the selected image.
This module checks that they agree, then retains those facts for regional
unmixing and output metadata. It also records the source's native pyramid
levels so a pixel-preserving copy can keep their actual dimensions and dtype.

Pixel-array storage and meaningful intensity range are recorded separately.
A decoded uint16 array has a 16-bit container, while OME SignificantBits can
state that only 12 bits contain meaningful measurements. The later digital-
range calculation uses those recorded facts when selecting the normalization
maximum. Collecting them here does not require decoding the whole image.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from ome_types import OME

from .backends import BioFormatsSource, OpenedSourceIdentity, ResolutionInfo, SourceStorageBits

# ==============================================================================
#                                  Source facts
# ==============================================================================


class SourceFactsError(RuntimeError):
    """

    Reports disagreement between the selected reader image and its OME metadata.
    """

@dataclass(frozen=True, slots=True)
class SourcePyramidLevel:
    """

    Describes one stored resolution of the selected analytical image.

    The level describes the array returned by the reader. Its dimensions and
    channel count are checked before it is used in a copy or processing record.

    Attributes:
        level_index: Zero-based native resolution index, with zero representing
            the full-resolution image.
        size_x: Pixel width of this level.
        size_y: Pixel height of this level.
        channel_count: Number of fluorescence channels retained at this level.
        dtype: NumPy dtype name of the decoded values, such as "uint16".
        container_bits: Bits in that dtype's storage container.
        samples_per_pixel: Samples within one channel plane; the supported
            analytical layout uses one scalar sample per channel pixel.
    """

    level_index: int
    size_x: int
    size_y: int
    channel_count: int
    dtype: str
    container_bits: int
    samples_per_pixel: int


@dataclass(frozen=True, slots=True)
class SourceImageFacts:
    """

    Retains the source-image facts used to read and interpret quantitative pixels.

    X and Y calibration are stored both in their original OME units and in
    µm/pixel. The original values preserve the acquisition description; the
    converted values support calculations in one physical unit throughout the
    pipeline. Missing calibration remains None until it is explicitly supplied.

    Attributes:
        identity: Opened source identity containing the file's recorded location
            and content identity.
        image_id: Selected OME Image ID.
        pixels_id: Pixels ID associated with that image.
        size_x: Full-resolution width in pixels.
        size_y: Full-resolution height in pixels.
        size_z: Number of Z positions.
        size_c: Number of image channels.
        size_t: Number of time points.
        dimension_order: OME dimension order used to interpret the source planes.
        container_dtype: Decoded NumPy pixel dtype name.
        container_bits: Number of bits occupied by that dtype.
        significant_bits: Explicit OME SignificantBits, or None when absent.
        physical_size_x: Original physical pixel width from OME.
        physical_size_x_unit: Unit attached to the original pixel width.
        physical_size_x_um: Pixel width converted to µm/pixel.
        physical_size_y: Original physical pixel height from OME.
        physical_size_y_unit: Unit attached to the original pixel height.
        physical_size_y_um: Pixel height converted to µm/pixel.
        native_resolutions: Reader descriptors for all stored levels of this image.
        storage_bits: Actual stored-bit information reported by the source reader,
            when the file format exposes it.
    """

    identity: OpenedSourceIdentity
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
    significant_bits: int | None
    physical_size_x: float | None
    physical_size_x_unit: str | None
    physical_size_x_um: float | None
    physical_size_y: float | None
    physical_size_y_unit: str | None
    physical_size_y_um: float | None
    native_resolutions: tuple[ResolutionInfo, ...]
    storage_bits: SourceStorageBits | None

    @property
    def native_pyramid_levels(self) -> tuple[SourcePyramidLevel, ...]:
        """

        Checks the stored pyramid levels and returns their processing-record descriptions.

        The first level must match the full-resolution dimensions. Subsequent levels
        must decrease in at least one spatial dimension and cannot increase in the
        other. Channel count, Z/time dimensions, dtype and dimension order must stay
        consistent throughout the pyramid. Native reductions need not be exactly
        twofold, so their measured dimensions are retained.

        Some readers label a stored lower-resolution level as a thumbnail. The first
        analytical level cannot be a thumbnail, but a reduced level with matching
        channels and decreasing dimensions is still part of the image's pyramid.

        Returns:
            Tuple of SourcePyramidLevel records in native resolution order.

        Raises:
            SourceFactsError: Levels are missing, improperly indexed, spatially
                inconsistent or incompatible with the selected image's channel and
                storage representation.
        """

        if type(self.native_resolutions) is not tuple or not self.native_resolutions:
            raise SourceFactsError("source requires a native resolution inventory")
        levels = []
        previous = None
        for index, level in enumerate(self.native_resolutions):
            if (
                type(level.index) is not int
                or level.index != index
                or any(
                    type(value) is not int or value <= 0
                    for value in (
                        level.size_x,
                        level.size_y,
                        level.size_z,
                        level.size_c,
                        level.size_t,
                    )
                )
                or (level.size_z, level.size_c, level.size_t)
                != (self.size_z, self.size_c, self.size_t)
                or type(level.rgb_samples) is not int
                or level.rgb_samples != 1
                or np.dtype(level.dtype).name != self.container_dtype
                or np.dtype(level.dtype).itemsize * 8 != self.container_bits
                or level.dimension_order != self.dimension_order
                # Some Bio-Formats readers label native reduced levels as
                # thumbnails. Selection excludes preview-only series at level 0;
                # subsequent levels are admitted by their exact channel/type and
                # decreasing geometry, not this reader display hint.
                or type(level.is_thumbnail) is not bool
                or (index == 0 and level.is_thumbnail)
            ):
                raise SourceFactsError("native resolution topology differs from source facts")
            size = (level.size_x, level.size_y)
            if (index == 0 and size != (self.size_x, self.size_y)) or (
                previous is not None
                and (size[0] > previous[0] or size[1] > previous[1] or size == previous)
            ):
                raise SourceFactsError("native resolution geometry is not a decreasing pyramid")
            levels.append(
                SourcePyramidLevel(
                    index,
                    *size,
                    level.size_c,
                    self.container_dtype,
                    self.container_bits,
                    1,
                )
            )
            previous = size
        return tuple(levels)


def resolve_source_image_facts(source: BioFormatsSource) -> SourceImageFacts:
    """

    Checks the selected OME image against the reader's level-zero descriptor.

    The reviewed-source wrapper presents one analytical image as series zero.
    Its OME dimensions, channel layout, dtype and dimension order must agree
    with the actual reader descriptor. SignificantBits must fit within the
    decoded container, and the reader cannot report more stored bits than that
    container can represent.

    The function then converts available X/Y calibration to µm/pixel and checks
    the complete native pyramid. These facts can be reused during unmixing,
    image copying and output-record construction without repeatedly resolving
    the same metadata relationships.

    Args:
        source: Opened reviewed image exposing its selected metadata, reader
            series and file identity through the BioFormatsSource interface.

    Returns:
        SourceImageFacts describing the selected image and its stored pyramid.

    Raises:
        SourceFactsError: Source identity, OME selection, reader dimensions,
            channel layout, bit counts, physical units or native levels disagree.
    """

    from .canonical_ome_tiff import CanonicalOmeTiffError, _ome_dtype, _physical_calibration

    try:
        identity = source.identity
        ome = source.metadata
        series = source.series
        if type(identity) is not OpenedSourceIdentity:
            raise SourceFactsError("reviewed source identity must be IO-owned")
        if not isinstance(ome, OME) or len(ome.images) != 1:
            raise SourceFactsError("reviewed source must contain one OME image")
        if len(series) != 1 or series[0].index != 0:
            raise SourceFactsError("reviewed source must expose canonical series zero")
        image = ome.images[0]
        pixels = image.pixels
        level = series[0].resolutions[0]
        dtype = _ome_dtype(pixels.type)
        order = str(getattr(pixels.dimension_order, "value", pixels.dimension_order))
        if (
            (level.size_x, level.size_y, level.size_z, level.size_c, level.size_t)
            != (pixels.size_x, pixels.size_y, pixels.size_z, pixels.size_c, pixels.size_t)
            or level.rgb_samples != 1
            or level.dtype != dtype
            or level.dimension_order != order
            or pixels.interleaved is True
            or any(int(channel.samples_per_pixel or 1) != 1 for channel in pixels.channels)
        ):
            raise SourceFactsError("reviewed OME and reader topology differ")
        container_bits = dtype.itemsize * 8
        significant = pixels.significant_bits
        if significant is not None and (
            type(significant) is not int or not 0 < significant <= container_bits
        ):
            raise SourceFactsError("OME SignificantBits exceed the decoded container")
        # Stored bits can be fewer than the bits in the decoded dtype.
        # Retain both values for the later effective-range calculation.
        evidence = series[0].storage_bits
        if evidence is not None and evidence.bits > container_bits:
            raise SourceFactsError("actual storage bits exceed the decoded container")
        x_value, x_unit, x_um = _physical_calibration(
            pixels.physical_size_x, pixels.physical_size_x_unit, "X"
        )
        y_value, y_unit, y_um = _physical_calibration(
            pixels.physical_size_y, pixels.physical_size_y_unit, "Y"
        )
        facts = SourceImageFacts(
            identity=identity,
            image_id=image.id,
            pixels_id=pixels.id,
            size_x=pixels.size_x,
            size_y=pixels.size_y,
            size_z=pixels.size_z,
            size_c=pixels.size_c,
            size_t=pixels.size_t,
            dimension_order=order,
            container_dtype=np.dtype(dtype).name,
            container_bits=container_bits,
            significant_bits=significant,
            physical_size_x=x_value,
            physical_size_x_unit=x_unit,
            physical_size_x_um=x_um,
            physical_size_y=y_value,
            physical_size_y_unit=y_unit,
            physical_size_y_um=y_um,
            native_resolutions=series[0].resolutions,
            storage_bits=evidence,
        )
        # Validate every native level now, before another stage relies
        # on these dimensions to copy a pyramid or schedule regional reads.
        facts.native_pyramid_levels
        return facts
    except (CanonicalOmeTiffError, AttributeError, IndexError, TypeError, ValueError) as exc:
        raise SourceFactsError(f"reviewed source facts are invalid: {exc}") from exc
