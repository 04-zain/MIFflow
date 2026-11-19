"""

Reads regions directly from the selected vendor image using its reviewed OME metadata.

Initial inspection records the source image/series, stored pyramid levels,
channel order and acquisition metadata. Review completes the component, marker
and missing acquisition fields in the shared OME representation. This module
then reopens the original vendor file and presents the selected analytical image
as series zero to the numerical workflow.

Every regional request is translated back to the original source-series index.
The pixels stay in the vendor file until the unmixing engine reads the requested
rectangle. The component intensities are written directly to the sharded OME-Zarr output,
without first creating a complete standardized raw image.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from numbers import Integral
from typing import Iterator

import numpy as np
from ome_types import OME, from_xml, to_xml

from .backends import (
    BackendVersions,
    BioFormatsBackend,
    BioFormatsSource,
    InvalidPlaneError,
    InvalidRegionError,
    InvalidResolutionError,
    InvalidSeriesError,
    OpenedSourceIdentity,
    PixelReadError,
    PlaneIndex,
    Region,
    SeriesInfo,
    SourceClosedError,
)
from .canonical_ingestion import (
    CanonicalIngestionError,
    InspectedSourceAuthority,
    _validate_inspected_source_stat,
    resolve_canonical_image_selection,
    validate_reviewed_ome,
)
from .ingestion import get_default_backend

# ==============================================================================
#                                Reviewed source
# ==============================================================================


def _integer(value: object) -> bool:
    """

    Checks whether a position is an integer-like value while excluding booleans.

    Args:
        value: Candidate series, resolution, plane or rectangle position.

    Returns:
        True for Integral values other than bool.
    """

    return isinstance(value, Integral) and not isinstance(value, bool)


class _ReviewedSource:
    """

    Presents one reviewed analytical image while keeping its regional pixels in the original file.

    The numerical workflow addresses the selected image as series zero. The wrapper
    maps that position back to the original Bio-Formats series and exposes the
    reviewed metadata together with the original stored-level descriptions.
    Closing the wrapper closes its underlying image reader.
    """

    def __init__(
        self,
        opened: BioFormatsSource,
        authority: InspectedSourceAuthority,
        reviewed_xml: str,
    ) -> None:
        """

        Prepares the selected-series view using an already opened image reader.

        Copy the selected series description with its public index set to zero, retain
        the original source-series index for reads, and store the reviewed OME XML.
        The source identity and reader versions come from the checked inspection.

        Args:
            opened: Open Bio-Formats reader that has passed the reopening comparisons.
            authority: Original source inspection and selected analytical series.
            reviewed_xml: Accepted single-image OME metadata serialized as XML.
        """

        self._opened = opened
        self._identity = authority.source_identity
        self._versions = authority.backend_versions
        # Keep the vendor position for reads while presenting one analytical
        # image at series zero to the downstream numerical interface.
        self._source_series_index = authority.selection.source_series_index
        selected = authority.series[self._source_series_index]
        self._series = (
            SeriesInfo(
                index=0,
                name=selected.name,
                resolutions=selected.resolutions,
                storage_bits=selected.storage_bits,
            ),
        )
        self._reviewed_xml = reviewed_xml
        self._closed = False

    @property
    def identity(self) -> OpenedSourceIdentity:
        """

        Returns the source file identity recorded for this reviewed image.
        """

        return self._identity

    @property
    def metadata(self) -> OME:
        """

        Parses the saved reviewed OME XML into the metadata model used by the caller.

        Returns:
            OME metadata for the selected image and its reviewed acquisition/channel fields.
        """

        return from_xml(self._reviewed_xml)

    @property
    def series(self) -> tuple[SeriesInfo, ...]:
        """

        Returns the selected image's stored pyramid description under public series index zero.
        """

        return self._series

    @property
    def versions(self) -> BackendVersions:
        """

        Returns the Bio-Formats and image-reader versions associated with source inspection.
        """

        return self._versions

    def read_region(
        self,
        *,
        series: int,
        resolution: int,
        plane: PlaneIndex,
        region: Region,
    ) -> np.ndarray:
        """

        Reads one checked rectangle from the original vendor series and selected channel plane.

        Require public series zero and a valid stored resolution. Check T, Z and channel
        positions against that resolution, then require the requested rectangle to fit
        within its width and height. The request is translated to the original
        source-series index before the underlying reader is called.

        File size/change identifiers are checked around the read. The result must have
        the declared dtype, expected rectangle dimensions and an independently owned
        contiguous array. For planar fluorescence, the expected shape is (height, width).
        If any check or read fails, the source is closed before the error is propagated.

        Args:
            series: Public selected-image position; must be zero.
            resolution: Native pyramid-level index to read.
            plane: Channel, Z-plane and time-point indices.
            region: Rectangle defined by x, y, width and height in the selected level's pixels.

        Returns:
            Pixel array for that rectangle, retaining the source dtype and channel order.

        Raises:
            SourceClosedError: The reviewed source has already been closed.
            InvalidSeriesError: The caller requested a series other than zero.
            InvalidResolutionError: The stored pyramid index is unavailable.
            InvalidPlaneError: A channel, Z or time position is invalid.
            InvalidRegionError: Rectangle coordinates or dimensions fall outside the selected level.
            PixelReadError: Returned dtype, shape or array ownership differs from the request.
        """

        if self._closed:
            raise SourceClosedError("reviewed source is closed")
        try:
            if not _integer(series) or int(series) != 0:
                raise InvalidSeriesError("reviewed source accepts only canonical series 0")
            levels = self._series[0].resolutions
            if not _integer(resolution) or not 0 <= int(resolution) < len(levels):
                raise InvalidResolutionError("reviewed source resolution is out of range")
            descriptor = levels[int(resolution)]
            if (
                not isinstance(plane, PlaneIndex)
                or not all(_integer(index) for index in (plane.t, plane.c, plane.z))
                or not (
                    0 <= plane.t < descriptor.size_t
                    and 0 <= plane.c < descriptor.size_c
                    and 0 <= plane.z < descriptor.size_z
                )
            ):
                raise InvalidPlaneError("reviewed source plane is out of range")
            if (
                not isinstance(region, Region)
                or not all(
                    _integer(value) for value in (region.x, region.y, region.width, region.height)
                )
                or (
                    region.x < 0
                    or region.y < 0
                    or region.width <= 0
                    or region.height <= 0
                    or region.x + region.width > descriptor.size_x
                    or region.y + region.height > descriptor.size_y
                )
            ):
                raise InvalidRegionError("reviewed source region is out of range")
            _validate_inspected_source_stat(self._identity)
            # Read the requested pixels directly from the original vendor
            # series; reviewed metadata does not require a converted raw TIFF.
            result = self._opened.read_region(
                series=self._source_series_index,
                resolution=int(resolution),
                plane=plane,
                region=region,
            )
            _validate_inspected_source_stat(self._identity)
            expected_shape = (
                (region.height, region.width)
                if descriptor.rgb_samples == 1
                else (region.height, region.width, descriptor.rgb_samples)
            )
            if not isinstance(result, np.ndarray) or (
                result.shape != expected_shape
                or result.dtype != descriptor.dtype
                or not result.flags.owndata
                or not result.flags.c_contiguous
                or result.base is not None
            ):
                raise PixelReadError(
                    "reviewed source returned pixels outside its declared shape, dtype, or ownership"
                )
            return result
        except BaseException:
            self._close(check_freshness=False)
            raise

    def _close(self, *, check_freshness: bool) -> None:
        """

        Closes the underlying image reader once and optionally checks the source file before closing.

        Args:
            check_freshness: Whether to compare the recorded file identifiers before
                closing. Error paths skip that secondary comparison so the original
                read error remains the reported failure.
        """

        if self._closed:
            return
        self._closed = True
        try:
            if check_freshness:
                _validate_inspected_source_stat(self._identity)
        finally:
            self._opened.close()

    def close(self) -> None:
        """

        Checks the source file identifiers and closes the selected image reader.
        """

        self._close(check_freshness=True)

    def __enter__(self) -> _ReviewedSource:
        """

        Starts context-managed use of an open reviewed image source.

        Returns:
            The current reviewed source.

        Raises:
            SourceClosedError: The reader was already closed.
        """

        if self._closed:
            raise SourceClosedError("reviewed source is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """

        Closes the source when leaving its context and preserves any existing read error.

        Args:
            exc_type: Type of an exception raised inside the context, or None.
            exc_value: Exception instance supplied by Python.
            traceback: Exception traceback supplied by Python.
        """

        self._close(check_freshness=exc_type is None)


@contextmanager
def open_reviewed_source(
    authority: InspectedSourceAuthority,
    reviewed_ome: OME,
    *,
    backend: BioFormatsBackend | None = None,
    require_stage1_review: bool = True,
) -> Iterator[BioFormatsSource]:
    """

    Reopens the inspected vendor image and associates its pixels with the accepted metadata review.

    Reuse the immutable inspection and validate the reviewed OME image. Open the
    original file and verify its identity, reader versions and live series geometry.
    The backend reuses saved OME for confirmed single-file sources; other sources
    retain fresh metadata interpretation and comparison. Apply the inspected
    image/series selection so processing reads the same analytical image.

    Once these comparisons pass, yield a reader exposing that selected image at
    series zero with the reviewed metadata. The reader translates regional pixel
    requests to the vendor series. Exiting the context closes it on both completion
    and failure.

    Args:
        authority: Source inspection saved when the analytical image was selected.
        reviewed_ome: Completed single-image OME metadata accepted for processing.
        backend: Optional selected Bio-Formats backend; None uses the configured backend.
        require_stage1_review: Require the target component/marker panel. Empirical
            references bind their role and dye separately and omit only that panel
            requirement; source, geometry and acquisition checks remain mandatory.

    Yields:
        Open regional source for direct numerical processing of the original vendor image.

    Raises:
        CanonicalIngestionError: Inspection, reviewed fields, source identity, image
            selection or reader interpretation no longer agree.
    """

    if not isinstance(authority, InspectedSourceAuthority):
        raise CanonicalIngestionError("reviewed source requires inspected authority")
    source_ome = authority.canonical_ome
    selection = authority.selection
    resolved = resolve_canonical_image_selection(
        source_ome,
        authority.series,
        image_index=selection.source_image_index,
        series_index=selection.source_series_index,
        require_planar_channels=True,
    )
    if resolved != selection:
        raise CanonicalIngestionError("inspected source selection differs from its inventory")
    validate_reviewed_ome(source_ome, reviewed_ome, selection, require_stage1_review=require_stage1_review)
    # Retain the completed metadata separately from the unchanged source
    # pixel file before reopening that file for regional processing.
    reviewed_xml = to_xml(reviewed_ome)
    opened = _open_inspected_source(authority, source_ome, backend=backend)
    view = _ReviewedSource(opened, authority, reviewed_xml)
    try:
        yield view
    except BaseException:
        view._close(check_freshness=False)
        raise
    else:
        view.close()


def _open_inspected_source(
    authority: InspectedSourceAuthority,
    source_ome: OME,
    *,
    backend: BioFormatsBackend | None = None,
) -> BioFormatsSource:
    """Open and verify the original series interface for unmixing or copying.

    The concrete backend can reuse inspected metadata for a verified single-file
    source. Other backends and grouped sources retain full metadata inspection.
    Identity, versions, geometry and metadata must agree before any pixel reads.
    The caller owns the returned reader and must close it.
    """
    _validate_inspected_source_stat(authority.source_identity)
    selected = backend if backend is not None else get_default_backend()
    reopen = getattr(selected, "open_inspected", None)
    opened = (
        reopen(authority, source_ome) if callable(reopen)
        else selected.open(authority.source_identity.resolved_path)
    )
    try:
        if opened.identity != authority.source_identity:
            raise CanonicalIngestionError("reopened source identity differs from inspection")
        if opened.versions != authority.backend_versions:
            raise CanonicalIngestionError("reopened Bio-Formats versions differ from inspection")
        if opened.series != authority.series:
            raise CanonicalIngestionError("reopened series inventory differs from inspection")
        metadata = opened.metadata
        if metadata is not source_ome and (
            not isinstance(metadata, OME)
            or metadata.model_dump(mode="json") != source_ome.model_dump(mode="json")
        ):
            raise CanonicalIngestionError("reopened canonical OME differs from inspection")
        _validate_inspected_source_stat(authority.source_identity)
    except BaseException:
        opened.close()
        raise
    return opened
