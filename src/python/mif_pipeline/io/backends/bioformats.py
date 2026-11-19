"""Private adapter for the bffile bridge.

The backend uses the selected Java/Bio-Formats installation and image-series identity. Image
access is exposed through pipeline-owned records so callers retain the reviewed metadata and
regional coordinate frame."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import math
from numbers import Integral
from pathlib import Path
from threading import RLock
from types import ModuleType
from typing import Any
from xml.etree import ElementTree

import numpy as np
from ome_types import OME
from ome_types.model import AnnotationRef, MapAnnotation, StructuredAnnotations

from ..metadata.canonical import BiomarkerAssignment, canonicalize_ome
from ..metadata.canonical import resolve_unmixing_input_metadata
from ..metadata.calibration import read_original_metadata
from .im3_metadata import supplement_im3_metadata
from ._versions import load_bffile, read_backend_versions
from .base import (
    BackendVersions,
    BioFormatsSource,
    OpenedSourceIdentity,
    PlaneIndex,
    Region,
    ResolutionInfo,
    SeriesInfo,
    SourceStorageBits,
    snapshot_opened_source_identity,
)
from .errors import (
    DependencyVersionError,
    IngestionError,
    InvalidPlaneError,
    InvalidRegionError,
    InvalidResolutionError,
    InvalidSeriesError,
    MetadataCanonicalizationError,
    PixelReadError,
    SourceClosedError,
    SourceNotFoundError,
    SourceOpenError,
    UnsupportedFormatError,
)

_UNSUPPORTED_FORMAT_EXCEPTION_NAMES = {
    "UnknownFormatException",
    "UnsupportedFormatException",
}


# ==============================================================================
#                                   Bioformats
# ==============================================================================


class _BffileBackend:
    """Construct private bffile-backed sources through a lazy module loader."""

    def __init__(self, *, module_loader: Callable[[], ModuleType] = load_bffile) -> None:
        self._module_loader = module_loader

    def open_inspected(self, inspection, source_ome: OME) -> BioFormatsSource:
        """Reopen pixels with saved metadata when the reader confirms one source file.

        Grouped sources retain full metadata interpretation. The ordinary open
        method remains the entry point for initial inspection and other backends.
        """
        return self.open(inspection.source_identity.resolved_path,
                         inspection=inspection, source_ome=source_ome)

    def open(self, path: Path, *, inspection=None, source_ome: OME | None = None) -> BioFormatsSource:
        source_path = Path(path)
        from ..acquired_plane_view import is_acquired_plane_view, AcquiredPlaneView
        if is_acquired_plane_view(source_path):
            return AcquiredPlaneView(source_path, backend=self,
                                     inspection=inspection, source_ome=source_ome)
        from ..source_channel_view import is_source_channel_view, SourceChannelView
        if is_source_channel_view(source_path):
            return SourceChannelView(source_path, backend=self,
                                     inspection=inspection, source_ome=source_ome)
        from ..aligned_collection import is_aligned_source_collection, AlignedCollectionSource
        if is_aligned_source_collection(source_path):
            return AlignedCollectionSource(source_path, backend=self,
                                           inspection=inspection, source_ome=source_ome)
        if source_path.is_dir() and (source_path / "zarr.json").is_file():
            from ..processed_image import ProcessedImageSource
            return ProcessedImageSource(source_path)
        if not source_path.is_file():
            raise SourceNotFoundError(
                f"Bio-Formats source is not an existing regular file: {source_path}"
            )

        try:
            module = self._module_loader()
        except IngestionError:
            raise
        except Exception as error:
            raise DependencyVersionError(
                f"Could not load the pinned bffile runtime for {source_path}: "
                f"{type(error).__name__}: {error}"
            ) from error

        return _BffileSource.open(source_path, module,
                                 inspection=inspection, source_ome=source_ome)


class _BffileSource:
    """Opened source whose public state contains only pipeline-owned values."""

    def __init__(
        self,
        *,
        path: Path,
        biofile: Any,
        metadata: OME,
        series: tuple[SeriesInfo, ...],
        versions: BackendVersions,
        identity: OpenedSourceIdentity,
    ) -> None:
        self._path = path
        self._biofile = biofile
        self._metadata = metadata
        self._series = series
        self._versions = versions
        self._identity = identity
        self._closed = False
        self._lock = RLock()

    @classmethod
    def open(cls, path: Path, module: ModuleType, *,
             inspection=None, source_ome: OME | None = None) -> _BffileSource:
        """Initialize decoding and bind it to current bytes and image geometry.

        An inspected single-file source reuses its saved OME and storage evidence.
        We initialize Bio-Formats, compare live geometry and versions, and
        reuse the inspected digest after checking the file's filesystem identity.
        Unknown or grouped file membership uses
        the full inspection path, including OriginalMetadata interpretation.
        """
        try:
            before = path.resolve(strict=True).stat()
            identity_before_open = (
                before.st_size,
                before.st_mtime_ns,
                before.st_dev,
                before.st_ino,
            )
        except Exception as error:
            raise SourceOpenError(
                f"Could not capture Bio-Formats source freshness before opening: {path}"
            ) from error
        bridge = None
        try:
            bridge = module.BioFile(path, meta=True, original_meta=True)
            biofile = bridge.open()
        except IngestionError:
            _destroy_quietly(bridge)
            raise
        except Exception as error:
            _destroy_quietly(bridge)
            error_type = (
                UnsupportedFormatError if _is_unsupported_format_error(error) else SourceOpenError
            )
            raise error_type(f"Could not open Bio-Formats source: {path}") from error

        try:
            versions = read_backend_versions(module)
        except IngestionError:
            _destroy_quietly(biofile)
            raise
        except Exception as error:
            _destroy_quietly(biofile)
            raise DependencyVersionError(
                f"Could not verify Bio-Formats runtime versions for {path}"
            ) from error

        try:
            series = _snapshot_series(biofile)
        except IngestionError:
            _destroy_quietly(biofile)
            raise
        except Exception as error:
            _destroy_quietly(biofile)
            raise SourceOpenError(
                f"Could not snapshot Bio-Formats series metadata for {path}"
            ) from error

        single_file = False
        if inspection is not None:
            # File membership comes from the initialized decoder, not suffixes.
            # Keep OriginalMetadata available for the grouped-source route on
            # this same reader; only proven single-file sources skip conversion.
            try:
                used = tuple(biofile.used_files(metadata_only=False))
                single_file = bool(used) and {
                    Path(value).resolve(strict=True) for value in used
                } == {path.resolve(strict=True)}
            except Exception:
                pass
        if inspection is not None and single_file:
            try:
                if versions != inspection.backend_versions:
                    raise SourceOpenError("Reopened Bio-Formats versions differ from inspection")
                expected = tuple(replace(item, storage_bits=None) for item in inspection.series)
                if series != expected:
                    raise SourceOpenError("Reopened series inventory differs from inspection")
                if not isinstance(source_ome, OME):
                    raise SourceOpenError("Inspected reopening requires the saved OME model")
                metadata = source_ome
                series = inspection.series
            except BaseException:
                _destroy_quietly(biofile)
                raise
        else:
            try:
                source_metadata = _retain_source_modulo_links(biofile.ome_metadata, getattr(biofile, "ome_xml", None))
                selected_reader = _selected_java_reader(biofile)
                if inspection is None:
                    source_metadata = supplement_im3_metadata(
                        path, source_metadata, str(selected_reader.getClass().getName())
                    )
                evidence = _selected_vectra_storage_bits(selected_reader, series, source_metadata)
                source_metadata = _selected_vectra_channel_metadata(selected_reader, series, source_metadata)
                acquisition_values = _selected_acquisition_values(selected_reader, series, source_metadata)
            except IngestionError:
                _destroy_quietly(biofile)
                raise
            except Exception as error:
                _destroy_quietly(biofile)
                raise SourceOpenError(
                    f"Could not interpret source reader image metadata for {path}"
                ) from error

            try:
                metadata = canonicalize_ome(
                    source_metadata,
                    versions=versions,
                    source_image_indices=_acquisition_image_indices(series, source_metadata),
                    vectra_source_image_index=0 if evidence is not None else None,
                    source_acquisition_values=acquisition_values,
                )
            except IngestionError:
                _destroy_quietly(biofile)
                raise
            except Exception as error:
                _destroy_quietly(biofile)
                raise MetadataCanonicalizationError(
                    f"Could not canonicalize OME metadata for {path}"
                ) from error

            try:
                if evidence is not None:
                    promoted = resolve_unmixing_input_metadata(metadata, 0)
                    if promoted is not None and promoted.source_storage_bits != evidence.bits:
                        raise SourceOpenError(
                            "Vectra IFD BitsPerSample conflicts with image metadata"
                        )
                    series = (replace(series[0], storage_bits=evidence), *series[1:])
            except IngestionError:
                _destroy_quietly(biofile)
                raise
            except Exception as error:
                _destroy_quietly(biofile)
                raise SourceOpenError(
                    f"Could not validate Vectra storage evidence for {path}"
                ) from error

        try:
            identity = snapshot_opened_source_identity(
                path, retained=inspection.source_identity if inspection is not None and single_file else None,
            )
        except Exception as error:
            _destroy_quietly(biofile)
            raise SourceOpenError(
                f"Could not bind the opened Bio-Formats reader to source identity: {path}"
            ) from error
        if (
            identity.size_bytes,
            identity.modified_time_ns,
            identity.device_id,
            identity.inode_id,
        ) != identity_before_open:
            _destroy_quietly(biofile)
            raise SourceOpenError(f"Bio-Formats source changed while opening: {path}")

        if inspection is not None and identity != inspection.source_identity:
            _destroy_quietly(biofile)
            raise SourceOpenError("Reopened source identity differs from inspection")

        return cls(
            path=path,
            biofile=biofile,
            metadata=metadata,
            series=series,
            versions=versions,
            identity=identity,
        )

    @property
    def identity(self) -> OpenedSourceIdentity:
        return self._identity

    @property
    def metadata(self) -> OME:
        return self._metadata

    @property
    def series(self) -> tuple[SeriesInfo, ...]:
        return self._series

    @property
    def versions(self) -> BackendVersions:
        return self._versions

    def read_region(
        self,
        *,
        series: int,
        resolution: int,
        plane: PlaneIndex,
        region: Region,
    ) -> np.ndarray:
        with self._lock:
            if self._closed:
                raise SourceClosedError(f"Bio-Formats source is closed: {self._path}")

            descriptor, series_index, resolution_index = self._validate_request(
                series=series,
                resolution=resolution,
                plane=plane,
                region=region,
            )
            try:
                result = self._biofile.read_plane(
                    t=plane.t,
                    c=plane.c,
                    z=plane.z,
                    y=slice(region.y, region.y + region.height),
                    x=slice(region.x, region.x + region.width),
                    series=series_index,
                    resolution=resolution_index,
                )
                array = np.asarray(result)
            except IngestionError:
                raise
            except Exception as error:
                raise PixelReadError(
                    f"Could not read {self._path}, series {series}, "
                    f"resolution {resolution}, plane {plane}, region {region}"
                ) from error

            expected_shape = (
                (region.height, region.width)
                if descriptor.rgb_samples == 1
                else (region.height, region.width, descriptor.rgb_samples)
            )
            if array.shape != expected_shape:
                raise PixelReadError(
                    f"Unexpected pixel shape {array.shape} for {self._path}, "
                    f"series {series}, resolution {resolution}; expected "
                    f"{expected_shape}"
                )
            if array.dtype.newbyteorder("=") != descriptor.dtype:
                raise PixelReadError(
                    f"Unexpected pixel dtype {array.dtype} for {self._path}, "
                    f"series {series}, resolution {resolution}; expected "
                    f"{descriptor.dtype}"
                )

            try:
                # Decode into native byte order during the required ownership
                # copy. This preserves scalar values; original storage byte
                # order remains recorded in the source OME metadata.
                owned = np.array(array, dtype=descriptor.dtype, copy=True, order="C", subok=False)
            except Exception as error:
                raise PixelReadError(
                    f"Could not copy pixels for {self._path}, series {series}, "
                    f"resolution {resolution}, plane {plane}, region {region}"
                ) from error
            if (
                owned.dtype != descriptor.dtype
                or owned.shape != expected_shape
                or not owned.flags.owndata
                or not owned.flags.c_contiguous
                or owned.base is not None
            ):
                raise PixelReadError(
                    f"Could not detach pixels from bridge storage for {self._path}, "
                    f"series {series}, resolution {resolution}"
                )
            return owned

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            biofile = self._biofile
            self._closed = True
            self._biofile = None
            try:
                biofile.destroy()
            except IngestionError:
                raise
            except Exception as error:
                raise SourceOpenError(
                    f"Could not close Bio-Formats source: {self._path}"
                ) from error

    def __enter__(self) -> _BffileSource:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _validate_request(
        self,
        *,
        series: int,
        resolution: int,
        plane: PlaneIndex,
        region: Region,
    ) -> tuple[ResolutionInfo, int, int]:
        if not _is_integer(series):
            raise InvalidSeriesError(
                f"Invalid series {series!r} for {self._path}; "
                f"valid range is [0, {len(self._series)})"
            )
        series_index = int(series)
        if not 0 <= series_index < len(self._series):
            raise InvalidSeriesError(
                f"Invalid series {series!r} for {self._path}; "
                f"valid range is [0, {len(self._series)})"
            )
        selected_series = self._series[series_index]
        if not _is_integer(resolution):
            raise InvalidResolutionError(
                f"Invalid resolution {resolution!r} for {self._path}, series {series}; "
                f"valid range is [0, {len(selected_series.resolutions)})"
            )
        resolution_index = int(resolution)
        if not 0 <= resolution_index < len(selected_series.resolutions):
            raise InvalidResolutionError(
                f"Invalid resolution {resolution!r} for {self._path}, series {series}; "
                f"valid range is [0, {len(selected_series.resolutions)})"
            )
        descriptor = selected_series.resolutions[resolution_index]

        if not isinstance(plane, PlaneIndex) or not all(
            _is_integer(index) for index in (plane.t, plane.c, plane.z)
        ):
            raise InvalidPlaneError(
                f"Invalid plane {plane!r} for {self._path}, series {series}, "
                f"resolution {resolution}"
            )
        if not (
            0 <= plane.t < descriptor.size_t
            and 0 <= plane.c < descriptor.size_c
            and 0 <= plane.z < descriptor.size_z
        ):
            raise InvalidPlaneError(
                f"Invalid plane {plane} for {self._path}, series {series}, "
                f"resolution {resolution}; bounds are "
                f"t<{descriptor.size_t}, c<{descriptor.size_c}, z<{descriptor.size_z}"
            )

        if not isinstance(region, Region) or not all(
            _is_integer(value) for value in (region.x, region.y, region.width, region.height)
        ):
            raise InvalidRegionError(
                f"Invalid region {region!r} for {self._path}, series {series}, "
                f"resolution {resolution}"
            )
        end_x = region.x + region.width
        end_y = region.y + region.height
        if (
            region.x < 0
            or region.y < 0
            or region.width <= 0
            or region.height <= 0
            or end_x > descriptor.size_x
            or end_y > descriptor.size_y
        ):
            raise InvalidRegionError(
                f"Invalid half-open region [{region.x}, {end_x}) x "
                f"[{region.y}, {end_y}) for {self._path}, series {series}, "
                f"resolution {resolution} with bounds "
                f"{descriptor.size_x}x{descriptor.size_y}"
            )
        return descriptor, series_index, resolution_index


def _retain_source_modulo_links(metadata: OME, original_xml: str | None) -> OME:
    """Keep historical Pixels-linked Modulo evidence on its selected image.

    Current OME Pixels has no AnnotationRef field. Preserve that original link
    at Image scope before typed projection can discard it; cycle admission then
    validates and rejects its unsupported dimension meaning.
    """
    if original_xml is None:
        return metadata
    root = ElementTree.fromstring(original_xml)
    local = lambda element: element.tag.rsplit('}', 1)[-1]
    modulo_ids = {element.attrib.get('ID') for element in root.iter()
        if local(element) == 'XMLAnnotation' and any(local(child) in
            {'ModuloAlongC', 'ModuloAlongT', 'ModuloAlongZ'} for child in element.iter())}
    if not modulo_ids:
        return metadata
    retained_ids = {annotation.id for annotation in metadata.structured_annotations.xml_annotations}
    images = {image.id: image for image in metadata.images}
    result = metadata.model_copy(deep=True)
    result_images = {image.id: image for image in result.images}
    for image in root:
        if local(image) != 'Image':
            continue
        pixels = next((child for child in image if local(child) == 'Pixels'), None)
        parents = (image,) if pixels is None else (image, pixels)
        references = {child.attrib.get('ID') for parent in parents for child in parent
            if local(child) == 'AnnotationRef'} & modulo_ids
        if not references:
            continue
        if image.attrib.get('ID') not in images or not references <= retained_ids:
            raise SourceOpenError('Original OME Modulo dimension metadata could not be retained')
        selected = result_images[image.attrib['ID']]
        existing = {reference.id for reference in selected.annotation_refs}
        selected.annotation_refs.extend(AnnotationRef(id=identifier) for identifier in sorted(references - existing))
    return result


def _snapshot_series(biofile: Any) -> tuple[SeriesInfo, ...]:
    snapshots: list[SeriesInfo] = []
    for series_index in range(len(biofile)):
        source_series = biofile[series_index]
        resolutions = tuple(
            _snapshot_resolution(biofile, series_index, resolution_index)
            for resolution_index in range(source_series.resolution_count)
        )
        snapshots.append(
            SeriesInfo(
                index=series_index,
                name=source_series.name,
                resolutions=resolutions,
            )
        )
    return tuple(snapshots)


def _selected_java_reader(biofile: Any) -> Any:
    """Unwrap the concrete reader already opened by the pinned bffile bridge.

    bffile 0.1.1 exposes no public Java-reader accessor. This confined bridge
    returns its existing ImageReader handle; it must never open another reader.
    """
    selected = biofile._ensure_java_reader()
    # bffile can wrap ImageReader in Memoizer/ChannelFiller. Its pinned
    # _reader_class_name accessor follows this same public getReader chain.
    while hasattr(selected, "getReader"):
        selected = selected.getReader()
    return selected


def _selected_vectra_storage_bits(
    selected: Any,
    series: tuple[SeriesInfo, ...],
    metadata: OME,
) -> SourceStorageBits | None:
    """Prove analytical image zero and its storage bits for the live Vectra reader."""
    if str(selected.getClass().getName()) != "loci.formats.in.VectraReader":
        return None
    if not series or not metadata.images:
        raise SourceOpenError("Vectra analytical series zero is missing")
    level = series[0].resolutions[0]
    pixels = metadata.images[0].pixels
    ome_dtype = str(getattr(pixels.type, "value", pixels.type))
    ome_dtype = {"float": "float32", "double": "float64"}.get(ome_dtype, ome_dtype)
    if (
        level.is_thumbnail
        or level.rgb_samples != 1
        or (level.size_x, level.size_y, level.size_z, level.size_c, level.size_t)
        != (pixels.size_x, pixels.size_y, pixels.size_z, pixels.size_c, pixels.size_t)
        or level.dtype != np.dtype(ome_dtype)
        or level.dimension_order
        != str(getattr(pixels.dimension_order, "value", pixels.dimension_order))
        or pixels.interleaved is True
        or any(int(channel.samples_per_pixel or 1) != 1 for channel in pixels.channels)
    ):
        raise SourceOpenError("Vectra analytical series zero differs from canonical OME")
    plane_count = level.size_z * level.size_c * level.size_t
    ifds = selected.getIFDs()
    if len(ifds) < plane_count:
        raise SourceOpenError("Vectra reader has fewer primary IFDs than analytical planes")
    observed: set[int] = set()
    for plane in range(plane_count):
        ifd = ifds.get(plane)
        if (
            int(ifd.getImageWidth()) != level.size_x
            or int(ifd.getImageLength()) != level.size_y
            or int(ifd.getSamplesPerPixel()) != level.rgb_samples
        ):
            raise SourceOpenError("Vectra analytical IFD topology differs from selected series")
        # IFD.getBitsPerSample() defaults an absent TIFF tag to one; presence
        # must be proven independently before using its decoded values.
        # JPype maps a Python int to java.lang.Long for Hashtable.containsKey,
        # whereas TIFF IFD keys are java.lang.Integer. Compare public keySet
        # values numerically to avoid a false "missing tag" result.
        if not any(int(tag) == 258 for tag in ifd.keySet()):
            raise SourceOpenError("Vectra analytical IFD has no BitsPerSample tag")
        values = tuple(int(value) for value in ifd.getBitsPerSample())
        if len(values) != level.rgb_samples or any(value <= 0 for value in values):
            raise SourceOpenError("Vectra analytical IFD BitsPerSample is invalid")
        observed.update(values)
    if len(observed) != 1:
        raise SourceOpenError("Vectra analytical planes have heterogeneous storage bits")
    bits = observed.pop()
    if bits > level.dtype.itemsize * 8:
        raise SourceOpenError("Vectra storage bits exceed decoded dtype width")
    return SourceStorageBits(bits=bits, origin="tiff_bits_per_sample")


def _selected_vectra_channel_metadata(
    selected: Any, series: tuple[SeriesInfo, ...], metadata: OME,
) -> OME:
    """Retain optional QPTIFF channel facts from the proven full-resolution IFDs.

    OriginalMetadata's sparse flat lists cannot establish channel positions. The
    pinned VectraReader 8.5.0 reads these same leading C IFD comments: Biomarker
    becomes Channel.Name and, when Biomarker exists, Name becomes Channel.Fluor.
    Name is a band/component label, not explicit dye evidence. Correct that known
    reader inference only here; other readers' genuine OME Fluor is untouched.
    ImagingCycle is the vendor's zero-based cycle and remains channel-bound.
    """
    if str(selected.getClass().getName()) != "loci.formats.in.VectraReader":
        return metadata
    if _selected_vectra_storage_bits(selected, series, metadata) is None:
        return metadata
    pixels = metadata.images[0].pixels
    if pixels.size_z != 1 or pixels.size_t != 1:
        return metadata
    result = metadata.model_copy(deep=True)
    assignments = []
    ifds = selected.getIFDs()
    for channel_index, channel in enumerate(result.images[0].pixels.channels):
        comment = ifds.get(channel_index).getComment()
        if comment is None or not str(comment).strip():
            continue
        try:
            root = ElementTree.fromstring(str(comment))
        except ElementTree.ParseError:
            continue
        fields = {}
        for node in root:
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in {"Name", "Biomarker", "ImagingCycle", "AutofluorescenceSubtracted"}:
                continue
            value = (node.text or "").strip()
            if not value:
                continue
            if tag in fields and fields[tag] != value:
                raise SourceOpenError(f"Vectra channel {channel_index} has conflicting {tag}")
            fields[tag] = value
        name, marker = fields.get("Name"), fields.get("Biomarker")
        values = {"source_reader": "loci.formats.in.VectraReader",
                  "source_ifd_index": str(channel_index)}
        if name is not None:
            values["source_channel_name"] = name
            channel.name = name
        if marker is not None:
            values["source_biomarker"] = marker
            assignments.append(BiomarkerAssignment(0, channel_index, biomarker=marker))
            if name is not None and channel.fluor == name:
                channel.fluor = None
        cycle = fields.get("ImagingCycle")
        if cycle is not None:
            if not cycle.isascii() or not cycle.isdecimal():
                raise SourceOpenError(f"Vectra channel {channel_index} ImagingCycle is invalid")
            values["imaging_cycle"] = str(int(cycle))
        af = fields.get("AutofluorescenceSubtracted")
        if af is not None:
            if af not in {"True", "False", "true", "false"}:
                raise SourceOpenError(f"Vectra channel {channel_index} AutofluorescenceSubtracted is invalid")
            values["autofluorescence_subtracted"] = af.lower()
        if not fields:
            continue
        if result.structured_annotations is None:
            result.structured_annotations = StructuredAnnotations()
        used = {annotation.id for annotation in result.structured_annotations}
        index = 0
        while f"Annotation:VectraChannel:{channel_index}:{index}" in used:
            index += 1
        annotation = MapAnnotation(id=f"Annotation:VectraChannel:{channel_index}:{index}",
            namespace="urn:mif-pipeline:source-channel:v1", value=values)
        result.structured_annotations.map_annotations.append(annotation)
        channel.annotation_refs.append(AnnotationRef(id=annotation.id))
    return canonicalize_ome(result, assignments=assignments)


def _acquisition_image_indices(series: tuple[SeriesInfo, ...], metadata: OME) -> tuple[int, ...]:
    """Select scalar-channel source images whose live geometry matches typed OME.

    We exclude thumbnails and packed RGB images. The returned positions authorize
    initial acquisition collection even when no vendor fallback is needed.
    """
    indices = []
    for item in series:
        index = item.index
        if index >= len(metadata.images):
            continue
        level, pixels = item.resolutions[0], metadata.images[index].pixels
        if (
            level.is_thumbnail or level.rgb_samples != 1
            or (level.size_x, level.size_y, level.size_z, level.size_c, level.size_t)
            != (pixels.size_x, pixels.size_y, pixels.size_z, pixels.size_c, pixels.size_t)
            or len(pixels.channels) != level.size_c
            or any(int(channel.samples_per_pixel or 1) != 1 for channel in pixels.channels)
        ):
            continue
        indices.append(index)
    return tuple(indices)


def _selected_acquisition_values(
    selected: Any,
    series: tuple[SeriesInfo, ...],
    metadata: OME,
) -> dict[int, dict[str, tuple[float | int, ...]]]:
    """Fill missing acquisition fields from attributed OME OriginalMetadata.

    NDPI exposure describes its main single-channel image. Bio-Formats prefixes
    LSM recording keys with the exact image name, or "Series N" for an unnamed
    image. We use that prefix only when it identifies one image. Repeated binning
    recordings must agree and describe square bins. Ambiguous names, unsupported
    binning and missing values remain for review. Complete typed fields cause no
    vendor annotation lookup, and this operation never queries Java metadata tables.

    Args:
        selected: Concrete reader used only to establish the vendor mapping.
        series: Recorded live geometry identifying corresponding OME images.
        metadata: Retained typed OME and OriginalMetadata from initial opening.

    Returns:
        Exposure or binning additions by image position. Collection uses them only
        for missing typed cells and preserves the source annotations.
    """
    reader = str(selected.getClass().getName())
    if reader not in {"loci.formats.in.ZeissLSMReader", "loci.formats.in.NDPIReader"}:
        return {}
    result: dict[int, dict[str, tuple[float | int, ...]]] = {}
    for index in _acquisition_image_indices(series, metadata):
        pixels = metadata.images[index].pixels
        if resolve_unmixing_input_metadata(metadata, index) is not None:
            continue
        if reader == "loci.formats.in.ZeissLSMReader":
            if all(
                channel.detector_settings is not None
                and channel.detector_settings.binning is not None
                and str(getattr(channel.detector_settings.binning, "value",
                                channel.detector_settings.binning)).lower() != "other"
                for channel in pixels.channels
            ):
                continue
            # FormatReader.setId prefixes series metadata with the image name
            # verbatim, falling back to "Series N" only for a blank/missing name.
            prefixes = [
                image.name if image.name and image.name.strip() else f"Series {i}"
                for i, image in enumerate(metadata.images)
            ]
            prefix = prefixes[index]
            if prefixes.count(prefix) != 1:
                continue
            key = prefix + " Recording Camera Binning"
            fields = read_original_metadata(metadata, {key}, include_numbered=True)
            values = [_lsm_square_binning(value) for value in fields.values()]
            if values and None not in values and len(set(values)) == 1:
                result[index] = {"binning": (values[0],) * pixels.size_c}
        elif index == 0 and (pixels.size_z, pixels.size_c, pixels.size_t) == (1, 1, 1):
            if any(
                plane.the_z == 0 and plane.the_t == 0 and plane.the_c == 0
                and plane.exposure_time is not None
                for plane in pixels.planes
            ):
                continue
            key = "Exposure time (microseconds)"
            value = read_original_metadata(metadata, {key}).get(key)
            try:
                seconds = float(str(value)) * 1e-6
            except (ValueError, TypeError):
                continue
            if math.isfinite(seconds) and seconds > 0:
                result[index] = {"exposure_seconds": (seconds,)}
    return result


def _lsm_square_binning(value: str) -> int | None:
    """Interpret the LSM recording's square camera binning.

    The pinned reader treats scalar N as NxN and zero as unavailable. Repeated
    recordings are compared by the caller; rectangular or unspecified binning
    remains available for review instead of acquiring an assumed correction.
    """
    parts = value.strip().lower().split("x")
    if len(parts) not in (1, 2) or any(not part.isdigit() for part in parts):
        return None
    sides = tuple(int(part) for part in parts)
    return sides[0] if sides[0] > 0 and len(set(sides)) == 1 else None


def _snapshot_resolution(
    biofile: Any,
    series_index: int,
    resolution_index: int,
) -> ResolutionInfo:
    core = biofile.core_metadata(series_index, resolution_index)
    return ResolutionInfo(
        index=resolution_index,
        size_x=int(core.shape.x),
        size_y=int(core.shape.y),
        size_z=int(core.shape.z),
        size_c=int(core.shape.c),
        size_t=int(core.shape.t),
        rgb_samples=int(core.shape.rgb),
        dtype=np.dtype(core.dtype).newbyteorder("="),
        dimension_order=str(core.dimension_order),
        is_thumbnail=bool(core.is_thumbnail_series),
    )


def _is_integer(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


def _is_unsupported_format_error(error: BaseException) -> bool:
    return any(
        cls.__name__.rsplit(".", 1)[-1] in _UNSUPPORTED_FORMAT_EXCEPTION_NAMES
        for cls in type(error).__mro__
    )


def _destroy_quietly(biofile: Any | None) -> None:
    if biofile is None:
        return
    try:
        biofile.destroy()
    except Exception:
        pass
